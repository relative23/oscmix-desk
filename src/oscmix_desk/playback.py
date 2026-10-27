"""Declared ownership of unobservable playback mute and matrix columns.

No device report establishes the backend's mute flags or playback matrix.
Both adjacent flags therefore belong to one declaration; releasing mute
requires explicitly replacing both columns at every declared device output.
This module validates declarations only and performs no I/O.
"""

from __future__ import annotations

from typing import Collection, Dict, FrozenSet, Mapping, Optional, Sequence, Set, Tuple

from .devices import device_for_name
from .errors import ConfigError
from .model import Config, Route
from .numeric import finite, integer, report_value
from .registers import Device, option_register


def mute_path(channel: int) -> str:
    return "/playback/%d/mute" % channel


def pair_left(channel: int) -> int:
    return channel - (channel - 1) % 2


def declared_mutes(config: Config) -> Dict[int, int]:
    """Validated final values; callers must validate the complete config first."""
    return {
        setting.channel: integer(setting.value)
        for setting in config.channels
        if setting.family == "playback" and setting.option == "mute"
    }


def retained_pairs(retained: Collection[str]) -> FrozenSet[int]:
    """An unreadable retained flag protects its adjacent partner as well."""
    found: Set[int] = set()
    for path in retained:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "playback" and parts[2] == "mute":
            try:
                channel = int(parts[1])
            except ValueError:
                continue
            if 1 <= channel <= 20:
                found.add(pair_left(channel))
    return frozenset(found)


def route_cells(config: Config, left: int) -> FrozenSet[Tuple[int, int]]:
    """Explicit matrix cells addressed by routes using either own source.

    Not a base's routes: those are REMEMBERed, so a repair or a reload would release the
    mute without rewriting them -- the columns an unmute owns are the desk's own.
    """
    return frozenset(
        (out, source)
        for route in config.routes if not route.base
        for source in route.playback
        if source in (left, left + 1)
        for out in route.output
    )


def _validated_mutes(config: Config, device: Optional[Device]) -> Dict[int, int]:
    """Validate each declaration before checking ownership across its pair."""
    mutes: Dict[int, int] = {}
    for setting in config.channels:
        if setting.family not in ("input", "output", "playback"):
            raise ConfigError("unknown channel family %r" % setting.family)
        if setting.family != "playback":
            continue
        if device is None or device.usb_id != config.usb_id or not device.registers:
            raise ConfigError(
                "no playback register model for %r / %s" % (config.device_name, config.usb_id)
            )
        if type(setting.channel) is not int:
            raise ConfigError("playback channel must be an integer")
        row = option_register(device, "playback", setting.option, setting.channel)
        if row is None:
            raise ConfigError(
                "playback channel %d has no settable option %r" % (setting.channel, setting.option)
            )
        try:
            report_value(setting.value, row)
            value = integer(setting.value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "playback %d %s: %s" % (setting.channel, setting.option, exc)
            ) from None
        if setting.option != "mute":
            raise ConfigError("unsupported playback setting %r" % setting.option)
        if setting.channel in mutes and mutes[setting.channel] != value:
            raise ConfigError("conflicting playback %d mute values" % setting.channel)
        mutes[setting.channel] = value
    return mutes


def _validate_playback_route(route: Route, device: Device) -> None:
    """A declared playback route must address valid mono cells or adjacent pairs."""
    if not isinstance(route.stereo, (int, float)) or route.stereo not in (0, 1):
        raise ConfigError("playback route %r stereo must be boolean" % route.name)
    for channels, family in ((route.playback, "playback"), (route.output, "output")):
        if (len(channels) not in (1, 2)
                or any(type(ch) is not int or ch not in device.channels_for(family)
                       for ch in channels)
                or (len(channels) == 2
                    and (channels[0] % 2 != 1 or channels[1] != channels[0] + 1))):
            raise ConfigError("invalid %s channels in route %r" % (family, route.name))
    if len(route.playback) != len(route.output):
        raise ConfigError("playback route %r must name one channel or one pair on both sides"
                          % route.name)
    try:
        level = finite(route.level)
    except (TypeError, ValueError) as exc:
        raise ConfigError("playback route %r: %s" % (route.name, exc)) from None
    if not -65 <= level <= 6 or (len(route.output) == 2 and not route.stereo and level > 0):
        raise ConfigError("invalid playback route level in %r" % route.name)


def _validate_route_links(routes: Sequence[Route], device: Device) -> None:
    """Own playback columns cannot depend on contradictory pair-link declarations."""
    links: Dict[Tuple[str, int], int] = {}
    for route in routes:
        if route.playback:
            _validate_playback_route(route, device)
        for family, channel, linked in route.link_requirements:
            key = family, channel
            if key in links and links[key] != linked:
                raise ConfigError("routes disagree on %s %d stereo" % key)
            links[key] = linked


def _require_complete_unmute(config: Config, device: Device,
                             mutes: Mapping[int, int], pairs: Sequence[int]) -> None:
    """Every released pair explicitly owns both columns, including zero cells."""
    for left in pairs:
        if mutes[left] == 0:
            required = {(out, source) for out in device.channels_for("output")
                        for source in (left, left + 1)}
            missing = required - route_cells(config, left)
            if missing:
                raise ConfigError(
                    "playback %d/%d unmute requires a complete explicit matrix "
                    "for both columns at all %d outputs (%d cells missing; a "
                    "[desk] base's routes do not count, the unmute owns its columns)"
                    % (left, left + 1, len(device.channels_for("output")), len(missing))
                )


def validate_playback_mute(config: Config) -> None:
    """Refuse invalid ownership or an incomplete unmute before any I/O.

    Existing route-only configs acquire no new matrix-ownership requirement.
    Mute-on owns both adjacent flags; mute-off also explicitly replaces both
    complete columns. No DEVICE report can supply an undeclared original.
    """
    device = device_for_name(config.device_name)
    mutes = _validated_mutes(config, device)
    if not mutes:
        return
    if device is None:
        raise ConfigError("no playback register model")
    pairs = sorted({pair_left(channel) for channel in mutes})
    for left in pairs:
        if left not in mutes or left + 1 not in mutes:
            raise ConfigError("playback mute requires both explicit partners %d/%d"
                              % (left, left + 1))
        if mutes[left] != mutes[left + 1]:
            raise ConfigError("playback %d/%d mute values must agree; live link state is unknown"
                              % (left, left + 1))
    _validate_route_links(config.routes, device)
    _require_complete_unmute(config, device, mutes, pairs)
