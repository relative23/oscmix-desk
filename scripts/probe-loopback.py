#!/usr/bin/env python3
"""Measure output loopback on every output and Room EQ delay through it (--execute: live state).

Execution requires a fresh physical safety confirmation outside this tool: UCX on, monitors off,
headphones disconnected, phantom power off. No tone is played and the playback matrix, which the
device cannot report, is never written. The signal is the noise of input pair 1/2 (3/4 for outputs
1/2): their gains are raised for the run, and one readable input-mix cell carries the pair to the
output pair under test, at 0 dB output volume. With `/output/N/loopback 1` capture channel N
carries output N and correlates with the capture channel of the input feeding it
(loopback_signal.py); with 0 it carries input N and does not.

Every register written is one the device reports, except loopback itself: its state before the
probe is measured from a recording with the route in place, and given back. Each output runs in a
lease of its own. The run ends with a fresh full dump compared register by register with the first.
Evidence holds verdicts, correlation peaks, lags and noise levels, never mixer values.
"""

import argparse
import contextlib
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from loopback_signal import CHANNELS, RATE, analyse, delay_slope
from measurement import build_evidence, observations

from oscmix_desk import open_desk
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.discovery import device_firmware, resolve_device
from oscmix_desk.errors import ConfigError, DeviceLockUnavailable, WriteFailed
from oscmix_desk.paths import discover_config_path
from oscmix_desk.protocol import Source
from oscmix_desk.registers import register_at

READ_SECONDS = 6.0
RECORD_SECONDS = 1.6
SETTLE_SECONDS = 0.4
GAIN = 60.0
DELAYS = (0.0, 0.001, 0.01, 0.05)
DELAY_ENDPOINT = 0.425
FLOOR = -65.0


def volatile(path):
    return path.endswith(('/level', '/meter')) or path == '/hardware/dspload'


