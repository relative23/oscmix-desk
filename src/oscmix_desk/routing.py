"""Translating routes into OSC messages, and applying them.

The order is load-bearing: channel links first, mix matrix second.
See docs/OSC-PROTOCOL.md for why."""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable, Dict, Mapping, Optional, Sequence

from .backend import Backend, loopback
from .constants import (
    DEFAULT_OSC_RECV_PORT,
    LINK_ECHO_TIMEOUT,
    LINK_SETTLE,
    LINK_SYNC_BLIND_DELAY,
)
from .errors import WriteFailed
from .log import log
from .model import Config, Route
from .observation import Observation
from .reconcile import Plan, desired, link_messages, plan
from .streams import PlaybackGuard

# Asked before every write and between every phase of the background
# verifier. See docs/decisions/0009-verifier-stop-contract.md: the
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


def wait_unless_stopped(seconds: float, should_stop: StopCheck) -> bool:
    """Sleep, waking early on a stop request. True if a stop was asked for.

    A plain ``time.sleep`` is what let ``LINK_SYNC_BLIND_DELAY`` outlast
    ``TimeoutStopSec``: the session would exit with the verifier still
    parked in it, and the daemon thread would be cut wherever it happened
    to be -- possibly between two mix writes. The delay is 5 s now
    (ADR 0010) and would fit either way, but the property this function
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
        time.sleep(min(0.1, remaining))


class LinkEcho(Enum):
    """What the wait for the link echo came to.

    A contradiction is not silence, and neither is cancellation. Only
    CONFIRMED is truthy; receive failures remain exceptions.
    """

    CONFIRMED = "confirmed"          # every register arrived at its value
    SILENT = "silent"                # the wait ran out
    UNOBSERVABLE = "unobservable"    # the mixer GUI holds the receive port
    CONTRADICTED = "contradicted"    # latest decoded value is wrong/invalid
    CANCELLED = "cancelled"          # no further write is permitted

    def __bool__(self) -> bool:
        return self is LinkEcho.CONFIRMED


def await_link_echo(expected: Mapping[str, int], recv_port: int,
                    timeout: Optional[float] = None, *,
                    backend: Optional[Backend] = None,
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
    a backend-specific settle for silence or a held port, never for a
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
    device = backend if backend is not None else loopback(0, recv_port)
    listener = device.listen()
    if listener is None:
        return LinkEcho.UNOBSERVABLE
    observed = Observation({path: ("i", (value,)) for path, value in expected.items()})
    deadline = time.monotonic() + timeout
    try:
        while not observed.complete:
            if should_stop():
                return LinkEcho.CANCELLED
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for report in listener.messages(min(.25, remaining)):
                observed.absorb(report)
        if should_stop():
            return LinkEcho.CANCELLED
        if observed.mismatched:
            return LinkEcho.CONTRADICTED
        return LinkEcho.CONFIRMED if observed.complete else LinkEcho.SILENT
    finally:
        listener.close()


def _cross_the_barrier(config: Config, recv_port: int,
                       device: Backend, *,
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
    echoed = await_link_echo(output_link_state(config.routes), recv_port,
                             timeout, backend=device, should_stop=should_stop)
    if echoed is LinkEcho.CANCELLED:
        raise OSError("stop requested during link observation; remaining writes refused")
    if echoed is LinkEcho.CONTRADICTED:
        raise OSError("link state contradicted; dependent writes refused")
    if require_confirmation and echoed is not LinkEcho.CONFIRMED:
        raise OSError("fresh link confirmation required for %s; remaining writes refused"
                      % ", ".join(sorted(require_confirmation)))
    if echoed is LinkEcho.UNOBSERVABLE:
        log.info("link echo unobservable (UDP %d in use); waiting %.1fs",
                 recv_port, LINK_SETTLE)
        if wait_unless_stopped(LINK_SETTLE, should_stop):
            raise OSError("stop requested during link settle; remaining writes refused")
    elif echoed is LinkEcho.SILENT:
        # Normal when the pairs were already linked: no change, no echo.
        log.info("no link change reported within %.1fs; mix matrix will "
                 "be re-applied after the register sync", timeout)
    else:
        log.info("channel pairs linked and confirmed by the device")


def apply_routing(config: Config, port: int,
                  recv_port: int = DEFAULT_OSC_RECV_PORT, *,
                  backend: Optional[Backend] = None,
                  leave_alone: Sequence[str] = (),
                  require_link_confirmation: Sequence[str] = (),
                  should_stop: StopCheck = never_stop) -> None:
    """Send the routing in two phases: link the pairs, then fill the mix.

    Both phases are separated by the link barrier above. Sending them in
    one burst is what silences every even output (see LINK_ECHO_TIMEOUT).

    The *what* is a ``reconcile.Plan``: the registers the config asks
    for, deduplicated and split at the barrier. What is left here is the
    *when* -- send, wait out the barrier, send -- which is this
    function's whole job and the only part that needs a socket and a
    clock.

    ``leave_alone`` names registers this apply must not touch -- the
    remembered ones somebody has turned. A re-apply writes the whole
    routing, so without it one unconfirmed link register drags every
    remembered fader back to the config value (ADR 0012).

    It takes the **whole config**, not a list of routes. Rebuilding one
    from routes alone silently dropped `config.channels`, so channel
    sections parsed, validated, showed in `--dry-run` and never reached
    the device. `tests/test_apply_routing.py` now fails on any function
    that rebuilds a Config from a subset of its fields.
    """
    skip = set(leave_alone)
    wanted = plan([e for e in desired(config) if e.path not in skip])
    # A caller may supply the backend. The profile switch does, because
    # the alternative -- its own send/barrier/send -- is what it had
    # first, and it dropped the barrier: applying and verifying a
    # profile took 48 ms on a live UCX II, which is not enough time for
    # a barrier that is measured in seconds.
    device = backend if backend is not None else loopback(port, recv_port)
    # Only the output links need the barrier: /playback/<n>/stereo goes
    # through setinputstereo(), which updates oscmix's state right away,
    # while /output/<n>/stereo relies on the device report -- see
    # backend.Traits.reports_link_state_on_write.
    _send_in_order(wanted, config, recv_port, device,
                   require_link_confirmation, should_stop)
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


def _send_in_order(wanted: Plan, config: Config, recv_port: int,
                   device: Backend, require_confirmation: Sequence[str],
                   should_stop: StopCheck) -> None:
    """Links, the barrier, the mix, then channel state.

    Channel state last: it does not depend on the barrier, and a fader or
    a reference level landing before the routing exists would be audible
    for the width of it. A burst that fails part of the way is reported
    for the whole apply: the backend knows how far its burst came, and how
    far the *apply* came is that plus the bursts on either side.
    """
    bursts = (wanted.links(), wanted.mix(), wanted.channel())
    guard = PlaybackGuard(config)
    for index, burst in enumerate(bursts):
        before = [w.path for done in bursts[:index] for w in done]
        after = [w.path for rest in bursts[index + 1:] for w in rest]
        if should_stop():
            raise WriteFailed(OSError("stop requested; remaining writes refused"),
                              before, [w.path for w in burst] + after)
        try:
            if index == 1 and burst:
                _cross_the_barrier(config, recv_port, device,
                                   require_confirmation=require_confirmation,
                                   should_stop=should_stop)
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


def send_mix(config: Config) -> None:
    """Write the mix matrix (and output volumes) of every route.

    Through the planner, like the apply: a register two routes share
    goes out once, and which writes belong to the mix phase is the
    plan's decision rather than a second walk over the routes. Until
    0.6.2 this built its own datagrams from ``mix_messages`` -- the last
    path that did, and the reason ``reconcile``'s docstring could still
    describe the runtime as four overlapping paths long after the apply
    had moved over.
    """
    PlaybackGuard(config).check()
    loopback(config.osc_port, config.osc_recv_port).send(
        write.message() for write in plan(desired(config)).mix())
    log.info("mix matrix re-applied against the synchronized link state")


def blind_reapply_mix(config: Config,
                      should_stop: StopCheck = never_stop,
                      why: Optional[str] = None) -> None:
    """Re-apply the mix when the device dump cannot be observed.

    ``why`` names the cause in the log when it is not the usual one.

    The mixer GUI holds the receive port, so the link reports are
    invisible; ``/refresh`` still has to go out because that dump is what
    teaches oscmix the device's real link state, and the wait afterwards
    is a plain guess at how long it takes.

    This is the longest-running path in the verifier, and the one a user
    actually hits: the GUI holding the port is the normal desktop case.
    A stop during the delay abandons the re-apply rather than writing
    routing at a backend that is being shut down.
    """
    if should_stop():
        return
    loopback(config.osc_port, config.osc_recv_port).request_dump()
    log.info("register sync unobservable (%s); re-applying mix after %.0fs",
             why or "UDP %d in use" % config.osc_recv_port,
             LINK_SYNC_BLIND_DELAY)
    if wait_unless_stopped(LINK_SYNC_BLIND_DELAY, should_stop):
        log.info("stop requested during the blind delay; mix not re-applied")
        return
    send_mix(config)
