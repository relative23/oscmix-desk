#!/usr/bin/env python3
"""Record playback mute on all ten pairs: the words on the wire and the effect (two phases).

Playback mute is never reported by the device, so the evidence is what the backend sends and what
reaches a loopback recording. Two phases:

``--execute`` (live) runs under a usbmon capture that was started before the backend: per pair
it switches the odd channel's mute on, then off, then the even channel's off (an idempotent OFF),
each step timed. One representative pair then plays a 997 Hz tone at -30 dBFS into an output
whose fader is at -65 dB, with loopback on: the tone has to make up the loopback recording
unmuted and restored, at the same level, and vanish muted. Every reported register is given
back and compared, as in
probe-loopback.py; the tone pair's cell goes back to what the desk's journal last wrote.

``--assess`` (offline) reads the capture: in each step's window the backend must send one message
with exactly the pair's 40 coefficient words, zero for ON and, for OFF, the value each register
last carried in the capture (zero when the backend never wrote it since it started, which the
capture covers). Nothing claims a device mute readback, cold-start behaviour or an acoustic output.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('probe_loopback', HERE / 'probe-loopback.py')
LOOP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOOP)

import usb_midi_words  # noqa: E402 -- after probe-loopback set the path
from loopback_signal import channel, level_dbfs, middle  # noqa: E402

from oscmix_desk.config import load_config  # noqa: E402
from oscmix_desk.discovery import resolve_device  # noqa: E402
from oscmix_desk.errors import ConfigError, DeviceLockUnavailable, WriteFailed  # noqa: E402
from oscmix_desk.journal import read_journals  # noqa: E402
from oscmix_desk.paths import discover_config_path  # noqa: E402

PHASE_SPEC = importlib.util.spec_from_file_location('probe_phase', HERE / 'probe-phase.py')
PHASE = importlib.util.module_from_spec(PHASE_SPEC)
PHASE_SPEC.loader.exec_module(PHASE)

PAIRS = tuple(range(1, 21, 2))
TONE_HZ = 997.0
TONE_DBFS = -30.0
TONE_SECONDS = 3.0
#: First playback column on the wire (ffucxii: playback begins at column 32).
PLAYBACK_COLUMN = 32


def cell_register(output, playback):
    """The coefficient register of playback channel ``playback`` (1-based) on ``output``."""
    return 0x4000 | (output - 1) << 6 | (PLAYBACK_COLUMN + playback - 1)


def pair_registers(pair):
    return sorted(cell_register(out, pair + side) for out in range(1, 21) for side in (0, 1))


def goertzel_dbfs(samples, frequency, rate=LOOP.RATE):
    """Power of one frequency in ``samples``, in dBFS of a sine (0 dBFS = full-scale amplitude)."""
    coefficient = 2 * math.cos(2 * math.pi * frequency / rate)
    previous = before = 0.0
    for sample in samples:
        previous, before = sample + coefficient * previous - before, previous
    power = previous * previous + before * before - coefficient * previous * before
    amplitude = 2 * math.sqrt(max(power, 0.0)) / len(samples)
    return 20 * math.log10(amplitude) if amplitude > 0 else -math.inf


def classify(tone, total, rates):
    """carried, silent or undetermined, from a stage's stored levels (None: no signal at all).

    The tone's absolute level is not the point: PipeWire applies the playback node's volume
    (-24.1 dB on 2026-11-07), so carried means the tone makes up the whole recording, far above
    the floor. A sine of amplitude A has a mean power of A^2 / 2, 3.01 dB below its peak level.
    """
    if rates != [LOOP.RATE, LOOP.RATE]:
        return 'undetermined'
    if tone is not None and total is not None and tone > -90 and total - (tone - 3.01) <= 1:
        return 'carried'
    if total is None or total < -100:
        return 'silent'
    return 'undetermined'


def tone_verdict(raw, output, rates):
    """Whether capture channel ``output`` carried the tone, was silent, or decides nothing."""
    samples = middle(channel(raw, output - 1), 16384)
    tone = goertzel_dbfs(samples, TONE_HZ)
    total = level_dbfs(samples)
    found = dict(tone_dbfs=None if math.isinf(tone) else round(tone, 2),
                 total_dbfs=None if math.isinf(total) else round(total, 2), device_rate=rates)
    found['verdict'] = classify(found['tone_dbfs'], found['total_dbfs'], rates)
    return found


def tone_passed(record):
    """Heard unmuted, silent muted, heard again at the same level; loopback given back as found."""
    stages = record['stages']
    heard = [s['tone_dbfs'] for s in stages if s['verdict'] == 'carried']
    return (record['before']['verdict'] in ('carried', 'silent')
            and record['loopback_given_back'] == int(record['before']['verdict'] == 'carried')
            and [s['verdict'] for s in stages] == ['carried', 'silent', 'carried']
            and abs(heard[0] - heard[1]) <= 0.5)


def tone_file(channel, path):
    """TONE_SECONDS of a 20-channel s32 stream, the tone on ``channel`` (0-based) only."""
    amplitude = 10 ** (TONE_DBFS / 20) * (2 ** 31 - 1)
    frame = struct.Struct('<%di' % LOOP.CHANNELS)
    with path.open('wb') as out:
        for index in range(int(TONE_SECONDS * LOOP.RATE)):
            values = [0] * LOOP.CHANNELS
            values[channel] = int(amplitude * math.sin(2 * math.pi * TONE_HZ * index / LOOP.RATE))
            out.write(frame.pack(*values))


def pro_sink(serial):
    listing = subprocess.run(['pw-cli', 'ls', 'Node'], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    names = [line.split('"')[1] for line in listing.splitlines() if 'node.name' in line]
    wanted = [n for n in names if 'Fireface_UCX_II__%s' % serial in n
              and n.startswith('alsa_output.') and n.endswith('.pro-output-0')]
    if len(wanted) != 1:
        raise ValueError('need exactly one pro-audio playback node of the UCX II, found %d'
                         % len(wanted))
    return wanted[0]


def process_start(pid):
    """Wall-clock start of a process, from /proc (clock ticks since boot)."""
    fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
    ticks = int(fields[19])
    boot = next(float(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                if line.startswith('btime'))
    return boot + ticks / os.sysconf('SC_CLK_TCK')


class MuteProbe(LOOP.Probe):
    def start(self):
        super().start()
        interface = resolve_device(self.config.usb_id, self.config.device_name,
                                   self.config.serial, Path('/proc'))
        self.journal = read_journals(self.path, interface.key)
        muted = sorted(path for path, (_tags, values, _at) in self.journal.items()
                       if path.startswith('/playback/') and path.endswith('/mute')
                       and values and values[0])
        if muted:
            raise ValueError('the journal has playback muted, which the probe cannot give back: '
                             + ', '.join(muted))
        self.card = PHASE.ucx_card(self.result['serial'])
        self.sink = pro_sink(self.result['serial'])

    def step(self, connection, record, name, *messages):
        record.setdefault('steps', []).append(dict(name=name, start=time.time()))
        self.write(connection, *messages)
        record['steps'][-1]['end'] = time.time()

    def pair(self, number):
        record = dict(pair=[number, number + 1], registers=pair_registers(number))
        self.result['cases'].append(record)
        with self.lease() as connection:
            self.step(connection, record, 'on', ('/playback/%d/mute' % number, 'i', (1,)))
            self.step(connection, record, 'off-target', ('/playback/%d/mute' % number, 'i', (0,)))
            self.step(connection, record, 'off-partner',
                      ('/playback/%d/mute' % (number + 1), 'i', (0,)))
        self.checkpoint()

    def tone(self, connection, channel, output, stage):
        """Record the loopback of ``output`` while the tone plays on playback ``channel``."""
        with tempfile.TemporaryDirectory() as work:
            stream = Path(work) / 'tone.raw'
            tone_file(channel - 1, stream)
            channel_map = ','.join('AUX%d' % i for i in range(LOOP.CHANNELS))
            player = subprocess.Popen(
                ['pw-play', '--raw', '--target=' + self.sink, '--rate=%d' % LOOP.RATE,
                 '--channels=%d' % LOOP.CHANNELS, '--channel-map=' + channel_map,
                 '--format=s32', str(stream)], stderr=subprocess.PIPE)
            try:
                LOOP.drain(connection, 0.6)
                raw, rates = PHASE.recorded(connection, self.node, self.card)
            finally:
                player.terminate()
                player.wait(timeout=5)
        stage.update(tone_verdict(raw, output, rates))

    def acoustic(self, pair, output):
        """The tone through pair ``pair`` to ``output`` with loopback: unmuted, muted, restored."""
        record = self.result['acoustic'] = dict(pair=[pair, pair + 1], output=output, stages=[])
        cell = '/mix/%d/playback/%d' % (output, pair)
        loopback = '/output/%d/loopback' % output
        before = self.journal_level(cell)
        record['cell_given_back'] = list(before)
        volumes = ['/output/%d/volume' % n for n in (output, output + 1)
                   if '/output/%d/volume' % n in self.first]
        undo = [*(self.given_back(path) for path in volumes), (cell, 'fi', before)]
        with self.lease() as connection:
            self.write(connection, *((path, 'f', (-65.0,)) for path in volumes),
                       (cell, 'fi', (0.0, 0)), undo=undo)
            try:
                # Loopback is never reported: whether it was on shows in the recording.
                stage = dict(name='before')
                self.tone(connection, pair, output, stage)
                record['loopback_given_back'] = int(stage['verdict'] == 'carried')
                record['before'] = stage
                self.write(connection, (loopback, 'i', (1,)),
                           undo=[(loopback, 'i', (record['loopback_given_back'],))])
                for name, mute in (('unmuted', 0), ('muted', 1), ('restored', 0)):
                    self.write(connection, ('/playback/%d/mute' % pair, 'i', (mute,)))
                    stage = dict(name=name)
                    record['stages'].append(stage)
                    self.tone(connection, pair, output, stage)
            finally:
                back = ([(loopback, 'i', (record['loopback_given_back'],))]
                        if 'loopback_given_back' in record else [])
                self.write(connection, ('/playback/%d/mute' % pair, 'i', (0,)), *back, *undo)
        record['passed'] = tone_passed(record)
        self.checkpoint()

    def journal_level(self, cell):
        """(level, pan) the desks' journals last wrote to ``cell``; silence when none did.

        A playback cell is never reported, so this is the value the backend holds when only
        journaled writers wrote it -- the desk's own claim, never a read-back.
        """
        entry = self.journal.get(cell)
        if entry is not None and len(entry[1]) == 2:
            return (float(entry[1][0]), int(entry[1][1]))
        return (-65.0, 0)


def assess(evidence, capture, bus, device):
    """The wire verdict of every step, from the capture alone."""
    started = evidence['running_backend']['started']
    bulks = list(usb_midi_words.bulks(capture, bus, device))
    if not bulks or bulks[0][0] >= started:
        raise ValueError('the capture does not cover the backend from its start')
    for case in evidence['cases']:
        registers = case['registers']
        wanted = set(registers)
        last = {}
        for when, words in bulks:
            if when >= case['steps'][0]['start']:
                break
            last.update((reg, value) for reg, value in words if reg in wanted)
        restore = {reg: last.get(reg, 0) for reg in registers}
        case['restore_words'] = [restore[reg] for reg in registers]
        for step in case['steps']:
            # A step ends after the probe's settle time, before the next one starts.
            inside = [words for when, words in bulks if step['start'] <= when <= step['end']]
            mine = [w for words in inside for w in words if 0x4000 <= w[0] < 0x8000]
            expected = {reg: 0 if step['name'] == 'on' else restore[reg] for reg in registers}
            one = [words for words in inside if any(r in wanted for r, _ in words)]
            step['coefficient_words'] = len(mine)
            step['messages'] = len(one)
            step['complete'] = (len(one) == 1 and sorted(r for r, _ in one[0]) == registers
                                and dict(one[0]) == expected and len(mine) == len(registers))
        case['passed'] = all(step['complete'] for step in case['steps'])
    # The verdicts follow from the stored levels; deciding them again here keeps one rule.
    acoustic = evidence['acoustic']
    for stage in [acoustic['before'], *acoustic['stages']]:
        stage['verdict'] = classify(stage['tone_dbfs'], stage['total_dbfs'], stage['device_rate'])
    acoustic['passed'] = tone_passed(acoustic)
    evidence['capture'] = dict(sha256=hashlib.sha256(capture.read_bytes()).hexdigest(),
                               bytes=capture.stat().st_size, messages=len(bulks),
                               first_message=bulks[0][0], bus=bus, device=device)
    evidence['assessed'] = datetime.now(UTC).isoformat()
    evidence['passed'] = (evidence.get('executed') is True and len(evidence['cases']) == 10
                          and all(case['passed'] for case in evidence['cases'])
                          and evidence['acoustic']['passed'] and evidence['restored'] is True)
    return evidence


def execute(args):
    result = dict(schema=2, kind='playback-mute-probes-ucx2', pairs=list(PAIRS), cases=[],
                  sources={}, started=datetime.now(UTC).isoformat(), hardware_used=False,
                  claims=dict(device_mute_readback=False, coefficient_device_readback=False,
                              acoustic_output_claim=False, cold_start_qualified=False))
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)

    def checkpoint():
        args.out.write_text(json.dumps(result, indent=1, allow_nan=False) + '\n')

    probe = None
    try:
        path = args.config or discover_config_path()
        probe = MuteProbe(args, load_config(path, strict=True), path, result, checkpoint)
        probe.start()
        backend = result['running_backend']
        backend['started'] = process_start(backend['pid'])
        result['hardware_used'] = True
        for number in PAIRS:
            probe.pair(number)
        probe.acoustic(args.tone_pair, args.tone_output)
        result['executed'] = True
    except (OSError, ValueError, TypeError, KeyError, ConfigError, DeviceLockUnavailable,
            WriteFailed, subprocess.SubprocessError) as exc:
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
    finally:
        if probe is not None and result['hardware_used']:
            try:
                probe.finish()
            except (OSError, ValueError, ConfigError, DeviceLockUnavailable, WriteFailed) as exc:
                result['restore_error'] = '%s: %s' % (type(exc).__name__, exc)
                result['restored'] = False
        result['finished'] = datetime.now(UTC).isoformat()
        checkpoint()
    print('probe-playback-mute: executed=%s restored=%s -> %s; run --assess next'
          % (result.get('executed'), result.get('restored'), args.out))
    return 0 if result.get('executed') and result.get('restored') else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--assess', action='store_true')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--capture', type=Path, help='usbmon capture (--assess)')
    parser.add_argument('--bus', type=int, help='USB bus of the interface (--assess)')
    parser.add_argument('--device', type=int, help='USB device number (--assess)')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--tone-pair', type=int, default=15)
    parser.add_argument('--tone-output', type=int, default=15)
    parser.add_argument('--backend-build', type=Path,
                        default=HERE.parent / 'build/coordinated')
    args = parser.parse_args()
    args.source, args.alternate, args.gain = 1, 3, 0.0
    if args.execute == args.assess:
        parser.error('one of --execute and --assess')
    if args.tone_pair not in PAIRS or not 1 <= args.tone_output < 20 or args.tone_output % 2 == 0:
        parser.error('the tone pair is an odd playback channel, the output an odd one below 20')
    if args.execute:
        return execute(args)
    if None in (args.capture, args.bus, args.device):
        parser.error('--assess needs --capture, --bus and --device')
    evidence = assess(json.loads(args.out.read_text()), args.capture, args.bus, args.device)
    args.out.write_text(json.dumps(evidence, indent=1, allow_nan=False) + '\n')
    print('probe-playback-mute: %s -> %s' % ('passed' if evidence['passed'] else 'FAILED',
                                            args.out))
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
