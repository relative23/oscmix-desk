"""Translating routes into OSC messages, and applying them.

The order is load-bearing: channel links first, mix matrix second.
See docs/OSC-PROTOCOL.md for why."""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .autogain import dependency_paths, flag_value, retained_gains
from .backend import Control
from .constants import LINK_ECHO_TIMEOUT, VERIFY_TIMEOUT
from .errors import WriteFailed
from .log import log
from .model import Config, Route
from .observation import Observation
from .osc import Args
from .reconcile import (
    ApplyIntent,
    Phase,
    Plan,
    Write,
    application_plan,
    link_messages,
    matrix_repair_plan,
    mix_messages,
    remembered_paths,
    retention_problem,
)
from .streams import PlaybackGuard

# Asked before every write and between every phase of the background
# verifier. The
# verifier may run for two verification windows plus a blind delay after
# READY=1, and everything it does in that time is a write to a device
# somebody may just have asked to stop.
StopCheck = Callable[[], bool]


def never_stop() -> bool:
    """The default stop check: nothing to stop for.

    Used by the foreground apply and by tests, which have no session to
    take a stop signal from.
    """
    return False


def wait_unless_stopped(seconds: float, should_stop: StopCheck,
                        backend: Optional[Control] = None) -> bool:
    """Sleep, waking early on a stop request. True if a stop was asked for.

    A plain ``time.sleep`` is what let ``LINK_SYNC_BLIND_DELAY`` outlast
    ``TimeoutStopSec``: the session would exit with the verifier still
    parked in it, and the daemon thread would be cut wherever it happened
    to be -- possibly between two mix writes. The delay is 5 s now,
    measured, and would fit either way, but the property this function
    provides must not depend on that: ``VERIFY_TIMEOUT`` is 10 s and the
    verifier can run two of them.
    """
    deadline = time.monotonic() + seconds
    while True:
        if should_stop():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if backend is None:
            time.sleep(min(0.1, remaining))
        else:
            backend.wait(min(0.1, remaining))


class LinkEcho(Enum):
    """What the wait for the link echo came to.

    A contradiction is not silence, and neither is cancellation. Only
    CONFIRMED is truthy; receive failures remain exceptions.
    """

    CONFIRMED = "confirmed"          # every register arrived at its value
    SILENT = "silent"                # the wait ran out
    CONTRADICTED = "contradicted"    # latest decoded value is wrong/invalid
    CANCELLED = "cancelled"          # no further write is permitted

    def __bool__(self) -> bool:
        return self is LinkEcho.CONFIRMED


def await_link_echo(expected: Mapping[str, int], backend: Control,
                    timeout: Optional[float] = None, *,
                    should_stop: StopCheck = never_stop) -> LinkEcho:
    """Wait until oscmix reports every register in ``expected`` at its value.

    ``expected`` maps an OSC path to the integer the device has to report
    for it. The report is what actually updates oscmix's internal link
    state, so this -- not a fixed sleep -- is the correct barrier before
    writing the mix matrix. The value matters: an unlinked route waits for
    0 just as a linked one waits for 1, and a stale report of the opposite
    value must not end the wait.

    A nonmatching or invalid decoded value revokes a match. Timeout
    distinguishes a known contradiction from silence. The caller may use
    a backend-specific settle for silence, never for a
    contradiction, cancellation or receive failure.

    ``backend`` is the caller's, when it has one. Without it this built
    its own from ``recv_port`` and ignored the one ``apply_routing`` had
    been handed -- the same "second implementation of something that
    already exists" that produced three defects in 0.3.0, and
    here it also meant the barrier was the one part of the write path a
    test could not stand in for. Every profile test paid 1.5 s of real
    waiting for an echo no double could send.
    """
    if should_stop():
        return LinkEcho.CANCELLED
    if not expected:
        return LinkEcho.CONFIRMED
    if timeout is None:
        timeout = LINK_ECHO_TIMEOUT
    observed = Observation({path: ("i", (value,)) for path, value in expected.items()})
    deadline = time.monotonic() + timeout
    while not observed.complete:
        if should_stop():
            return LinkEcho.CANCELLED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for report in backend.messages(min(.25, remaining)):
            observed.absorb(report)
    if should_stop():
        return LinkEcho.CANCELLED
    if observed.mismatched:
        return LinkEcho.CONTRADICTED
    return LinkEcho.CONFIRMED if observed.complete else LinkEcho.SILENT


