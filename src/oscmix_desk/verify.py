"""Reading the applied routing back from the device."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .backend import Backend, loopback
from .config import Config
from .constants import DUMP_LISTEN_SETTLE, VERIFY_SETTLE, VERIFY_TIMEOUT
from .errors import ReceivePortError
from .log import log
from .reconcile import desired, matches, policy_for
from .registers import (
    PIN,
    VERIFIABLE,
    Device,
    cold_plug_complete,
    device_for_name,
    register_at,
    verify_class,
)
from .routing import (
    StopCheck,
    apply_routing,
    blind_reapply_mix,
    never_stop,
    output_link_state,
    send_mix,
    wait_unless_stopped,
)

# One expected register: its OSC type tags and the arguments it must
# report back. Keyed by OSC path.
Registers = Dict[str, Tuple[str, Tuple[object, ...]]]


def expected_registers(config: Config) -> Registers:
    """The register state ``config`` should produce, keyed by OSC path.

    Takes the whole ``Config``, not its routes. It used to take
    ``Sequence[Route]`` and rebuild ``Config(routes=...)`` internally,
    which silently dropped ``config.channels``: every ``[input:N]`` and
    ``[output:N]`` register was written to the device and then left out
    of the read-back, so the run reported "routing verified" without
    having looked at any of it.

    That is the *same* defect as the one on the write path a commit
    earlier -- a function that took a part of the config and reconstructed
    the rest as empty. Both were invisible because everything they did
    report was correct. Taking the whole config is the fix and also the
    guard: there is no longer a part to forget.

    This is ``reconcile.desired`` projected to the shape the read-back
    uses. ``tests/test_reconcile.py`` asserts the two agree for every
    config shape in its table, so they cannot drift apart quietly.
    """
    return {entry.path: (entry.tags, entry.args) for entry in desired(config)}


def _register_matches(want_types: str, want_args: Sequence[object],
                      got_args: Sequence[object]) -> bool:
    """Compare a reported register against the expected value.

    Delegates to ``reconcile.matches`` so the read-back and the plan
    cannot disagree about what "equal" means -- which they would have,
    the moment one of them learned that a muted gain reads back as -inf
    and the other did not.
    """
    return matches(want_types, tuple(want_args), tuple(got_args))

def register_ever_reported(path: str,
                           device: Optional[Device] = None) -> bool:
    """Whether this backend reports the register *at all*.

    One family never appears, measured: the playback *mix matrix*,
    ``/mix/<out>/playback/<pb>``. Anything the register model calls
    write-only is excluded too.

    Note the shape of that path. This rule used to exclude everything
    under ``/playback/`` as well, which is wrong and was wrong in a
    released version: the recorded dump carries 42 registers under
    ``/playback/``, including every ``/playback/<n>/stereo`` -- the
    input-side link flags, which arrive first, at 0.0 s. Excluding them
    meant a lost link write was never counted as a problem and never
    retried, on the exact register family the stereo-link race is about.

    ``tests/test_verify.py`` now holds this function against
    ``tests/data/refresh-dump.json`` register by register, so the rule
    cannot drift away from the recording again.

    Distinct from :func:`register_promptly_reported`, and the split
    matters. That function used to answer both questions -- "is absence
    a problem?" and "may the observation window close?" -- and channel
    state answers them differently: it is reported on a warm dump but
    ragged after a cold plug. Sharing one answer meant the window closed
    as soon as the *stereo flags* matched, so `/output/1/volume` was
    never confirmed even when the device had already reported it.

    Measured on a UCX II: applied, correct at the device (0.0 -> -6.0),
    and reported unverified by a read-back that had stopped listening.
    """
    if device is not None:
        klass = verify_class(device, path)
        if klass is not None:
            # The table's word, one source: VERIFIABLE is reported,
            # WRITE_ONLY and REESTABLISHED never are. Until 0.6.3 the
            # playback matrix was excluded by a string rule *beside* the
            # table that already classed it -- two places for one fact.
            return klass == VERIFIABLE
    # Without a model, the one hand-written rule left: the playback
    # matrix, measured never to appear (ADR 0002).
    return not (path.startswith("/mix/") and "/playback/" in path)


def register_promptly_reported(path: str,
                               device: Optional[Device] = None) -> bool:
    """Whether an *absent* register is worth re-sending for.

    A hint, not a filter: every register that *does* appear in the dump
    is compared, whatever this says. It steers exactly one thing --
    whether a missing register is a problem (probably lost, re-send) or
    merely unverifiable (a note).

    It used to steer the early exit of the observation window as well.
    That was one answer to two questions, and channel state answers them
    differently; see :func:`register_ever_reported` for what that cost.

    Everything :func:`register_ever_reported` rules out is ruled out
    here too.

    **And channel state is not reported *completely* after a cold plug.**
    Measured across a real USB replug: 1234 of 1932 non-meter registers
    arrived and nothing followed for 272 s. Only the stereo flags came
    back for every channel. `/output/N/mute` returned for channels 1, 2,
    3, 8, 9 and 10 and not for 4-7 or 11-20 -- ragged, so a truncated
    stream rather than a rule.

    Without this, an `[output:N]` section would be reported unconfirmed
    on every hotplug and the whole routing re-sent, every time. The
    registers verified before 0.3.0 are all in the fast,
    complete part, which is exactly why nothing noticed until channel
    state arrived.
    """
    if not register_ever_reported(path, device):
        return False
    if device is not None and _is_channel_state(device, path):
        # Modelled, verifiable, and not guaranteed whole after a
        # hotplug. Absence is a note; a value that *does* arrive is
        # still compared like any other.
        return cold_plug_complete(device, path)
    return True


def _is_channel_state(device: Device, path: str) -> bool:
    """Whether the model declares this path as something a config sets.

    Asked the register model rather than `settable_options`, which knows
    only the *flat* options of a family. Every nested one answered "not
    channel state" and so skipped the cold-plug rule written for exactly
    this -- 480 EQ registers, of which a cold plug delivers 332, each
    one classified as promptly reported and therefore re-sent on every
    hotplug. Declaring dynamics would have added 320 more.
    """
    register = register_at(device, path)
    return (register is not None and register.domain is not None
            and register.template.startswith(("/input/{ch}/",
                                              "/output/{ch}/")))

@dataclass
class VerifyResult:
    """Per-register outcome of a routing read-back."""

    confirmed: List[str]
    mismatched: List[str]
    unobserved: List[str]


def _absorb(report: Tuple[str, str, Sequence[object]], registers: Registers,
            confirmed: Set[str], mismatched: Set[str],
            on_observed: Optional[Callable[[str, Sequence[object]],
                                           None]]) -> None:
    """Classify one reported register against what was expected.

    Decoding and the "skip a malformed message rather than end the dump"
    rule moved into the backend seam, which is where reading off a
    socket belongs. What is left here is the judgement.
    """
    path, _tags, args = report
    if on_observed is not None:
        on_observed(path, args)
    expected = registers.get(path)
    if expected is None or path in confirmed:
        return
    if _register_matches(expected[0], expected[1], args):
        confirmed.add(path)
        mismatched.discard(path)
    else:
        mismatched.add(path)


def _observe(listener: object, registers: Registers, prompt: Set[str],
             confirmed: Set[str], mismatched: Set[str],
             on_observed: Optional[Callable[[str, Sequence[object]], None]],
             should_stop: StopCheck, timeout: float) -> None:
    """Read reports until the window closes, a stop is asked, or time runs out.

    A stop request ends the window at the top of the loop, so the longest
    this can hold a shutdown is one socket timeout (0.25 s). What has
    been observed so far stays in the caller's sets rather than being
    discarded -- the caller decides whether to act on it, and it will not.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_stop():
            return
        if _window_may_close(registers, prompt, confirmed, mismatched):
            return
        for report in listener.messages(0.25):  # type: ignore[attr-defined]
            _absorb(report, registers, confirmed, mismatched, on_observed)


