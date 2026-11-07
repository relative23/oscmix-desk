"""Pure per-input gain ownership while UCX II AutoSet can change the gain.

Callers supply the last typed DEVICE reports from their completed fresh
window. This module opens no device and cannot establish freshness itself.
An absent or malformed prerequisite preserves gain rather than permitting
a write. The declared state remains unchanged for readback and diff.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .devices import UCX2, device_for_name
from .errors import ConfigError
from .model import Config
from .numeric import number_value
from .osc import Args
from .registers import PIN, option_register

Reports = (Mapping[str, tuple[str, Args] | None]
           | Iterable[tuple[str, tuple[str, Args] | None]])


def flag_value(report: tuple[str, Args] | None) -> int | None:
    """Return a strict typed Boolean DEVICE value, or unknown.

    Python bools, floating-point equivalents, strings, wrong type tags and
    extra arguments cannot authorize an AutoSet-dependent gain write.
    """
    if not isinstance(report, (tuple, list)) or len(report) != 2:
        return None
    tags, args = report
    if (tags != "i" or not isinstance(args, (tuple, list)) or len(args) != 1
            or type(args[0]) is not int):
        return None
    return args[0] if args[0] in (0, 1) else None


def _supported(config: Config) -> bool:
    device = device_for_name(config.device_name)
    return device is not None and device.name == UCX2.name


def _pairs(config: Config) -> tuple[int, ...]:
    if not _supported(config):
        return ()
    pairs = {setting.channel - (setting.channel - 1) % 2
             for setting in config.channels
             if setting.family == "input"
             and setting.option in ("gain", "autoset")
             and type(setting.channel) is int
             and setting.channel in (1, 2, 3, 4)}
    # A route can write its source's link and indirectly change gain,
    # even when it declares neither gain nor AutoSet itself.
    pairs.update(channel - (channel - 1) % 2
                 for route in config.routes for channel in route.input
                 if type(channel) is int and channel in (1, 2, 3, 4))
    return tuple(sorted(pairs))


def _declared_flag(value: object) -> int | None:
    # Config booleans become Int32 writes; exact numeric 0/1 retains the
    # existing programmatic BOOL contract. Wire observations stay strict.
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    return None


def dependency_paths(config: Config) -> tuple[str, ...]:
    """Prerequisites for configured gains, AutoSet and input-source links.

    Both AutoSet flags matter even if only one gain or flag is declared.
    Stereo is reported as a pair; its odd address is the dependency anchor.
    Inputs 5-8 and unsupported devices acquire no new dependencies.
    """
    return tuple(path for left in _pairs(config) for path in (
        f"/input/{left}/autoset", f"/input/{left + 1}/autoset",
        f"/input/{left}/stereo"))


def retained_gains(config: Config, latest: Reports = (), *,
                   linked: Iterable[str] = (),
                   use_declarations: bool = True) -> frozenset[str]:
    """Gain paths an operation must retain, including undeclared partners.

    Repair/reconcile includes declared AutoSet-on as well as live-on or
    unknown flags. Initial/explicit callers use ``use_declarations=False``
    for their live precondition: confirmed off permits an intentional gain
    starting value before a declared AutoSet-on is enabled.

    Only a fresh valid mono observation excludes an indirectly affected
    partner. A planned linking write overrides that mono observation.
    Missing or malformed last reports never mean AutoSet or linkage is off.
    """
    reports = dict(latest)
    planned_links = set(linked)
    declared_on = {setting.channel for setting in config.channels
                   if setting.family == "input" and setting.option == "autoset"
                   and _declared_flag(setting.value) == 1} if use_declarations else set()
    retained: set[str] = set()
    for left in _pairs(config):
        pair = (left, left + 1)
        protected = {channel for channel in pair
                     if channel in declared_on
                     or flag_value(reports.get(f"/input/{channel}/autoset")) != 0}
        link = f"/input/{left}/stereo"
        if protected and (link in planned_links or flag_value(reports.get(link)) != 0):
            protected.update(pair)
        retained.update(f"/input/{channel}/gain" for channel in protected)
    return frozenset(retained)


def _declared_mono_pairs(config: Config) -> frozenset[int]:
    """Static mono declarations, without making a physical-state claim."""
    links: dict[int, set[bool]] = {}
    for route in config.routes:
        family, source = route.source
        if family != "input" or not source or type(source[0]) is not int:
            continue
        left = source[0] - (source[0] - 1) % 2
        if left in (1, 3):
            links.setdefault(left, set()).add(len(source) == 2)
    return frozenset(left for left, values in links.items() if values == {False})


def validate_autogain(config: Config) -> None:
    """Reject explicit gain-PIN/declared-AutoSet conflicts before I/O.

    The gain register's existing PIN default is not an explicit conflict:
    AutoSet can own the live gain after its initial starting value. Only an
    explicit ``input.gain = pin`` override contradicts declared AutoSet-on
    for a configured affected gain. A declared mono route excludes the
    partner from this static conflict; runtime still needs fresh linkage.
    """
    if not _supported(config):
        return
    declared_on, configured_gains = set(), set()
    for setting in config.channels:
        if setting.family != "input":
            continue
        if setting.option == "autoset":
            if type(setting.channel) is not int or setting.channel not in (1, 2, 3, 4):
                raise ConfigError("input %s does not have AutoSet on %s" %
                                  (setting.channel, UCX2.name))
            value = _declared_flag(setting.value)
            if value is None:
                raise ConfigError("input %s autoset: expected a boolean value" % setting.channel)
            if value:
                declared_on.add(setting.channel)
        elif setting.option == "gain" and setting.channel in (1, 2, 3, 4):
            if type(setting.channel) is not int:
                raise ConfigError("input gain: expected an integer channel")
            register = option_register(UCX2, "input", "gain", setting.channel)
            if register is None:
                raise ConfigError("input %s does not have gain on %s" %
                                  (setting.channel, UCX2.name))
            number_value(setting.value, register)
            configured_gains.add(setting.channel)
    if config.policies.get(("input", "gain")) != PIN:
        return
    mono = _declared_mono_pairs(config)
    for channel in sorted(configured_gains):
        left = channel - (channel - 1) % 2
        protected = {channel} if left in mono else {left, left + 1}
        if declared_on.intersection(protected):
            raise ConfigError("input %s gain PIN conflicts with declared AutoSet on its %s" %
                              (channel, "channel" if left in mono else "pair"))
