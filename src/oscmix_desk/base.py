"""Whole-state desks: what `[desk] base =` writes beneath the declared routes.

TotalMix offers to reset the mix before building one (manual: Options, Reset Mix; and the DAW
mode). A base does that for a desk, so a profile is a whole matrix and an omitted route is a
silent one:

- ``keep`` (default): nothing; cells no route sets keep whatever the device holds.
- ``clear-inputs``: every input cell no route sets goes to the backend's explicit zero
  (-65 dB) -- no hardware monitoring beyond the routes; playback cells keep what they hold.
- ``clear-submixes``: every input and playback cell no route sets goes to that zero.
- ``straight-playback``: playback pair N plays on output pair N at 0 dB, every other playback
  cell is silent; input cells are left alone.
- ``daw``: straight playback and no hardware monitoring -- every input cell silent too.

Silence needs no link. Where the desk declares no link for a block's pairs, silence is written
to every register of the block, one per channel: the backend folds a write to the even channel
of a linked pair onto the pair (oscmix.c ``setmix``), and silence is the same in either
addressing, so it lands whatever the device's link state. On a linked pair the device reports
only the odd/odd register, so the others are compared when a dump reports them (``--diff``)
and a read-back does not wait for them. Where a route declares the links, the block is
addressed as declared and every register is compared.

Straight playback is 0 dB on a diagonal, which the two addressings do not share, so it is
declared as routes of the base (``base_routes``), and those state the links they need like any
route: a pair the desk names keeps its declared link; a pair it does not name is linked, or
unlinked when its partner pair is declared as two mono channels. Linking hands the odd
channel's settings to the pair on the device, so a base links only a pair whose two channels
the desk sets alike, and never an input. It never replaces a route of the desk for the same
cells, and a mirror target gets its source's base routes, as with any route.

Everything a base writes is written on an explicit start or switch and then REMEMBERed
(``Route.base``, ``reconcile.Entry.base``), so a later change in TotalMix stays until the next
switch. TotalMix's Total Reset, which also resets every channel effect, is not offered: in
class-compliant mode the device runs its CC firmware, which has no TotalMix and offers no Clear
Mix setup to take the defaults from, and desk does not guess them.
"""

from __future__ import annotations

from typing import AbstractSet, Dict, List, Optional, Sequence, Set, Tuple

from .constants import LEVEL_MIN
from .errors import ConfigError
from .model import ChannelSetting, Route
from .osc import Args
from .registers import Device

BASES = ("keep", "clear-inputs", "clear-submixes", "straight-playback", "daw")

#: Which source matrices a base silences.
_SILENCES = {"keep": (), "clear-inputs": ("input",), "clear-submixes": ("input", "playback"),
             "straight-playback": ("playback",), "daw": ("input", "playback")}
_STRAIGHT = {"straight-playback", "daw"}

#: (path, tags, args, compared) of one silent cell; a register that may fold is not compared.
BaseEntry = Tuple[str, str, Args, bool]
Links = Dict[Tuple[str, int], bool]


def check_base(base: str) -> None:
    if base not in BASES:
        raise ConfigError("[desk] base: expected one of %s, got %r" % (", ".join(BASES), base))


def _declared_links(routes: Sequence[Route]) -> Links:
    declared: Links = {}
    for route in routes:
        for kind, channel, linked in route.link_requirements:
            declared[(kind, channel)] = linked
    return declared


def _odd(device: Device, family: str) -> List[int]:
    channels = device.channels_for(family)
    return [ch for ch in channels if ch % 2 and ch + 1 in channels]


def base_entries(base: str, routes: Sequence[Route], declared: AbstractSet[str],
                 device: Device) -> Tuple[BaseEntry, ...]:
    """The silent cells of ``base`` where no route writes (``declared``), in block order."""
    check_base(base)
    links = _declared_links(routes)
    entries: List[BaseEntry] = []
    for out in _odd(device, "output"):
        outs = (out,) if links.get(("output", out)) else (out, out + 1)
        for kind in _SILENCES[base]:
            for source in _odd(device, kind):
                sources = (source,) if links.get((kind, source)) else (source, source + 1)
                for target in outs:
                    for channel in sources:
                        path = "/mix/%d/%s/%d" % (target, kind, channel)
                        if path not in declared:
                            entries.append((path, "fi", (LEVEL_MIN, 0),
                                            (target == out or ("output", out) in links)
                                            and (channel == source or (kind, source) in links)))
    return tuple(entries)