def _window_may_close(registers: Registers, reportable: Set[str],
                      confirmed: Set[str], mismatched: Set[str]) -> bool:
    """Whether the observation window has learned everything it can.

    A mismatch always keeps it open: a stale value echoed during
    settling is normal, and a later correcting report must be able to
    override it.

    Otherwise it closes once every register this backend *can* report
    has been confirmed. Waiting past that only waits for registers the
    backend will never send -- the playback mix matrix, and anything
    write-only -- so the remaining time buys nothing.

    ``reportable`` used to be the *promptly* reported set, which is a
    smaller one, and the difference was not academic: the stereo flags
    always arrive first and always match, so the window closed before
    any channel state could arrive and `/output/<n>/volume` came back
    unconfirmed while sitting correct on the device.
    """
    if mismatched:
        return False
    if len(confirmed) == len(registers):
        return True
    return bool(reportable) and reportable <= confirmed


def verify_routing(registers: Registers, send_port: int, recv_port: int,
                   timeout: float = VERIFY_TIMEOUT,
                   on_observed: Optional[Callable[[str, Sequence[object]],
                                                  None]] = None,
                   should_stop: StopCheck = never_stop,
                   *,
                   device_model: Optional[Device] = None,
                   backend: Optional[Backend] = None,
                   ) -> Optional[VerifyResult]:
    # device_model is keyword-only and last on purpose: inserting it into
    # the positional signature silently shifted `timeout` into it for
    # every existing caller, which the suite caught immediately and a
    # reader would not have.
    """Ask oscmix to dump its state and compare it against ``registers``.

    Returns ``None`` when verification is impossible because the receive
    port is taken, normally by the mixer GUI; a port that cannot be bound
    for any other reason raises ``ReceivePortError``. Otherwise
    a :class:`VerifyResult` classifying every expected register as
    confirmed (reported with a matching value), mismatched (reported
    with a different value -- a later matching report overrides), or
    unobserved (never reported within the window).

    A short settle precedes the request; see ``DUMP_LISTEN_SETTLE``.

    ``on_observed`` is called with each register path and its reported
    arguments the moment it is first reported. That is how the mix re-apply hooks into this dump
    instead of requesting a second one: two overlapping dumps measurably
    starve each other and confirm fewer registers.
    """
    confirmed: Set[str] = set()
    mismatched: Set[str] = set()
    # The early exit turns on what the backend reports *ever*, not on
    # what it reports promptly: closing the window on the prompt set
    # meant channel state was structurally unconfirmable, because the
    # stereo flags always arrive first and always match.
    #
    # The cost is paid on a cold plug, where channel state is ragged and
    # this now waits out the full window instead of exiting early. That
    # is once per hotplug, against a verdict that was otherwise wrong
    # every time.
    prompt = {path for path in registers
              if register_ever_reported(path, device_model)}
    # A caller may hand in its own backend -- the profile switch does,
    # so that the switch and the session share one read-back loop
    # instead of two that can disagree. Both defects 0.3.0 fixed
    # were a second implementation of something that already existed.
    device = backend if backend is not None else loopback(send_port,
                                                          recv_port)
    listener = device.listen()
    if listener is None:
        return None
    try:
        time.sleep(DUMP_LISTEN_SETTLE)   # see the constant: ICMP backlog
        device.request_dump()
        _observe(listener, registers, prompt, confirmed, mismatched,
                 on_observed, should_stop, timeout)
        unobserved = [path for path in registers
                      if path not in confirmed and path not in mismatched]
        return VerifyResult(sorted(confirmed), sorted(mismatched),
                            sorted(unobserved))
    finally:
        listener.close()


