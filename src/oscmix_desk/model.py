"""A desk as data: what a ``routing.conf`` says, once it has been read.

Routes, channel and global settings, device identity and retained legacy
OSC ports. Nothing here parses or talks to anything; ``config`` builds
these and everything else reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from .constants import (
    CHANNEL_MAX,
    CHANNEL_MIN,
    DEFAULT_DEVICE_NAME,
    DEFAULT_OSC_PORT,
    DEFAULT_OSC_RECV_PORT,
    DEFAULT_USB_ID,
    LEVEL_MAX,
    LEVEL_MIN,
)
from .registers import ENABLE_OPTION, Policy

#: What a channel or global setting may hold once it is parsed: a switch,
#: a number, or an enum's name as the device spells it.
SettingValue = Union[bool, int, float, str]


@dataclass(frozen=True)
class Route:
    """One source -> hardware output route (mono or a stereo pair).

    The source is either a software ``playback`` pair or a hardware
    ``input`` pair, never both. Input sources are what makes
    zero-latency direct monitoring expressible -- the reason TotalMix
    exists on a tracking session -- and unlike the playback matrix, the
    registers they write are **reported back by the device**, so a
    monitoring path can be verified rather than only re-established.
    """

    name: str
    #: Required. Every route has a destination; only the *source* is a
    #: choice, which is why `playback` gained a default and this did not.
    output: Tuple[int, ...]
    playback: Tuple[int, ...] = ()
    level: float = 0.0
    volume: Optional[float] = None
    stereo: bool = True
    #: Hardware input channels, as an alternative to ``playback``.
    input: Tuple[int, ...] = ()
    #: Balance of a linked stereo source and output pair, as OSC Int32.
    #: Level and balance belong to one compound matrix register.
    pan: int = 0
    #: The outputs this route copies, when a `mirror =` declared it rather than the file.
    mirror_of: Tuple[int, ...] = ()
    #: Declared by `[desk] base`, not by the file: REMEMBERed after an explicit apply.
    base: bool = False

    def __post_init__(self) -> None:
        _ = self.checked_pan

    @property
    def checked_pan(self) -> int:
        """A declared balance supported by this route's addressing shape."""
        if type(self.pan) is not int:
            raise ValueError("pan must be an integer, not a boolean or float")
        if not -100 <= self.pan <= 100:
            raise ValueError("pan out of range -100..100")
        if self.pan:
            if bool(self.input) == bool(self.playback):
                raise ValueError("nonzero pan requires exactly one input or playback source")
            _kind, source = self.source
            if (self.stereo is not True or len(source) != 2 or len(self.output) != 2
                    or any(type(ch) is not int for ch in (*source, *self.output))
                    or any(not CHANNEL_MIN <= ch <= CHANNEL_MAX
                           for ch in (*source, *self.output))
                    or any(pair[0] % 2 != 1 or pair[1] != pair[0] + 1
                           for pair in (source, self.output))):
                raise ValueError("nonzero pan requires linked source and output stereo pairs")
            if (isinstance(self.level, bool) or not isinstance(self.level, (int, float))
                    or not LEVEL_MIN < self.level <= LEVEL_MAX):
                raise ValueError("nonzero pan is neither preserved nor reported for a silent "
                                 "route; level must be finite and above -65 dB, at most +6 dB")
        return self.pan

    @property
    def source(self) -> Tuple[str, Tuple[int, ...]]:
        """``("input", channels)`` or ``("playback", channels)``.

        One accessor rather than a branch at every call site: the OSC
        path segment and the register family differ only in this word,
        and spreading that choice is how the two drift apart.
        """
        return ("input", self.input) if self.input else ("playback", self.playback)

    @property
    def link_requirements(self) -> Tuple[Tuple[str, int, bool], ...]:
        """The pair flags required to address exactly this route's channels.

        Even channel addresses fold onto the odd partner while linked.
        A mono route therefore requires both its source and destination
        pairs unlinked. Parsing checks agreement across routes; planning
        writes these same requirements before the mix.
        """
        kind, source = self.source
        return (
            (kind, source[0] if source[0] % 2 else source[0] - 1,
             len(source) == 2),
            ("output", self.output[0] if self.output[0] % 2 else self.output[0] - 1,
             len(self.output) == 2 and self.stereo),
        )


@dataclass(frozen=True)
class ChannelSetting:
    """One option a ``[input:N]`` / ``[output:N]`` section pins.

    Kept as (family, channel, option, value) rather than as a nested
    structure: it is one row of the register table with a value, which
    is exactly what the plan consumes.
    """

    family: str
    channel: int
    option: str
    value: SettingValue


@dataclass(frozen=True)
class GlobalSetting:
    """One option a ``[<family>]`` section pins, for a family with no
    channel dimension -- `[echo]`, and the four that follow it.

    Separate from ``ChannelSetting`` rather than that class with a
    ``None`` channel: "the channel is None" is a state every consumer
    then has to remember to handle, and forgetting is silent. A distinct
    type makes the plan's two loops obviously two.
    """

    family: str
    option: str
    value: SettingValue

    @property
    def path(self) -> str:
        """The OSC path this writes.

        The family's own register has no segment of its own, which is
        what ``ENABLE_OPTION`` exists for.
        """
        if self.option == ENABLE_OPTION:
            return "/%s" % self.family
        return "/%s/%s" % (self.family, self.option)


class Overrides(NamedTuple):
    """Config overrides: device name and the retained legacy OSC port."""

    device_name: Optional[str] = None
    osc_port: Optional[int] = None


#: Compatibility name for callers constructing command-line overrides.
CommandLine = Overrides