def _merged(settings: Sequence[ChannelSetting], family: str, left: int) -> Optional[str]:
    """What a link of ``family`` pair ``left`` would merge that the desk keeps apart, or None."""
    odd = {s.option: s.value for s in settings if s.family == family and s.channel == left}
    even = {s.option: s.value for s in settings if s.family == family and s.channel == left + 1}
    apart = sorted(option for option in {*odd, *even} if odd.get(option) != even.get(option))
    if not apart:
        return None
    return ("%s %d and %d set %s differently" % (family, left, left + 1, ", ".join(apart)))


def _diagonal(left: int, playback: Optional[bool], output: Optional[bool],
              taken: AbstractSet[Tuple[int, int]], base: str) -> List[Route]:
    """Playback pair ``left`` on output pair ``left`` at 0 dB, shaped by the declared links."""
    pair, name = (left, left + 1), "[desk] base %d/%d" % (left, left + 1)
    if playback is False and output is True:
        raise ConfigError("[desk] base = %s: playback %d/%d is two mono channels and output "
                          "%d/%d a linked pair, so playback cannot play 1:1 there; declare "
                          "both alike" % (base, left, left + 1, left, left + 1))
    if playback is False:
        return [Route("[desk] base %d" % ch, (ch,), playback=(ch,), base=True)
                for ch in pair if (ch, ch) not in taken]
    if taken & {(out, pb) for out in pair for pb in pair}:
        return []
    return [Route(name, pair, playback=pair, stereo=output is not False, base=True)]


def _check_mirrors(base: str, left: int, diagonal: Sequence[Route], mirrored: AbstractSet[int],
                   alone: AbstractSet[int], paired: AbstractSet[int]) -> None:
    """Refuse a 1:1 the mirrors around it cannot copy, naming the mirror rather than the base."""
    if not diagonal:
        return
    pair = "%d/%d" % (left, left + 1)
    shapes = {len(route.output) for route in diagonal}
    problem = ""
    if {left, left + 1} & mirrored:
        problem = "output %s is half a mirror target" % pair
    elif 2 in shapes and {left, left + 1} & alone:
        problem = "one of those outputs is mirrored alone"
    elif 1 in shapes and left in paired:
        problem = "it plays there as two mono channels and the pair is mirrored whole"
    if problem:
        raise ConfigError("[desk] base = %s plays playback %s on output %s, but %s; mirror "
                          "pairs whole, or declare %s with routes of your own"
                          % (base, pair, pair, problem, pair))


def base_routes(base: str, routes: Sequence[Route], settings: Sequence[ChannelSetting],
                mirrors: Sequence[Tuple[Tuple[int, ...], Tuple[int, ...]]],
                device: Device) -> Tuple[Route, ...]:
    """The routes ``straight-playback`` and ``daw`` declare: each playback pair on its outputs.

    Skips a mirror target, which gets its source's, and every cell a route already sets.
    """
    check_base(base)
    if base not in _STRAIGHT:
        return ()
    links = _declared_links(routes)
    targets = {source[0]: target[0] for target, source in mirrors if len(source) == 2}
    mirrored: Set[int] = {channel for target, _source in mirrors for channel in target}
    alone = {source[0] for _target, source in mirrors if len(source) == 1}
    paired = {source[0] for _target, source in mirrors if len(source) == 2}
    taken = {(out, pb) for route in routes for out in route.output for pb in route.playback}
    playback = set(_odd(device, "playback"))
    added: List[Route] = []
    for left in _odd(device, "output"):
        if left not in playback or {left, left + 1} <= mirrored:
            continue
        diagonal = _diagonal(left, links.get(("playback", left)), links.get(("output", left)),
                             taken, base)
        _check_mirrors(base, left, diagonal, mirrored, alone, paired)
        owned = {(kind, channel) for route in diagonal
                 for kind, channel, linked in route.link_requirements
                 if linked and (kind, channel) not in links}
        if ("output", left) in owned and left in targets:
            # Its mirror gets a copy, which links the target pair too.
            owned.add(("output", targets[left]))
        for family, channel in sorted(owned):
            merged = _merged(settings, family, channel)
            if merged:
                raise ConfigError("[desk] base = %s links %s %d/%d to play 1:1, but %s; "
                                  "declare the pair with a route of its own (stereo = false "
                                  "keeps it two channels)"
                                  % (base, family, channel, channel + 1, merged))
        added += diagonal
    return tuple(added)