def _link_sync_observer(config: Config, pending_links: Dict[str, int],
                        reapplied: Dict[str, bool], should_stop: StopCheck
                        ) -> Callable[[str, Sequence[object]], None]:
    """Watch the dump for the link state, and re-apply the mix once it lands.

    This is the point of sharing one ``/refresh`` between verification
    and the re-apply (ADR 0002): the moment the dump has reported every
    ``/output/<n>/stereo`` at its expected value, oscmix's own link state
    is correct and the mix matrix can be written from a known-good state.
    Requesting a second dump for it measurably starves both.

    ``pending_links`` and ``reapplied`` are mutated in place; they are
    the caller's, because the caller still needs to know afterwards
    whether the re-apply happened.
    """
    def on_observed(path: str, args: Sequence[object]) -> None:
        # Only a report of the *expected* link value means oscmix's state
        # is right; a stale opposite value must not release the re-apply.
        if path in pending_links and args:
            try:
                reported = int(args[0])  # type: ignore[call-overload]
            except (TypeError, ValueError):
                return
            if reported != pending_links[path]:
                return
            del pending_links[path]
        if not pending_links and not reapplied["done"]:
            # Write 1 of 3 (ADR 0009). Reached from inside the dump
            # observation, so the verify loop's own stop check has not
            # run since this datagram arrived.
            if should_stop():
                return
            reapplied["done"] = True
            send_mix(config)

    return on_observed


