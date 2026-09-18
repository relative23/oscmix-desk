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

Usage (needs a Fireface, a running backend, and UDP 8222 free -- close
the mixer GUI first, it holds that port):

    python3 scripts/record-dump.py --out tests/data/refresh-dump.json

Exits 77 when the port is taken or nothing arrives, so it is safe to
call on a machine with no device.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oscmix_desk.constants import (
    DEFAULT_OSC_PORT,
    DEFAULT_OSC_RECV_PORT,
    DEFAULT_USB_ID,
)
from oscmix_desk.discovery import device_firmware
from oscmix_desk.osc import decode_osc, encode_osc, iter_osc_messages

EXIT_SKIP = 77
# Long enough to catch a meter cycle, short enough not to be a wait.
BASELINE_SECONDS = 4.0
# The dump is over when nothing but streamed registers has arrived for
# this long. Generous on purpose: the prose in this repository has said
# "15-20 s" since 0.1.x, and the measurement below says 1.9 s. Whichever
# is right, the window has to outlast it.
QUIET_SECONDS = 6.0
MAX_SECONDS = 90.0


def running_matches_build(build_dir: Path) -> Optional[str]:
    """Whether the installed backend is the binary in ``build_dir``.

    The revision below is read from the *source* tree, and a recording
    labelled with a revision it was not taken against is worse than an
    unlabelled one -- ADR 0008 rests on that label. This caught itself
    once: a new upstream revision was built in a scratch directory and
    installed, `build/oscmix` was left where it was, and the recording
    came out stamped with the old SHA.

    Returns a complaint, or None when they agree or cannot be compared.
    """
    built = build_dir / "oscmix"
    installed = Path.home() / ".local" / "bin" / "oscmix"
    if not built.is_file() or not installed.is_file():
        return None
    if built.read_bytes() == installed.read_bytes():
        return None
    return ("%s and %s differ, so the revision below would be a guess -- "
            "rebuild or reinstall before recording" % (built, installed))


