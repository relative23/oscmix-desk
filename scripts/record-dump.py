#!/usr/bin/env python3
"""Record what a ``/refresh`` dump reports, and when, as a test fixture.

Roadmap item L. ``register_promptly_reported`` decides whether a missing
register is a warning or a note. It is a hand-maintained list, measured
once against a UCX II and checked against nothing since. Now that the
backend revision is pinned, a dump from exactly that revision can be
recorded, and the classification becomes a test against a measurement.

Two things are recorded, and neither is a register *value*: values are
the user's mixer state and have no business in a repository, and the
question here is which registers appear and how soon -- not what they
say.

**Which registers stream on their own.** The device pushes level meters
continuously, whether or not anything asked. So this listens first
*without* sending ``/refresh`` and marks everything that arrives as
streamed. Otherwise the meters drown the dump: a first recording caught
54970 messages in 60 s, 6389 of them from four meter registers, and the
dump never went quiet because the meters never stop.

**When each register first arrives**, relative to the ``/refresh``. That
is the number ``register_promptly_reported`` encodes as a yes/no, and
the reason the ``/playback/*`` family is classified the way it is.

Usage (needs a coordinated backend, the matching prepared build, and a
confirmed safe monitoring setup):

    python3 scripts/record-dump.py --backend-build build/coordinated \
        --out tests/data/refresh-dump.json

Cached/derived reports are recorded separately and never called hardware
confirmation. The operation holds the same file lock and backend lease as
desk; GTK can stay open and receives the same reports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measurement import build_evidence, observations

from oscmix_desk import discover_config_path, load_config
from oscmix_desk.backend import connect_backend
from oscmix_desk.discovery import device_firmware, resolve_device
from oscmix_desk.locking import take_device_lock

EXIT_SKIP = 77
# Long enough to catch a meter cycle, short enough not to be a wait.
BASELINE_SECONDS = 4.0
# The dump is over when nothing but streamed registers has arrived for
# this long. Generous on purpose: the prose in this repository has said
# "15-20 s" since 0.1.x, and the measurement below says 1.9 s. Whichever
# is right, the window has to outlast it.
QUIET_SECONDS = 6.0
MAX_SECONDS = 60.0


def collect(connection, seconds: float) -> Set[str]:
    """Paths seen without a refresh, including explicitly marked meter data."""
    seen: Set[str] = set()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for path, _tags, _args, _origin, _sequence in observations(connection, .25):
            seen.add(path)
    return seen


def record_dump(connection, streamed: Set[str]):
    """Time each first DEVICE report separately from cached and meter reports."""
    by_origin: Dict[int, Dict[str, Tuple[str, float]]] = {1: {}, 2: {}, 4: {}}
    connection.request_dump()
    started = last_new = time.monotonic()
    dspvers = None
    while time.monotonic() - started < MAX_SECONDS:
        if time.monotonic() - last_new > QUIET_SECONDS:
            break
        for path, tags, args, origin, _sequence in observations(connection, .25):
            if origin == 1 and path == '/hardware/dspvers' and args:
                dspvers = args[0]
            first = by_origin[origin]
            if path in first:
                continue
            first[path] = (tags, round(time.monotonic() - started, 2))
            if path not in streamed:
                last_new = time.monotonic()
    return by_origin, round(last_new - started, 1), dspvers


def _render(fixture: dict) -> str:
    """Pretty-print with one register per line.

    json.dumps(indent=2) puts every list element on its own line, which
    turns 2000 registers into 8000 lines of noise; indent=None turns it
    into one unreviewable line. This is the middle.
    """
    parts = []
    for key, value in fixture.items():
        if key == "registers":
            rows = ",\n".join('    %s: %s' % (json.dumps(path), json.dumps(entry))
                               for path, entry in value.items())
            parts.append('  "registers": {\n%s\n  }' % rows)
        elif key == "streamed":
            rows = ",\n".join("    " + json.dumps(path) for path in value)
            parts.append('  "streamed": [\n%s\n  ]' % rows)
        else:
            parts.append("  %s: %s" % (json.dumps(key),
                                       json.dumps(value, indent=2)
                                       .replace("\n", "\n  ")))
    return "{\n" + ",\n".join(parts) + "\n}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--backend-build", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build/coordinated")
    args = parser.parse_args()
    config_path = args.config or discover_config_path()
    config = load_config(config_path)
    proc = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    interface = resolve_device(config.usb_id, config.device_name, config.serial, proc)
    lock = take_device_lock(config_path, interface.key)
    if lock is None:
        print("record-dump: device lock unavailable", file=sys.stderr)
        return 1
    connection = None
    try:
        connection = connect_backend(config, config_path, sources=7)
        evidence = build_evidence(connection, args.backend_build, proc)
        connection.begin()
        print("listening %.0fs before requesting a refresh..." % BASELINE_SECONDS)
        streamed = collect(connection, BASELINE_SECONDS)
        recorded, duration, dspvers = record_dump(connection, streamed)
        connection.finish()
    except (OSError, ValueError) as exc:
        print("record-dump: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
        lock.release()

    if not recorded[1]:
        print("record-dump: no device-origin register report arrived", file=sys.stderr)
        return EXIT_SKIP
    fixture = {
        "schema": 2,
        "recorded": time.strftime("%Y-%m-%d"),
        "device": config.device_name,
        "serial": interface.serial,
        "oscmix_revision": evidence['upstream'],
        "running_backend": evidence,
        "firmware": device_firmware(
            config.usb_id,
            Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices")),
            {"/hardware/dspvers": dspvers}),
        "dump_seconds": duration,
        "note": [
            "Register shape and arrival times only; mixer values are private.",
            "registers contains DEVICE-origin reports; derived and meters are separate.",
            "DEVICE identifies the decoding path, not an atomic hardware snapshot.",
            "A first report after the request ACK can be unsolicited device traffic.",
            "Cached/derived reports do not confirm hardware state.",
        ],
        "registers": dict(sorted(recorded[1].items())),
        "derived": dict(sorted(recorded[2].items())),
        "meters": dict(sorted(recorded[4].items())),
        "streamed": sorted(streamed),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(_render(fixture))
    print("dump finished after %.1fs: %d device-origin registers -> %s"
          % (duration, len(recorded[1]), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
