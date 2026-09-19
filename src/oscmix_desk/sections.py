"""The sections the register table declares: channels, families, globals.

``[input:3]``, ``[eq:output:1]``, ``[reverb]``: which of them exist, which
options they take and what each may hold is not written here but read
from the device's table (``registers``), so a register row is all it
takes for a section to parse. A device nobody modelled has no such
sections; they are passed over with a warning that says what is modelled
(ADR 0006).
"""

from __future__ import annotations

import configparser
from typing import List, Optional

from .devices import device_for_name, modelled_names
from .errors import ConfigError
from .log import log
from .model import (
    ChannelSetting,
    Config,
    GlobalSetting,
    SettingValue,
)
from .registers import (
    BOOL,
    ENABLE_OPTION,
    ENUM,
    NUMBER,
    Device,
    Register,
    global_families,
    nested_families,
    option_channels,
    option_register,
    settable_globals,
    settable_nested,
    settable_options,
)


def _parse_bool(raw: str, section: str, option: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("1", "yes", "true", "on"):
        return True
    if lowered in ("0", "no", "false", "off"):
        return False
    raise ConfigError("[%s] %s: %r is not a boolean" % (section, option, raw))


def _has_register_model(config: "Config") -> bool:
    """Whether the configured device comes with a register table.

    Two things answer "no": a name the model has never heard of, and a
    device it lists without rows -- the 802, whose channel map is read
    from upstream and whose registers nobody has measured. Both get no
    opinion on routes (ADR 0006). Both must *say so* when a section asks
    for registers they cannot supply, rather than stay silent.
    """
    device = device_for_name(config.device_name)
    return device is not None and bool(device.registers)


def _warn_unknown_section(section: str, config: "Config") -> None:
    """Say that a section is ignored, and say the right why.

    A warning, not an error. See
    docs/decisions/0006-routing-conf-compatibility.md: a section this
    version does not know is how a *newer* version adds a feature, and
    refusing the whole file over it leaves the device in whatever state
    the last boot left it, with no restart (RestartPreventExitStatus=2).
    An unknown *option* inside a known section stays an error -- that is
    what a typo looks like, and a silently ignored 'levl = -20' is a
    wrong device state nobody is told about.

    Two situations land here and used to get one message. On a device
    without a register table every `[eq:input:3]` and `[clock]` fell
    through to the "newer version" text, which sent the reader to the
    changelog when the cause was the device table. Nothing was newer;
    the model has no rows for that device, and the warning says so.
    """
    if not _has_register_model(config):
        log.warning(
            "ignoring [%s]: no register model for %r declares it, so "
            "nothing in it could reach the device (modelled: %s)",
            section, config.device_name, modelled_names())
        return
    log.warning(
        "ignoring unknown section [%s] -- this config may have been "
        "written by a newer version of oscmix-desk (known: %s)",
        section, _known_sections(config))


def _known_sections(config: "Config") -> str:
    """The section names this version understands, for the warning.

    Derived rather than spelled out. The hand-written list went stale
    the moment `[echo]` landed and again with `[eq:input:<n>]`, and a
    diagnostic that lists the wrong alternatives is worse than one that
    lists none -- it reads as authoritative.
    """
    device = device_for_name(config.device_name)
    names = ["[device]", "[osc]", "[pin]", "[route:<name>]",
             "[input:<n>]", "[output:<n>]"]
    names += ["[%s]" % family for family in global_families(device)]
    for family in ("input", "output"):
        names += ["[%s:%s:<n>]" % (sub, family)
                  for sub in nested_families(device, family)]
    return ", ".join(names)


def _is_nested_section(section: str, config: "Config") -> bool:
    """Whether ``[<sub>:<family>:<n>]`` names something the model carries.

    Checked before the section is parsed so an unknown sub-family still
    falls through to the "newer version" warning rather than being
    claimed and then rejected -- which is precisely the failure ADR 0014
    measured in 0.3.0 and moved the format to avoid.
    """
    parts = section.split(":")
    if len(parts) != 3 or not parts[2].strip().isdigit():
        return False
    sub, family = parts[0], parts[1]
    device = device_for_name(config.device_name)
    return family in ("input", "output") and sub in nested_families(device,
                                                                    family)


def _parse_nested_section(parser: "configparser.ConfigParser", section: str,
                          device: Optional[Device]) -> List[ChannelSetting]:
    """Parse ``[eq:input:3]`` and the families that follow it.

    Produces ``ChannelSetting`` like a flat section does, with the option
    carrying the rest of the path -- ``eq/band1freq``, or ``eq`` for the
    sub-family's own switch. One settings type rather than two, because
    the plan consumes them identically and a second type would be a
    second loop to forget.
    """
    sub, family, raw = section.split(":")
    channel = int(raw)
    known = settable_nested(device, sub, family)
    if not known:
        # Two different situations produce an empty set, and they call
        # for opposite answers. An unmodelled device has no opinion, so
        # the section passes through as it always has. A family the
        # model *does* know and declares unsettable must be refused,
        # because accepting it delivers nothing while looking exactly
        # like a section that worked. Room EQ was that example until
        # 0.6.0: the pin moved, the device took the writes, and the
        # table says so -- which is why the rule reads the table rather
        # than naming families here.
        if device is not None and sub in nested_families(
                device, family):
            raise ConfigError(
                "[%s]: %s is reported by the device but cannot be set -- "
                "oscmix accepts the write and the register does not change"
                % (section, sub))
        return []
    if not _has_channel(device, family, sub, channel):
        raise ConfigError(
            "[%s]: %s has no channel %d with %s"
            % (section, family, channel, sub))
    unknown = set(parser.options(section)) - set(known)
    if unknown:
        raise ConfigError(
            "[%s]: unknown option(s) %s (valid: %s)"
            % (section, ", ".join(sorted(unknown)), ", ".join(sorted(known))))
    found = []
    for option in parser.options(section):
        register = known[option]
        value = _parse_domain(parser.get(section, option), section, option,
                              register)
        rest = sub if option == ENABLE_OPTION else "%s/%s" % (sub, option)
        found.append(ChannelSetting(family, channel, rest, value))
    return found


def _has_channel(device: Optional[Device], family: str, sub: str,
                 channel: int) -> bool:
    """Whether this device has that channel in that sub-family."""
    if device is None:
        return False
    for register in settable_nested(device, sub, family).values():
        return channel in device.channels_for(register.channels)
    return False


def _parse_global_section(parser: "configparser.ConfigParser", section: str,
                          device: Optional[Device]) -> List[GlobalSetting]:
    """Parse ``[echo]`` and the other channel-less families.

    Same rule as a channel section and for the same reason: which
    options exist and what values they take comes from the register
    model, not from a list kept here. A second list is a second place to
    disagree with the device.
    """
    known = settable_globals(device, section)
    if not known:
        # Only reachable for a family the model lists with no settable
        # row. The UCX II has none as of 0.6.2, and the branch used to
        # return an empty list in silence -- the same shape the refusal
        # in `_parse_nested_section` exists to prevent.
        raise ConfigError(
            "[%s]: the device reports this family but nothing in it can "
            "be set from a config" % section)
    unknown = set(parser.options(section)) - set(known)
    if unknown:
        raise ConfigError(
            "[%s]: unknown option(s) %s (valid: %s)"
            % (section, ", ".join(sorted(unknown)), ", ".join(sorted(known))))
    found = []
    for option in parser.options(section):
        register = known[option]
        value = _parse_domain(parser.get(section, option), section, option,
                              register)
        found.append(GlobalSetting(section, option, value))
    return found


def _parse_channel_section(parser: "configparser.ConfigParser", section: str,
                           family: str, device: Optional[Device],
                           device_name: str) -> List[ChannelSetting]:
    """Parse ``[input:N]`` / ``[output:N]``.

    Everything here is checked against the register model rather than
    against a list kept in this file: which options exist, which
    channels have them, and what values they take. A second list would
    be a second place to disagree with the device.
    """
    raw = section.split(":", 1)[1].strip()
    try:
        channel = int(raw)
    except ValueError:
        raise ConfigError(
            "[%s]: %r is not a channel number" % (section, raw)) from None

    known = settable_options(device, family)
    if device is None or not known:
        # No rows for this device: an unmodelled name, or the 802, which
        # lists channels and no registers. Routes still get no opinion
        # (ADR 0006). A section that asks for registers the model cannot
        # supply must not: it used to return here in silence, having
        # parsed, shown nothing in `--dry-run` and delivered nothing at
        # the device, while looking exactly like a section that worked.
        # That is the shape of the 0.6.1 route defect, one file over.
        log.warning(
            "ignoring [%s]: no register model for %r declares settable %s "
            "options, so nothing in it could reach the device "
            "(modelled: %s)", section, device_name, family, modelled_names())
        return []

    unknown = set(parser.options(section)) - set(known)
    if unknown:
        extra = ""
        if family == "input" and "48v" in unknown:
            extra = (" -- '48v' is deliberately not settable from a config "
                     "yet: phantom power stays out until a hardware case "
                     "proves the channel it names is the channel it hits")
        raise ConfigError(
            "[%s]: unknown option(s) %s (valid: %s)%s"
            % (section, ", ".join(sorted(unknown)),
               ", ".join(sorted(known)), extra))

    settings = []
    for option in parser.options(section):
        # By channel, not by name: an option can have several rows when
        # the device's limits differ per channel, and the wrong row
        # validates against the wrong ceiling. See `option_register`.
        register = option_register(device, family, option,
                                   channel)
        if register is None:
            valid = option_channels(device, family, option)
            raise ConfigError(
                "[%s] %s: channel %d does not have it on a %s (it has %s "
                "on %d..%d)"
                % (section, option, channel,
                   device.name, option, min(valid), max(valid)))
        settings.append(ChannelSetting(
            family, channel, option,
            _parse_domain(parser.get(section, option), section, option,
                          register)))
    return settings


def _parse_domain(raw: str, section: str, option: str,
                  register: Register) -> SettingValue:
    """Read a value according to the register's declared domain."""
    domain = register.domain
    if domain == BOOL:
        return 1 if _parse_bool(raw, section, option) else 0
    if domain == ENUM:
        choices = register.choices
        value = raw.strip()
        if value not in choices:
            raise ConfigError(
                "[%s] %s: %r is not one of %s -- these are the device's own "
                "names, not ours" % (section, option, value,
                                     ", ".join(choices)))
        return value
    if domain == NUMBER:
        return _parse_number(raw, section, option, register)
    raise ConfigError("[%s] %s: no value domain declared" % (section, option))


def _parse_number(raw: str, section: str, option: str,
                  register: Register) -> float:
    """A quantity, checked against the bounds the register declares.

    The bounds come from upstream's node table -- and upstream does not
    enforce them. At the pinned revision `.min`/`.max` are read nowhere:
    `setfixed` and `setint` both end in `setval`, which converts the
    control to a register and writes, with no comparison in between.
    So this check is not a second opinion agreeing with oscmix; it is
    the only thing between a config file and the register.

    That makes it worth being right rather than strict. Where upstream
    declares no bound neither does the model, and this only checks that
    the text is a number -- inventing a range would reject values the
    device accepts, which is a config that will not load rather than an
    error the device reports.

    What the *hardware* does with an out-of-range value has been measured
    once, and it clamps: `/output/5/lowcut/slope` returns 3 for 4, 7 and
    -1 alike. One register at one revision is not a rule, so nothing here
    relies on it -- but it is the reason `slope` carries bounds at all
    when upstream declares none.
    """
    unit = getattr(register, "unit", "") or ""
    suffix = (" in %s" % unit) if unit else ""
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("[%s] %s: %r is not a number%s"
                          % (section, option, raw, suffix)) from None
    lo = getattr(register, "lo", None)
    hi = getattr(register, "hi", None)
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(
            "[%s] %s: %.1f%s out of range %s..%s"
            % (section, option, value, (" " + unit) if unit else "",
               "-inf" if lo is None else ("%.1f" % lo),
               "inf" if hi is None else ("%.1f" % hi)))
    return value
