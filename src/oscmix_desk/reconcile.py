"""Desired state, observed state, and ordered write plans.

This module is pure: no transport, clock or device access. Apply, repair,
preview, diff and config export use the same register declarations.
Verification class and policy decide which values are compared or restored.

Each plan contains a register once. Shared routes must agree on its value;
link requirements precede matrix addressing, including explicit unlinking
for mono routes. Tests compare complete message sequences with the oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Callable, Collection, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .autogain import dependency_paths, flag_value, retained_gains, validate_autogain
from .base import base_entries
from .constants import LEVEL_MIN, UNLINKED_GAIN_OFFSET
from .devices import device_for_name
from .model import (
    Config,
    Route,
    SettingValue,
    validate_route_pan,
)
from .numeric import equal_float, finite, integer, number_value, report_value
from .osc import (
    Args,
    Message,
    Value,
)
from .playback import declared_mutes, mute_path, retained_pairs, validate_playback_mute
from .registers import (
    BOOL,
    ENUM,
    NUMBER,
    PIN,
    REESTABLISHED,
    VERIFIABLE,
    WRITE_ONLY,
    Device,
    Policy,
    Register,
    option_channels,
    option_register,
    register_at,
    register_policy,
    settable_globals,
    verify_class,
)


def link_messages(route: Route) -> List[Message]:
    """The channel-pair link state a route needs before its mix is written.

    These must reach the device -- and be reported back to oscmix -- before
    any ``/mix`` message of the same route, see ``LINK_ECHO_TIMEOUT``.

    ``stereo = false`` states the link explicitly rather than assuming it:
    the hard-panned pair of ``/mix`` messages it produces is only correct
    against an *unlinked* pair. Applied to a linked one, both messages
    address the same pair register and the second overwrites the first,
    which leaves one half of the pair completely silent.
    """
    return [("/%s/%d/stereo" % (kind, channel), "i", (int(linked),))
            for kind, channel, linked in route.link_requirements]


def mix_messages(route: Route) -> List[Message]:
    """The mix-matrix and volume writes of a route.

    oscmix folds stereo-linked channels onto the odd (left) channel of a
    pair: a ``/mix`` message addressed to either half of a linked pair
    writes the *same* pair register, and the pan argument acts as the
    pair's balance. Per-channel messages panned hard left/right (the
    TotalMix pattern for unlinked channels) therefore self-overwrite --
    the last message wins and the whole mix ends up panned hard right.
    A pair route instead links the playback and output pairs and writes
    the single pair register with pan 0 (= plain stereo pass-through at
    ``level`` dB).
    """
    pan = route.checked_pan
    messages: List[Message] = []
    kind, source = route.source
    if len(route.output) == 2:
        left, right = route.output
        pb_left = source[0]
        if route.stereo:
            messages.append(("/mix/%d/%s/%d" % (left, kind, pb_left), "fi",
                             (route.level, pan)))
        else:
            # Unlinked outputs: feed each side the matching half of the
            # (linked) source pair via the pair balance. oscmix halves
            # the gain on this path (setlevel(): ll = vol / 2), so the
            # request is raised by 6 dB to make `level` mean the same
            # thing as it does for a linked route -- measured on a UCX II
            # as an exact 6 dB deficit before this compensation.
            #
            # That measurement was taken on a *playback* source. oscmix
            # runs both kinds through the same setlevel(), so the same
            # halving is expected for an input source -- but expected is
            # not measured, and it needs a signal on a hardware input to
            # check. Flagged here rather than assumed silently.
            # Mute must remain the backend's explicit zero. Adding the
            # compensation to -65 dB produces a nonzero hardware gain.
            unlinked = (LEVEL_MIN if route.level <= LEVEL_MIN
                        else min(route.level, 0.0) + UNLINKED_GAIN_OFFSET)
            messages.append(("/mix/%d/%s/%d" % (left, kind, pb_left), "fi",
                             (unlinked, -100)))
            messages.append(("/mix/%d/%s/%d" % (right, kind, pb_left), "fi",
                             (unlinked, 100)))
        if route.volume is not None:
            for out in (left, right):
                messages.append(("/output/%d/volume" % out, "f", (route.volume,)))
    else:
        (out,) = route.output
        (pb,) = source
        messages.append(("/mix/%d/%s/%d" % (out, kind, pb), "fi",
                         (route.level, 0)))
        if route.volume is not None:
            messages.append(("/output/%d/volume" % out, "f", (route.volume,)))
    return messages


class Phase(IntEnum):
    """When a register is written, which is also the order of an apply.

    The link barrier sits between the first two: every link of every
    route goes out, the device reports back, and only then is the mix
    matrix written. Phase is a property of the register, not of the route
    it came from -- which is exactly what walking route by route got
    wrong. Channel state -- `[input:N]` / `[output:N]` -- comes after the
    mix because it does not depend on the barrier, and because a fader or
    a reference level landing before the routing exists would be audible
    for the width of it.

    AutoSet-off precedes these phases and must be freshly confirmed before
    dependent gains. AutoSet-on follows gains. Preview and transport use the
    same phase ordering, independent of channel declaration order.

    An enum since 0.7.0 prevents arbitrary values or swapped arguments.
    """

    PLAYBACK_MUTE_ON = -2
    AUTOSET_OFF = -1
    LINK = 0
    MIX = 1
    CHANNEL = 2
    AUTOSET_ON = 3
    PLAYBACK_MUTE_OFF = 4


PHASE_LINK, PHASE_MIX, PHASE_CHANNEL = Phase.LINK, Phase.MIX, Phase.CHANNEL


class ApplyIntent(Enum):
    """Only a new session or an explicit desk selection owns starting values."""

    INITIAL = "initial-session"
    EXPLICIT = "explicit-desk"
    REPAIR = "repair-after-apply"
    RECONCILE = "selective-reconcile"

    @property
    def preserves_remember(self) -> bool:
        return self in (ApplyIntent.REPAIR, ApplyIntent.RECONCILE)


class WriteReason(str, Enum):
    """Why a write is in the plan. Printed by its ``.value``: what
    ``str()`` and ``format()`` make of a string enum differs between the
    Python versions this runs on."""

    MISSING = "missing"              # observed nothing for it
    MISMATCHED = "mismatched"        # observed a different value
    REWRITE = "re-established"       # unverifiable, link-dependent: always
    UNCONDITIONAL = "unconditional"  # nothing was observed (a blind apply)
    PREREQUISITE = "prerequisite"    # visible temporary protection before release


MISSING, MISMATCHED = WriteReason.MISSING, WriteReason.MISMATCHED
REWRITE, UNCONDITIONAL = WriteReason.REWRITE, WriteReason.UNCONDITIONAL


@dataclass(frozen=True)
class Entry:
    """One register the config asks for."""

    path: str
    tags: str
    args: Args
    phase: Phase
    #: Written by `[desk] base` (its silent cells and routes): REMEMBERed after an explicit
    #: apply.
    base: bool = False
    #: False for a register that may fold onto its linked partner, which the device then
    #: never reports: written like any other, compared only when a dump reports it, and not
    #: waited for by a read-back.
    compared: bool = True


@dataclass(frozen=True)
class Write:
    """One register the plan says to write, and why."""

    path: str
    tags: str
    args: Args
    phase: Phase
    reason: WriteReason

    def message(self) -> Tuple[str, str, Args]:
        """The shape the OSC encoder and the dry run both consume."""
        return self.path, self.tags, self.args


@dataclass(frozen=True)
class Plan:
    """What to write, plus what did not need writing and what cannot be told."""

    writes: Tuple[Write, ...]
    confirmed: Tuple[str, ...]
    unverifiable: Tuple[str, ...]
    # Includes omitted indirect PIN paths, without claiming confirmation.
    retained: Tuple[str, ...] = ()

    def links(self) -> Tuple[Write, ...]:
        return tuple(w for w in self.writes if w.phase == PHASE_LINK)

    def mix(self) -> Tuple[Write, ...]:
        return tuple(w for w in self.writes if w.phase == PHASE_MIX)

    def channel(self) -> Tuple[Write, ...]:
        return tuple(w for w in self.writes
                     if w.phase not in (PHASE_LINK, PHASE_MIX))

    def messages(self) -> Tuple[Tuple[str, str, Args], ...]:
        """Every write in send order: all links, the barrier, then all mix."""
        return tuple(w.message() for w in self.writes)


def desired(config: Config) -> Tuple[Entry, ...]:
    """The register state a config asks the device to be in.

    Ordered the way it is sent, which is per *routing* and not per
    route: every link of every route, then every mix write. Walking
    route by route and emitting link-then-mix for each is the reading
    that silenced every even output, and it is why phase lives on the
    entry rather than being recovered from the path later.

    A later route targeting the same register wins, matching the
    file-order rule the apply already follows -- but the *position* of
    the register is the first one that claimed it, so a duplicate does
    not reorder the plan.
    """
    validate_route_pan(config.routes)
    validate_autogain(config)
    validate_playback_mute(config)
    device = device_for_name(config.device_name)
    if device is not None:
        for route in config.routes:
            if route.checked_pan:
                for family, channels in (route.source, ("output", route.output)):
                    if any(ch not in device.channels_for(family) for ch in channels):
                        raise ValueError("route %r pan: %s channels do not exist on %s"
                                         % (route.name, family, device.name))
    entries: Dict[str, Entry] = {}
    for phase, produce in ((PHASE_LINK, link_messages), (PHASE_MIX, mix_messages)):
        for route in config.routes:
            for path, tags, args in produce(route):
                # A route of the desk wins over one of the base on a shared register.
                if not (route.base and path in entries and not entries[path].base):
                    entries[path] = Entry(path, tags, tuple(args), phase, base=route.base)
    ordered = sorted(entries.values(), key=_send_order(config))
    if device is not None:
        ordered += [Entry(path, tags, args, PHASE_MIX, base=True, compared=compared)
                    for path, tags, args, compared in base_entries(config.base, config.routes,
                                                                   set(entries), device)]
    combined = tuple(ordered) + channel_entries(config) + global_entries(config)
    return tuple(sorted(combined, key=lambda entry: entry.phase))


def remembered_paths(config: Config,
                     observed: Optional[Mapping[str, Tuple[str, Args]]] = None,
                     *, linked: Sequence[str] = ()) -> Tuple[str, ...]:
    """Declared REMEMBER and gains owned by AutoSet, including undeclared partners."""
    device = device_for_name(config.device_name)
    kept = {entry.path for entry in desired(config)
            if entry.base or policy_for(entry.path, device, config.policies) != PIN}
    kept.update(retained_gains(config, observed or {}, linked=linked))
    return tuple(sorted(kept))


def application_plan(config: Config, intent: ApplyIntent,
                     leave_alone: Sequence[str] = (),
                     confirmed: Sequence[str] = (), *,
                     observed: Optional[Mapping[str, Tuple[str, Args]]] = None) -> Plan:
    """Select writes without turning preserved state into confirmed state.

    Later operations preserve every REMEMBER path, even when it matched or
    never reported. Do not repeat already confirmed links: re-linking could
    change remembered partner settings. PIN matrix writes remain deliberate.
    """
    skip = set(leave_alone)
    entries = desired(config)
    if intent.preserves_remember:
        skip.update(entry.path for entry in entries
                    if entry.phase == PHASE_LINK and entry.path in confirmed)
        linked = [entry.path for entry in entries
                  if entry.phase == PHASE_LINK and entry.path not in skip
                  and entry.args == (1,)]
        skip.update(remembered_paths(config, observed, linked=linked))
    skip.update(protected_entries(config, skip, confirmed))
    selected = plan([entry for entry in entries if entry.path not in skip])
    return _playback_prerequisites(config, selected, skip, confirmed)


def protected_entries(config: Config, retained: Collection[str],
                      confirmed: Sequence[str] = ()) -> Tuple[str, ...]:
    """Indirect paths an unobservable retained playback mute cannot authorize.

    Retention includes both mute flags, the pair link and its matrix writes.
    An output link can affect these unknown columns too. A freshly matching
    output link is already omitted by repair; an unconfirmed link and every
    route depending on it stay retained, including any PIN disagreement.
    No retained path is promoted to a confirmed one by this projection.
    """
    pairs = retained_pairs(retained)
    if not pairs:
        return ()
    protected = {mute_path(ch) for left in pairs for ch in (left, left + 1)}
    protected.update("/playback/%d/stereo" % ch
                     for left in pairs for ch in (left, left + 1))
    for route in config.routes:
        links = {path for path, _tags, _args in link_messages(route)}
        protected.update(path for path in links
                         if path.startswith("/output/") and path not in confirmed)
    for route in config.routes:
        touched = any(channel in (left, left + 1)
                      for left in pairs for channel in route.playback)
        links = {path for path, _tags, _args in link_messages(route)}
        if touched or links.intersection(protected):
            protected.update(path for path, _tags, _args in mix_messages(route))
    return tuple(sorted(protected))


def _playback_prerequisites(config: Config, selected: Plan,
                            retained: Collection[str], confirmed: Sequence[str]) -> Plan:
    """A complete selected OFF releases only after visible ON/link/mix phases."""
    off = [write for write in selected.writes
           if write.phase == Phase.PLAYBACK_MUTE_OFF]
    paths = {write.path for write in selected.writes}
    prerequisites = []
    mutes = declared_mutes(config)
    for write in off:
        channel = int(write.path.split("/")[2])
        left = channel - (channel - 1) % 2
        required = {mute_path(left), mute_path(left + 1)}
        links: Set[str] = set()
        for route in config.routes:
            if set(route.playback).intersection((left, left + 1)):
                required.update(path for path, _tags, _args in mix_messages(route)
                                if path.startswith("/mix/"))
                links.update(path for path, _tags, _args in link_messages(route))
        if (mutes.get(left) != 0 or mutes.get(left + 1) != 0 or not required <= paths
                or not links <= paths | set(confirmed)):
            raise ValueError(
                "playback %d/%d unmute cannot bypass retained or omitted matrix writes"
                % (left, left + 1))
        prerequisites.append(Write(write.path, "i", (1,), Phase.PLAYBACK_MUTE_ON,
                                   WriteReason.PREREQUISITE))
    writes = tuple(sorted(prerequisites + list(selected.writes), key=lambda write: write.phase))
    return Plan(writes, selected.confirmed, selected.unverifiable,
                tuple(sorted(set(retained))))


def matrix_repair_plan(config: Config, confirmed: Sequence[str] = (), *,
                       observed: Optional[Mapping[str, Tuple[str, Args]]] = None) -> Plan:
    """Matrix repair cannot discard selected mute safety prerequisites.

    A legacy matrix-only operation keeps its existing MIX scope. An owned
    mute selects the same mute/link/mix phases as application planning,
    preserving visible ON and complete matrix replacement before any OFF.
    Unrelated PIN channel/global settings do not enter a matrix repair.
    """
    wanted = application_plan(config, ApplyIntent.REPAIR, confirmed=confirmed,
                              observed=observed)
    phases = {PHASE_MIX}
    mute_phases = {Phase.PLAYBACK_MUTE_ON, Phase.PLAYBACK_MUTE_OFF}
    if any(write.phase in mute_phases for write in wanted.writes):
        phases.update(mute_phases)
        phases.add(PHASE_LINK)
    return Plan(tuple(write for write in wanted.writes if write.phase in phases),
                wanted.confirmed, wanted.unverifiable, wanted.retained)


def retention_problem(config: Config, selected: Sequence[Write], retained: Sequence[str],
                      confirmed: Sequence[str], *,
                      observed: Optional[Mapping[str, Tuple[str, Args]]] = None) -> Optional[str]:
    """Refuse indirect changes to retained settings and unknown retained links.

    Linking may copy a partner's settings. A scalar write may also propagate
    to its linked partner. Without sufficient observation, preserving a path
    means declining the operation that could change it indirectly.
    """
    keep, known = set(retained), set(confirmed)
    writes = {write.path for write in selected}
    blocked = writes.intersection(protected_entries(config, retained, confirmed))
    if blocked:
        return ("writes could change unobservable retained playback mute or its matrix: %s"
                % ", ".join(sorted(blocked)))
    for route in config.routes:
        matrix = {p for p, _t, _a in mix_messages(route) if p.startswith("/mix/")}
        if not writes.intersection(matrix):
            continue
        missing = {p for p, _t, _a in link_messages(route)}.intersection(keep) - known
        if missing:
            return "retained link state is not confirmed for %s" % ", ".join(sorted(missing))
    declarations = {entry.path: entry.args for entry in desired(config)}
    refreshed = set(dependency_paths(config)) if observed is not None else set()
    for write in selected:
        parts = write.path.strip("/").split("/", 2)
        if len(parts) != 3 or parts[0] not in ("input", "output"):
            continue
        family, channel, option = parts
        number = int(channel)
        left = number - (number - 1) % 2
        pair = ("/%s/%d/" % (family, left), "/%s/%d/" % (family, left + 1))
        if option == "stereo":
            affected = sorted(path for path in keep if path.startswith(pair))
            if affected:
                return "link write %s could change retained partner settings: %s" % (
                    write.path, ", ".join(affected))
        else:
            partner = "/%s/%d/%s" % (family, left + (number == left), option)
            link = pair[0] + "stereo"
            mono = (flag_value((observed or {}).get(link)) == 0
                    if link in refreshed or link in (observed or {}) else
                    link in known and declarations.get(link) == (0,))
            joining = any(w.path == link and w.args == (1,) for w in selected)
            if partner in keep and not (mono and not joining):
                return "write %s could change retained stereo partner %s" % (write.path, partner)
    return None


def channel_entries(config: Config) -> Tuple[Entry, ...]:
    """The `[input:N]` / `[output:N]` settings, as registers to write.

    Enums go out as their **index**, not their name. Upstream accepts
    either for `/output/<n>/reflevel` (`setenum`) and only an int for
    `/input/<n>/reflevel` (`setint`) -- an asymmetry that writing names
    would have hit on inputs alone, silently, since an ignored write
    draws no reply.

    The write tags therefore differ from the report tags: a reflevel is
    written ``,i`` and reported ``,is`` with the name appended. The
    comparison only reads as many arguments as were asked for, so the
    extra name does not make it a mismatch.
    """
    device = device_for_name(config.device_name)
    if device is None:
        return ()
    out = []
    for setting in config.channels:
        # `option_register` resolves flat and nested options alike: a
        # nested option's name is the tail of its template, so
        # "eq/band1freq" assembles to "/input/{ch}/eq/band1freq" and the
        # sub-family switch "eq" to the switch register. A separate
        # nested lookup lived here until the write sweep's survivors
        # showed it had become unreachable -- the parser refuses invalid
        # channels and this finds every valid option, so its 17
        # surviving mutants were dead code, not missing tests. Its
        # history matters though: resolving only in `settable_options`
        # once dropped every EQ setting between the config and the wire.
        register = option_register(device, setting.family, setting.option,
                                   setting.channel)
        if register is None:
            if option_channels(device, setting.family, setting.option):
                raise ValueError("channel %s does not have %s on %s" % (
                    setting.channel, setting.option, device.name))
            continue
        path = "/%s/%d/%s" % (setting.family, setting.channel, setting.option)
        out.append(_encode(path, register, setting.value))
    return tuple(out)


def global_entries(config: Config) -> Tuple[Entry, ...]:
    """The `[echo]`-style settings, as registers to write.

    Same encoding rules as `channel_entries`, and deliberately the same
    phase: a family with no channel is still channel state as far as the
    ordering is concerned -- it depends on no link and must not land
    before the routing exists.

    The value encoding is shared with `channel_entries` rather than
    written twice. A second copy of "an enum goes out as its index" is
    how the two come to disagree, and the reflevel asymmetry that rule
    exists for is invisible until a write is silently ignored.
    """
    device = device_for_name(config.device_name)
    if device is None:
        return ()
    out = []
    for setting in config.globals:
        known = settable_globals(device, setting.family)
        register = known.get(setting.option)
        if register is None:
            continue
        out.append(_encode(setting.path, register, setting.value))
    return tuple(out)


def _enum_value(register: "Register", value: SettingValue) -> int:
    """The wire value for an enum name.

    Its position, unless the register declares otherwise. Upstream's
    `setenum` reads a `,i` argument as the raw value rather than as an
    index, so `/controlroom/mainout` -- where "None" sits at position 10
    and means -1 -- would be written as 10 by a positional encoder, and
    10 is not one of its values.
    """
    position = register.choices.index(str(value))
    if register.values:
        return register.values[position]
    return position


def _encode(path: str, register: "Register",
            value: SettingValue) -> Entry:
    """One setting as the entry that writes it.

    The **declared** tag decides the wire type, not the Python one.
    Getting that backwards is silent: upstream's `oscgetint` rejects a
    float argument with "incorrect argument type", `setint` then returns
    without writing, and a write draws no reply to notice it by. A
    config asking for `band1freq = 80` would parse, validate, reach the
    device and change nothing.

    Enums are the one place where the written tag differs from the
    reported one: `,i` with the index going out, `,is` with the name
    coming back.
    """
    if register.domain == ENUM:
        return Entry(path, "i", (_enum_value(register, value),),
                     PHASE_CHANNEL)
    if register.domain == NUMBER:
        number_value(value, register)
    elif register.domain == BOOL:
        report_value(value, register)
    if register.tags.startswith("f"):
        return Entry(path, "f", (float(value),),
                     PHASE_CHANNEL)
    number = integer(value)
    if path.startswith("/playback/") and path.endswith("/mute"):
        phase = Phase.PLAYBACK_MUTE_ON if number else Phase.PLAYBACK_MUTE_OFF
    else:
        phase = (Phase.AUTOSET_ON if number else Phase.AUTOSET_OFF) if path.endswith("/autoset") \
            else PHASE_CHANNEL
    return Entry(path, "i", (number,), phase)


def _send_order(config: Config) -> Callable[[Entry], int]:
    """Sort key restoring the order the messages were produced in."""
    position: Dict[str, int] = {}
    index = 0
    for produce in (link_messages, mix_messages):
        for route in config.routes:
            for path, _tags, _args in produce(route):
                if path not in position:
                    position[path] = index
                    index += 1
    return lambda entry: position[entry.path]


def observed(reports: Mapping[str, Sequence[Value]]) -> Dict[str, Args]:
    """The device's own view, as the dump reported it.

    A plain projection. It is a named step because
    ``--dump-config`` is this rendered as config, and because "what the
    device says" deserves to be a value rather than a dict that happens
    to be lying around inside the verifier.
    """
    return {path: tuple(args) for path, args in reports.items()}


def plan(entries: Sequence[Entry],
         seen: Optional[Mapping[str, Args]] = None,
         device: Optional[Device] = None,
         tolerance: Optional[float] = None) -> Plan:
    """What to write to get from ``seen`` to ``entries``.

    ``seen=None`` means *nothing was observed* -- the dump could not be
    read, or this is a first apply. Then every entry is written, which
    is exactly what a first apply does, and why the equivalence test
    can compare the two.

    ``device=None`` means the register model has no opinion, and every
    entry is treated as comparable. That keeps an unmodelled interface
    behaving as it always did.

    Float comparisons use each register's encoding. The logarithmic mix
    gain keeps its measured tolerance; fixed-point scalars do not share it.
    """
    writes: List[Write] = []
    confirmed: List[str] = []
    unverifiable: List[str] = []
    blind = seen is None
    observations: Mapping[str, Args] = {} if seen is None else seen

    for entry in entries:
        klass = verify_class(device, entry.path) if device else VERIFIABLE
        if blind:
            writes.append(Write(entry.path, entry.tags, entry.args,
                                entry.phase, UNCONDITIONAL))
            continue
        if klass in (REESTABLISHED, WRITE_ONLY) or (not entry.compared
                                                    and entry.path not in observations):
            # Never reported, so rewritten from the declared state rather than compared:
            # a "report" of it could only be an echo the dump never carries.
            writes.append(Write(entry.path, entry.tags, entry.args,
                                entry.phase, REWRITE))
            unverifiable.append(entry.path)
            continue
        if entry.path not in observations:
            writes.append(Write(entry.path, entry.tags, entry.args,
                                entry.phase, MISSING))
            continue
        if matches(entry.tags, entry.args, observations[entry.path], tolerance,
                   register=register_at(device, entry.path)):
            confirmed.append(entry.path)
        else:
            writes.append(Write(entry.path, entry.tags, entry.args,
                                entry.phase, MISMATCHED))

    writes.sort(key=lambda w: w.phase)
    return Plan(tuple(writes), tuple(confirmed), tuple(unverifiable))


def matches(tags: str, want: Args, got: Args,
            tolerance: Optional[float] = None, *,
            register: Optional[Register] = None) -> bool:
    """Whether a reported value satisfies a desired one.

    Extra trailing arguments in the report are ignored, so a richer
    upstream dump format cannot break the comparison.

    **A gain at or below the mute floor reads back as -inf.** Upstream
    stores it as zero and reports zero as negative infinity::

        level.vol = vol <= -65.f ? 0 : powf(10.f, vol / 20.f);   # setmix
        ...vol > 0 ? 20.f * log10f(level.vol) : -INFINITY        # newmix

    So a route written at ``level = -65`` -- which routing.conf
    documents as mute -- comes back as ``-inf``, and a plain difference
    is infinite. That was invisible while the only mix registers were
    the playback ones, which the dump never reports; input routes are
    verifiable, so a muted monitoring path would have been reported
    mismatched on every start and re-sent every time.

    The two are the same value expressed twice, so they compare equal.
    """
    if len(got) < len(want) or len(tags) != len(want) or not tags:
        return False
    for tag, wanted, reported in zip(tags, want, got):
        try:
            if tag == "f":
                if not equal_float(wanted, reported, register, tolerance):
                    return False
            elif tag == "i":
                if register is not None and register.domain == NUMBER:
                    number_value(wanted, register)
                if register is not None and register.domain is not None:
                    report_value(reported, register)
                if register is not None and register.mix_level:
                    if (type(wanted) is not int or type(reported) is not int
                            or not -100 <= reported <= 100):
                        return False
                    # calclevel reports pan=0 for a silent stereo source.
                    # Once the level is digital zero, its balance cannot
                    # differ audibly; malformed pan arguments still fail.
                    if (finite(want[0]) <= LEVEL_MIN and got[0] == float("-inf")
                            and -100 <= integer(wanted) <= 100):
                        continue
                if integer(wanted) != integer(reported):
                    return False
            else:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True



def policy_for(path: str, device: Optional[Device] = None,
               overrides: Optional[Mapping[Tuple[str, str], Policy]] = None
               ) -> Policy:
    """PIN or REMEMBER for a path, config override beating the table.

    Pure, and here rather than in ``registers`` because the override
    comes from a ``Config`` and the register model deliberately knows
    nothing about configs.

    The override is keyed by ``(family, option)`` -- per kind of setting,
    not per channel. That is the granularity the question actually has:
    "should a monitor fader come back after a restart" is one answer for
    the installation, and a per-channel version would be four more lines
    of config for a distinction nobody asked for. If a real case turns
    up, the key grows a channel and old configs keep meaning what they
    meant.
    """
    if overrides:
        parts = path.strip("/").split("/")
        if len(parts) == 3:
            family, _channel, option = parts
            override = overrides.get((family, option))
            if override is not None:
                return override
    return register_policy(device, path)
