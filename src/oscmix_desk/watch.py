"""`[desk] enforce = watch`: a device report that contradicts the desk asks for a reconcile.

The front panel, an ARC, a loaded device setup or another OSC client can change a register the
desk pins. By default the change stays until the next start, switch or reload. With
``enforce = watch`` the session keeps one reader connection open and turns such a report into
the same selective reconcile a reload runs:

- Event-driven: it waits for device reports and never reads on a timer. So it sees what
  reaches subscribers: link changes, which the device announces itself (measured), and every
  value of a state dump another client asks for, such as the GTK mixer when it connects.
  Whether the device announces other front-panel changes is still to be measured.
- It judges only what that reconcile would write and the device reports (PIN and VERIFIABLE).
  REMEMBER paths, a desk's base and gains AutoSet owns are never a reason.
- Reports that arrive while any operation holds the backend lease are that operation's own
  writes and are not judged, nor are reports queued before it began. Every report still updates
  what the watcher knows of links and AutoSet flags, which decide what a reconcile may write.
- It judges once the reports rest (``WATCH_SETTLE``), at the latest ``WATCH_LONGEST`` after the
  first, against the desk in effect read from disk. The reconcile reads it again under the
  device lock, so a request judged against a desk a switch has just replaced costs one
  reconcile that writes what the switch wrote.
- A register the device keeps setting back is re-asserted at most ``WATCH_REPEATS`` times in
  ``WATCH_REPEAT_WINDOW``, then left to the device until the desk changes or a reload.
- The watcher never writes. It asks the session's supervise loop, the one place a reconcile runs.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .backend import Control, connect_backend
from .constants import (
    WATCH_CONNECTIONS,
    WATCH_LONGEST,
    WATCH_REPEAT_WINDOW,
    WATCH_REPEATS,
    WATCH_SETTLE,
)
from .devices import device_for_name
from .errors import BackendControlError, ConfigError
from .log import log
from .model import Config
from .observation import Observation
from .osc import Args
from .protocol import Source
from .reconcile import ApplyIntent, WriteReason, application_plan
from .registers import VERIFIABLE, verify_class

#: Distinct paths the watcher remembers reports for; a device has a few thousand registers.
KNOWN_LIMIT = 20000
#: Seconds one receive waits; the socket wait of the loop, not a device read.
SLICE = 0.25
#: The last path segment of the level meters the device reports as DEVICE observations,
#: some 270 a second on a UCX II (measured 2026-10-27): never a register, never a reason.
METER = "meter"

Reports = Dict[str, Tuple[str, Args]]


def reader(config: Config, config_path: Optional[Path],
           expected_pid: int) -> Callable[[Callable[[], bool]], Control]:
    """Connections to the started backend that subscribe to device reports alone."""
    return lambda stop: connect_backend(config, config_path, reader=True, sources=Source.DEVICE,
                                        should_stop=stop, expected_pid=expected_pid)


def drift(desk: Config, reports: Mapping[str, Tuple[str, Args]],
          known: Mapping[str, Tuple[str, Args]]) -> Tuple[str, ...]:
    """PIN paths a reconcile of ``desk`` would write whose last report contradicts it.

    ``known`` is every report seen, for the links and AutoSet flags that decide what a reconcile
    may write; ``reports`` are the ones to judge. A malformed report is not a contradiction.
    """
    device = device_for_name(desk.device_name)
    if device is None or not reports:
        return ()
    try:
        selected = application_plan(desk, ApplyIntent.RECONCILE, observed=known)
    except (ConfigError, ValueError) as exc:
        log.debug("watch: no plan for the desk in effect (%s)", exc)
        return ()
    expected = {write.path: (write.tags, write.args) for write in selected.writes
                if write.reason != WriteReason.PREREQUISITE
                and verify_class(device, write.path) == VERIFIABLE}
    observation = Observation(expected, device)
    for path, (tags, args) in reports.items():
        observation.absorb((path, tags, args))
    return tuple(sorted(observation.mismatched - observation.invalid))


class Repeats:
    """How often watch re-asserted each register, and the ones it left to the device."""

    def __init__(self) -> None:
        self._times: Dict[str, List[float]] = {}
        self._desk: Optional[Config] = None
        self.left: Set[str] = set()

    def forgive(self) -> None:
        self._times.clear()
        self.left.clear()

    def admit(self, desk: Config, paths: Sequence[str], now: float) -> Tuple[str, ...]:
        """The paths still worth a reconcile; a new desk starts every count again."""
        if desk != self._desk:
            self.forgive()
            self._desk = desk
        admitted = []
        for path in paths:
            if path in self.left:
                continue
            times = [then for then in self._times.get(path, ())
                     if now - then < WATCH_REPEAT_WINDOW]
            if len(times) >= WATCH_REPEATS:
                self.left.add(path)
                log.warning("watch: the device set %s back %d times within %.0fs; leaving it "
                            "to the device until the desk changes or a reload",
                            path, len(times), WATCH_REPEAT_WINDOW)
                continue
            self._times[path] = [*times, now]
            admitted.append(path)
        return tuple(admitted)


class Watch:
    """One reader connection that asks for a reconcile when the device contradicts the desk.

    ``connect`` opens a reader connection that subscribes to device reports and stops with the
    stop check it is given. ``desk`` returns the desk in effect, or None while it is unusable.
    ``request`` asks the session for a reconcile; it must only set flags.
    """

    def __init__(self, connect: Callable[[Callable[[], bool]], Control],
                 desk: Callable[[], Optional[Config]],
                 request: Callable[[Tuple[str, ...]], None],
                 should_stop: Callable[[], bool],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._connect = connect
        self._desk = desk
        self._request = request
        self._should_stop = should_stop
        self._clock = clock
        self._forgiven = threading.Event()
        self._repeats = Repeats()
        self.known: Reports = {}
        self._pending: Reports = {}
        self._first: Optional[float] = None
        self._last = 0.0

    def forgive(self) -> None:
        """A reload: registers left to the device are watched again. Any thread may call it."""
        self._forgiven.set()

    def run(self) -> None:
        """Watch until a stop, the desk stops asking for it, or connections keep failing.

        ``WATCH_CONNECTIONS`` failures in a row end it; a connection that served for
        ``WATCH_LONGEST`` before it ended starts the count again.
        """
        failures, failure = 0, ""
        while failures < WATCH_CONNECTIONS:
            if self._should_stop():
                return
            started = self._clock()
            try:
                control = self._connect(self._should_stop)
            except OSError as exc:
                failures, failure = failures + 1, str(exc)
                continue
            try:
                self._serve(control)
            except BackendControlError as exc:
                if self._should_stop():
                    return
                lasted = self._clock() - started >= WATCH_LONGEST
                failures, failure = (1 if lasted else failures + 1), str(exc)
                log.warning("watch: the reader connection ended (%s)", exc)
            else:
                return
            finally:
                control.close()
        log.warning("watch: no reader connection (%s); changes on the device are no longer "
                    "reconciled until the session restarts", failure or "unknown")

    def _serve(self, control: Control) -> None:
        while not self._should_stop():
            reports = tuple(control.messages(SLICE))
            self._absorb(reports, owned=control.lease_held)
            if self._pending and self._settled() and not self._judge():
                return

    def _absorb(self, reports: Sequence[Tuple[str, str, Args]], *, owned: bool) -> None:
        if owned:
            # An operation's own writes, and whatever it is about to overwrite.
            self._pending.clear()
            self._first = None
        registers = [(path, tags, args) for path, tags, args in reports
                     if path.rsplit("/", 1)[-1] != METER]
        for path, tags, args in registers:
            if path in self.known or len(self.known) < KNOWN_LIMIT:
                self.known[path] = (tags, tuple(args))
            if not owned:
                self._pending[path] = (tags, tuple(args))
        if registers and not owned:
            now = self._clock()
            self._first = now if self._first is None else self._first
            self._last = now

    def _settled(self) -> bool:
        now = self._clock()
        first = now if self._first is None else self._first
        return now - self._last >= WATCH_SETTLE or now - first >= WATCH_LONGEST

    def _judge(self) -> bool:
        """Ask for a reconcile if the desk is contradicted; False once watching should end."""
        pending, self._pending = self._pending, {}
        self._first = None
        desk = self._desk()
        if desk is None:
            return True
        if desk.enforce != "watch":
            log.info("watch: the desk in effect no longer asks for enforce = watch; "
                     "watching ends")
            return False
        if self._forgiven.is_set():
            self._forgiven.clear()
            self._repeats.forgive()
        paths = self._repeats.admit(desk, drift(desk, pending, self.known), self._clock())
        if paths:
            log.info("watch: the device changed %s, which the desk pins; reconciling",
                     ", ".join(paths))
            self._request(paths)
        return True