def _resolve_overrides(overrides: Optional[Overrides], said: Optional[Overrides]) -> Overrides:
    """Accept the legacy keyword once, without choosing between two inputs."""
    if overrides is not None and said is not None:
        raise TypeError("use overrides or the legacy said argument, not both")
    if overrides is not None:
        return overrides
    return said if said is not None else Overrides()


class Machine(NamedTuple):
    """Device identity and the two retained legacy OSC port values.

    Only device_name, usb_id and serial select the machine. The ports
    remain parseable config data and do not participate in identity checks.
    """

    device_name: str
    usb_id: str
    serial: str
    osc_port: int
    osc_recv_port: int

    def differs_from(self, other: "Machine") -> str:
        """The identity settings that differ, for a person."""
        return ", ".join(
            "%s %r (not %r)" % (name.replace("_", " "), mine, theirs)
            for name, mine, theirs in zip(self._fields, self, other)
            if name not in ("osc_port", "osc_recv_port") and mine != theirs)

    def under(self, overrides: Optional[Overrides] = None, *,
              said: Optional[Overrides] = None) -> "Machine":
        """This machine as a start resolves it: the command line over the file."""
        overrides = _resolve_overrides(overrides, said)
        return self._replace(
            device_name=overrides.device_name or self.device_name,
            osc_port=self.osc_port if overrides.osc_port is None else overrides.osc_port)

    def elsewhere(self, live: "Machine") -> str:
        """Why this names another device than the session, or "".

        ``self`` is resolved under the session's command-line overrides.
        An empty serial means the box its start pinned. Device names are
        compared as written, even when another spelling would match the
        same sequencer client. Legacy ports never change the answer.
        """
        mine = self if self.serial else self._replace(serial=live.serial)
        return mine.differs_from(live)


@dataclass(frozen=True)
class Config:
    """A desk, parsed and validated, and not changed after that.

    Overrides, pinned serials and retained machine settings produce a new
    config with ``dataclasses.replace``. Tuple fields and the policy map
    are owned and immutable; consumers cannot alter the caller's desk.
    """

    device_name: str = DEFAULT_DEVICE_NAME
    usb_id: str = DEFAULT_USB_ID
    #: Which box, when the machine has more than one of the same
    #: model. Empty means "the only one", and two unnamed boxes
    #: share one lock rather than racing.
    serial: str = ""
    osc_port: int = DEFAULT_OSC_PORT
    osc_recv_port: int = DEFAULT_OSC_RECV_PORT
    routes: Tuple[Route, ...] = ()
    channels: Tuple[ChannelSetting, ...] = ()
    #: Settings from `[<family>]` sections -- families with no channel.
    globals: Tuple[GlobalSetting, ...] = ()
    #: ``(family, option) -> "pin" | "remember"`` from a ``[pin]``
    #: section, overriding the register table's default for that option.
    policies: Mapping[Tuple[str, str], Policy] = field(default_factory=dict)
    #: The machine settings as the *file* resolved them, set by
    #: ``load_config`` and never changed. The five attributes above can be
    #: replaced afterwards -- by ``--device`` and ``--osc-port``, by the
    #: serial a start pins, by a running session that keeps its own -- and
    #: this is what remembers which interface the file was validated for
    #: before command-line overrides. The OSC ports select no backend.
    #: None only for a ``Config``
    #: that ``load_config`` never saw; no file resolves to the defaults.
    loaded: Optional[Machine] = None
    #: What the command line put in place of the file's settings. No file
    #: this process reads again has a say in these, exactly as at its start.
    overrides: Overrides = field(default_factory=Overrides)
    #: `[desk] base`: what the matrix is beneath the routes (see `base.BASES`).
    base: str = "keep"
    #: `[desk] enforce`: ``apply``, or ``watch`` to reconcile when the device
    #: contradicts a pinned value (watch.py). routing.conf's; profiles inherit it.
    enforce: str = "apply"

    def __post_init__(self) -> None:
        # A frozen dataclass alone leaves a dict writable. Copy first:
        # retaining the caller's mapping would let it change which values
        # a later reconcile pins, after this desk was validated.
        object.__setattr__(self, "policies",
                           MappingProxyType(dict(self.policies)))
        validate_route_pan(self.routes)


def validate_route_pan(routes: Sequence[Route]) -> None:
    """Reject incompatible compound claims where a new balance is declared.

    Pure pan-0 duplicates keep their existing file-order semantics. A
    nonzero balance owns the whole level/pan register, including aliases
    folded through the pair links. It must agree with every other claim.
    """
    if not any(route.checked_pan for route in routes):
        return
    links: Dict[Tuple[str, int], int] = {}
    claims: Dict[Tuple[int, str, int], List[Route]] = {}
    for route in routes:
        for family, channel, linked in route.link_requirements:
            key = (family, channel)
            if key in links and links[key] != linked:
                raise ValueError("routes disagree on linked %s pair %d/%d while declaring pan"
                                 % (family, channel, channel + 1))
            links[key] = linked
        source, output = route.link_requirements
        cell_key = (output[1] if output[2] else route.output[0], source[0],
               source[1] if source[2] else route.source[1][0])
        claims.setdefault(cell_key, []).append(route)
    for (out, kind, src), declarations in claims.items():
        if not any(route.checked_pan for route in declarations):
            continue
        first = declarations[0]
        for route in declarations[1:]:
            if (first.level, first.pan) != (route.level, route.pan):
                raise ValueError("routes %r and %r disagree on level/pan for /mix/%d/%s/%d"
                                 % (first.name, route.name, out, kind, src))
