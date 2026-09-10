"""Desired state, observed state, and the plan between them.

What this project *is*, is a reconciler: desired state from the config,
observed state from the device dump, the difference applied. Before
0.4.0 the code was four partly overlapping paths over the same data --
``apply_routing``, ``send_mix``, ``blind_reapply_mix`` and
``verify_and_repair`` -- each with its own idea of what to write and
when.

Stated as ``desired(config)``, ``observed(reports)`` and
``plan(desired, observed)``, apply and verify are one path, and two
features fell out of it instead of becoming paths five and six:
``--dump-config`` is ``observed()`` rendered as config, and ``--diff`` is
``plan()`` printed instead of sent.

This module is deliberately **pure**. It opens no socket, reads no
clock and decides nothing about timing; it answers what should be
written, in what order, and why. That is what makes it testable against
the recordings rather than against a device.

The register model is what makes it pay for itself: a plan is a set of
registers, and whether an entry is compared, skipped or rewritten
unconditionally is a property of its row in that table
(``registers.verify_class``) rather than a branch in the routing code.

**Everything that writes routing goes through here.** ``apply_routing``
and the dry run have since 0.4.0, ``--diff`` and ``--dump-config`` read
it, and since 0.6.2 the verifier's mix re-apply (``send_mix``) does too
-- the last path that still built its own datagrams. What it replaced
was a route-by-route walk (``routing_plan``, now the oracle in
``tests/oracle.py``), and
the one difference on the wire is pinned by ``tests/test_reconcile.py``
rather than argued: ``plan()`` against an empty observation is that
walk's datagram sequence, ordering included, *minus repeats*. A register
two routes share -- ``/output/5/stereo`` for two routes feeding the same
pair, ``/playback/1/stereo`` for three routes fed from the same source --
goes out once instead of two or three times. A state holds each register
once.

Every dropped repeat must carry the value already in the plan, and a
second test holds that: two routes sharing an output pair must agree on
its link state (``_check_link_agreement`` rejects configs where they do
not), and ``/playback/N/stereo`` is always 1.

This module used to carry the note that nothing in the runtime wrote
through it yet. It was true for one release and then not, and it stayed
for three more -- an outside review found it. A claim about what the
rest of the code does belongs next to a test that would fail when it
stops being true, or it belongs nowhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import ChannelSetting, Config, GlobalSetting, Route
from .constants import LEVEL_MIN, UNLINKED_GAIN_OFFSET
from .registers import (
    BOOL,
    ENABLE_OPTION,
    ENUM,
    PIN,
    REESTABLISHED,
    VERIFIABLE,
    Device,
    Register,
    device_for_name,
    global_families,
    nested_families,
    option_register,
    register_at,
    register_policy,
    settable_globals,
    settable_nested,
    settable_option_rows,
    verify_class,
)

Args = Tuple[object, ...]
Message = Tuple[str, str, Args]


def link_messages(route: Route) -> List[Tuple[str, str, Tuple[object, ...]]]:
    """The channel-pair link state a route needs before its mix is written.

    These must reach the device -- and be reported back to oscmix -- before
    any ``/mix`` message of the same route, see ``LINK_ECHO_TIMEOUT``.

    ``stereo = false`` states the link explicitly rather than assuming it:
    the hard-panned pair of ``/mix`` messages it produces is only correct
    against an *unlinked* pair. Applied to a linked one, both messages
    address the same pair register and the second overwrites the first,
    which leaves one half of the pair completely silent.
    """
    if len(route.output) != 2:
        return []
    kind, source = route.source
    return [
        ("/%s/%d/stereo" % (kind, source[0]), "i", (1,)),
        ("/output/%d/stereo" % route.output[0], "i",
         (1 if route.stereo else 0,)),
    ]


def mix_messages(route: Route) -> List[Tuple[str, str, Tuple[object, ...]]]:
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
    messages: List[Tuple[str, str, Tuple[object, ...]]] = []
    kind, source = route.source
    if len(route.output) == 2:
        left, right = route.output
        pb_left = source[0]
        if route.stereo:
            messages.append(("/mix/%d/%s/%d" % (left, kind, pb_left), "fi",
                             (route.level, 0)))
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
            # check. Flagged in the roadmap rather than assumed silently.
            unlinked = min(route.level, 0.0) + UNLINKED_GAIN_OFFSET
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


#: The two phases of an apply. The link barrier sits between them: every
#: link of every route goes out, the device reports back, and only then
#: is the mix matrix written. Phase is a property of the register, not of
#: the route it came from -- which is exactly what walking route by route
#: got wrong.
PHASE_LINK = 0
PHASE_MIX = 1
#: Channel state -- `[input:N]` / `[output:N]`. After the mix because it
#: does not depend on the link barrier, and because a fader or a
#: reference level landing before the routing exists would be audible
#: for the width of the barrier.
PHASE_CHANNEL = 2

#: Why a write is in the plan.
MISSING = "missing"            # observed nothing for it
MISMATCHED = "mismatched"      # observed a different value
REWRITE = "re-established"     # unverifiable and link-dependent; always written
UNCONDITIONAL = "unconditional"  # nothing was observed at all (a blind apply)


@dataclass(frozen=True)
class Entry:
    """One register the config asks for."""

    path: str
    tags: str
    args: Args
    phase: int


@dataclass(frozen=True)
class Write:
    """One register the plan says to write, and why."""

    path: str
    tags: str
    args: Args
    phase: int
    reason: str

    def message(self) -> Tuple[str, str, Args]:
        """The shape the OSC encoder and the dry run both consume."""
        return self.path, self.tags, self.args


@dataclass(frozen=True)
class Plan:
    """What to write, plus what did not need writing and what cannot be told."""

    writes: Tuple[Write, ...]
    confirmed: Tuple[str, ...]
    unverifiable: Tuple[str, ...]

    def links(self) -> Tuple[Write, ...]:
        return tuple(w for w in self.writes if w.phase == PHASE_LINK)

    def mix(self) -> Tuple[Write, ...]:
        return tuple(w for w in self.writes if w.phase == PHASE_MIX)

    def channel(self) -> Tuple[Write, ...]:
        return tuple(w for w in self.writes if w.phase == PHASE_CHANNEL)

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
    entries: Dict[str, Entry] = {}
    for phase, produce in ((PHASE_LINK, link_messages), (PHASE_MIX, mix_messages)):
        for route in config.routes:
            for path, tags, args in produce(route):
                entries[path] = Entry(path, tags, tuple(args), phase)
    ordered = sorted(entries.values(), key=_send_order(config))
    return tuple(ordered) + channel_entries(config) + global_entries(config)


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


def _enum_value(register: "Register", value: object) -> int:
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


def _encode(path: str, register: "Register", value: object) -> Entry:
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
    if register.tags.startswith("f"):
        return Entry(path, "f", (float(value),),  # type: ignore[arg-type]
                     PHASE_CHANNEL)
    return Entry(path, "i", (int(value),), PHASE_CHANNEL)  # type: ignore[call-overload]


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


def observed(reports: Mapping[str, Sequence[object]]) -> Dict[str, Args]:
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
         tolerance: float = 0.5) -> Plan:
    """What to write to get from ``seen`` to ``entries``.

    ``seen=None`` means *nothing was observed* -- the dump could not be
    read, or this is a first apply. Then every entry is written, which
    is exactly what a first apply does, and why the equivalence test
    can compare the two.

    ``device=None`` means the register model has no opinion, and every
    entry is treated as comparable. That keeps an unmodelled interface
    behaving as it always did.

    Floats compare with a tolerance because the device quantizes levels;
    0.5 dB is the value the read-back has used since 0.1.2.
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
        if klass == REESTABLISHED:
            # Unverifiable *and* dependent on link state: rewritten from
            # a known-good state rather than compared. Comparing it would
            # mean trusting a value the dump never carries.
            writes.append(Write(entry.path, entry.tags, entry.args,
                                entry.phase, REWRITE))
            unverifiable.append(entry.path)
            continue
        if entry.path not in observations:
            writes.append(Write(entry.path, entry.tags, entry.args,
                                entry.phase, MISSING))
            continue
        if matches(entry.tags, entry.args, observations[entry.path], tolerance):
            confirmed.append(entry.path)
        else:
            writes.append(Write(entry.path, entry.tags, entry.args,
                                entry.phase, MISMATCHED))

    writes.sort(key=lambda w: w.phase)
    return Plan(tuple(writes), tuple(confirmed), tuple(unverifiable))


