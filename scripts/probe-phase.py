#!/usr/bin/env python3
"""Measure what the phase switches do to the signal, through loopback (--execute: live state).

The same safety conditions, signal and restoration as probe-loopback.py: UCX on, monitors off,
headphones disconnected, phantom power off; the noise of input pairs 1/2 and 3/4 carried by one
readable mix cell to the output under test, with loopback on; every register written given back
and a final dump compared with the first. No tone is played and the playback matrix is not written.

The sign of the correlation peak between the source input's capture channel and the looped-back
output is the polarity of the path between them (positive on every output with phase off,
2026-11-05). Per case the switch is turned on and off again:

- ``/output/N/phase 1`` turns the peak negative if it inverts output N before the loopback tap
  (after Room EQ, before the output fader);
- ``/input/S/phase 1`` turns it negative if it inverts either the recorded input or its share of
  the mix, but not both; it stays positive if it inverts both or neither.

The running backend's identity is part of the evidence: a backend that only writes the register
and one that also turns the mix coefficients around (as TotalMix FX does on this interface) give
different answers, and the record says which one ran.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('probe_loopback', HERE / 'probe-loopback.py')
LOOP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOOP)

from loopback_signal import PRESENT  # noqa: E402 -- after probe-loopback set the path

from oscmix_desk.config import load_config  # noqa: E402
from oscmix_desk.errors import ConfigError, DeviceLockUnavailable, WriteFailed  # noqa: E402
from oscmix_desk.paths import discover_config_path  # noqa: E402


def card_rate(card):
    """The UCX II capture stream's rate as ALSA has it open now; None when it is closed."""
    for line in (card / 'pcm0c/sub0/hw_params').read_text().splitlines():
        if line.startswith('rate:'):
            return int(line.split()[1])
    return None


def ucx_card(serial):
    """/proc/asound/cardN of the interface, by the serial in its ALSA name."""
    found = [Path('/proc/asound') / ('card' + line.split()[0])
             for line in Path('/proc/asound/cards').read_text().splitlines()
             if line[:3].strip().isdigit() and serial in line]
    if len(found) != 1:
        raise ValueError('need exactly one ALSA card of the UCX II, found %d' % len(found))
    return found[0]


def recorded(connection, node, card):
    """probe-loopback's recording, with the device rate read twice while pw-record runs.

    PipeWire may run the interface at another rate when a client asks for one (an allowed-rates
    list beyond 48000 lets it); a stream recorded at 48000 is then resampled and its correlation
    proves nothing about the switch under test.
    """
    channel_map = ','.join('AUX%d' % i for i in range(LOOP.CHANNELS))
    rates = []
    with tempfile.TemporaryFile() as out:
        process = subprocess.Popen(
            ['pw-record', '--raw', '--target=' + node, '--rate=%d' % LOOP.RATE,
             '--channels=%d' % LOOP.CHANNELS, '--channel-map=' + channel_map, '--format=s32',
             '-'], stdout=out, stderr=subprocess.PIPE)
        try:
            LOOP.drain(connection, 0.3)
            rates.append(card_rate(card))
            LOOP.drain(connection, LOOP.RECORD_SECONDS - 0.3)
            rates.append(card_rate(card))
        finally:
            process.terminate()
            process.wait(timeout=5)
        out.seek(0)
        raw = out.read()
    if len(raw) < 4 * LOOP.CHANNELS * LOOP.RATE // 2:
        raise ValueError('pw-record delivered %d bytes' % len(raw))
    return raw, rates


def polarity(off, on):
    """What turning a switch on did to the sign of the peak."""
    if ('undetermined' in (off['verdict'], on['verdict'])
            or abs(off['peak']) < PRESENT or abs(on['peak']) < PRESENT):
        return 'undetermined'
    return 'inverted' if (off['peak'] > 0) != (on['peak'] > 0) else 'unchanged'


