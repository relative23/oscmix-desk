"""Reading the applied routing back from the device."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .backend import Control
from .constants import VERIFY_SETTLE, VERIFY_TIMEOUT
from .devices import device_for_name
from .log import log
from .model import Config
from .observation import Observation
from .osc import (
    Args,
    Value,
)
from .reconcile import PHASE_LINK, ApplyIntent, desired, policy_for, remembered_paths
from .registers import (
    PIN,
    VERIFIABLE,
    Device,
    Policy,
    cold_plug_complete,
    register_at,
    verify_class,
)
from .routing import (
    StopCheck,
    apply_routing,
    never_stop,
    send_mix,
    wait_unless_stopped,
)

# One expected register: its OSC type tags and the arguments it must
# report back. Keyed by OSC path.
Registers = Dict[str, Tuple[str, Args]]


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


def register_ever_reported(path: str,
                           device: Optional[Device] = None) -> bool:
    """Whether the device reports the register *at all*.

    Two families never do: the playback *mix matrix*,
    ``/mix/<out>/playback/<pb>``, and the playback link flags,
    ``/playback/<n>/stereo``. Anything the register model calls
    write-only is excluded too.

    The link flags are in the recorded dumps, first and at 0.0 s,
    because the backend answers a refresh with its own view of them
    before the device says anything. The coordinated backend labels that
    view backend-derived and the client drops it, so waiting for it
    counted every start as incomplete. The mix registers still depend
    on the flags; the backend applies a link write before the next
    message on the same connection.

    ``tests/test_verify.py`` holds this function against
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
    # Without a model, the hand-written rule left: the playback matrix,
    # measured never to appear, and the backend's own link flags.
    if path.startswith("/playback/") and path.endswith("/stereo"):
        return False
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
    # Invalid is a subset of mismatched, preserving strict callers' meaning
    # while avoiding a claim that a malformed value was a user adjustment.
    invalid: List[str] = field(default_factory=list)