def _both_muted(wanted: float, reported: float) -> bool:
    """Whether both values mean "no signal", written differently.

    Kept separate so the rule is one place and the citation above is
    not repeated: at or below ``LEVEL_MIN`` upstream stores zero, and
    zero is reported as -inf.
    """
    return wanted <= LEVEL_MIN and reported == float("-inf")


def matches(tags: str, want: Args, got: Args,
            tolerance: float = 0.5) -> bool:
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
    if len(got) < len(want):
        return False
    for tag, wanted, reported in zip(tags, want, got):
        try:
            if tag == "f":
                if _both_muted(float(wanted), float(reported)):  # type: ignore[arg-type]
                    continue
                if abs(float(wanted) - float(reported)) > tolerance:  # type: ignore[arg-type]
                    return False
            elif int(wanted) != int(reported):  # type: ignore[call-overload]
                return False
        except (TypeError, ValueError):
            return False
    return True


# --------------------------------------------------------------------------
# observed() rendered as config -- `--dump-config`.
#
# The inverse of `mix_messages`, and only as complete as the device is
# willing to report. `/mix/<out>/input/<in>` comes back; the playback
# matrix does not (ADR 0002), so a dump reproduces monitoring paths and
# cannot reproduce software routing. Saying that loudly is the whole
# difference between a useful tool and one that silently loses half a
# config.
# --------------------------------------------------------------------------

