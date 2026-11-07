"""The connect check: the device's input mix against the desk's, before a start overwrites it.

TotalMix FX does this when it connects to an interface: it compares the input mix a refresh
reports with its own and asks the user on a difference, with a tolerance of 0.07 dB for a level
and 0.007 of full scale for a pan; playback is never compared, the device does not report it
(static reading of TotalMix FX 2.10, 2026-11-03). The desk compares the same way for the input
routes it declares, when a session starts -- which is when it meets the interface: a
re-appearing interface starts a new session. A difference is reported, in the log and through
``--status`` from the record beside the journal (``seen``), and the start then applies the desk
exactly as it would without the check: no prompt, no refusal, no wait for anybody.

One refresh, inside the start's lease and before its first write, read until every declared
cell has reported (at most ``VERIFY_TIMEOUT``). When the apply needs a fresh AutoSet
observation first, the same refresh supplies it, so such a start still reads the device once
before writing. A desk without input routes compares nothing and reads nothing here. The check
never writes, and a check that cannot read the device only says so.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .autogain import dependency_paths
from .backend import Control
from .constants import LEVEL_MIN, VERIFY_TIMEOUT
from .log import log
from .model import Config
from .observation import Observation
from .osc import Args
from .reconcile import desired, mix_messages
from .seen import NOTED, Record, cell_value, check_record

#: TotalMix FX's tolerances: a level in dB, a pan as a fraction of full scale (-1..1).
LEVEL_TOLERANCE = 0.07
PAN_TOLERANCE = 0.007
#: The backend's pan argument runs from -100 to 100.
PAN_FULL_SCALE = 100
#: Float rounding, far below the device's 0.1 dB steps: 0.07 dB stays within the tolerance.
ROUNDING = 1e-6

Reports = dict[str, tuple[str, Args]]


@dataclass(frozen=True)
class Cell:
    """One input-matrix register a declared input route sets, and the route that sets it."""

    route: str
    level: float
    pan: int


@dataclass(frozen=True)
class ConnectCheck:
    """What the device reported for the declared cells, before the start wrote them."""

    at: float
    compared: int
    #: (path, declared cell, device arguments), outside TotalMix's tolerances.
    mismatched: tuple[tuple[str, Cell, Args], ...]
    unreported: tuple[str, ...]
    invalid: tuple[str, ...]

    def record(self) -> Record:
        """The check as the record beside the journal stores it."""
        mismatched: list[Record] = [
            {"route": cell.route, "path": path, "device": cell_value(float(args[0]), args[1]),
             "declared": cell_value(_silenced(cell.level), cell.pan)}
            for path, cell, args in self.mismatched]
        return check_record(self.at, self.compared, mismatched, list(self.unreported),
                            list(self.invalid))


def declared_cells(config: Config) -> dict[str, Cell]:
    """The input-matrix registers the desk's input routes set, with their routes' names.

    A later route wins a shared register, as it does in the apply; the base's silent cells are
    not routes and are not compared, nor is a cell the device may fold onto its partner.
    """
    names: dict[str, str] = {}
    for route in config.routes:
        if route.source[0] == "input" and not route.base:
            names.update((path, route.name) for path, _tags, _args in mix_messages(route)
                         if path.startswith("/mix/"))
    return {entry.path: Cell(names[entry.path], float(entry.args[0]), int(entry.args[1]))
            for entry in desired(config)
            if entry.path in names and entry.compared and entry.tags == "fi"}


def _silenced(level: float) -> float:
    """A level as the device holds it: at or below the mute floor it is digital zero."""
    return -math.inf if level <= LEVEL_MIN else level


def agrees(cell: Cell, tags: str, args: Args) -> bool | None:
    """Whether a report is the declared cell within TotalMix's tolerances; None if no value."""
    if not tags.startswith("fi") or len(args) < 2:
        return None
    level, pan = args[0], args[1]
    if (isinstance(level, bool) or not isinstance(level, (int, float)) or math.isnan(level)
            or level == math.inf or isinstance(pan, bool) or not isinstance(pan, int)
            or not -PAN_FULL_SCALE <= pan <= PAN_FULL_SCALE):
        return None
    reported, declared = _silenced(float(level)), _silenced(cell.level)
    if math.isinf(reported) or math.isinf(declared):
        # A silent cell has no balance to compare; the backend reports pan 0 for it.
        return reported == declared
    return (abs(reported - declared) <= LEVEL_TOLERANCE + ROUNDING
            and abs(pan - cell.pan) / PAN_FULL_SCALE <= PAN_TOLERANCE + ROUNDING)


