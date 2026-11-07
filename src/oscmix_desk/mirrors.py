"""Mirrored outputs: one output's declared submix, kept equal on another (manual 27.3).

``[output:3]`` with ``mirror = 1/2`` makes outputs 3/4 send what 1/2 send: every route the
config declares for 1/2 is declared again for 3/4, with the same source, level, balance and
link, and without the route's ``volume`` -- as on the device, a mirror's own fader, EQ and
dynamics stay its own. ``mirror = 5`` on ``[output:7]`` does the same for one mono output.

The routes are expanded when the config loads, so planning, applying, verifying and comparing
see ordinary routes, and each apply keeps the two submixes equal. Only what the desk declares
is mirrored: a cell set by hand on the source output is not the desk's to copy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .errors import ConfigError
from .model import Route
from .registers import Device

MIRROR_OPTION = "mirror"

#: (target outputs, source outputs), as one channel or one adjacent pair each.
Mirror = tuple[tuple[int, ...], tuple[int, ...]]


def parse_mirror(raw: str, section: str, channel: int, device: Device | None) -> Mirror:
    """``mirror = S`` or ``mirror = S/S+1`` in ``[output:N]``; the target has its width."""
    try:
        source = tuple(int(part) for part in raw.strip().split("/"))
    except ValueError:
        raise ConfigError("[%s] mirror: expected an output or an output pair such as 1/2, "
                          "got %r" % (section, raw)) from None
    outputs = device.channels_for("output") if device is not None else ()
    if len(source) == 2:
        if source[0] % 2 != 1 or source[1] != source[0] + 1:
            raise ConfigError("[%s] mirror: %r is not an output pair" % (section, raw))
        if channel % 2 != 1:
            raise ConfigError("[%s] mirror: a pair mirrors onto a pair, so the section must "
                              "name its odd output" % section)
        target: tuple[int, ...] = (channel, channel + 1)
    elif len(source) == 1:
        target = (channel,)
    else:
        raise ConfigError("[%s] mirror: one output or one pair, not %r" % (section, raw))
    if any(ch not in outputs for ch in (*source, *target)):
        raise ConfigError("[%s] mirror: %s has no output %s" % (
            section, device.name if device is not None else "this device",
            "/".join(map(str, target if set(target) - set(outputs) else source))))
    if set(source) & set(target):
        raise ConfigError("[%s] mirror: an output cannot mirror itself" % section)
    return target, source


def expand_mirrors(routes: Sequence[Route], mirrors: Sequence[Mirror]) -> list[Route]:
    """The declared routes plus, for each mirror, a copy of its source's routes.

    Refused: two mirrors writing one output, a mirror of a mirror, and a mirror target that
    also has routes of its own -- a submix kept equal to another has no third source.
    """
    if not mirrors:
        return list(routes)
    claimed: dict[int, tuple[int, ...]] = {}
    for target, source in mirrors:
        for channel in target:
            if channel in claimed:
                raise ConfigError("output %d is the target of two mirrors" % channel)
            claimed[channel] = source
    for target, source in mirrors:
        if any(channel in claimed for channel in source):
            raise ConfigError("output %s mirrors %s, which is itself a mirror; name the "
                              "original" % ("/".join(map(str, target)),
                                            "/".join(map(str, source))))
    for route in routes:
        for _target, source in mirrors:
            if set(route.output) & set(source) and route.output != source:
                raise ConfigError("route %r writes %s, which only partly covers the mirrored "
                                  "output%s %s; mirror the same width the routes use"
                                  % (route.name, "/".join(map(str, route.output)),
                                     "s" if len(source) == 2 else "",
                                     "/".join(map(str, source))))
        taken = [channel for channel in route.output if channel in claimed]
        if taken:
            raise ConfigError("route %r writes output %d, which mirrors %s; a mirror's "
                              "submix comes only from its source"
                              % (route.name, taken[0], "/".join(map(str, claimed[taken[0]]))))
    expanded = list(routes)
    for target, source in mirrors:
        for route in routes:
            if route.output == source:
                expanded.append(replace(
                    route, name="%s-mirror-%s" % (route.name, "-".join(map(str, target))),
                    output=target, volume=None, mirror_of=source))
    return expanded