def _cross_the_barrier(config: Config, device: Control, *,
                       expected: Optional[Mapping[str, int]] = None,
                       require_confirmation: Sequence[str] = (),
                       should_stop: StopCheck = never_stop) -> None:
    """Confirm, settle for silence, or refuse dependent writes with cause."""
    if device.traits.reports_link_state_on_write and not require_confirmation:
        # The barrier exists for one upstream detail: setbool leaves
        # oscmix's own view of the stereo flag untouched until the device
        # echoes it. A backend that updates its view on write -- the
        # patched one, if michaelforney/oscmix#31 lands -- has nothing to
        # wait for. This is the branch the trait's docstring promised
        # since 0.2.0; until 0.6.2 nothing read the flag.
        log.info("this backend updates its link state on write; no barrier")
        return
    timeout = LINK_ECHO_TIMEOUT
    echoed = await_link_echo(output_link_state(config.routes) if expected is None else expected,
                             device, timeout, should_stop=should_stop)
    if echoed is LinkEcho.CANCELLED:
        raise OSError("stop requested during link observation; remaining writes refused")
    if echoed is LinkEcho.CONTRADICTED:
        raise OSError("link state contradicted; dependent writes refused")
    if require_confirmation and echoed is not LinkEcho.CONFIRMED:
        raise OSError("fresh link confirmation required for %s; remaining writes refused"
                      % ", ".join(sorted(require_confirmation)))
    if echoed is LinkEcho.SILENT:
        # Normal when the pairs were already linked: no change, no echo.
        log.info("no link change reported within %.1fs; mix matrix will "
                 "be re-applied after the register sync", timeout)
    else:
        log.info("channel pairs linked and confirmed by the device")


def apply_routing(config: Config, backend: Control, *,
                  leave_alone: Sequence[str] = (),
                  require_link_confirmation: Sequence[str] = (),
                  intent: ApplyIntent = ApplyIntent.INITIAL,
                  confirmed: Sequence[str] = (),
                  observed: Optional[Mapping[str, Tuple[str, Args]]] = None,
                  should_stop: StopCheck = never_stop) -> None:
    """Send the routing in two phases: link the pairs, then fill the mix.

    Both phases are separated by the link barrier above. Sending them in
    one burst is what silences every even output (see LINK_ECHO_TIMEOUT).

    The *what* is a ``reconcile.Plan``: the registers the config asks
    for, deduplicated and split at the barrier. What is left here is the
    *when* -- send, wait out the barrier, send -- which is this
    function's whole job and the only part that needs a socket and a
    clock.

    ``intent`` determines write ownership. Only initial application and
    explicit desk selection write REMEMBER starting values. Later repair
    and reconcile preserve them regardless of feedback. ``leave_alone``
    can retain additional paths; dependency checks include indirect effects.

    It takes the **whole config**, not a list of routes. Rebuilding one
    from routes alone silently dropped `config.channels`, so channel
    sections parsed, validated, showed in `--dry-run` and never reached
    the device. `tests/test_apply_routing.py` now fails on any function
    that rebuilds a Config from a subset of its fields.
    """
    # Validate the complete declaration before requesting or sending anything.
    application_plan(config, intent, leave_alone, confirmed, observed=observed)
    dependencies = dependency_paths(config)
    latest = ({p: (tags, tuple(args)) for p, (tags, args) in observed.items()}
              if observed is not None else
              _read_gain_dependencies(backend, dependencies, should_stop))
    skip = set(leave_alone)
    if not intent.preserves_remember and retained_gains(config, latest, use_declarations=False):
        stable = [w.path for w in application_plan(config, intent).links()
                  if w.path.startswith("/input/")
                  and flag_value(latest.get(w.path)) == w.args[0]]
        skip.update(stable)
        confirmed = tuple(set(confirmed) | set(stable))
    if intent.preserves_remember:
        linked = [w.path for w in application_plan(config, intent, leave_alone, confirmed,
                                                  observed=latest).links() if w.args == (1,)]
        skip.update(remembered_paths(config, latest, linked=linked))
    wanted = application_plan(config, intent, sorted(skip), confirmed, observed=latest)
    problem = retention_problem(config, wanted.writes, sorted(skip), confirmed, observed=latest)
    if problem:
        raise WriteFailed(OSError(problem), [], [write.path for write in wanted.writes])
    off = tuple(w for w in wanted.channel() if w.path.endswith("/autoset") and w.args == (0,))
    if not off:
        problem = _gain_problem(config, wanted.writes, latest, intent, sorted(skip), confirmed)
        if problem:
            raise WriteFailed(OSError(problem), [], [write.path for write in wanted.writes])
    # A caller may supply the backend. The profile switch does, because
    # the alternative -- its own send/barrier/send -- is what it had
    # first, and it dropped the barrier: applying and verifying a
    # profile took 48 ms on a live UCX II, which is not enough time for
    # a barrier that is measured in seconds.
    # Only the output links need the barrier: /playback/<n>/stereo goes
    # through setinputstereo(), which updates oscmix's state right away,
    # while /output/<n>/stereo relies on the device report -- see
    # backend.Traits.reports_link_state_on_write.
    _send_in_order(wanted, config, backend,
                   require_link_confirmation, should_stop, intent, latest, sorted(skip), confirmed)
    _log_apply(config, wanted, sorted(skip))