def _linked(seen: Mapping[str, Args], family: str, channel: int) -> bool:
    """Whether a pair is stereo-linked, as the device reported it."""
    args = seen.get("/%s/%d/stereo" % (family, channel - (channel - 1) % 2))
    return bool(args and args[0])


def _mix_entries(seen: Mapping[str, Args]) -> Dict[Tuple[int, int], Args]:
    """Every reported input-matrix cell that is not muted."""
    entries: Dict[Tuple[int, int], Args] = {}
    for path, args in seen.items():
        parts = path.split("/")
        if len(parts) != 5 or parts[1] != "mix" or parts[3] != "input":
            continue
        if not args or not isinstance(args[0], float):
            continue
        if args[0] == float("-inf") or args[0] <= LEVEL_MIN:
            continue
        try:
            entries[(int(parts[2]), int(parts[4]))] = args
        except ValueError:
            continue
    return entries


def channels_from_observed(seen: Mapping[str, Args],
                           device: Optional[Device] = None
                           ) -> Tuple[ChannelSetting, ...]:
    """Channel state read back out of a dump, as config would express it.

    Only options a config can actually set: the register model's
    ``settable_options``. Anything else the device reports is state this
    project has no vocabulary for, and inventing one in the dump writer
    is how a config grows options nothing can parse.

    Written because ``render_config`` could format channel sections and
    nothing produced any -- the renderer was reachable only from tests.
    That is the same shape as the two defects 0.3.0 already
    fixed: a capability built, correct, and wired to nothing.
    """
    if device is None:
        return ()
    found: List[ChannelSetting] = []
    for family in ("input", "output"):
        # Rows, not names: one option can have several registers when
        # the device's limits differ per channel, and a dict keyed by
        # name keeps only the last -- which dropped /input/1-2/gain out
        # of every dump, because the surviving row covered 3-4.
        wanted = list(settable_option_rows(device, family))
        for sub in nested_families(device, family):
            for option, register in settable_nested(device, sub, family).items():
                key = sub if option == ENABLE_OPTION else "%s/%s" % (sub, option)
                wanted.append((key, register))
        for option, register in sorted(wanted, key=lambda row: row[0]):
            for channel in device.channels.get(register.channels, ()):
                args = seen.get(register.path(ch=channel))
                if args:
                    found.append(ChannelSetting(family, channel, option,
                                                _config_value(register, args)))
    return tuple(found)


def globals_from_observed(seen: Mapping[str, Args],
                          device: Optional[Device] = None
                          ) -> Tuple[GlobalSetting, ...]:
    """Channel-less settings read back out of a dump.

    The counterpart to `channels_from_observed` for the families with no
    channel dimension. Without it a dump reads as though the device had
    no echo, reverb, control room, clock or hardware settings at all --
    42 registers the device reports and the file does not mention.
    """
    if device is None:
        return ()
    found: List[GlobalSetting] = []
    for family in global_families(device):
        for option, register in sorted(settable_globals(device, family).items()):
            args = seen.get(register.template)
            if args:
                found.append(GlobalSetting(family, option,
                                           _config_value(register, args)))
    return tuple(found)


def _unnameable(register: "Register", value: object) -> bool:
    """Whether an enum value is one this backend cannot put a name to.

    Written for `/controlroom/mainout`, which reported -1 for "no main
    out" with no name attached: as `mainout = -1` it produced a file
    that would not load -- a dump of a working device that refuses to be
    a config. The round trip caught it, which is what the round trip is
    for.

    That case is gone: the pin moved to 55802a6 and e8151cd gave enums a
    value list, so -1 now arrives as "None". This stays because the
    situation is not specific to that register -- any enum reporting a
    value the declared names do not cover would produce the same
    unloadable file -- and because the alternative is finding out again
    on somebody's desk. `tests/test_dump_sections.py` constructs the
    case rather than relying on a device to produce it.
    """
    return register.domain == ENUM and value not in register.choices


