"""What an apply reads and refuses before it writes: AutoSet ownership, links, DSP limits.

Split out of routing in 0.9.0. Every check here runs on a fresh, completed DEVICE window and
refuses before the first write it would make wrong; routing decides when each one runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from .autogain import dependency_paths, flag_value, retained_gains
from .backend import Control
from .constants import VERIFY_TIMEOUT
from .errors import WriteFailed
from .limits import dsp_problem, observation_paths, switches_on, switching_on
from .log import log
from .model import Config
from .observation import Observation
from .osc import Args
from .reconcile import (
    ApplyIntent,
    Plan,
    Write,
    link_messages,
    mix_messages,
    retention_problem,
)

StopCheck = Callable[[], bool]


def read_dependencies(backend: Control, paths: Sequence[str],
                            should_stop: StopCheck, *,
                            links: Mapping[str, int] | None = None,
                            purpose: str = "AutoSet",
                            settled: Callable[[Observation], bool] | None = None
                            ) -> dict[str, tuple[str, Args]]:
    """One fresh, completed DEVICE window; missing or malformed stays unknown.

    The window closes once every Boolean dependency and expected link reported validly, or
    when ``settled`` says so for registers that are not Boolean.
    """
    if not paths:
        return {}
    if should_stop():
        raise OSError("stop requested before %s observation; writes refused" % purpose)
    observed = Observation({path: ("i", (value,)) for path, value in (links or {}).items()},
                           dependency_paths=paths)
    done = settled or (lambda seen: seen.dependencies_complete and seen.complete)
    backend.request_dump()
    deadline = time.monotonic() + VERIFY_TIMEOUT
    while not done(observed) and time.monotonic() < deadline:
        if should_stop():
            raise OSError("stop requested during %s observation; writes refused" % purpose)
        for report in backend.messages(.25):
            observed.absorb(report)
    if should_stop():
        raise OSError("stop requested after %s observation; writes refused" % purpose)
    return dict(observed.latest)


def volume_reader(backend: Control, should_stop: StopCheck
                   ) -> Callable[[Sequence[str]], dict[str, tuple[str, Args]]]:
    """One dependency read of the output volumes a peak-safe switch has to give back."""
    return lambda paths: read_dependencies(
        backend, paths, should_stop, purpose="peak-safe",
        settled=lambda seen: all(path in seen.latest for path in paths))


def dsp_limit_problem(config: Config, selected: Sequence[Write],
                       latest: Mapping[str, tuple[str, Args]], backend: Control,
                       should_stop: StopCheck) -> str | None:
    """Room EQ and Crossfeed counted over every output before anything is switched on."""
    writes = [(write.path, write.args) for write in selected]
    if not switches_on(config, writes):
        return None
    missing = [path for path in observation_paths(config) if path not in latest]
    counted = dict(latest)
    # Crossfeed is a level (0..5), not a flag: any report of each register ends the window.
    counted.update(read_dependencies(
        backend, missing, should_stop, purpose="DSP limit",
        settled=lambda seen: all(path in seen.latest for path in missing)))
    return dsp_problem(config, writes, counted)


def within_dsp_limits(config: Config, wanted: Plan, latest: Mapping[str, tuple[str, Args]],
                       backend: Control, should_stop: StopCheck, intent: ApplyIntent) -> Plan:
    """The plan if it keeps the DSP limits; refused on an explicit apply, else without them.

    A start, repair or reconcile that refused everything over a Room EQ the device already
    runs elsewhere would leave the desk unapplied -- and a start failing, restarting and
    failing again. It leaves the switch-ons to the device and says so; a switch refuses.
    """
    problem = dsp_limit_problem(config, wanted.writes, latest, backend, should_stop)
    if problem is None:
        return wanted
    if intent == ApplyIntent.EXPLICIT:
        raise WriteFailed(OSError(problem), [], [write.path for write in wanted.writes])
    held = set(switching_on(config, [(write.path, write.args) for write in wanted.writes]))
    log.warning("%s; not switching on %s now, so the rest of the desk applies",
                problem, ", ".join(sorted(held)))
    return replace(wanted, writes=tuple(w for w in wanted.writes if w.path not in held))


def gain_problem(config: Config, selected: Sequence[Write],
                  latest: Mapping[str, tuple[str, Args]], intent: ApplyIntent,
                  retained: Sequence[str], confirmed: Sequence[str]) -> str | None:
    """No direct or indirect gain write may defeat AutoSet's ownership."""
    linked = [w.path for w in selected if w.path.endswith("/stereo") and w.args == (1,)]
    gains = retained_gains(config, latest, linked=linked,
                           use_declarations=intent.preserves_remember)
    blocked = sorted(w.path for w in selected if w.path in gains)
    if blocked:
        return "AutoSet is active or not confirmed off for gain writes: %s" % ", ".join(blocked)
    pending_links = {w.path for w in selected if w.path.endswith("/stereo")}
    wrong = sorted(path for path, value in input_link_expectations(config, selected).items()
                   if path not in pending_links and flag_value(latest.get(path)) != value)
    if wrong:
        return ("fresh input link state is not confirmed for dependent mix writes: %s"
                % ", ".join(wrong))
    return retention_problem(config, selected, tuple(set(retained) | gains), confirmed,
                             observed=latest)


def input_link_expectations(config: Config, selected: Sequence[Write]) -> dict[str, int]:
    """AutoSet-relevant input links needed by the selected matrix writes."""
    writes = {w.path for w in selected}
    dependencies = set(dependency_paths(config))
    links: dict[str, int] = {}
    for route in config.routes:
        kind, _source = route.source
        if kind != "input" or not writes.intersection(
                p for p, _t, _a in mix_messages(route) if p.startswith("/mix/")):
            continue
        for path, _tags, args in link_messages(route):
            if path in dependencies and path.endswith("/stereo"):
                links[path] = int(args[0])
    return links


def require_gain_ownership(config: Config, selected: Sequence[Write],
                            latest: Mapping[str, tuple[str, Args]], intent: ApplyIntent,
                            retained: Sequence[str], confirmed: Sequence[str]) -> None:
    problem = gain_problem(config, selected, latest, intent, retained, confirmed)
    if problem:
        raise OSError(problem)