class PhaseProbe(LOOP.Probe):
    def analysed(self, connection, source, target, **kwargs):
        if not hasattr(self, 'card'):
            self.card = ucx_card(self.result['serial'])
        raw, rates = recorded(connection, self.node, self.card)
        found = LOOP.analyse(raw, source, target, **kwargs)
        found['device_rate'] = rates
        if rates != [LOOP.RATE, LOOP.RATE]:
            found['verdict'] = 'undetermined'
        return found

    def route(self, output, source):
        """probe-loopback's route, with the other source pair's cells to ``output`` turned off.

        Both source pairs run at raised gain for the whole probe. Input 3/4 noise is about 40 dB
        below input 1/2's (their gain stops at 24 dB), so an existing input 1/2 cell on outputs
        1/2 buried it (2026-11-07). The cells are readable and given back like the rest.
        """
        on, off = super().route(output, source)
        cell = output - 1 if output % 2 == 0 and self.linked('output', output) else output
        mine = {source, source + 1} if self.linked('input', source) else {source}
        for other in self.inputs():
            path = '/mix/%d/input/%d' % (cell, other)
            if other in mine or path not in self.first:
                continue
            given = LOOP.mix_level(LOOP.value(self.first, path)[1])
            if given[0] > LOOP.FLOOR:
                on.append((path, 'fi', (LOOP.FLOOR, 0)))
                off.append((path, 'fi', given))
        return on, off

    def partners(self, kind, number):
        """The switch and, on a linked pair, its partner's: both are given back."""
        odd = number - (number - 1) % 2
        pair = (odd, odd + 1) if self.linked(kind, odd) else (number,)
        return ['/%s/%d/phase' % (kind, n) for n in pair
                if '/%s/%d/phase' % (kind, n) in self.first]

    def phase(self, kind, output):
        cell_input, source = self.feed(output)
        number = output if kind == 'output' else source
        switch = '/%s/%d/phase' % (kind, number)
        if LOOP.flag(self.first, switch):
            raise ValueError(switch + ' must read 0 before the probe')
        loopback = '/output/%d/loopback' % output
        case = dict(kind=kind, switch=switch, output=output, source=source, mix_input=cell_input)
        self.result['cases'].append(case)
        on, off = self.route(output, cell_input)
        undo = [*off, *(self.given_back(p) for p in self.partners(kind, number))]
        with self.lease() as connection:
            self.write(connection, *on, undo=undo)
            try:
                case['before'] = self.analysed(connection, source, output)
                given = 1 if case['before']['verdict'] == 'carried' else 0
                case['given_back'] = given
                self.write(connection, (loopback, 'i', (1,)), undo=[(loopback, 'i', (given,))])
                case['phase_off'] = self.analysed(connection, source, output)
                self.write(connection, (switch, 'i', (1,)))
                case['phase_on'] = self.analysed(connection, source, output)
                self.write(connection, (switch, 'i', (0,)))
                case['phase_off_again'] = self.analysed(connection, source, output)
            finally:
                back = [(loopback, 'i', (case['given_back'],))] if 'given_back' in case else []
                self.write(connection, *back, *undo)
        case['verdict'] = polarity(case['phase_off'], case['phase_on'])
        case['reversible'] = polarity(case['phase_off'], case['phase_off_again']) == 'unchanged'
        case['passed'] = (case['phase_off']['verdict'] == 'carried' and case['reversible']
                          and case['verdict'] != 'undetermined')
        self.checkpoint()

    def levels(self, output, levels):
        """The sign of one cell at each level: the coefficient word changes form at 0.5.

        Bits 0-14 of a coefficient are a 15-bit two's complement, bit 15 a x8 range flag
        (TotalMix FX on Windows, 2026-11-06). Above 0.5 the backend writes the compressed form;
        at exactly 0.5 (-6.0206 dB) it writes 0x4000, which that format reads as -0.5.
        """
        cell_input, source = self.feed(output)
        loopback = '/output/%d/loopback' % output
        case = dict(kind='level', output=output, source=source, mix_input=cell_input, points=[],
                    input_linked=self.linked('input', cell_input - (cell_input - 1) % 2),
                    output_linked=self.linked('output', output - (output - 1) % 2))
        self.result['cases'].append(case)
        on, off = self.route(output, cell_input)
        cell = on[0][0]
        with self.lease() as connection:
            self.write(connection, *on, undo=off)
            try:
                case['before'] = self.analysed(connection, source, output)
                case['given_back'] = 1 if case['before']['verdict'] == 'carried' else 0
                self.write(connection, (loopback, 'i', (1,)),
                           undo=[(loopback, 'i', (case['given_back'],))])
                # -6.0206 dB is a coefficient of exactly 0.5 only where the cell's gain
                # is the level itself (width 100, or mono to mono): measured at 0 dB first.
                self.write(connection, (cell, 'fi', (0.0, 0)))
                unity = self.analysed(connection, source, output)
                case['unity'] = unity
                gain = (unity['target_dbfs'] - unity['source_dbfs']
                        if None not in (unity['target_dbfs'], unity['source_dbfs']) else None)
                case['unity_gain_db'] = gain
                if unity['verdict'] != 'carried' or gain is None or abs(gain) > 0.05:
                    raise ValueError('the cell is not at unity gain (%s dB); its levels would '
                                     'not hit the coefficients asked for' % gain)
                for level in levels:
                    self.write(connection, (cell, 'fi', (level, 0)))
                    case['points'].append(dict(level_db=level,
                                               **self.analysed(connection, source, output)))
            finally:
                back = [(loopback, 'i', (case['given_back'],))] if 'given_back' in case else []
                self.write(connection, *back, *off)
        signs = [p['peak'] > 0 for p in case['points'] if p['verdict'] == 'carried']
        case['signs'] = ['+' if sign else '-' for sign in signs]
        case['passed'] = len(signs) == len(levels)
        self.checkpoint()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--gain', type=float, default=LOOP.GAIN)
    parser.add_argument('--outputs', default='1-20', help='outputs whose phase is switched')
    parser.add_argument('--input-outputs', default='1-4',
                        help='outputs carrying the source inputs whose phase is switched')
    parser.add_argument('--levels', default='',
                        help='cell levels in dB for a sign case, e.g. -6.1,-6.0206')
    parser.add_argument('--level-output', type=int, default=3)
    parser.add_argument('--no-phase', action='store_true', help='only the level case')
    parser.add_argument('--backend-build', type=Path,
                        default=HERE.parent / 'build/coordinated')
    args = parser.parse_args()
    args.source, args.alternate = 1, 3

    def span(text):
        low, _, high = text.partition('-')
        return list(range(int(low), int(high or low) + 1))

    outputs, carriers = ([], []) if args.no_phase else (span(args.outputs),
                                                        span(args.input_outputs))
    levels = [float(item) for item in args.levels.split(',') if item.strip()]
    if not (set(outputs) | set(carriers) | {args.level_output} <= set(range(1, 21))
            and 0 <= args.gain <= 75 and all(-65 <= level <= 6 for level in levels)
            and (outputs or levels)):
        parser.error('outputs within 1..20, gain 0..75 dB, levels -65..6 dB, something to do')
    plan = dict(outputs=outputs, input_outputs=carriers, source=1, alternate=3, gain=args.gain,
                levels=levels, level_output=args.level_output)
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    if args.out is None:
        parser.error('--execute requires --out')
    result = dict(schema=1, kind='phase-probes-ucx2', plan=plan, cases=[], sources={},
                  started=datetime.now(UTC).isoformat(), hardware_used=False)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)

    def checkpoint():
        args.out.write_text(json.dumps(result, indent=1, allow_nan=False) + '\n')

    probe = None
    try:
        path = args.config or discover_config_path()
        probe = PhaseProbe(args, load_config(path, strict=True), path, result, checkpoint)
        probe.start()
        result['hardware_used'] = True
        with probe.lease() as connection:
            probe.sources(connection)
        for number in outputs:
            probe.phase('output', number)
        seen = set()
        for number in carriers:
            if probe.feed(number)[1] not in seen:
                seen.add(probe.feed(number)[1])
                probe.phase('input', number)
        if levels:
            probe.levels(args.level_output, levels)
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
                            and bool(result['cases'])
                            and all(c.get('passed') for c in result['cases']))
        checkpoint()
    verdicts = sorted({(c['kind'], c.get('verdict') or ''.join(c.get('signs', ())))
                       for c in result['cases']})
    print('probe-phase: %s; restored=%s; %s -> %s'
          % ('passed' if result['passed'] else 'FAILED', result.get('restored'), verdicts,
             args.out))
    if result.get('restored') is False:
        print('probe-phase: RESTORATION UNCONFIRMED; stop and inspect the device', file=sys.stderr)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