def _config_value(register: "Register", args: Args) -> object:
    """One reported register as the value a config would carry.

    Enums report ``(index, name)`` and a config writes the name; booleans
    report an int and a config writes true/false. Getting this wrong is
    silent -- the dump looks fine and the file it produces sets something
    else -- so the round trip is asserted in tests/test_pin_remember.py.
    """
    if register.domain == ENUM:
        return args[1] if len(args) > 1 else args[0]
    if register.domain == BOOL:
        return bool(args[0])
    return args[0]


def routes_from_observed(seen: Mapping[str, Args]) -> Tuple[Route, ...]:
    """Reconstruct the routes a device's reported state implies.

    Deterministic in name and order, because the round trip has to be a
    fixed point: dumping, applying and dumping again must produce the
    same file, and a name derived from anything but the channels would
    not survive that.

    Only what the dump carries. See ``unrecoverable`` for the rest.
    """
    entries = _mix_entries(seen)
    routes = []
    claimed = set()
    for (out, src) in sorted(entries):
        if (out, src) in claimed:
            continue
        cell = entries[(out, src)]
        # Args is a tuple of `object`; the filter in _mix_entries already
        # established that the first is a float, and the pan is written
        # as an int by everything that produces these registers.
        level = float(cell[0])          # type: ignore[arg-type]
        pan = int(cell[1]) if len(cell) > 1 else 0   # type: ignore[call-overload]
        out_linked = _linked(seen, "output", out)

        if out_linked and out % 2 == 1:
            # A linked pair folds onto its odd channel, and the register
            # is the pair's. pan 0 is a plain stereo pass-through.
            routes.append(Route(name="in%d-%d-out%d-%d" % (src, src + 1, out, out + 1),
                                input=(src, src + 1), output=(out, out + 1),
                                level=round(level, 1)))
            claimed.add((out, src))
        elif not out_linked and pan in (-100, 100) and (out + 1, src) in entries:
            # The hard-panned pair an unlinked route writes. oscmix
            # halved the gain on the way in, so the 6 dB compensation
            # comes back off to recover the `level` the config asked for.
            routes.append(Route(name="in%d-%d-out%d-%d-split" % (src, src + 1, out, out + 1),
                                input=(src, src + 1), output=(out, out + 1),
                                level=round(level - UNLINKED_GAIN_OFFSET, 1),
                                stereo=False))
            claimed.update({(out, src), (out + 1, src)})
        elif pan == 0 and not out_linked:
            routes.append(Route(name="in%d-out%d" % (src, out),
                                input=(src,), output=(out,),
                                level=round(level, 1)))
            claimed.add((out, src))
    return tuple(routes)


def unrecoverable(device: Optional[Device] = None) -> Tuple[str, ...]:
    """Register families a dump cannot reproduce, and why it matters.

    Read from the register model rather than listed here, so a family
    that becomes reportable stops being an excuse the moment the
    recording says so.
    """
    if device is None:
        return ()
    return tuple(r.template for r in device.registers
                 if r.verify == REESTABLISHED)


def render_config(config: Config, device: Optional[Device] = None) -> str:
    """A ``routing.conf`` that reproduces what the device reported.

    What a dump does with an observed value is the pin/remember question
    in its other form, and the register table now answers it: **pinned
    options are emitted as config, remembered ones as comments.**

    A dump cannot tell "I meant this" from "this is where I left it".
    For a reference level or a hi-Z switch the distinction barely
    matters -- both readings say the cable needs it. For a fader it is
    the whole difference between a useful config and one that forces
    every hand-set level back to wherever it happened to be the day the
    dump was taken. So a remembered value is written out commented, with
    the value visible: uncommenting it is a decision the person makes,
    which is exactly the decision a dump cannot make for them.
    """
    missing = unrecoverable(device)
    lines = [
        "# Generated by oscmix-session --dump-config.",
        "#",
        "# This is what the device reported, not everything it is doing.",
    ]
    if missing:
        lines += [
            "#",
            "# NOT IN HERE, because the device does not report it:",
        ]
        lines += ["#   %s" % t for t in missing]
        lines += [
            "#",
            "# A `/mix` write to the playback matrix draws no reply and the",
            "# state dump omits it, so software routing cannot be read back",
            "# -- only re-established from a config. If you had playback",
            "# routes, they are not below and this file will not restore",
            "# them. Merge, do not replace.",
        ]
    lines += [
        "#",
        "# Values are emitted as config where the register model pins them,",
        "# and commented out where it remembers them -- a dump cannot tell",
        "# 'I meant this' from 'this is where I left it', so for anything a",
        "# person turns during a session it does not decide. Uncomment to",
        "# make it a pin. See",
        "# docs/decisions/0012-pin-and-remember.md.",
        "",
        "[device]",
        "name = %s" % config.device_name,
        "usb-id = %s" % config.usb_id,
        "",
        "[osc]",
        "port = %d" % config.osc_port,
        "",
    ]
    if not config.routes:
        lines += ["# No input routing was reported. That is a device with no",
                  "# direct monitoring set up, not an error."]
    for route in config.routes:
        kind, source = route.source
        lines += [
            "[route:%s]" % route.name,
            "%s = %s" % (kind, "/".join(map(str, source))),
            "output = %s" % "/".join(map(str, route.output)),
            "level = %.1f" % route.level,
        ]
        if not route.stereo:
            lines.append("stereo = false")
        lines.append("")
    lines += _global_sections(config, device)
    lines += _channel_sections(config, device)
    return "\n".join(lines).rstrip() + "\n"