def backend_revision(build_dir: Path) -> Optional[str]:
    """The commit the running backend was built from, if it can be told."""
    try:
        result = subprocess.run(
            ["git", "-C", str(build_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def bind_or_skip(recv_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", recv_port))
    except OSError as exc:
        sock.close()
        if exc.errno != errno.EADDRINUSE:
            # Not the GUI, and not a reason to skip (ADR 0025).
            print("record-dump: cannot bind the receive port UDP %d: %s"
                  % (recv_port, exc.strerror or exc), file=sys.stderr)
            raise SystemExit(1) from None
        print("record-dump: UDP %d is in use -- close the mixer GUI, it "
              "holds that port" % recv_port, file=sys.stderr)
        raise SystemExit(EXIT_SKIP) from None
    sock.settimeout(0.5)
    return sock


def collect(sock: socket.socket, seconds: float) -> Set[str]:
    """Every register path seen in a window, with nothing requested."""
    seen: Set[str] = set()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            datagram, _ = sock.recvfrom(65536)
        except socket.timeout:
            continue
        for message in iter_osc_messages(datagram):
            try:
                path, _tags, _args = decode_osc(message)
            except ValueError:
                continue
            seen.add(path)
    return seen


def record_dump(sock: socket.socket, send_port: int, streamed: Set[str]
                ) -> Tuple[Dict[str, Tuple[str, float]], float]:
    """Send /refresh and time the first arrival of every register."""
    first: Dict[str, Tuple[str, float]] = {}
    started = time.monotonic()
    sock.sendto(encode_osc("/refresh"), ("127.0.0.1", send_port))
    last_new = started
    while time.monotonic() - started < MAX_SECONDS:
        if time.monotonic() - last_new > QUIET_SECONDS:
            break
        try:
            datagram, _ = sock.recvfrom(65536)
        except socket.timeout:
            continue
        for message in iter_osc_messages(datagram):
            try:
                path, tags, _args = decode_osc(message)
            except ValueError:
                continue
            if path in first:
                continue
            first[path] = (tags, round(time.monotonic() - started, 2))
            # Streamed registers arrive whatever happens, so they must
            # not keep the window open -- that is what made the first
            # recording run for the full timeout.
            if path not in streamed:
                last_new = time.monotonic()
    return first, round(last_new - started, 1)


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


def read_one(sock: socket.socket, port: int, wanted: str,
             seconds: float = 3.0) -> Optional[object]:
    """One register's value from a fresh /refresh, or None."""
    sock.sendto(encode_osc("/refresh"), ("127.0.0.1", port))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            datagram, _ = sock.recvfrom(65536)
        except (socket.timeout, OSError):
            continue
        for message in iter_osc_messages(datagram):
            try:
                path, _tags, args = decode_osc(message)
            except (ValueError, struct.error):
                continue
            if path == wanted and args:
                return args[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_OSC_PORT)
    parser.add_argument("--recv-port", type=int, default=DEFAULT_OSC_RECV_PORT)
    parser.add_argument("--device", default="Fireface UCX II")
    args = parser.parse_args()

    build_dir = Path(__file__).resolve().parent.parent / "build" / "oscmix"
    complaint = running_matches_build(build_dir)
    if complaint:
        print("record-dump: %s" % complaint, file=sys.stderr)
        return 2

    sock = bind_or_skip(args.recv_port)
    try:
        print("listening %.0fs without asking, to find the streamed "
              "registers..." % BASELINE_SECONDS)
        streamed = collect(sock, BASELINE_SECONDS)
        print("  %d register(s) stream on their own" % len(streamed))
        print("sending /refresh...")
        first, duration = record_dump(sock, args.port, streamed)
        dspvers = read_one(sock, args.port, "/hardware/dspvers")
    finally:
        sock.close()

    if not first:
        print("record-dump: nothing arrived on UDP %d -- is the backend "
              "running?" % args.recv_port, file=sys.stderr)
        return EXIT_SKIP

    fixture = {
        "recorded": time.strftime("%Y-%m-%d"),
        "device": args.device,
        "oscmix_revision": backend_revision(build_dir),
        # The fixture holds shape, not values, but the firmware is
        # provenance rather than mixer state: a device that reports a
        # different set of registers after an update is a different
        # recording, and the file has to say which one it is.
        "firmware": device_firmware(
            DEFAULT_USB_ID,
            Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices")),
            {"/hardware/dspvers": dspvers}),
        "dump_seconds": duration,
        "note": [
            "Register shape and arrival times, never values -- values are",
            "the user's mixer state, and the question this answers is",
            "which registers a dump reports and how soon.",
            "",
            "'streamed' means the register arrives on its own, without a",
            "/refresh (the level meters). 'first_seen' is seconds after",
            "the /refresh went out; for streamed registers it means",
            "nothing and is recorded only for completeness.",
            "",
            "Recorded by scripts/record-dump.py against the pinned oscmix",
            "revision above. Re-record when the pin moves (ADR 0008).",
            "",
            "MEASURED, and it contradicts what this repository says:",
            "the whole dump finishes in ~1.9 s, and /playback/*/stereo",
            "arrives at 0.0 s -- not 'near the end of a dump that streams",
            "for many seconds', which is what register_promptly_reported",
            "claims. Confirmed against a cold backend (restart + immediate",
            "/refresh) and passively (restart, listen 45 s, send nothing):",
            "2006 registers, all within ~2 s, no 15 s tail.",
            "",
            "The cold device -- the condition this recording could not",
            "cover, since only the backend was restarted -- is measured",
            "separately in tests/data/cold-plug-timeline.json, captured",
            "across a real USB replug on both OSC ports. It agrees: the",
            "dump is over in ~4 s. LINK_SYNC_BLIND_DELAY is 5 s on that",
            "evidence, see ADR 0010.",
        ],
        # [type tags, seconds after the /refresh]. One line per register:
        # a fixture nobody can read is a fixture nobody reviews.
        "registers": {path: [tags, seen]
                      for path, (tags, seen) in sorted(first.items())},
        "streamed": sorted(streamed),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(_render(fixture))
    print("dump finished after %.1fs: %d registers (%d streamed) -> %s"
          % (duration, len(first), len(streamed), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