def _observe(backend: Control, observed: Observation, prompt: Set[str],
             on_observed: Optional[Callable[[str, Sequence[Value]], None]],
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
        if _window_may_close(observed.expected, prompt,
                             observed.confirmed, observed.mismatched):
            return
        for report in backend.messages(0.25):
            observed.absorb(report)
            if on_observed is not None:
                on_observed(report[0], report[2])


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


def verify_routing(registers: Registers, backend: Control,
                   timeout: float = VERIFY_TIMEOUT,
                   on_observed: Optional[Callable[[str, Sequence[Value]],
                                                  None]] = None,
                   should_stop: StopCheck = never_stop,
                   *,
                   device_model: Optional[Device] = None,
                   ) -> VerifyResult:
    # device_model is keyword-only and last on purpose: inserting it into
    # the positional signature silently shifted `timeout` into it for
    # every existing caller, which the suite caught immediately and a
    # reader would not have.
    """Ask oscmix to dump its state and compare it against ``registers``.

    Returns a :class:`VerifyResult` classifying every expected register as
    confirmed or mismatched according to the last decoded report received
    for that path in this window, or unobserved when none arrived. Arrival
    order is not an atomic hardware snapshot or a device timestamp.

    The checked request ACK opens the observation window. A failed connection,
    malformed delivery or unavailable fresh window raises ``ReceivePortError``;
    GTK cannot take this connection's observations away.

    ``on_observed`` is called for each decoded report. It is an observation
    hook, not a safe write boundary: the remainder of the delivery may
    contradict that report. The internal mix reapply uses the completed
    result and shares this request rather than starting a second dump.
    """
    observed = Observation(registers, device_model)
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
    if should_stop():
        return VerifyResult([], [], sorted(registers))
    backend.request_dump()
    _observe(backend, observed, prompt, on_observed, should_stop, timeout)
    return VerifyResult(sorted(observed.confirmed), sorted(observed.mismatched),
                        sorted(observed.unobserved), sorted(observed.invalid))


def _report(result: VerifyResult, config: Config, device: Optional[Device],
            attempt: int) -> List[str]:
    """Classify results; write ownership is selected independently by intent."""
    kept = _kept_by_the_device(result, device, config.policies)
    if kept:
        # Information, not a warning: the config asked for one value,
        # somebody set another, and this session is not going to argue.
        # Logged by name so "why is my fader not what the config says"
        # has an answer in the journal.
        log.info("device value kept for %s (remembered, not pinned)",
                 ", ".join(kept))
    problems = _unconfirmed(result, device, config.policies)
    retained_unknown = [path for path in result.unobserved
                        if policy_for(path, device, config.policies) != PIN]
    retained_invalid = [path for path in result.invalid
                        if policy_for(path, device, config.policies) != PIN]
    absent = [path for path in result.unobserved if path not in retained_unknown]
    prompt = sum(register_promptly_reported(path, device) for path in absent)
    unavailable = sum(not register_ever_reported(path, device) for path in absent)
    log.info("%s (%d confirmed; %d kept by REMEMBER; %d differing PIN; "
             "%d missing prompt; %d not observed; %d backend-unreportable; "
             "%d REMEMBER retained without feedback; %d REMEMBER invalid feedback)%s",
             "routing read-back needs repair" if problems else
             "routing verified against device state under PIN/REMEMBER policy",
             len(result.confirmed), len(kept),
             len(result.mismatched) - len(kept) - len(retained_invalid),
             prompt, len(absent) - prompt - unavailable, unavailable,
             len(retained_unknown), len(retained_invalid),
             "" if attempt == 1 else " -- after retry")
    return problems


def _unconfirmed(result: VerifyResult, device: Optional[Device] = None,
                 overrides: Optional[Mapping[Tuple[str, str], Policy]] = None
                 ) -> List[str]:
    """The registers that count as a problem worth re-sending for.

    Absent counts only for the families the dump reports promptly --
    ``/mix/*/playback/*`` never appears at all, so treating its absence
    as a problem would put every run into a retry it cannot win.

    Only PIN grants repair permission. Missing, invalid, matching and
    differing REMEMBER reports all leave ownership with the device;
    their separate result classifications must not select writes.
    """
    lost = [path for path in result.unobserved
            if policy_for(path, device, overrides) == PIN
            and register_promptly_reported(path, device)]
    insisted = [path for path in result.mismatched
                if policy_for(path, device, overrides) == PIN]
    return sorted(insisted + lost)


def _kept_by_the_device(result: VerifyResult,
                        device: Optional[Device] = None,
                        overrides: Optional[Mapping[Tuple[str, str], Policy]] = None
                        ) -> List[str]:
    """Mismatches this session is deliberately letting the device keep."""
    return sorted(path for path in result.mismatched
                  if path not in result.invalid and policy_for(path, device, overrides) != PIN)


def reconcile_now(config: Config, reason: str, backend: Control,
                  should_stop: StopCheck = never_stop) -> bool:
    """Re-establish PIN while preserving every declared REMEMBER path.

    Read first to check link dependencies and classify results, not to
    decide REMEMBER ownership. An unavailable receiver refuses this
    operation; contradictions and unsafe indirect writes raise WriteFailed.
    Returns True after the selected plan completes, including an empty one.
    Triggers are enumerated and never a timer.
    """
    device = device_for_name(config.device_name)
    result = verify_routing(expected_registers(config), backend, VERIFY_TIMEOUT,
                            should_stop=should_stop, device_model=device)
    if should_stop():
        return False

    kept = remembered_paths(config)
    drifted = _report(result, config, device, 1)
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
    apply_routing(config, backend,
                  intent=ApplyIntent.RECONCILE, confirmed=result.confirmed,
                  require_link_confirmation=[entry.path for entry in desired(config)
                                             if entry.phase == PHASE_LINK
                                             and entry.path in result.mismatched],
                  should_stop=should_stop)
    return True


def verify_and_repair(config: Config, backend: Control,
                      should_stop: StopCheck = never_stop) -> bool:
    """Read the applied routing back and re-send once on problems.

    A register is a *problem* when the device reported a different value
    (mismatched) or when a promptly-reported register never appeared
    (probably lost). Registers the dump is known not to report in time
    are logged as information, never as a warning -- but if one of them
    does appear, it is compared like any other, so a future oscmix that
    dumps more (or different) registers is handled without code changes.

    Value mismatches are advisory and permit at most one PIN repair.
    Transport/ownership failures propagate and stop this operation instead
    of being treated as missing values; its owner decides the lifecycle result.

    The completed read-back also supplies the link-sync decision. No
    callback writes inside a partially decoded delivery. Wrong or invalid
    links block the mix; a repair requires fresh confirmation before its
    dependent writes. Receive failures propagate without a blind reapply.
    """
    device = device_for_name(config.device_name)
    registers = expected_registers(config)
    links = {entry.path for entry in desired(config) if entry.phase == PHASE_LINK}

    problems: List[str] = []
    for attempt in (1, 2):
        if should_stop():
            return False
        result = verify_routing(registers, backend,
                                VERIFY_TIMEOUT, should_stop=should_stop,
                                device_model=device)
        if should_stop():
            return False
        wrong_links = links.intersection(result.mismatched)
        problems = _report(result, config, device, attempt)
        if wrong_links:
            log.warning("link state contradicted (%s); mix reapply withheld",
                        ", ".join(sorted(wrong_links)))
        if attempt == 1 and problems:
            # Write 3 of 3, and the only full re-apply. Both phases of it
            # would run against a terminating backend.
            if should_stop():
                return False
            log.warning("%d register(s) unconfirmed (%s); re-sending routing",
                        len(problems), ", ".join(problems))
            # Everything except what the device is allowed to keep. A
            # re-apply is a whole-routing write, so without this a single
            # lost link register drags every remembered fader back to the
            # config value -- the policy would be real in the log and
            # absent at the device.
            apply_routing(config, backend,
                          intent=ApplyIntent.REPAIR, confirmed=result.confirmed,
                          require_link_confirmation=sorted(wrong_links),
                          should_stop=should_stop)
            if wait_unless_stopped(VERIFY_SETTLE, should_stop, backend):
                return False
            continue
        if not wrong_links and links:
            missing = {path for path in links.intersection(result.unobserved)
                       if register_ever_reported(path, device)}
            if missing:
                log.warning("dump never reported %s; re-applying mix without confirmation",
                            ", ".join(sorted(missing)))
            send_mix(config, backend, confirmed=result.confirmed)
        if not problems:
            return not wrong_links
    log.warning("unconfirmed after retry: %s", ", ".join(problems))
    return False