def _channel_sections(config: Config,
                      device: Optional[Device]) -> List[str]:
    """``[input:N]`` / ``[output:N]`` blocks for the observed channel state.

    Pinned options become config lines, remembered ones become comments
    carrying the same value. A section whose every option is remembered
    is still emitted: seeing what the device holds is most of why anyone
    runs a dump, and hiding it would make the file look like the channel
    had no state at all.
    """
    # Grouped by the section a setting belongs in, not by channel: a
    # nested option goes to `[eq:input:3]` and a flat one to `[input:3]`
    # (ADR 0014), so one channel produces several sections and they must
    # not be run together.
    by_section: Dict[Tuple[str, str, int], List[ChannelSetting]] = {}
    for setting in config.channels:
        sub = setting.option.split("/", 1)[0] if "/" in setting.option else ""
        if not sub and setting.option in nested_families(device,
                                                         setting.family):
            sub = setting.option          # the sub-family's own switch
        by_section.setdefault((sub, setting.family, setting.channel),
                              []).append(setting)
    lines: List[str] = []
    for (sub, family, channel), settings in sorted(by_section.items()):
        header = ("[%s:%s:%d]" % (sub, family, channel) if sub
                  else "[%s:%d]" % (family, channel))
        lines.append(header)
        for setting in sorted(settings, key=lambda s: s.option):
            path = "/%s/%d/%s" % (family, channel, setting.option)
            name = (ENABLE_OPTION if setting.option == sub
                    else setting.option.split("/", 1)[-1])
            lines.append(_setting_line(name, setting.value, path, device))
        lines.append("")
    return lines


def _global_sections(config: Config, device: Optional[Device]) -> List[str]:
    """`[echo]` and the other channel-less families."""
    by_family: Dict[str, List[GlobalSetting]] = {}
    for setting in config.globals:
        by_family.setdefault(setting.family, []).append(setting)
    lines: List[str] = []
    for family, settings in sorted(by_family.items()):
        lines.append("[%s]" % family)
        for setting in sorted(settings, key=lambda s: s.option):
            lines.append(_setting_line(setting.option, setting.value,
                                       setting.path, device))
        lines.append("")
    return lines


def _setting_line(name: str, value: object, path: str,
                  device: Optional[Device]) -> str:
    """One config line, live or commented, with the reason for commenting."""
    register = register_at(device, path)
    entry = "%s = %s" % (name, _render_value(value, register))
    if register is not None and _unnameable(register, value):
        return ("# %s   # this backend reports no name for it; see "
                "michaelforney/oscmix#30" % entry)
    if policy_for(path, device) == PIN:
        return entry
    return "# %s   # remembered: the device's value wins" % entry


def _render_value(value: object, register: "Optional[Register]" = None) -> str:
    """A value as the config file spells it.

    Keyed off the declared domain rather than the Python type, for the
    same reason `_encode` is: a bool arrives from the device as `True`
    and from the parser as `1` (the wire form), and a renderer that
    asked `isinstance` wrote `true` the first time and `1` the second.
    Both parse, so nothing failed -- the dump of a dump just quietly
    stopped matching the dump. The fixed-point test caught it.
    """
    if register is not None and register.domain == BOOL:
        return "true" if value else "false"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "%.1f" % value
    return str(value)


def policy_for(path: str, device: Optional[Device] = None,
               overrides: Optional[Mapping[Tuple[str, str], str]] = None
               ) -> str:
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