def _reapply_without_confirmation(config: Config,
                                  pending_links: Dict[str, int],
                                  reapplied: Dict[str, bool]) -> None:
    """Write 2 of 3: the dump ended without ever reporting the links.

    The device reports a register only when it *changes*, so a pair that
    was already linked produces no report however long the window is.
    Writing the mix anyway is correct in that case and harmless in the
    other; leaving it unwritten would strand the routing on whatever the
    foreground apply managed before the barrier.
    """
    reapplied["done"] = True
    log.warning("dump never reported %s; re-applying mix anyway",
                ", ".join(sorted(pending_links)))
    send_mix(config)


def _report(result: VerifyResult, config: Config, device: Optional[Device],
            attempt: int) -> Tuple[List[str], List[str]]:
    """Say what the read-back found; return (problems, kept).

    Split out of ``verify_and_repair`` when it crossed the length
    ceiling, and the split is where the seam already was: this is the
    judgement, the caller is the retry loop.

    Both lists are returned rather than the caller recomputing ``kept``
    for the re-apply. Two computations of one fact is how a log and an
    action come to disagree -- and "real in the log, absent at the
    device" is a defect 0.3.0 shipped once.
    """
    kept = _kept_by_the_device(result, device, config.policies)
    if kept:
        # Information, not a warning: the config asked for one value,
        # somebody set another, and this session is not going to argue.
        # Logged by name so "why is my fader not what the config says"
        # has an answer in the journal.
        log.info("device value kept for %s (remembered, not pinned)",
                 ", ".join(kept))
    problems = _unconfirmed(result, device, config.policies)
    if not problems:
        log.info("routing verified against device state "
                 "(%d confirmed; %d not reported by the device dump)%s",
                 len(result.confirmed), len(result.unobserved),
                 "" if attempt == 1 else " -- after retry")
    return problems, kept


def _unconfirmed(result: VerifyResult, device: Optional[Device] = None,
                 overrides: Optional[Dict[Tuple[str, str], str]] = None
                 ) -> List[str]:
    """The registers that count as a problem worth re-sending for.

    Absent counts only for the families the dump reports promptly --
    ``/mix/*/playback/*`` never appears at all, so treating its absence
    as a problem would put every run into a retry it cannot win.

    A mismatch counts only for PIN registers. On a REMEMBER register a
    mismatch is the *user*, not a fault: they turned something between
    the apply and the dump, and re-sending would undo it while they
    watched. That is the whole pin/remember distinction, and this is the
    one place it changes behaviour.
    """
    lost = [path for path in result.unobserved
            if register_promptly_reported(path, device)]
    insisted = [path for path in result.mismatched
                if policy_for(path, device, overrides) == PIN]
    return sorted(insisted + lost)


def _kept_by_the_device(result: VerifyResult,
                        device: Optional[Device] = None,
                        overrides: Optional[Dict[Tuple[str, str], str]] = None
                        ) -> List[str]:
    """Mismatches this session is deliberately letting the device keep."""
    return sorted(path for path in result.mismatched
                  if policy_for(path, device, overrides) != PIN)


def reconcile_now(config: Config, reason: str,
                  should_stop: StopCheck = never_stop,
                  backend: Optional[Backend] = None) -> bool:
    """Re-apply the routing, minus what the device is allowed to keep.

    The trigger side of the pin/remember model (ADR 0012). Pinned
    registers are written back; remembered ones the device is holding at
    a different value are left exactly as they are, because somebody
    turned them.

    Reads the device *first* -- that read is the only way to know which
    remembered registers to protect, and it is why this is a reconcile
    rather than a re-apply. Without it the write would be indiscriminate
    and every hand-set fader would snap back on every trigger, which is
    the behaviour this model exists to end.

    Returns whether the routing was written. False means the receive
    port is held (the mixer GUI), and this refuses rather than writing
    blind: with no dump there is no way to tell a remembered register
    from a pinned one at the device, so a blind write would silently do
    the indiscriminate thing.

    Triggers are enumerated and never a timer: docs/decisions/0013.
    """
    device = device_for_name(config.device_name)
    result = verify_routing(expected_registers(config), config.osc_port,
                            config.osc_recv_port, VERIFY_TIMEOUT,
                            should_stop=should_stop, device_model=device,
                            backend=backend)
    if should_stop():
        return False
    if result is None:
        log.warning("reconcile (%s) skipped: UDP %d in use -- with no dump "
                    "there is no way to tell what to leave alone",
                    reason, config.osc_recv_port)
        return False

    kept = _kept_by_the_device(result, device, config.policies)
    drifted = _unconfirmed(result, device, config.policies)
    # "drifted", not "to correct". The write below is not selective: it
    # re-applies everything except `kept`, because the playback mix
    # matrix is never reported and so can never be shown to be intact.
    # Saying "N to correct" implied a selectivity that is not there, and
    # a log line that overstates what happened is the first thing
    # somebody reads when a fader did not move.
    log.info("reconcile (%s): %d confirmed, %d drifted%s; re-applying",
             reason, len(result.confirmed), len(drifted),
             ", %d left to the device (%s)" % (len(kept), ", ".join(kept))
             if kept else "")
    apply_routing(config, config.osc_port, config.osc_recv_port,
                  leave_alone=kept, backend=backend)
    return True


