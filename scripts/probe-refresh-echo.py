#!/usr/bin/env python3
"""Count the coefficient words a refresh makes the backend send (--execute, then --assess).

Upstream oscmix writes every reported input-mix cell back as a coefficient: a refresh of the UCX II
reports 800 mix words, and each became a coefficient write, rounded to the report's 0.1 dB.
Patch 0019 writes only what the cache would not publish. ``--execute`` (live, under a usbmon
capture) writes one readable input cell off the 0.1 dB grid, requests REFRESHES dumps, each in a
timed window, and gives everything back as probe-loopback.py does. ``--assess`` (offline) counts
the host's coefficient words in every window and lists those of the off-grid cell.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('probe_loopback', HERE / 'probe-loopback.py')
LOOP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOOP)

import usb_midi_words  # noqa: E402 -- after probe-loopback set the path

from oscmix_desk.config import load_config  # noqa: E402
from oscmix_desk.errors import ConfigError, DeviceLockUnavailable, WriteFailed  # noqa: E402
from oscmix_desk.paths import discover_config_path  # noqa: E402

REFRESHES = 5
#: A dump of the UCX II arrives in about 2.5 s with a 100 ms keep-alive.
WINDOW_SECONDS = 4.0
OFF_GRID_DB = -3.05


def input_cell_registers(output, source, linked_output, linked_input):
    """Coefficient registers of one input-matrix cell (1-based), both channels when linked."""
    outputs = (output, output + 1) if linked_output else (output,)
    sources = (source, source + 1) if linked_input else (source,)
    return sorted(0x4000 | (o - 1) << 6 | (s - 1) for o in outputs for s in sources)


class EchoProbe(LOOP.Probe):
    def run(self, output, source):
        cell = '/mix/%d/input/%d' % (output, source)
        registers = input_cell_registers(output, source, self.linked('output', output),
                                         self.linked('input', source))
        record = self.result['off_grid'] = dict(cell=cell, level_db=OFF_GRID_DB,
                                                registers=registers)
        with self.lease() as connection:
            write = dict(start=time.time())
            self.write(connection, (cell, 'fi', (OFF_GRID_DB, 0)),
                       undo=[(cell, 'fi', LOOP.mix_level(LOOP.value(self.first, cell)[1]))])
            write['end'] = time.time()
            record['write'] = write
            for _ in range(REFRESHES):
                window = dict(start=time.time())
                connection.request_dump()
                LOOP.drain(connection, WINDOW_SECONDS)
                window['end'] = time.time()
                self.result['refreshes'].append(window)
                self.checkpoint()


def assess(evidence, capture, bus, device):
    bulks = list(usb_midi_words.bulks(capture, bus, device))
    cell = set(evidence['off_grid']['registers'])

    def inside(window):
        return [(reg, value) for when, words in bulks if window['start'] <= when <= window['end']
                for reg, value in words if 0x4000 <= reg < 0x8000 and (reg & 0x3F) < 20]

    written = inside(evidence['off_grid']['write'])
    evidence['off_grid']['written_words'] = sorted(w for w in written if w[0] in cell)
    for window in evidence['refreshes']:
        words = inside(window)
        window['input_coefficient_words'] = len(words)
        window['off_grid_words'] = sorted(w for w in words if w[0] in cell)
    evidence['capture'] = dict(sha256=hashlib.sha256(capture.read_bytes()).hexdigest(),
                               bytes=capture.stat().st_size, messages=len(bulks), bus=bus,
                               device=device)
    evidence['assessed'] = datetime.now(UTC).isoformat()
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--assess', action='store_true')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--bus', type=int)
    parser.add_argument('--device', type=int)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=int, default=3, help='output of the off-grid cell')
    parser.add_argument('--source', type=int, default=1, help='input of the off-grid cell')
    parser.add_argument('--backend-build', type=Path,
                        default=HERE.parent / 'build/coordinated')
    args = parser.parse_args()
    cell_source = args.source
    args.source, args.alternate, args.gain = 1, 3, 0.0
    if args.execute == args.assess:
        parser.error('one of --execute and --assess')
    if args.assess:
        if None in (args.capture, args.bus, args.device):
            parser.error('--assess needs --capture, --bus and --device')
        evidence = assess(json.loads(args.out.read_text()), args.capture, args.bus, args.device)
        args.out.write_text(json.dumps(evidence, indent=1, allow_nan=False) + '\n')
        counts = [w['input_coefficient_words'] for w in evidence['refreshes']]
        print('probe-refresh-echo: input coefficient words per refresh %s -> %s'
              % (counts, args.out))
        return 0
    result = dict(schema=1, kind='refresh-echo-ucx2', refreshes=[], sources={},
                  started=datetime.now(UTC).isoformat(), hardware_used=False)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)

    def checkpoint():
        args.out.write_text(json.dumps(result, indent=1, allow_nan=False) + '\n')

    probe = None
    try:
        path = args.config or discover_config_path()
        probe = EchoProbe(args, load_config(path, strict=True), path, result, checkpoint)
        probe.start()
        result['hardware_used'] = True
        probe.run(args.output, cell_source)
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
    print('probe-refresh-echo: executed=%s restored=%s -> %s; run --assess next'
          % (result.get('executed'), result.get('restored'), args.out))
    return 0 if result.get('executed') and result.get('restored') else 1


if __name__ == '__main__':
    sys.exit(main())
