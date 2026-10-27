"""DSP limits the device documents, refused before a write instead of found after it.

Two limits of the UCX II hold across channels, so no single register is out of range when they
are broken. The manual (v1.6) allows Room EQ on at most 16 output channels -- 8 stereo -- (25.3.3)
and Crossfeed on at most 4 stereo output channels (25.3.1). Beyond that the device's load limiter
leaves the extra ones off, and a verifier repairing them would never converge.

What counts is the state an apply leaves behind: desk's own writes over the observed state of
every output it does not write. A config alone is checked when it loads; an apply additionally
counts what the device already runs. A plan that switches nothing on needs no observation and is
never refused here: a limit the device already exceeds on its own is not desk's write.

A linked output pair runs one Room EQ on both channels -- the device answers a linked pair on
the partner path, as measured for pan and M/S -- so Room EQ on either channel of a pair the desk
links, or the device reports linked, counts twice. Crossfeed is counted per pair anyway. A plan
that only links a pair is not counted: that would cost nearly every apply a state read, and the
device's own limiter still holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Dict, Mapping, Optional, Sequence, Set, Tuple

from .devices import device_for_name
from .errors import ConfigError
from .model import Config
from .numeric import integer
from .osc import Args


@dataclass(frozen=True)
class DspLimits:
    """How many output channels may run each DSP feature at once."""

    roomeq_channels: int
    crossfeed_pairs: int


#: Per device model, from its manual. A model without an entry is not limited here.
LIMITS: Mapping[str, DspLimits] = {
    "Fireface UCX II": DspLimits(roomeq_channels=16, crossfeed_pairs=4),
}

ROOMEQ = "/output/%d/roomeq"
CROSSFEED = "/output/%d/crossfeed"
LINK = "/output/%d/stereo"


def _limits(config: Config) -> Optional[Tuple[DspLimits, Tuple[int, ...]]]:
    device = device_for_name(config.device_name)
    limits = LIMITS.get(config.device_name)
    if device is None or limits is None:
        return None
    return limits, device.channels_for("output")


def _pair(channel: int) -> int:
    return channel - (channel - 1) % 2


def _problem(limits: DspLimits, roomeq: Mapping[int, int],
             crossfeed: Mapping[int, int], what: str,
             linked: AbstractSet[int] = frozenset()) -> Optional[str]:
    """The first exceeded limit, counted over one state of the outputs."""
    rooms = sorted({channel for channel, value in roomeq.items() if value}
                   | {channel for left in linked for channel in (left, left + 1)
                      if roomeq.get(left) or roomeq.get(left + 1)})
    if len(rooms) > limits.roomeq_channels:
        return ("%s Room EQ on %d output channels (%s); the device runs it on at most %d"
                % (what, len(rooms), ", ".join(map(str, rooms)), limits.roomeq_channels))
    pairs = sorted({_pair(channel) for channel, value in crossfeed.items() if value})
    if len(pairs) > limits.crossfeed_pairs:
        return ("%s Crossfeed on %d stereo outputs (%s); the device runs it on at most %d"
                % (what, len(pairs), ", ".join("%d/%d" % (left, left + 1) for left in pairs),
                   limits.crossfeed_pairs))
    return None


def _declared(config: Config) -> Tuple[Dict[int, int], Dict[int, int]]:
    roomeq: Dict[int, int] = {}
    crossfeed: Dict[int, int] = {}
    for setting in config.channels:
        if setting.family == "output" and setting.option == "roomeq":
            roomeq[setting.channel] = integer(setting.value)
        elif setting.family == "output" and setting.option == "crossfeed":
            crossfeed[setting.channel] = integer(setting.value)
    return roomeq, crossfeed


def _declared_links(config: Config) -> Set[int]:
    return {channel for route in config.routes
            for kind, channel, linked in route.link_requirements if kind == "output" and linked}


def validate_dsp_limits(config: Config) -> None:
    """Refuse a config whose own declarations already exceed a limit."""
    found = _limits(config)
    if found is None:
        return
    problem = _problem(found[0], *_declared(config), "the config declares",
                       _declared_links(config))
    if problem:
        raise ConfigError(problem)


def _written(writes: Sequence[Tuple[str, Args]], template: str,
             channels: Sequence[int]) -> Dict[int, int]:
    paths = {template % channel: channel for channel in channels}
    return {paths[path]: integer(args[0]) for path, args in writes if path in paths}


def switching_on(config: Config, writes: Sequence[Tuple[str, Args]]) -> Tuple[str, ...]:
    """The writes of a plan that turn Room EQ or Crossfeed on, so the limits need counting."""
    found = _limits(config)
    if found is None:
        return ()
    return tuple(template % channel for template in (ROOMEQ, CROSSFEED)
                 for channel, value in _written(writes, template, found[1]).items() if value)


def switches_on(config: Config, writes: Sequence[Tuple[str, Args]]) -> bool:
    return bool(switching_on(config, writes))


def observation_paths(config: Config) -> Tuple[str, ...]:
    """Every output register the count reads, for a device that has limits."""
    found = _limits(config)
    if found is None:
        return ()
    return (*(template % channel for channel in found[1] for template in (ROOMEQ, CROSSFEED)),
            *(LINK % channel for channel in found[1] if channel % 2))


def dsp_problem(config: Config, writes: Sequence[Tuple[str, Args]],
                observed: Mapping[str, Tuple[str, Args]]) -> Optional[str]:
    """The limit this plan would break, counting its writes over the observed outputs.

    An output that did not report is counted as off: the observation cannot prove it on, and
    the device's own limiter still holds. Only plans that switch something on are counted.
    """
    found = _limits(config)
    if found is None or not switches_on(config, writes):
        return None
    limits, channels = found
    state = []
    for template in (ROOMEQ, CROSSFEED, LINK):
        values: Dict[int, int] = {}
        for channel in channels:
            report = observed.get(template % channel)
            try:
                values[channel] = integer(report[1][0]) if report else 0
            except (IndexError, TypeError, ValueError):
                values[channel] = 0
        values.update(_written(writes, template, channels))
        state.append(values)
    linked = {channel for channel, value in state[2].items() if channel % 2 and value == 1}
    return _problem(limits, state[0], state[1], "this apply would leave", linked)