def _log_apply(config: Config, wanted: Plan, skip: Sequence[str]) -> None:
    for route in config.routes:
        kind, source = route.source
        log.info(
            "route %r: %s %s -> output %s at %+.1f dB",
            route.name, kind,
            "/".join(map(str, source)),
            "/".join(map(str, route.output)),
            route.level,
        )
    # Counted from what was *written*, not from what the config holds.
    # Those differ as soon as `leave_alone` is in play, and a log line
    # claiming work it did not do is worse than no line: it is the first
    # place anybody looks when a fader did not move.
    written = {w.path for w in wanted.channel()}
    if written:
        log.info("channel state: %d setting(s) on %d channel(s)%s",
                 len(written),
                 len({tuple(p.split("/")[1:3]) for p in written}),
                 "" if not skip else " (%d left to the device)" % len(skip))
    elif skip:
        log.info("channel state: nothing to write; %d setting(s) left to "
                 "the device", len(skip))


def _read_gain_dependencies(backend: Control, paths: Sequence[str],
                            should_stop: StopCheck, *,
                            links: Optional[Mapping[str, int]] = None
                            ) -> Dict[str, Tuple[str, Args]]:
    """One fresh, completed DEVICE window; missing or malformed stays unknown."""
    if not paths:
        return {}
    if should_stop():
        raise OSError("stop requested before AutoSet observation; writes refused")
    observed = Observation({path: ("i", (value,)) for path, value in (links or {}).items()},
                           dependency_paths=paths)
    backend.request_dump()
    deadline = time.monotonic() + VERIFY_TIMEOUT
    while (not observed.dependencies_complete or not observed.complete) \
            and time.monotonic() < deadline:
        if should_stop():
            raise OSError("stop requested during AutoSet observation; writes refused")
        for report in backend.messages(.25):
            observed.absorb(report)
    if should_stop():
        raise OSError("stop requested after AutoSet observation; writes refused")
    return dict(observed.latest)


def _gain_problem(config: Config, selected: Sequence[Write],
                  latest: Mapping[str, Tuple[str, Args]], intent: ApplyIntent,
                  retained: Sequence[str], confirmed: Sequence[str]) -> Optional[str]:
    """No direct or indirect gain write may defeat AutoSet's ownership."""
    linked = [w.path for w in selected if w.path.endswith("/stereo") and w.args == (1,)]
    gains = retained_gains(config, latest, linked=linked,
                           use_declarations=intent.preserves_remember)
    blocked = sorted(w.path for w in selected if w.path in gains)
    if blocked:
        return "AutoSet is active or not confirmed off for gain writes: %s" % ", ".join(blocked)
    pending_links = {w.path for w in selected if w.path.endswith("/stereo")}
    wrong = sorted(path for path, value in _input_link_expectations(config, selected).items()
                   if path not in pending_links and flag_value(latest.get(path)) != value)
    if wrong:
        return ("fresh input link state is not confirmed for dependent mix writes: %s"
                % ", ".join(wrong))
    return retention_problem(config, selected, tuple(set(retained) | gains), confirmed,
                             observed=latest)


def _input_link_expectations(config: Config, selected: Sequence[Write]) -> Dict[str, int]:
    """AutoSet-relevant input links needed by the selected matrix writes."""
    writes = {w.path for w in selected}
    dependencies = set(dependency_paths(config))
    links: Dict[str, int] = {}
    for route in config.routes:
        kind, _source = route.source
        if kind != "input" or not writes.intersection(
                p for p, _t, _a in mix_messages(route) if p.startswith("/mix/")):
            continue
        for path, _tags, args in link_messages(route):
            if path in dependencies and path.endswith("/stereo"):
                links[path] = int(args[0])
    return links


def _require_gain_ownership(config: Config, selected: Sequence[Write],
                            latest: Mapping[str, Tuple[str, Args]], intent: ApplyIntent,
                            retained: Sequence[str], confirmed: Sequence[str]) -> None:
    problem = _gain_problem(config, selected, latest, intent, retained, confirmed)
    if problem:
        raise OSError(problem)


