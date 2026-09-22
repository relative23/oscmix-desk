"""A desk as data: what a ``routing.conf`` says, once it has been read.

Routes, channel and global settings, and the five settings that say
*where* the desk goes rather than what it is. Nothing here parses,
validates or talks to anything; ``config`` builds these and everything
else reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, NamedTuple, Optional, Tuple, Union

from .constants import (
    DEFAULT_DEVICE_NAME,
    DEFAULT_OSC_PORT,
    DEFAULT_OSC_RECV_PORT,
    DEFAULT_USB_ID,
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


class CommandLine(NamedTuple):
    """What ``--device`` and ``--osc-port`` put in place of a file's settings."""

    device_name: Optional[str] = None
    osc_port: Optional[int] = None


class Machine(NamedTuple):
    """The five settings that say *where* a desk goes, not what it is."""

    device_name: str
    usb_id: str
    serial: str
    osc_port: int
    osc_recv_port: int

    def differs_from(self, other: "Machine") -> str:
        """The settings that differ, for a person: ``serial '2' (not '1')``."""
        return ", ".join(
            "%s %r (not %r)" % (name.replace("_", " "), mine, theirs)
            for name, mine, theirs in zip(self._fields, self, other)
            if mine != theirs)

    def under(self, said: CommandLine) -> "Machine":
        """This machine as a start resolves it: the command line over the file."""
        return self._replace(
            device_name=said.device_name or self.device_name,
            osc_port=self.osc_port if said.osc_port is None else said.osc_port)

    def elsewhere(self, live: "Machine") -> str:
        """Why this is not the machine a session runs on, or "" when it is.

        ``self`` is what a restart of that session would resolve its file
        to, the command line included (``under``), and ``live`` what the
        session runs with. Naming no serial means "the only one", which a
        running session takes to be the box its start pinned: it does not
        count the boxes again, so with a second one plugged in since, a
        restart asks for the serial where a reload goes on writing to
        the pinned box. And a name is compared as written, where a start
        looks for it as a substring: another spelling that finds the
        same client is refused until a restart. The first cuts compared the bare file and refused
        the session's own desk: the pinned box once routing.conf named
        it, then a file under ``--osc-port`` (found by review, 0.6.11).
        """
        mine = self if self.serial else self._replace(serial=live.serial)
        return mine.differs_from(live)


@dataclass(frozen=True)
class Config:
    """A desk, parsed and validated, and not changed after that.

    Frozen since 0.7.0 (first outside review). It was built by assignment
    and then assigned to from four more places -- the command line's
    overrides, the serial a start pins, a running session keeping its
    machine settings, a profile's base -- so "the config" was whatever
    the last of them had left in it, and a function handed one could not
    know whether its caller's copy moved with it. Each of those makes a
    new one now (``dataclasses.replace``).
    """

    device_name: str = DEFAULT_DEVICE_NAME
    usb_id: str = DEFAULT_USB_ID
    #: Which box, when the machine has more than one of the same
    #: model. Empty means "the only one", and two unnamed boxes
    #: share one lock rather than racing (ADR 0023).
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
    #: and which backend it names (0.6.11). None only for a ``Config``
    #: that ``load_config`` never saw; no file resolves to the defaults.
    loaded: Optional[Machine] = None
    #: What the command line put in place of the file's settings. No file
    #: this process reads again has a say in these, exactly as at its start.
    overrides: CommandLine = field(default_factory=CommandLine)

    def __post_init__(self) -> None:
        # A frozen dataclass alone leaves a dict writable. Copy first:
        # retaining the caller's mapping would let it change which values
        # a later reconcile pins, after this desk was validated.
        object.__setattr__(self, "policies",
                           MappingProxyType(dict(self.policies)))
