#!/usr/bin/env python3
"""Ask the device whether a register it reports is a register it accepts.

Every settable register in the model is declared `verifiable`, which
promises the device reports a write back. The read direction of that
promise is measured -- the recordings prove the model matches the dump.
The write direction never has been, and two of the unchecked promises
turned out to be false: Room EQ accepts writes and ignores them
(upstream #33), and output phase is never put on the wire at all
(upstream #34). Both were found by accident.

This walks every settable register, writes it a different legal value,
and records whether the device answered.

The subtlety is what silence means. The device reports only on *change*,
so a write that quantises onto the value already held is answered with
nothing, and that is correct behaviour rather than a defect. A larger
step separates the two cases: if it reports, the first step was
quantisation; if nothing reports anywhere inside the declared domain,
the register is deaf. So the step size is not a parameter guessed in
advance, it is the discriminator -- and starting small keeps the sweep
quiet on families that sit in the signal path.

`reflevel` is skipped by name. It is the only member of ADR 0016's
dangerous class that is settable at all -- `48v` has no value domain, so
neither a config nor this tool can reach it -- and changing a reference
level on a live output is audible and potentially loud. Skipped
registers are named in the artifact rather than omitted from it.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oscmix_desk import registers as R
from oscmix_desk.backend import loopback
from oscmix_desk.constants import (
    DEFAULT_OSC_PORT,
    DEFAULT_OSC_RECV_PORT,
    DEFAULT_USB_ID,
    DUMP_LISTEN_SETTLE,
)
from oscmix_desk.discovery import (
    built_backend_revision,
    device_firmware,
    device_key,
    device_serial,
)
from oscmix_desk.profiles import take_device_lock

#: Steps as a fraction of the declared range, smallest first. One percent
#: is below the quantisation of several families, which is the point: it
#: produces the ambiguous silence this tool exists to resolve, and the
#: escalation resolves it.
STEPS = (0.01, 0.10, 0.50)

#: Absolute steps for registers upstream declares with no bounds, in the
#: register's own unit. A guessed range would be worse than none: it
#: would reject values the device accepts.
UNBOUNDED_STEPS = (1.0, 10.0, 50.0)

#: Seconds between writes inside a pass. A burst is dropped: oscmix
#: turns each write into a MIDI SysEx message, and that wire carries
#: roughly a thousand registers a second -- the measured rate of a
#: refresh dump. Sending 295 writes in a few milliseconds lost 40 of
#: them here, and those losses read as `ignored` on registers that
#: accept the write perfectly well when asked one at a time. Pacing is
#: what makes the difference between measuring the device and measuring
#: this tool's own overrun.
WRITE_PACE = 0.010

#: Registers this tool refuses to touch, matched on the last path
#: segment. See ADR 0016.
DANGEROUS = ("reflevel",)

#: What the artifact records about how the numbers were taken. In the
#: artifact itself rather than only in the docs, because the file
#: outlives the session that produced it.
METHOD = ("Each register is written a different legal value from its own "
          "declared domain and the result read back from a refresh dump. "
          "An echo on the written path is never waited for: the device "
          "answers a linked pair on the partner path and is silent on the "
          "one addressed. Passes are split by channel parity so a linked "
          "pair is never written against itself, writes are paced because "
          "a burst is dropped, and every register gets three attempts. "
          "Each pass restores what it wrote before the next begins.")


def is_dangerous(path: str) -> bool:
    """True for registers ADR 0016 keeps out of reach of a text file."""
    return path.rsplit("/", 1)[-1] in DANGEROUS


def _number_candidates(register: R.Register,
                       current: float) -> List[Tuple[float, float]]:
    """Escalating (value, step) pairs for a NUMBER, all inside bounds.

    The direction is away from the nearer bound, so a large step never
    clips against one. That is also why no candidate needs filtering
    against `current`: clipping can only happen at the *far* bound, which
    is not where the value started. A guard for it was written, found to
    be unreachable by any test, and removed -- the invariant it protected
    is held by the direction rule and by every step being non-zero.

    Where upstream declares no bounds, absolute steps stand in:
    inventing a range here would reject values the device accepts, which
    is the mistake the model avoids everywhere else.
    """
    if register.lo is None or register.hi is None:
        return [(current + step, step) for step in UNBOUNDED_STEPS]
    span = register.hi - register.lo
    if span <= 0:
        return []
    up = (current - register.lo) <= (register.hi - current)
    out = []
    for fraction in STEPS:
        step = span * fraction
        value = current + step if up else current - step
        value = min(register.hi, max(register.lo, value))
        out.append((value, step))
    return out


def _enum_candidates(register: R.Register, current: object) -> List[Tuple[object, float]]:
    """The next value, then the most distant one.

    "Larger" has no meaning for an enum, so the escalation is distance in
    the declared order instead: a neighbour first, then the far end, on
    the same reasoning that a bigger change is harder for the device to
    quantise away.
    """
    wire = register.values or tuple(range(len(register.choices)))
    others = [v for v in wire if v != current]
    if not others:
        return []
    if len(others) == 1:
        return [(others[0], 1.0)]
    return [(others[0], 1.0), (others[-1], 1.0)]


def candidates(register: R.Register, current: object) -> List[Tuple[object, float]]:
    """Legal values to try, smallest change first.

    Each pair is the value to write and the size of the change it makes,
    which `verdict` needs to tell "the device followed the write" from
    "the device moved somewhere else of its own accord".
    """
    if register.domain == R.BOOL:
        return [(0 if current else 1, 1.0)]
    if register.domain == R.ENUM:
        return _enum_candidates(register, current)
    if register.domain == R.NUMBER:
        try:
            return list(_number_candidates(register, float(current)))
        except (TypeError, ValueError):
            return []
    return []


def _followed(written: object, reported: object, step: float) -> bool:
    """True when the report is the value that was asked for.

    The test is whether the device landed nearer the written value than
    half the distance the write moved, which reads the same for all three
    domains: a fixed-point register quantises to within a fraction of the
    step, and an enum or bool with a step of 1.0 has to match exactly.
    """
    try:
        return abs(float(reported) - float(written)) < max(step, 1.0) / 2.0
    except (TypeError, ValueError):
        return reported == written


def verdict(path: str, current: object,
            attempts: Sequence[Tuple[object, float, object]],
            bounded: bool = True) -> Dict[str, object]:
    """What a register's attempts mean.

    Four outcomes, and the two in the middle are why this is worth
    running. `clamped` is the device disagreeing with a bound *this
    model* declares -- a defect in the table rather than in the stack.
    `ignored` is a write that goes nowhere, which is what Room EQ and
    output phase both look like from here, for entirely different reasons
    and in different components. This tool does not attribute it; a
    trace does. It says only that the promise is not kept.
    """
    finding: Dict[str, object] = {"path": path, "current": current,
                                  "attempts": len(attempts)}
    if not attempts:
        finding["verdict"] = "undetermined"
        finding["detail"] = "no legal alternative value exists"
        return finding
    for index, (written, step, reported) in enumerate(attempts):
        if reported is None:
            continue
        if _followed(written, reported, step):
            finding["verdict"] = "confirmed"
            finding["step"] = index + 1
            finding["wrote"] = written
            return finding
        finding["verdict"] = "clamped"
        finding["step"] = index + 1
        finding["wrote"] = written
        finding["reported"] = reported
        return finding
    if not bounded:
        # Nothing moved, and the probe values came from UNBOUNDED_STEPS
        # rather than from a declared range -- so "deaf" and "every
        # value I tried was out of range" are indistinguishable here.
        # This is not hypothetical: /reverb/width sits at 0.6 on a 0..1
        # scale, the absolute steps asked for 1.6, 10.6 and 50.6, and
        # the device refused all three without a word. Calling that
        # `ignored` reported a defect that did not exist.
        finding["verdict"] = "undetermined"
        finding["detail"] = ("no declared range; every probe value was "
                             "refused, which may mean out of range")
        finding["wrote"] = [a[0] for a in attempts]
        return finding
    finding["verdict"] = "ignored"
    finding["wrote"] = [a[0] for a in attempts]
    return finding


def skipped(path: str, reason: str) -> Dict[str, object]:
    """A register the sweep declined to touch, kept in the artifact.

    Reporting 1228 of 1228 while quietly omitting fourteen would be a
    worse artifact than one that names what it did not do.
    """
    return {"path": path, "verdict": "skipped", "detail": reason}


STREAMING = ("/level", "/meter")


def read_all(device, listener, seconds: float = 6.0) -> Dict[str, object]:
    """Every register the backend reports, as a path -> value map."""
    seen: Dict[str, object] = {}
    time.sleep(DUMP_LISTEN_SETTLE)
    device.request_dump()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for path, _tags, args in listener.messages(0.25):
            if not path.endswith(STREAMING) and args:
                seen[path] = args[0]
    return seen


def as_tag(value: object, tags: str) -> object:
    """Coerce a candidate to the type its register's tag declares.

    A float on an `,i` register is not the same message, and the device
    answers a malformed one with silence -- which this tool would then
    report as `ignored`. A type slip here manufactures the exact defect
    it is looking for.
    """
    if tags[:1] == "i":
        return round(float(value))
    if tags[:1] == "f":
        return float(value)
    return value


def settable(limit: Optional[int] = None,
             match: str = "") -> List[Tuple[str, R.Register]]:
    """Every register a config can set, in declaration order."""
    out = []
    for path in R.declared_paths(R.UCX2):
        register = R.register_at(R.UCX2, path)
        if register is None or register.domain is None:
            continue
        if match and match not in path:
            continue
        out.append((path, register))
    return out[:limit] if limit else out


def channel_of(path: str) -> Optional[int]:
    """The channel number in a path, or None for a global register."""
    parts = path.split("/")
    for part in parts[2:3]:
        if part.isdigit():
            return int(part)
    return None


def _unchanged(before: object, after: object) -> bool:
    """True when a register reads back exactly where it started."""
    if after is None:
        return True
    try:
        return abs(float(after) - float(before)) < 1e-6
    except (TypeError, ValueError):
        return after == before


def write_batch(device, writes: Sequence[Tuple[str, R.Register, object]],
                pace: float = WRITE_PACE) -> None:
    """Put a pass of writes on the wire, slowly enough to survive.

    One datagram at a time with a gap between them. The gap is the whole
    reason this works: see `WRITE_PACE`.
    """
    for path, register, value in writes:
        device.send([(path, register.tags[:1],
                      (as_tag(value, register.tags),))])
        time.sleep(pace)


def _pass(device, listener, group: Sequence[Tuple[str, R.Register]],
          state: Dict[str, object], step: int,
          attempts: Dict[str, list]) -> Tuple[Dict[str, object], List[str]]:
    """Write one candidate to each register in a group, then read back.

    Reading back is the only way to see the result. Waiting for an echo
    on the path just written cannot work here: the device answers a
    linked pair on the *partner* path and stays silent on the one
    addressed, and a register with no linked partner draws no reply at
    all even though the value lands. Measured on a UCX II -- writing
    `/output/3/volume` reports `/output/4/volume`, and a dump afterwards
    shows both moved.
    """
    writes = []
    for path, register in group:
        options = candidates(register, state[path])
        if not options:
            continue
        # A bool has exactly one legal alternative, so it cannot
        # escalate -- and a single dropped datagram would condemn it as
        # deaf on its only attempt. Repeating the last candidate gives
        # every register the same three tries. Rewriting a value the
        # device already holds is harmless: it reports only on change.
        value, size = options[min(step, len(options) - 1)]
        writes.append((path, register, value, size))
    if not writes:
        return state, []
    write_batch(device, [(p, r, v) for p, r, v, _s in writes])
    after = read_all(device, listener)
    settled = []
    for path, _register, value, size in writes:
        got = after.get(path)
        reported = None if _unchanged(state[path], got) else got
        attempts[path].append((value, size, reported))
        if reported is not None:
            settled.append(path)
    write_batch(device, [(p, r, state[p]) for p, r, _v, _s in writes])
    return read_all(device, listener), settled


def sweep(device, listener, targets: Sequence[Tuple[str, R.Register]],
          state: Dict[str, object],
          note=None) -> List[Dict[str, object]]:
    """Probe every register, escalating only where nothing moved.

    Passes are split by channel parity because a linked pair moves
    together: writing channel 3 drags channel 4 with it, so writing both
    in one batch would leave the first looking like it landed somewhere
    it was not asked to go. Odd and even never share a pass, and no pair
    is ever written against itself.

    Each pass restores what it wrote before the next one starts, so the
    desk is never more than one pass from where it began.
    """
    say = note or (lambda _text: None)
    findings = []
    pending = []
    for path, register in targets:
        if is_dangerous(path):
            findings.append(skipped(path, "dangerous: ADR 0016"))
        elif path not in state:
            findings.append(skipped(path, "device did not report it"))
        else:
            pending.append((path, register))
    attempts: Dict[str, list] = {path: [] for path, _r in pending}
    for step in range(max(len(STEPS), len(UNBOUNDED_STEPS))):
        for odd in (True, False):
            group = [(p, r) for p, r in pending
                     if bool((channel_of(p) or 1) % 2) is odd]
            if not group:
                continue
            state, settled = _pass(device, listener, group, state, step,
                                   attempts)
            say("step %d, %s: %d written, %d answered"
                 % (step + 1, "odd" if odd else "even", len(group),
                    len(settled)))
            pending = [(p, r) for p, r in pending if p not in set(settled)]
    known = dict(targets)
    for path in [p for p, _r in targets]:
        if path in attempts:
            register = known[path]
            findings.append(verdict(
                path, state.get(path), attempts[path],
                bounded=register.domain != R.NUMBER or register.lo is not None))
    return findings


def summarise(findings: Sequence[Dict[str, object]]) -> Dict[str, int]:
    """How many registers landed in each verdict."""
    counts: Dict[str, int] = {}
    for finding in findings:
        key = str(finding.get("verdict"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def drifted(before: Dict[str, object],
            after: Dict[str, object]) -> List[str]:
    """Registers the sweep failed to put back.

    Compared over the whole reported surface, not only the registers
    touched: a write that moves a *neighbour* is the interesting failure,
    and one restricted to the touched set could not see it.
    """
    return sorted(path for path in set(before) | set(after)
                  if before.get(path) != after.get(path)
                  and not path.endswith(STREAMING))


def repair(device, listener, reference: Dict[str, object],
           current: Dict[str, object], rounds: int = 3,
           readback=None) -> Tuple[Dict[str, object], List[str]]:
    """Re-write what drifted until it matches, or say what would not.

    The per-pass restoration is a single write, and this repository has
    measured that a single write can be dropped on the wire. One run
    left `/output/7/eq/band2q` and its partner off by a probe value for
    exactly that reason, with the desk otherwise clean. Restoration is a
    promise the artifact makes (`not_restored`), so it gets the same
    treatment as the probes themselves: escalate before concluding.
    """
    for _round in range(rounds):
        wrong = drifted(reference, current)
        if not wrong:
            return current, []
        writes = []
        for path in wrong:
            register = R.register_at(R.UCX2, path)
            if register is not None and path in reference:
                writes.append((path, register, reference[path]))
        write_batch(device, writes)
        current = (readback or read_all)(device, listener)
    return current, drifted(reference, current)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="probe only the first N registers")
    parser.add_argument("--match", default="",
                        help="probe only paths containing this substring")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the artifact here")
    parser.add_argument("--osc-port", type=int, default=DEFAULT_OSC_PORT)
    parser.add_argument("--osc-recv-port", type=int,
                        default=DEFAULT_OSC_RECV_PORT)
    args = parser.parse_args()

    # This walks every settable register and writes each one a different
    # value. It is the loudest writer in the repository, and until 0.6.8
    # it took no lock at all: a sweep and the unit's reconcile could
    # interleave on one device (ADR 0023). No config directory is needed
    # for it -- the shared lock directory does not depend on one.
    lock = take_device_lock(None, device_key(DEFAULT_USB_ID))
    if lock is None:
        sys.stderr.write("another writer holds the device lock; not "
                         "sweeping\n")
        return 1

    device = loopback(args.osc_port, args.osc_recv_port)
    listener = device.listen()
    if listener is None:
        lock.release()
        sys.stderr.write("UDP %d is in use -- close the mixer GUI\n"
                         % args.osc_recv_port)
        return 1
    try:
        before = read_all(device, listener)
        if not before:
            sys.stderr.write("the backend reported nothing -- is it running, "
                             "and is the Fireface connected?\n")
            return 1
        targets = settable(args.limit, args.match)
        sys.stderr.write("probing %d of %d settable registers\n"
                         % (len(targets), len(settable())))
        started = time.monotonic()
        findings = sweep(device, listener, targets, before,
                         lambda text: sys.stderr.write(text + "\n"))
        elapsed = time.monotonic() - started
        _after, unrestored = repair(device, listener, before,
                                    read_all(device, listener))
    finally:
        listener.close()
        lock.release()

    serial = device_serial()
    artifact = {
        "taken": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "device": ("Fireface UCX II, serial %s" % serial if serial
                   else "serial unknown"),
        "oscmix_revision": built_backend_revision(
            Path(__file__).resolve().parent.parent) or "unknown",
        "firmware": device_firmware(
            DEFAULT_USB_ID,
            Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices")),
            before),
        "probed": len(targets),
        "seconds": round(elapsed, 2),
        "write_pace": WRITE_PACE,
        "method": METHOD,
        "summary": summarise(findings),
        "not_restored": unrestored,
        "findings": findings,
    }
    text = json.dumps(artifact, indent=2, sort_keys=False)
    if args.out:
        args.out.write_text(text + "\n")
        sys.stderr.write("wrote %s\n" % args.out)
    else:
        sys.stdout.write(text + "\n")
    sys.stderr.write("%.1f s for %d registers; %s\n"
                     % (elapsed, len(targets), summarise(findings)))
    if artifact["not_restored"]:
        sys.stderr.write("NOT RESTORED: %s\n" % artifact["not_restored"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