def judge(cells: Mapping[str, Cell], latest: Mapping[str, tuple[str, Args]],
          at: float) -> ConnectCheck:
    """Classify every declared cell by its last report in the window."""
    mismatched: list[tuple[str, Cell, Args]] = []
    unreported: list[str] = []
    invalid: list[str] = []
    for path, cell in sorted(cells.items()):
        report = latest.get(path)
        verdict = None if report is None else agrees(cell, *report)
        if report is None:
            unreported.append(path)
        elif verdict is None:
            invalid.append(path)
        elif not verdict:
            mismatched.append((path, cell, tuple(report[1])))
    return ConnectCheck(at, len(cells), tuple(mismatched), tuple(unreported), tuple(invalid))


def describe(level: float, pan: object) -> str:
    """A cell for a log line."""
    level = _silenced(level)
    return "silent" if math.isinf(level) else "%+.2f dB, pan %s" % (level, pan)


def log_check(check: ConnectCheck) -> None:
    """Say what the check found; a difference is a warning, never an error."""
    if check.mismatched:
        log.warning("connect check: the device's input mix differs from the desk in %d of %d "
                    "route cell(s) (TotalMix tolerances: %.2f dB, pan %.3f); the desk "
                    "overwrites them now", len(check.mismatched), check.compared,
                    LEVEL_TOLERANCE, PAN_TOLERANCE)
        for path, cell, args in check.mismatched:
            log.warning("connect check: route %r %s: device %s, desk %s", cell.route, path,
                        describe(float(args[0]), args[1]), describe(cell.level, cell.pan))
    elif not (check.unreported or check.invalid):
        log.info("connect check: the device's input mix matches the desk's %d route cell(s)",
                 check.compared)
    if check.unreported or check.invalid:
        log.info("connect check: %d of %d route cell(s) not compared (%d not reported, %d "
                 "unreadable)", len(check.unreported) + len(check.invalid), check.compared,
                 len(check.unreported), len(check.invalid))


def _window_done(observed: Observation, cells: Mapping[str, Cell]) -> bool:
    return observed.dependencies_complete and all(path in observed.latest for path in cells)


def check_at_connect(config: Config, backend: Control, should_stop: Callable[[], bool]
                     ) -> tuple[ConnectCheck | None, Reports | None]:
    """Read and judge the declared input cells once; never refuses, never writes.

    Returns the check (None when nothing was compared) and, when the desk has AutoSet
    dependencies and this window observed all of them, their reports for the apply to use
    instead of a refresh of its own -- what ``routing`` would have read itself. A stop, or a
    window that left a dependency unobserved, hands nothing over, so the apply reads and
    refuses exactly as it does without the check.
    """
    cells = declared_cells(config)
    if not cells or should_stop():
        return None, None
    dependencies = dependency_paths(config)
    observed = Observation({}, dependency_paths=dependencies, noted=(*cells, *NOTED))
    try:
        backend.request_dump()
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while not _window_done(observed, cells) and time.monotonic() < deadline:
            if should_stop():
                return None, None
            for report in backend.messages(.25):
                observed.absorb(report)
    except OSError as exc:
        if not should_stop():
            log.warning("connect check: the device's input mix could not be read (%s); "
                        "applying the desk without the comparison", exc)
        return None, None
    check = judge(cells, observed.latest, time.time())
    log_check(check)
    if not dependencies or not observed.dependencies_complete or should_stop():
        return check, None
    return check, {path: observed.latest[path] for path in dependencies}