def _read_back(registers: Registers, config: Config,
               on_observed: Callable[[str, Sequence[object]], None],
               should_stop: StopCheck,
               device: Optional[Device]) -> Optional[VerifyResult]:
    """``verify_routing`` for the verifier, with the desk left whole.

    For the desk a port that cannot be bound is the held port over again:
    the dump that syncs oscmix's link state cannot be watched, so the mix
    is re-established blind. For the caller it is not -- a port that can
    never be read is not a verification "skipped" -- so the error goes on
    once the mix is safe (0.6.11).
    """
    try:
        return verify_routing(registers, config.osc_port,
                              config.osc_recv_port, VERIFY_TIMEOUT,
                              on_observed=on_observed,
                              should_stop=should_stop, device_model=device)
    except ReceivePortError as exc:
        blind_reapply_mix(config, should_stop, why=exc.strerror)
        raise


def verify_and_repair(config: Config,
                      should_stop: StopCheck = never_stop) -> None:
    """Read the applied routing back and re-send once on problems.

    A register is a *problem* when the device reported a different value
    (mismatched) or when a promptly-reported register never appeared
    (probably lost). Registers the dump is known not to report in time
    are logged as information, never as a warning -- but if one of them
    does appear, it is compared like any other, so a future oscmix that
    dumps more (or different) registers is handled without code changes.

    Verification is advisory: OSC over UDP has no delivery guarantee, so
    a failed read-back is logged (and retried once) but never brings the
    service down -- a restart loop would not improve anything.

    The dump this requests doubles as the link-state sync -- see
    ``_link_sync_observer``. The mix matrix itself is unverifiable (a
    ``/mix`` write draws no reply and the dump omits the playback
    matrix), so it is re-established rather than checked.

    ``should_stop`` is asked between every phase and before each of the
    three writes below, and the session waits for this thread before
    exiting: docs/decisions/0009-verifier-stop-contract.md.

    Raises ``ReceivePortError`` when the receive port cannot be bound for
    a reason other than a holder -- after the mix has been re-established
    blind, so the desk is whole when it does (ADR 0025).
    """
    device = device_for_name(config.device_name)
    registers = expected_registers(config)
    pending_links = output_link_state(config.routes)
    reapplied = {"done": not pending_links}
    on_observed = _link_sync_observer(config, pending_links, reapplied,
                                      should_stop)

    problems: List[str] = []
    for attempt in (1, 2):
        if should_stop():
            return
        result = _read_back(registers, config, on_observed, should_stop,
                            device)
        if should_stop():
            return
        if result is None:
            log.info("routing verification skipped: UDP %d in use "
                     "(mixer GUI running?)", config.osc_recv_port)
            blind_reapply_mix(config, should_stop)
            return
        if not reapplied["done"]:
            _reapply_without_confirmation(config, pending_links, reapplied)
        problems, kept = _report(result, config, device, attempt)
        if not problems:
            return
        if attempt == 1:
            # Write 3 of 3, and the only full re-apply. Both phases of it
            # would run against a terminating backend.
            if should_stop():
                return
            log.warning("%d register(s) unconfirmed (%s); re-sending routing",
                        len(problems), ", ".join(problems))
            # Everything except what the device is allowed to keep. A
            # re-apply is a whole-routing write, so without this a single
            # lost link register drags every remembered fader back to the
            # config value -- the policy would be real in the log and
            # absent at the device.
            apply_routing(config, config.osc_port, config.osc_recv_port,
                          leave_alone=kept)
            if wait_unless_stopped(VERIFY_SETTLE, should_stop):
                return
    log.warning("unconfirmed after retry: %s", ", ".join(problems))
