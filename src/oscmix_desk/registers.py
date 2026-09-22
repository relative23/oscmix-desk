"""What a register is, and the questions a device's table answers.

The facts about the register surface are data (``devices``), not
knowledge spread over the code that uses them: a row says what a
register is called, which channels have it, what it may hold, whether
the dump reports it and whether the device or the config decides its
value. This module is the shape of such a row and of a device, and the
lookups the parser, the reconciler and the verifier ask.

**Indexed by device from the first line.** Every question takes the
device, and None -- a device nobody modelled -- is a normal argument
with a normal answer: no opinion.

Nothing in this module talks to a device or decides anything. It answers
questions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------
# Verification classes.
#
# The dump splits the surface into three, and naming the class is what
# keeps verification from over-claiming as the surface grows. Before
# 0.3.0 the distinction lived in `register_promptly_reported` and in
# prose, which was fine for six registers and not for sixty.
# --------------------------------------------------------------------------

class VerifyClass(str, Enum):
    """How a register can be checked. A closed set since it was named;
    an enum since 0.7.0, like the two below (first outside review). All
    three compare as the strings they were, and are printed by ``.value``:
    what ``str()`` makes of a string enum differs between Python versions.
    """

    #: Reported by the dump, so a value is confirmed, mismatched or missing.
    VERIFIABLE = "verifiable"
    #: Accepted by the device, never reported back. The verifier must say
    #: *unverifiable*, never *confirmed* -- `/input/*/name`,
    #: `/output/*/name`, `/output/*/loopback`, all confirmed absent from a
    #: full dump.
    WRITE_ONLY = "write-only"
    #: Unverifiable *and* dependent on link state, so it is rewritten from
    #: a known-good state rather than checked. The playback mix matrix is
    #: the only member: a `/mix` write draws no reply and the dump omits it.
    REESTABLISHED = "re-established"


VERIFIABLE, WRITE_ONLY = VerifyClass.VERIFIABLE, VerifyClass.WRITE_ONLY
REESTABLISHED = VerifyClass.REESTABLISHED
VERIFY_CLASSES: Tuple[VerifyClass, ...] = tuple(VerifyClass)


# --------------------------------------------------------------------------
# Who wins after the initial write.
#
# Measured on a UCX II, and the measurement is what shapes this. Of every
# register a config can set, exactly one is *pushed* to listeners when it
# changes: `/output/{ch}/stereo`, which the device echoes over MIDI --
# the echo the two-phase apply already waits for. (`/clock/samplerate` is
# pushed as well, measured later; no config sets it, so it does not
# change the argument below -- but it is the one register a session can
# react to without asking.) `volume`, `mute`,
# `hi-z`, `gain`, `reflevel` and `/playback/{ch}/stereo` all change
# silently; only a `/refresh` reveals them.
#
# So "pin" cannot mean "snaps back when the mixer GUI changes it". There
# is nothing to react to short of polling a 2252-register dump, against
# a device already streaming ~880 meter datagrams a second. What pin can
# honestly mean is: **the config wins for as long as this session is
# still looking** -- through the read-back window, and through any
# future reconcile trigger.
#
# What that replaced was an accident. Before 0.3.0 a declared option
# behaved as pinned for roughly the two seconds the apply and dump took,
# and as remembered after -- measured by turning a fader at 0.5, 1.5, 3
# and 6 seconds after a restart: only the 0.5 s change was overwritten,
# and by the ordinary start-up apply rather than by the verifier. The
# cut-off was the shape of the timing, not anybody's decision.

class Policy(str, Enum):
    """Who wins after the initial write."""

    #: The config wins. A device value that disagrees is a mismatch: the
    #: read-back re-sends it, and so does any later reconcile.
    PIN = "pin"
    #: The device wins after the initial write. The config value is
    #: applied at start and then let go -- a later disagreement is the
    #: user having turned something, which is information, not a fault.
    REMEMBER = "remember"


PIN, REMEMBER = Policy.PIN, Policy.REMEMBER
POLICIES: Tuple[Policy, ...] = tuple(Policy)


class Domain(str, Enum):
    """Value domains, as a config author has to satisfy them."""

    BOOL = "bool"
    ENUM = "enum"
    #: A quantity with a declared range and unit; see ``NUMBER`` below.
    NUMBER = "number"


BOOL, ENUM = Domain.BOOL, Domain.ENUM
#: The capability a register names when it has no channel dimension at
#: all -- `/echo/delay`, `/clock/source`, `/controlroom/dim`. There are
#: 42 of these on a UCX II across five families, and they are the half
#: of 0.4.0 that needs no config-format decision (docs/ROADMAP.md).
#:
#: Not a channel list of length one: a global register has no channel,
#: and giving it a fake one would put `/echo/delay/1` within reach of
#: every loop that expands templates.
GLOBAL = "global"


#: A quantity with a declared range and unit. Replaces the separate
#: GAIN (0..75 dB) and DB (LEVEL_MIN..LEVEL_MAX) domains, which were the
#: same shape with different bounds and a hand-written message each --
#: two ways to say one thing, which is how a validator and a register
#: table come to disagree about what is legal.
#:
#: ``lo``/``hi`` are ``None`` where upstream's node table declares no
#: bound. A range this project invented would reject values the device
#: accepts, and that is worse than accepting one it does not: the first
#: is a config that will not load, the second is an error the device
#: reports.
NUMBER = Domain.NUMBER


@dataclass(frozen=True)
class Register:
    """One register family: where it lives, what it holds, how it verifies.

    ``template`` uses ``{ch}``, or ``{out}`` and ``{pb}``/``{in_}`` for
    the matrix families. ``channels`` names a capability in the device's
    channel map rather than repeating a range, because the same range is
    shared by several registers and a copy is a place to disagree.
    """

    template: str
    tags: str
    verify: VerifyClass
    channels: str
    #: What a config may set it to, or None when this project does not
    #: expose it as a setting. A register with no domain is readable and
    #: writable by the code, not by a `routing.conf`.
    domain: Optional[Domain] = None
    #: For ENUM: the accepted names, exactly as the device reports them.
    #: Taken from upstream's device table, which is where the device's
    #: own vocabulary lives -- inventing synonyms here would mean a
    #: config that reads well and sets nothing.
    choices: Tuple[str, ...] = ()
    #: For ENUM: the wire value of each name, when it is not simply the
    #: name's position. Upstream's `setenum` takes a `,i` argument as the
    #: **raw value**, not as an index -- so a discontinuous enum written
    #: by position writes the wrong register. `/controlroom/mainout` is
    #: the one that has this: "None" sits at position 10 and its value is
    #: -1 (upstream `.enumvals`, added in e8151cd for #30). Empty means
    #: position and value agree, which is true of every other enum here.
    values: Tuple[int, ...] = ()
    #: For NUMBER: the inclusive bounds and the unit, taken from
    #: upstream's node table (``min``/``max``/``scale``) rather than
    #: from what a device happened to report. ``None`` means upstream
    #: declares no bound.
    lo: Optional[float] = None
    hi: Optional[float] = None
    unit: str = ""
    #: Who wins after the initial write, PIN or REMEMBER. The default is
    #: REMEMBER because that is ADR 0003's rule -- do not wipe what the
    #: user left in the mixer -- and a register that forgot to declare a
    #: policy should fall on the side that surprises nobody.
    #:
    #: PIN belongs to registers that describe the *installation* rather
    #: than a preference: a reference level or a hi-Z switch has to match
    #: the cable that is plugged in, and a wrong value there is a real
    #: signal problem rather than a matter of taste. REMEMBER belongs to
    #: everything a person reaches for during a session.
    policy: Policy = REMEMBER
    #: Scalar fixed-point scale in the pinned backend. None means no
    #: fixed-point contract is declared, not a universal dB tolerance.
    scale: Optional[float] = None
    #: Input gain uses float32 multiplication followed by C truncation,
    #: unlike setfixed's lroundf division. Both report tenths of a dB.
    truncates: bool = False
    #: Only mix-level arguments use setmix's logarithmic gain and -inf
    #: mute report. Thresholds and fixed-point output faders do not.
    mix_level: bool = False

    @property
    def per_channel(self) -> bool:
        """Whether one channel number names the register.

        The matrix families take two (``/mix/{out}/input/{in_}``), and a
        caller that assumes one gets a KeyError -- so the distinction is
        answered here rather than rediscovered at each call site.
        """
        return "{ch}" in self.template

    def path(self, **channels: int) -> str:
        return self.template.format(**channels)


@dataclass(frozen=True)
class Device:
    """A Fireface, and what its registers are.

    ``channels`` maps a capability name to the channels that have it.
    ``supported`` states the bar from the roadmap plainly: a device is
    supported when its register table is declared, its channel
    capabilities are recorded, and one hardware evidence artifact exists
    for it. Below that line it is "may work", and saying so in the data
    is better than saying it in a README nobody reads at the right time.
    """

    key: str
    name: str
    usb_id: str
    channels: Mapping[str, Tuple[int, ...]]
    registers: Tuple[Register, ...]
    supported: bool
    #: Families measured to arrive *complete*, for every channel, within
    #: seconds of a cold plug. See ``cold_plug_complete`` -- everything
    #: not listed here may be partially delivered, and 0.3.0 must not
    #: fail a verification over it.
    complete_after_cold_plug: Tuple[str, ...] = ()
    evidence: Optional[str] = None
    #: The USB release (``bcdDevice``) the table, the hardware evidence and
    #: the write sweep were recorded on. A start says so when the
    #: interface reports another one: what was measured was measured on
    #: this firmware, and nothing else is claimed.
    firmware: Optional[str] = None

    def channels_for(self, capability: str) -> Tuple[int, ...]:
        return self.channels.get(capability, ())

    def has(self, capability: str, channel: int) -> bool:
        return channel in self.channels_for(capability)


def register_policy(device: Optional[Device], path: str) -> Policy:
    """PIN or REMEMBER for a concrete path, from the register table.

    REMEMBER for anything the model does not know, matching the field
    default: an unmodelled register is not something this project should
    start insisting on.
    """
    if device is None:
        return REMEMBER
    for register in device.registers:
        if _matches(register.template, path):
            return register.policy
    return REMEMBER


def verify_class(device: Optional[Device],
                 path: str) -> Optional[VerifyClass]:
    """How a concrete OSC path verifies, or None when nothing is known.

    Concrete paths, not templates: the caller has a path off the wire.
    """
    if device is None:
        return None
    for register in device.registers:
        if _matches(register.template, path):
            return register.verify
    return None


def _matches(template: str, path: str) -> bool:
    """Whether a concrete path is an instance of a template."""
    want = template.split("/")
    got = path.split("/")
    if len(want) != len(got):
        return False
    for part_want, part_got in zip(want, got):
        if part_want.startswith("{") and part_want.endswith("}"):
            if not part_got.isdigit():
                return False
        elif part_want != part_got:
            return False
    return True


def _channel_in(template: str, path: str) -> Optional[int]:
    """The channel a concrete path names, or None for a global register.

    The *first* placeholder is the one the capability describes: a matrix
    row is indexed by its output, and its second index is a different
    capability entirely.
    """
    for part_want, part_got in zip(template.split("/"), path.split("/")):
        if part_want.startswith("{") and part_want.endswith("}"):
            return int(part_got)
    return None


def register_at(device: Optional[Device], path: str) -> Optional[Register]:
    """The register a concrete path is an instance of, or None.

    One lookup for the two modules that were each doing their own. The
    reconciler wants it to render a value the way its domain spells it;
    the verifier wants it to decide whether a path is channel state at
    all, and asking `settable_options` for that was the bug this
    replaces -- see `verify._is_channel_state`.
    """
    if device is None:
        return None
    for register in device.registers:
        if not _matches(register.template, path):
            continue
        channel = _channel_in(register.template, path)
        if channel is not None and not device.has(register.channels, channel):
            continue
        return register
    return None


def cold_plug_complete(device: Optional[Device], path: str) -> bool:
    """Whether a cold plug reports this register for *every* channel.

    Measured across a real USB replug: the device delivers 1234 of 1932
    non-meter registers within seconds, and the rest may not arrive for
    minutes -- 276 s of further observation saw nothing more. The stereo
    flags come complete for all 20 channels; `/output/N/mute` came back
    for 1, 2, 3, 8, 9 and 10 but not for 4-7 or 11-20.

    That ragged set is a truncated stream, not a rule, so this answers
    only the question that generalises: *is this family known to arrive
    whole?* Anything else is False, including registers nobody measured
    -- a verifier must not fail a register into a warning because a
    hotplug was still filling the cache.
    """
    if device is None:
        return False
    return any(_matches(template, path)
               for template in device.complete_after_cold_plug)


def declared_paths(device: Device, capability_channels: Optional[
        Dict[str, Sequence[int]]] = None) -> Tuple[str, ...]:
    """Every concrete path the model declares for a device.

    Used by the tests to check the model against a recording. Matrix
    families are skipped: their second index is a different capability
    and enumerating the cross product says nothing the per-family checks
    do not already say.
    """
    paths = []
    for register in device.registers:
        if "{out}" in register.template:
            continue
        if not register.per_channel:
            # No placeholder, so the template *is* the path. Expanding it
            # over a channel list would produce nothing at all, which is
            # how a family can be declared and never checked.
            paths.append(register.template)
            continue
        channels = (capability_channels or {}).get(
            register.channels, device.channels_for(register.channels))
        for channel in channels:
            paths.append(register.path(ch=channel))
    return tuple(paths)


#: What a `[<family>]` section calls the family's own on/off register.
#:
#: `/echo` is a node that carries a value *and* a subtree, so the switch
#: has no path segment of its own and therefore no name in the device's
#: vocabulary. This one is ours. Every other option name is the last
#: segment of a real path, which is why this is the only invented word in
#: the model and why it is written down here rather than in the parser.
ENABLE_OPTION = "enabled"


def settable_globals(device: Optional[Device],
                     family: str) -> Dict[str, Register]:
    """Options a ``[<family>]`` section may set, for a global family.

    Keyed the way a config writes them: the last path segment, or
    ``ENABLE_OPTION`` for the family's own register. Derived from the
    templates rather than listed separately, so a row added to the table
    is settable without touching the parser -- and one removed stops
    being settable without a second edit to forget.
    """
    if device is None:
        return {}
    prefix = "/" + family
    found: Dict[str, Register] = {}
    for register in device.registers:
        if register.channels != GLOBAL or register.domain is None:
            continue
        if register.template == prefix:
            found[ENABLE_OPTION] = register
        elif register.template.startswith(prefix + "/"):
            found[register.template[len(prefix) + 1:]] = register
    return found


def global_families(device: Optional[Device]) -> Tuple[str, ...]:
    """Every family name a `[<family>]` section may use."""
    if device is None:
        return ()
    names = {r.template.strip("/").split("/")[0]
             for r in device.registers if r.channels == GLOBAL}
    return tuple(sorted(names))


def settable_options(device: Optional[Device], family: str) -> Dict[str, Register]:
    """Options a ``[input:N]`` / ``[output:N]`` section may set.

    Derived from the model rather than listed twice: a register is
    settable exactly when it declares a value domain. That is also how
    ``48v`` stays out -- it is modelled, verifiable and readable, and it
    has no domain, so no config can reach it.

    The roadmap's rule for phantom power is why: it may not be settable
    from a text file until a hardware case proves the channel it names
    is the channel it hits. An off-by-one in a silent output is a bug;
    an off-by-one in phantom power is a damaged ribbon microphone.

    Flat options only, and "flat" has two halves. A nested register --
    `/input/{ch}/eq/band1freq` -- would otherwise land here as
    `eq/band1freq`; and a sub-family's own *switch* -- `/input/{ch}/eq` --
    is flat by path shape while belonging to the nested section, so it is
    excluded by having children. Both would be settable from `[input:3]`,
    which is the one shape an installed 0.3.0 refuses the whole file over
    (ADR 0014). Both live in `settable_nested` instead, the switch under
    ``ENABLE_OPTION``.
    """
    return dict(settable_option_rows(device, family))


def settable_option_rows(device: Optional[Device],
                         family: str) -> Tuple[Tuple[str, Register], ...]:
    """The same options, one entry per *row* rather than per name.

    `settable_options` is keyed by option name, which is right for "what
    may a `[input:N]` section say" and wrong for "walk everything that is
    settable": `/input/{ch}/gain` is three rows and a dict keeps one of
    them. Walking the dict dropped inputs 1-2 out of `--dump-config`
    entirely, because the surviving row's capability was the instrument
    channels.
    """
    if device is None:
        return ()
    prefix = "/%s/{ch}/" % family
    parents = {r.template.rsplit("/", 1)[0] for r in device.registers}
    return tuple((r.template[len(prefix):], r) for r in device.registers
                 if r.domain is not None and r.template.startswith(prefix)
                 and "/" not in r.template[len(prefix):]
                 and r.template not in parents)


def nested_families(device: Optional[Device], family: str) -> Tuple[str, ...]:
    """The sub-families a `[<sub>:<family>:<n>]` section may name."""
    if device is None:
        return ()
    prefix = "/%s/{ch}/" % family
    found = set()
    for register in device.registers:
        if not register.template.startswith(prefix):
            continue
        rest = register.template[len(prefix):]
        if "/" in rest:
            found.add(rest.split("/", 1)[0])
    return tuple(sorted(found))


def option_register(device: Optional[Device], family: str, option: str,
                    channel: int) -> Optional[Register]:
    """The register a ``[family:channel]`` section's option resolves to.

    One option name can have several rows when the device's own limits
    differ by channel: `/input/{ch}/gain` is three rows, because upstream
    clamps the two mic preamps at 75 dB, the two instrument channels at
    24, and Analog 5-8 at nothing at all. Picking by name alone returns
    whichever row happens to be last and validates a config against the
    wrong ceiling.
    """
    if device is None:
        return None
    template = "/%s/{ch}/%s" % (family, option)
    for register in device.registers:
        if (register.template == template and register.domain is not None
                and device.has(register.channels, channel)):
            return register
    return None


def option_channels(device: Optional[Device], family: str,
                    option: str) -> Tuple[int, ...]:
    """Every channel that can set this option, across all its rows."""
    if device is None:
        return ()
    template = "/%s/{ch}/%s" % (family, option)
    found: Set[int] = set()
    for register in device.registers:
        if register.template == template and register.domain is not None:
            found.update(device.channels_for(register.channels))
    return tuple(sorted(found))


def settable_nested(device: Optional[Device], sub: str,
                    family: str) -> Dict[str, Register]:
    """Options a ``[<sub>:<family>:<n>]`` section may set.

    Keyed like every other section: the last path segment, or
    ``ENABLE_OPTION`` for the sub-family's own switch --
    `/input/{ch}/eq` carries a value as well as a subtree, the same
    shape `/echo` has.

    Empty unless ``sub`` really is a sub-family, and that guard is not
    decoration. Without it `settable_nested(device, "gain", "input")`
    returns the *gain* register under ``ENABLE_OPTION``, because its
    template matches the prefix exactly -- so a childless option would
    answer to a section shape it has no business in. Only
    ``_is_nested_section`` stood between that and `[gain:input:3]` being
    accepted, which is two places having to agree about one fact.
    """
    if device is None or sub not in nested_families(device, family):
        return {}
    prefix = "/%s/{ch}/%s" % (family, sub)
    found: Dict[str, Register] = {}
    for register in device.registers:
        if register.domain is None:
            continue
        if register.template == prefix:
            found[ENABLE_OPTION] = register
        elif register.template.startswith(prefix + "/"):
            found[register.template[len(prefix) + 1:]] = register
    return found
