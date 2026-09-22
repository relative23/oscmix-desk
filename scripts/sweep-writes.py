#!/usr/bin/env python3
"""Measure writes and reported values against the pinned backend contract.

This changes live mixer state. Disconnect or physically mute monitoring
before use. Probe steps may escalate to half a register's declared range;
starting with a small step does not make a full sweep quiet.

Refresh dumps, rather than unsolicited echoes, judge whether each value
landed. Linked channels can report a partner's change, and small writes
can quantise to the current value. A changed report must match the
parameter's encoding, independently of the probe step's size. A mismatch
does not by itself identify a clamp, firmware defect or backend defect.

Both probes and restoration refuse 48v, reflevel and read-only registers.
Indirect changes to protected state remain visible as unrestored state.
SIGINT/SIGTERM stop probing and attempt bounded restoration; device loss,
SIGKILL, process failure or power loss can prevent that restoration.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oscmix_desk import devices
from oscmix_desk import registers as R
from oscmix_desk.backend import loopback
from oscmix_desk.constants import (
    DEFAULT_DEVICE_NAME,
    DEFAULT_OSC_PORT,
    DEFAULT_OSC_RECV_PORT,
    DEFAULT_USB_ID,
    DUMP_LISTEN_SETTLE,
    __version__,
)
from oscmix_desk.discovery import (
    Device,
    built_backend_revision,
    device_firmware,
    resolve_device,
)
from oscmix_desk.errors import DeviceAmbiguous
from oscmix_desk.locking import take_device_lock
from oscmix_desk.numeric import (
    expected_report,
    finite,
    float32,
    integer,
    number_value,
    report_value,
)
from oscmix_desk.process import port_holder
from oscmix_desk.reconcile import matches

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
DANGEROUS = ("48v", "reflevel")

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
          "Each pass attempts to restore its direct writes; a final repair "
          "also checks permitted partner effects. Protected changes are "
          "reported, never written back. Judgement uses parameter encoding, "
          "not a tolerance derived from the probe step. 'clamped' is the "
          "legacy name for a changed-but-wrong report, not a diagnosis.")


def is_dangerous(path: str) -> bool:
    """True for registers ADR 0016 keeps out of reach of a text file."""
    return path.rsplit("/", 1)[-1] in DANGEROUS


def permitted(path: str, register: R.Register) -> bool:
    """Explicit permission, shared by probes and all restoration paths.

    A refresh includes protected and read-only registers too. Their
    presence in a reference snapshot is never permission to write them.
    Safe partner-channel changes remain restorable, not only probe paths.
    """
    return (register.domain is not None and register.verify == R.VERIFIABLE
            and not is_dangerous(path))


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
    try:
        report_value(current, register)
    except (TypeError, ValueError):
        return []
    if register.domain == R.BOOL:
        return [(0 if current else 1, 1.0)]
    if register.domain == R.ENUM:
        return _enum_candidates(register, current)
    if register.domain == R.NUMBER:
        values = []
        for value, step in _number_candidates(register, finite(current)):
            try:
                candidate = as_tag(value, register.tags)
                number_value(candidate, register, reported=True)
            except (TypeError, ValueError):
                continue
            if candidate != current:
                values.append((candidate if register.tags == "i" else value, step))
        return values
    return []


def _followed(path: str, written: object, reported: object) -> bool:
    """True when the report is the value that was asked for.

    A large probe step never licenses a large error. Quantisation comes
    from the parameter's encoding, independently of how far it moved.
    """
    register = R.register_at(devices.UCX2, path)
    tags = register.tags[:1] if register is not None else "f"
    return matches(tags, (written,), (reported,), register=register)


def verdict(path: str, current: object,
            attempts: Sequence[Tuple[object, float, object]],
            bounded: bool = True) -> Dict[str, object]:
    """What a register's attempts mean.

    The legacy label `clamped` means a changed-but-wrong report. This
    observation alone cannot attribute the difference to the table,
    firmware, backend, interference or actual clamping.
    `ignored` is a write that goes nowhere, which is what Room EQ and
    output phase both looked like from here until the pin moved in 0.6.0,
    for entirely different reasons and in different components. This
    tool does not attribute it; a trace does. It says only that the
    promise is not kept.
    """
    finding: Dict[str, object] = {
        "path": path, "current": current, "attempts": len(attempts),
        "observations": [_observation(path, value, step, got)
                         for value, step, got in attempts],
    }
    if not attempts:
        finding["verdict"] = "undetermined"
        finding["detail"] = "no legal alternative value exists"
        return finding
    for index, (written, _step, reported) in enumerate(attempts):
        if reported is None or _unchanged(current, reported):
            continue
        if _followed(path, written, reported):
            finding["verdict"] = "confirmed"
            finding["step"] = index + 1
            finding["wrote"] = written
            finding["reported"] = reported
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


def _observation(path: str, value: object, step: float,
                  got: object) -> Dict[str, object]:
    register = R.register_at(devices.UCX2, path)
    tags = register.tags[:1] if register is not None else "f"
    expected = None
    if register is not None and register.domain == R.NUMBER:
        try:
            expected = expected_report(value, register)
        except (TypeError, ValueError):
            pass
    return {"requested": value, "encoded": as_tag(value, tags),
            "tags": tags, "probe_step": step, "reported": got,
            "expected_report": expected,
            "comparison": "parameter encoding; no probe-step tolerance",
            "scale": register.scale if register is not None else None,
            "truncates": register.truncates if register is not None else False}


def skipped(path: str, reason: str) -> Dict[str, object]:
    """A register the sweep declined to touch, kept in the artifact.

    Reporting 1228 of 1228 while quietly omitting fourteen would be a
    worse artifact than one that names what it did not do.
    """
    return {"path": path, "verdict": "skipped", "detail": reason}


STREAMING = ("/level", "/meter")


class Readback(dict):
    """Scalar view for probes plus complete OSC messages for restoration.

    A mix reports both level and pan. Comparing only args[0] would hide
    a changed panorama when its level stayed the same. Enum names and
    message types are retained too, including malformed empty reports.
    """

    def __init__(self):
        super().__init__()
        self.messages = {}


def read_all(device, listener, seconds: float = 6.0) -> Dict[str, object]:
    """Every register the backend reports, as a path -> value map."""
    seen = Readback()
    time.sleep(DUMP_LISTEN_SETTLE)
    device.request_dump()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for path, tags, args in listener.messages(0.25):
            if not path.endswith(STREAMING):
                seen.messages[path] = (tags, tuple(args))
                if args:
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
        return integer(round(finite(value)))
    if tags[:1] == "f":
        return float32(value)
    return value


def settable(limit: Optional[int] = None,
             match: str = "") -> List[Tuple[str, R.Register]]:
    """Every register a config can set, in declaration order."""
    out = []
    for path in R.declared_paths(devices.UCX2):
        register = R.register_at(devices.UCX2, path)
        if register is None or register.domain is None:
            continue
        if match and match not in path:
            continue
        out.append((path, register))
    return out if limit is None else out[:limit]


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
    messages = []
    for path, register, value in writes:
        if not permitted(path, register):
            raise ValueError("sweep write not permitted: %s" % path)
        report_value(value, register)
        messages.append((path, register.tags[:1], (as_tag(value, register.tags),)))
    for message in messages:
        device.send([message])
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
        attempts[path].append((value, size, got))
        if got is not None and not _unchanged(state[path], got):
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
    reference = dict(state)
    for path, register in targets:
        if not permitted(path, register):
            findings.append(skipped(path, "write not permitted: ADR 0016"))
        elif path not in state:
            findings.append({"path": path, "verdict": "undetermined",
                             "detail": "device did not report it"})
        elif not candidates(register, state[path]):
            findings.append({"path": path, "verdict": "undetermined",
                             "detail": "no valid observation and legal alternative"})
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
                path, reference.get(path), attempts[path],
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
    original = before.messages if isinstance(before, Readback) else before
    current = after.messages if isinstance(after, Readback) else after
    return sorted(path for path in set(original) | set(current)
                  if original.get(path) != current.get(path)
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
            register = R.register_at(devices.UCX2, path)
            if (register is not None and path in reference
                    and permitted(path, register)):
                try:
                    report_value(reference[path], register)
                except (TypeError, ValueError):
                    continue
                writes.append((path, register, reference[path]))
        write_batch(device, writes)
        current = (readback or read_all)(device, listener)
    return current, drifted(reference, current)


class CheckedBackend:
    """Hold the sweep to the same single interface and backend throughout.

    The cooperative lock does not stop hot-unplug or a foreign process
    taking the UDP port. Recheck before sending, including restoration;
    a lost identity is a reason to leave drift visible, never to guess.
    """

    def __init__(self, interface: Device, port: int, recv_port: int,
                 proc_root: Path = Path("/proc")) -> None:
        self.interface = interface
        self.port = port
        self.proc_root = proc_root
        self.holder = port_holder(port, proc_root)
        self.check_identity()
        self.device = loopback(port, recv_port)
        self.sent = []

    def check_identity(self) -> None:
        current = resolve_device(DEFAULT_USB_ID, DEFAULT_DEVICE_NAME, "", self.proc_root)
        holder = port_holder(self.port, self.proc_root)
        if (not current.serial or current.client is None or current != self.interface
                or holder is None or not holder.oscmix or holder != self.holder
                or holder.client != current.client
                or (holder.serial is not None and holder.serial != current.serial)):
            raise ValueError("sweep backend/interface identity cannot be confirmed")

    def send(self, messages) -> None:
        self.check_identity()
        self.device.send(messages)
        self.sent.extend(messages)

    def request_dump(self) -> None:
        self.check_identity()
        self.device.request_dump()

    def listen(self):
        return self.device.listen()

    def binary_evidence(self) -> Dict[str, object]:
        if self.holder is None:
            raise ValueError("no backend to identify")
        executable = self.proc_root / str(self.holder.pid) / "exe"
        built = Path(__file__).resolve().parent.parent / "build/oscmix/oscmix"
        running_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        build_hash = hashlib.sha256(built.read_bytes()).hexdigest()
        if running_hash != build_hash:
            raise ValueError("running backend differs from the recorded build")
        return {"pid": self.holder.pid, "sha256": running_hash,
                "matches_local_build": True}


def measure_and_restore(device, listener, targets, before, note=None):
    """Capture failure and restoration evidence even after an interrupted probe."""
    findings = []
    error = None
    after = {}
    unrestored = sorted(before)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def interrupted(number, _frame):
        raise KeyboardInterrupt(signal.Signals(number).name)

    for sig in previous:
        signal.signal(sig, interrupted)
    try:
        findings = sweep(device, listener, targets, before, note)
    except (Exception, KeyboardInterrupt) as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        # A second polite stop must not interrupt the bounded cleanup.
        # SIGKILL remains available to the operator and cannot be handled.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            after, unrestored = repair(device, listener, before,
                                      read_all(device, listener))
        except (Exception, KeyboardInterrupt) as exc:
            error = "%s; restoration failed: %s: %s" % (
                error or "probe finished", type(exc).__name__, exc)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return findings, after, unrestored, error


def json_safe(value):
    """Preserve invalid raw observations without emitting invalid JSON numbers."""
    if isinstance(value, float):
        try:
            finite(value)
        except ValueError:
            return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def positive_limit(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError('--limit must be greater than zero')
    return value


def source_evidence():
    """Identify all runtime code used by the comparison, not only this script."""
    root = Path(__file__).resolve().parents[1]
    package = Path(R.__file__).resolve().parent
    commit = None
    dirty = None
    try:
        top = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, check=True).stdout.strip()
        if Path(top).resolve() == root:
            commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True, check=True).stdout.strip()
            dirty = bool(subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                                        capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {'version': __version__, 'commit': commit, 'dirty': dirty,
            'python': sys.version,
            'runtime_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in sorted(package.glob('*.py'))},
            'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=positive_limit, default=None,
                        help="probe only the first N registers")
    parser.add_argument("--match", default="",
                        help="probe only paths containing this substring")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the artifact here")
    parser.add_argument("--osc-port", type=int, default=DEFAULT_OSC_PORT)
    parser.add_argument("--osc-recv-port", type=int,
                        default=DEFAULT_OSC_RECV_PORT)
    args = parser.parse_args()
    targets = settable(args.limit, args.match)
    if not targets:
        parser.error('no settable register matches the selection; nothing was written')
    source = source_evidence()

    # This walks every settable register and writes each one a different
    # value. It is the loudest writer in the repository, and until 0.6.8
    # it took no lock at all: a sweep and the unit's reconcile could
    # interleave on one device (ADR 0023). No config directory is needed
    # for it -- the shared lock directory does not depend on one.
    try:
        interface = resolve_device(DEFAULT_USB_ID, DEFAULT_DEVICE_NAME, "",
                                   Path("/proc"))
    except DeviceAmbiguous as exc:
        # The sweep writes to whichever backend holds the default port and
        # names the box in its artifact; with two boxes it could do
        # neither honestly (ADR 0024).
        sys.stderr.write("%s; the sweep supports one interface\n" % exc)
        return 1
    lock = take_device_lock(None, interface.key)
    if lock is None:
        sys.stderr.write("another writer holds the device lock; not "
                         "sweeping\n")
        return 1

    try:
        device = CheckedBackend(interface, args.osc_port, args.osc_recv_port)
        running_backend = device.binary_evidence()
        listener = device.listen()
    except (OSError, ValueError, DeviceAmbiguous) as exc:
        lock.release()
        sys.stderr.write("%s\n" % exc)
        return 1
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
        sys.stderr.write("probing %d of %d settable registers\n"
                         % (len(targets), len(settable())))
        started = time.monotonic()
        findings, after, unrestored, failure = measure_and_restore(
            device, listener, targets, before,
            lambda text: sys.stderr.write(text + "\n"))
        elapsed = time.monotonic() - started
    finally:
        listener.close()
        lock.release()

    source_after = source_evidence()
    if (source['runtime_sha256'] != source_after['runtime_sha256']
            or source['tool_sha256'] != source_after['tool_sha256']):
        failure = (failure + '; ' if failure else '') + 'source files changed during measurement'

    serial = interface.serial or None
    artifact = {
        "schema": 2,
        "taken": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "device": ("Fireface UCX II, serial %s" % serial if serial
                   else "serial unknown"),
        "oscmix_revision": built_backend_revision(
            Path(__file__).resolve().parent.parent) or "unknown",
        "running_backend": running_backend,
        "sent": device.sent,
        "tool_sha256": source['tool_sha256'],
        "desk_source": source,
        "firmware": device_firmware(
            DEFAULT_USB_ID,
            Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices")),
            before),
        "probed": len(targets),
        "seconds": round(elapsed, 2),
        "write_pace": WRITE_PACE,
        "method": METHOD,
        "error": failure,
        "before": before,
        "after": after,
        "before_messages": before.messages if isinstance(before, Readback) else {},
        "after_messages": after.messages if isinstance(after, Readback) else {},
        "summary": summarise(findings),
        "not_restored": unrestored,
        "findings": findings,
    }
    text = json.dumps(json_safe(artifact), indent=2, sort_keys=False, allow_nan=False)
    sys.stderr.write("%.1f s for %d registers; %s\n"
                     % (elapsed, len(targets), summarise(findings)))
    if artifact["not_restored"]:
        sys.stderr.write("NOT RESTORED: %s\n" % artifact["not_restored"])
    try:
        if args.out:
            args.out.write_text(text + "\n")
            sys.stderr.write("wrote %s\n" % args.out)
        else:
            sys.stdout.write(text + "\n")
    except OSError as exc:
        sys.stderr.write("could not save sweep evidence: %s\n" % exc)
        return 1
    if artifact["not_restored"]:
        return 1
    if failure or any(f["verdict"] not in ("confirmed", "skipped") for f in findings):
        sys.stderr.write("sweep incomplete: %s\n" % (failure or summarise(findings)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