def _send_in_order(wanted: Plan, config: Config,
                   device: Control, require_confirmation: Sequence[str],
                   should_stop: StopCheck, intent: ApplyIntent,
                   latest: Mapping[str, Tuple[str, Args]], retained: Sequence[str],
                   confirmed: Sequence[str]) -> None:
    """Links, the barrier, the mix, then channel state.

    Channel state last: it does not depend on the barrier, and a fader or
    a reference level landing before the routing exists would be audible
    for the width of it. A burst that fails part of the way is reported
    for the whole apply: the backend knows how far its burst came, and how
    far the *apply* came is that plus the bursts on either side.
    """
    # Explicit AutoSet-off is a prerequisite, AutoSet-on follows every gain.
    off = tuple(w for w in wanted.channel() if w.path.endswith("/autoset") and w.args == (0,))
    phases = tuple(Phase)
    bursts = tuple(tuple(w for w in wanted.writes if w.phase == phase) for phase in phases)
    echoes = {write.path: int(write.args[0]) for write in wanted.links()
              if write.path.startswith("/output/") or write.path in require_confirmation}
    guard = PlaybackGuard(config)
    for index, burst in enumerate(bursts):
        phase = phases[index]
        before = [w.path for done in bursts[:index] for w in done]
        after = [w.path for rest in bursts[index + 1:] for w in rest]
        if not burst and not after:
            break
        if should_stop():
            raise WriteFailed(OSError("stop requested; remaining writes refused"),
                              before, [w.path for w in burst] + after)
        try:
            if phase == Phase.LINK and off:
                latest = _read_gain_dependencies(device, dependency_paths(config), should_stop)
                _require_gain_ownership(config, tuple(w for rest in bursts[index:] for w in rest),
                                        latest, intent, retained, confirmed)
            if phase == Phase.MIX and burst and (echoes or require_confirmation):
                _cross_the_barrier(config, device,
                                   expected=echoes,
                                   require_confirmation=require_confirmation,
                                   should_stop=should_stop)
            if phase == Phase.MIX and burst:
                needed = _input_link_expectations(config, burst)
                if needed and wanted.links():
                    latest = _read_gain_dependencies(device, dependency_paths(config), should_stop,
                                                     links=needed)
                _require_gain_ownership(config, burst, latest, intent, retained, confirmed)
            if phase == Phase.CHANNEL and burst:
                if (any(w.path.startswith("/input/") for w in wanted.links())
                        and not _input_link_expectations(config, wanted.mix())):
                    latest = _read_gain_dependencies(device, dependency_paths(config), should_stop)
                _require_gain_ownership(config, burst, latest, intent, retained, confirmed)
            if burst:
                guard.check()
                device.send(w.message() for w in burst)
        except OSError as exc:
            if isinstance(exc, WriteFailed):
                before.extend(exc.written)
                remaining = list(exc.unwritten)
            else:
                # A mode check refused this phase before it sent anything.
                # Earlier phases are still partial writes, not a rollback.
                remaining = [w.path for w in burst]
            raise WriteFailed(exc, before, remaining + after) from exc


def output_link_state(routes: Sequence[Route]) -> Dict[str, int]:
    """The ``/output/<n>/stereo`` values a routing depends on.

    Maps each register to the value the device has to report before the
    mix matrix may be written. Routes are applied in file order, so a
    later route targeting the same pair wins -- the same rule the mix
    writes follow.
    """
    state: Dict[str, int] = {}
    for route in routes:
        for path, _types, args in link_messages(route):
            if path.startswith("/output/"):
                state[path] = int(args[0])
    return state


def send_mix(config: Config, backend: Control, *, confirmed: Sequence[str] = (),
             observed: Optional[Mapping[str, Tuple[str, Args]]] = None) -> None:
    """Repair the mix matrix without reasserting REMEMBER starting values.

    Through the planner, like the apply: a register two routes share
    goes out once, and which writes belong to the mix phase is the
    plan's decision rather than a second walk over the routes. Until
    0.6.2 this built its own datagrams from ``mix_messages`` -- the last
    path that did, and the reason ``reconcile``'s docstring could still
    describe the runtime as four overlapping paths long after the apply
    had moved over.
    """
    wanted = matrix_repair_plan(config, confirmed, observed=observed)
    retained = remembered_paths(config, observed)
    problem = retention_problem(config, wanted.writes, retained, confirmed,
                                observed=observed)
    if problem:
        raise WriteFailed(OSError(problem), [], [write.path for write in wanted.writes])
    mute_phases = (Phase.PLAYBACK_MUTE_ON, Phase.PLAYBACK_MUTE_OFF)
    if any(write.phase in mute_phases for write in wanted.writes):
        latest = (dict(observed) if observed is not None else
                  _read_gain_dependencies(backend, dependency_paths(config), never_stop))
        problem = _gain_problem(config, wanted.writes, latest, ApplyIntent.REPAIR,
                                retained, confirmed)
        if problem:
            raise WriteFailed(OSError(problem), [], [write.path for write in wanted.writes])
        _send_in_order(wanted, config, backend, (), never_stop, ApplyIntent.REPAIR,
                       latest, retained, confirmed)
    else:
        PlaybackGuard(config).check()
        backend.send(write.message() for write in wanted.mix())
    log.info("mix matrix re-applied against the synchronized link state")