def drain(connection, seconds):
    """Keep the lease and discard deliveries (meters stream at ~270/s) for ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        connection.next_delivery(min(.1, max(0, deadline - time.monotonic())))


def snapshot(connection):
    connection.request_dump()
    captured = {}
    deadline = time.monotonic() + READ_SECONDS
    while time.monotonic() < deadline:
        for path, tags, args, origin, _sequence in observations(
                connection, min(.25, max(0, deadline - time.monotonic()))):
            if origin == Source.DEVICE and not volatile(path):
                captured[path] = (tags, tuple(args))
    return captured


def compare(before, after):
    return {'missing': sorted(set(before) - set(after)),
            'changed': sorted(p for p in before.keys() & after.keys() if before[p] != after[p]),
            'new': sorted(set(after) - set(before))}


def digest(state):
    text = json.dumps(sorted((p, t, list(a)) for p, (t, a) in state.items()), allow_nan=True)
    return hashlib.sha256(text.encode()).hexdigest()


def pro_source(serial):
    listing = subprocess.run(['pw-cli', 'ls', 'Node'], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    names = [line.split('"')[1] for line in listing.splitlines() if 'node.name' in line]
    wanted = [n for n in names if 'Fireface_UCX_II__%s' % serial in n
              and n.startswith('alsa_input.') and n.endswith('.pro-input-0')]
    if len(wanted) != 1:
        raise ValueError('need exactly one pro-audio capture node of the UCX II, found %d'
                         % len(wanted))
    return wanted[0]


def record(connection, node):
    """All capture channels for RECORD_SECONDS, keeping the lease while pw-record runs."""
    channel_map = ','.join('AUX%d' % i for i in range(CHANNELS))
    with tempfile.TemporaryFile() as out:
        process = subprocess.Popen(
            ['pw-record', '--raw', '--target=' + node, '--rate=%d' % RATE,
             '--channels=%d' % CHANNELS, '--channel-map=' + channel_map, '--format=s32', '-'],
            stdout=out, stderr=subprocess.PIPE)
        try:
            drain(connection, RECORD_SECONDS)
        finally:
            process.terminate()
            process.wait(timeout=5)
        out.seek(0)
        raw = out.read()
    if len(raw) < 4 * CHANNELS * RATE // 2:
        raise ValueError('pw-record delivered %d bytes' % len(raw))
    return raw


def value(state, path):
    if path not in state:
        raise ValueError('the first dump did not report ' + path)
    return state[path]


def flag(state, path):
    return int(value(state, path)[1][0])


def mix_level(args):
    level, pan = args
    return (FLOOR if not math.isfinite(level) or level < FLOOR else float(level), int(pan))


class Probe:
    def __init__(self, args, config, path, result, checkpoint):
        self.args, self.config, self.path = args, config, path
        self.result, self.checkpoint = result, checkpoint
        #: path -> the message that gives it back, for every path this run has written.
        self.restore = {}

    @contextlib.contextmanager
    def lease(self):
        with open_desk(self.config, self.path) as desk:
            yield desk._begin()

    def write(self, connection, *messages, undo=()):
        for message in undo:
            self.restore.setdefault(message[0], message)
        connection.send(list(messages))
        drain(connection, SETTLE_SECONDS)

    def given_back(self, path):
        """The first dump's message for a reported path."""
        return (path, *value(self.first, path))

    def analysed(self, connection, source, target, **kwargs):
        return analyse(record(connection, self.node), source, target, **kwargs)

    def start(self):
        with open_desk(self.config, self.path) as desk:
            self.result['running_backend'] = build_evidence(
                desk._control, self.args.backend_build, Path('/proc'))
            connection = desk._begin()
            self.first = snapshot(connection)
        self.result['first_dump'] = dict(paths=len(self.first), sha256=digest(self.first))
        interface = resolve_device(self.config.usb_id, self.config.device_name,
                                   self.config.serial, Path('/proc'))
        self.node = pro_source(interface.serial)
        self.result['serial'] = interface.serial
        self.result['firmware'] = device_firmware(
            self.config.usb_id, Path('/sys/bus/usb/devices'),
            {'/hardware/dspvers': value(self.first, '/hardware/dspvers')[1][0]},
            serial=interface.serial)
        for channel in self.inputs():
            for name in ('48v', 'autoset'):
                path = '/input/%d/%s' % (channel, name)
                if path in self.first and flag(self.first, path):
                    raise ValueError('%s must read 0 for a noise source' % path)

    def linked(self, family, channel):
        return flag(self.first, '/%s/%d/stereo' % (family, channel)) == 1

    def inputs(self):
        """Both source pairs' channels: their noise is the signal."""
        return [n for first in (self.args.source, self.args.alternate) for n in (first, first + 1)]

    def feed(self, output):
        """(input whose mix cell feeds ``output``, capture channel that output must copy)."""
        pair = (self.args.source, self.args.source + 1)
        source = self.args.alternate if output in pair else self.args.source
        stereo = self.linked('input', source) and self.linked('output', output)
        return source, source + ((output - 1) % 2 if stereo else 0)

    def sources(self, connection):
        """Raise every source channel's gain as far as its register goes, unmuted."""
        messages, undo = [], []
        for channel in self.inputs():
            gain, mute = '/input/%d/gain' % channel, '/input/%d/mute' % channel
            row = register_at(UCX2, gain)
            level = min(self.args.gain, row.hi if row is not None and row.hi is not None else 0.0)
            messages += [(gain, 'f', (level,)), (mute, 'i', (0,))]
            undo += [self.given_back(gain), self.given_back(mute)]
            self.result['sources'][str(channel)] = dict(gain_db=level)
        self.write(connection, *messages, undo=undo)
        raw = record(connection, self.node)
        for channel in self.inputs():
            level = analyse(raw, channel, channel, max_lag=1)['source_dbfs']
            self.result['sources'][str(channel)]['dbfs'] = level
            if level is None or level < -130:
                raise ValueError('input %d carries no usable noise (%s dBFS)' % (channel, level))

    def route(self, output, source):
        """Messages that put ``source`` on ``output``'s pair at 0 dB, and the ones undoing them."""
        first = self.first
        cell = output - 1 if output % 2 == 0 and self.linked('output', output) else output
        path = '/mix/%d/input/%d' % (cell, source)
        on = [(path, 'fi', (0.0, 0))]
        off = [(path, 'fi', mix_level(value(first, path)[1]))]
        channels = (cell, cell + 1) if self.linked('output', cell) else (cell,)
        for channel in channels:
            mute, volume = '/output/%d/mute' % channel, '/output/%d/volume' % channel
            if mute in first and flag(first, mute):
                on.append((mute, 'i', (0,)))
                off.append(self.given_back(mute))
            on.append((volume, 'f', (0.0,)))
            off.append(self.given_back(volume))
        return on, off

    def output(self, number):
        cell_input, source = self.feed(number)
        loopback = '/output/%d/loopback' % number
        case = dict(output=number, source=source, mix_input=cell_input)
        self.result['cases'].append(case)
        on, off = self.route(number, cell_input)
        with self.lease() as connection:
            self.write(connection, *on, undo=off)
            try:
                case['before'] = self.analysed(connection, source, number)
                given = 1 if case['before']['verdict'] == 'carried' else 0
                self.write(connection, (loopback, 'i', (1,)), undo=[(loopback, 'i', (given,))])
                case['on'] = self.analysed(connection, source, number)
                self.write(connection, (loopback, 'i', (0,)))
                case['off'] = self.analysed(connection, source, number)
            finally:
                given = 1 if case.get('before', {}).get('verdict') == 'carried' else 0
                self.write(connection, (loopback, 'i', (given,)), *off)
                case['given_back'] = given
        case['passed'] = (case['on']['verdict'] == 'carried'
                          and case['off']['verdict'] == 'not-carried'
                          and case['before']['verdict'] != 'undetermined')
        self.checkpoint()

    def delay(self):
        number = self.args.delay_output
        cell_input, source = self.feed(number)
        delay = '/output/%d/roomeq/delay' % number
        loopback = '/output/%d/loopback' % number
        original = value(self.first, delay)
        record_ = self.result['delay'] = dict(output=number, source=source, points=[])
        on, off = self.route(number, cell_input)
        before = self.case_before(number)
        partners = [n for n in (number - 1, number + 1)
                    if 1 <= n <= 20 and '/output/%d/roomeq/delay' % n in self.first]
        undo = [*off, self.given_back(delay), (loopback, 'i', (before,))]
        undo += [self.given_back('/output/%d/roomeq/delay' % n) for n in partners]
        with self.lease() as connection:
            self.write(connection, *on, (loopback, 'i', (1,)), undo=undo)
            try:
                values = list(self.args.delays or DELAYS)
                for index, setting in enumerate(values):
                    self.write(connection, (delay, original[0], (setting,)))
                    found = self.analysed(connection, source, number, length=16384, max_lag=8000)
                    record_['points'].append(dict(osc=setting, **found))
                    if index == len(DELAYS) - 1 and not self.args.delays:
                        done = [(p['osc'], p['lag_ms']) for p in record_['points']]
                        slope, _ = delay_slope(done)
                        if slope * DELAY_ENDPOINT < 150:
                            values.append(DELAY_ENDPOINT)
            finally:
                self.write(connection, *undo)
        points = [(p['osc'], p['lag_ms']) for p in record_['points'] if p['verdict'] == 'carried']
        slope, intercept = delay_slope(points)
        residual = max(abs(y - (slope * x + intercept)) for x, y in points)
        record_.update(ms_per_osc_unit=round(slope, 3), path_latency_ms=round(intercept, 4),
                       residual_ms=round(residual, 4),
                       carried=len(points) == len(record_['points']))
        self.checkpoint()

    def case_before(self, number):
        for case in self.result['cases']:
            if case['output'] == number:
                return case['given_back']
        raise ValueError('measure output %d before its delay' % number)

    def finish(self):
        """Give back every path written, from the first dump (loopback: as measured)."""
        with self.lease() as connection:
            if self.restore:
                self.write(connection, *self.restore.values())
            drain(connection, 1.0)
            last = snapshot(connection)
        difference = compare(self.first, last)
        self.result['final_dump'] = dict(paths=len(last), sha256=digest(last), **difference)
        self.result['restored'] = not any(difference.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--source', type=int, default=1)
    parser.add_argument('--alternate', type=int, default=3)
    parser.add_argument('--gain', type=float, default=GAIN)
    parser.add_argument('--outputs', default='1-20')
    parser.add_argument('--delay-output', type=int, default=3)
    parser.add_argument('--delays', type=lambda text: [float(x) for x in text.split(',')],
                        help='OSC values of the delay sweep instead of the default; '
                             '0.001 OSC units = 0.01 ms, the manual allows up to 4.2 (42 ms)')
    parser.add_argument('--backend-build', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'build/coordinated')
    args = parser.parse_args()
    low, _, high = args.outputs.partition('-')
    outputs = list(range(int(low), int(high or low) + 1))
    if not ({args.source, args.alternate} == {1, 3} and set(outputs) <= set(range(1, 21))
            and args.delay_output in outputs and 0 <= args.gain <= 75
            and all(0 <= x <= 4.2 for x in args.delays or ())):
        parser.error('the sources are the input pairs 1/2 and 3/4, outputs within 1..20, '
                     'the delay output among them, gain 0..75 dB, delays 0..4.2')
    plan = dict(outputs=outputs, source=args.source, alternate=args.alternate, gain=args.gain,
                delay_output=args.delay_output,
                delays=args.delays or [*DELAYS, DELAY_ENDPOINT])
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    if args.out is None:
        parser.error('--execute requires --out')
    result = dict(schema=1, kind='output-loopback-probes-ucx2', plan=plan, cases=[], sources={},
                  started=datetime.now(UTC).isoformat(), hardware_used=False)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)

    def checkpoint():
        args.out.write_text(json.dumps(result, indent=1, allow_nan=False) + '\n')

    probe = None
    try:
        path = args.config or discover_config_path()
        probe = Probe(args, load_config(path, strict=True), path, result, checkpoint)
        probe.start()
        result['hardware_used'] = True
        with probe.lease() as connection:
            probe.sources(connection)
        for number in outputs:
            probe.output(number)
        probe.delay()
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
        result['passed'] = (not result.get('error') and result.get('restored') is True
                            and all(c.get('passed') for c in result['cases'])
                            and result.get('delay', {}).get('carried') is True)
        checkpoint()
    print('probe-loopback: %s; restored=%s -> %s' % ('passed' if result['passed'] else 'FAILED',
                                                     result.get('restored'), args.out))
    if result.get('restored') is False:
        print('probe-loopback: RESTORATION UNCONFIRMED; stop and inspect the device',
              file=sys.stderr)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
