"""routing.conf: the Route/Config model and its parser.

Parsing is total -- every input yields a Config or a ConfigError that
names the section and option, never a traceback."""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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
from .errors import ConfigError
from .log import log
from .registers import (
    BOOL,
    DEVICES,
    ENABLE_OPTION,
    ENUM,
    NUMBER,
    POLICIES,
    device_for_name,
    global_families,
    nested_families,
    option_channels,
    option_register,
    settable_globals,
    settable_nested,
    settable_options,
)


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
    value: object


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
    value: object

    @property
    def path(self) -> str:
        """The OSC path this writes.

        The family's own register has no segment of its own, which is
        what ``ENABLE_OPTION`` exists for.
        """
        if self.option == ENABLE_OPTION:
            return "/%s" % self.family
        return "/%s/%s" % (self.family, self.option)


@dataclass
class Config:
    device_name: str = DEFAULT_DEVICE_NAME
    usb_id: str = DEFAULT_USB_ID
    #: Which box, when the machine has more than one of the same
    #: model. Empty means "the only one", and two unnamed boxes
    #: share one lock rather than racing (ADR 0023).
    serial: str = ""
    osc_port: int = DEFAULT_OSC_PORT
    osc_recv_port: int = DEFAULT_OSC_RECV_PORT
    routes: List[Route] = field(default_factory=list)
    channels: List[ChannelSetting] = field(default_factory=list)
    #: Settings from `[<family>]` sections -- families with no channel.
    globals: List[GlobalSetting] = field(default_factory=list)
    #: ``(family, option) -> "pin" | "remember"`` from a ``[pin]``
    #: section, overriding the register table's default for that option.
    policies: Dict[Tuple[str, str], str] = field(default_factory=dict)


def discover_config_path() -> Optional[Path]:
    """Return the first existing config file in the search order."""
    env = os.environ.get("OSCMIX_CONFIG")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for candidate in (Path(xdg) / "oscmix" / "routing.conf",
                      Path("/etc/oscmix/routing.conf")):
        if candidate.is_file():
            return candidate
    return None


def _parse_channels(raw: str, section: str, option: str) -> Tuple[int, ...]:
    """Parse ``1/2`` (stereo pair) or ``3`` (mono) into a channel tuple."""
    parts = [p.strip() for p in raw.split("/")]
    if len(parts) not in (1, 2) or not all(parts):
        raise ConfigError(
            "[%s] %s: expected a channel ('3') or a pair ('1/2'), got %r"
            % (section, option, raw)
        )
    channels = []
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            raise ConfigError(
                "[%s] %s: %r is not a channel number" % (section, option, part)
            ) from None
        if not CHANNEL_MIN <= value <= CHANNEL_MAX:
            raise ConfigError(
                "[%s] %s: channel %d out of range %d..%d"
                % (section, option, value, CHANNEL_MIN, CHANNEL_MAX)
            )
        channels.append(value)
    return tuple(channels)


def _parse_db(raw: str, section: str, option: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(
            "[%s] %s: %r is not a dB value" % (section, option, raw)
        ) from None
    if not LEVEL_MIN <= value <= LEVEL_MAX:
        raise ConfigError(
            "[%s] %s: %.1f dB out of range %.0f..%.0f"
            % (section, option, value, LEVEL_MIN, LEVEL_MAX)
        )
    return value


def _parse_bool(raw: str, section: str, option: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("1", "yes", "true", "on"):
        return True
    if lowered in ("0", "no", "false", "off"):
        return False
    raise ConfigError("[%s] %s: %r is not a boolean" % (section, option, raw))


_KNOWN_OPTIONS = {
    "device": {"name", "usb-id", "serial"},
    "osc": {"port", "recv-port"},
    "route": {"playback", "input", "output", "level", "volume", "stereo"},
}

#: Overrides for who wins after the initial write, as
#: ``<family>.<option> = pin|remember``. The register table carries a
#: default for every option; this is for the installation that disagrees
#: with it -- a fixed venue that really does want its monitor faders
#: pinned, or a studio that would rather ride an input gain by hand.
#:
#: A section rather than an option inside ``[output:N]``, and that is not
#: a style choice: ADR 0006 makes an unknown *option* in a known section
#: an error, so putting it there would mean every config using it is
#: rejected whole by 0.2.x. An unknown *section* only warns, so this one
#: degrades to "the defaults apply", which is the behaviour those
#: versions already have.


def _parse_pin(parser: "configparser.ConfigParser", section: str,
               config: "Config") -> None:
    """Read ``[pin]``: per-option overrides of the register table default.

    Keys are ``<family>.<option>``; values are ``pin`` or ``remember``.
    Both halves are checked against the register model rather than
    accepted as strings, because a typo here is silent by nature -- the
    routing still applies, and the only symptom is a fader that does or
    does not come back weeks later.
    """
    device = device_for_name(config.device_name)
    for key in parser.options(section):
        raw = parser.get(section, key).strip().lower()
        if raw not in POLICIES:
            raise ConfigError(
                "[pin] %s: expected one of %s, got %r"
                % (key, " or ".join(sorted(POLICIES)), raw))
        if key.count(".") != 1:
            raise ConfigError(
                "[pin] %s: expected '<family>.<option>', e.g. 'output.volume'"
                % key)
        family, option = key.split(".", 1)
        if family not in ("input", "output"):
            raise ConfigError(
                "[pin] %s: unknown family %r (input or output)" % (key, family))
        known = settable_options(device, family)
        if option not in known:
            raise ConfigError(
                "[pin] %s: %s has no settable option %r (valid: %s)"
                % (key, family, option, ", ".join(sorted(known)) or "none"))
        config.policies[(family, option)] = raw


def _check_options(section: str, kind: str, options: Sequence[str]) -> None:
    unknown = set(options) - _KNOWN_OPTIONS[kind]
    if unknown:
        raise ConfigError(
            "[%s]: unknown option(s) %s (valid: %s)"
            % (section, ", ".join(sorted(unknown)),
               ", ".join(sorted(_KNOWN_OPTIONS[kind])))
        )


def _parse_route(parser: configparser.ConfigParser, section: str) -> Route:
    name = section.split(":", 1)[1].strip() or section
    _check_options(section, "route", parser.options(section))
    if not parser.has_option(section, "output"):
        raise ConfigError("[%s]: missing required option 'output'" % section)

    # Exactly one source. Both would be two routes wearing one name, and
    # neither leaves nothing to route -- either is a config the author
    # did not mean, so neither is guessed at.
    has_playback = parser.has_option(section, "playback")
    has_input = parser.has_option(section, "input")
    if has_playback and has_input:
        raise ConfigError(
            "[%s]: 'playback' and 'input' are alternatives -- a route has "
            "one source. Split it into two routes." % section)
    if not has_playback and not has_input:
        raise ConfigError(
            "[%s]: missing a source -- give it 'playback' (software) or "
            "'input' (a hardware input, for direct monitoring)" % section)

    kind = "input" if has_input else "playback"
    source = _parse_channels(parser.get(section, kind), section, kind)
    output = _parse_channels(parser.get(section, "output"), section, "output")
    if len(source) != len(output):
        raise ConfigError(
            "[%s]: %s (%s) and output (%s) must both be mono or both be a pair"
            % (section, kind, parser.get(section, kind),
               parser.get(section, "output"))
        )
    playback = source if kind == "playback" else ()
    inputs = source if kind == "input" else ()
    level = 0.0
    if parser.has_option(section, "level"):
        level = _parse_db(parser.get(section, "level"), section, "level")
    volume = None
    if parser.has_option(section, "volume"):
        volume = _parse_db(parser.get(section, "volume"), section, "volume")
    stereo = True
    if parser.has_option(section, "stereo"):
        stereo = _parse_bool(parser.get(section, "stereo"), section, "stereo")
    return Route(name=name, playback=playback, input=inputs, output=output,
                 level=level, volume=volume, stereo=stereo)


def _parse_osc(parser: configparser.ConfigParser, section: str,
               config: Config) -> None:
    """The [osc] section: two ports, both bounded."""
    _check_options(section, "osc", parser.options(section))
    for option, attr in (("port", "osc_port"), ("recv-port", "osc_recv_port")):
        raw = parser.get(section, option, fallback=str(getattr(config, attr)))
        try:
            port = int(raw)
        except ValueError:
            raise ConfigError(
                "[osc] %s: %r is not a port number" % (option, raw)) from None
        if not 1 <= port <= 65535:
            raise ConfigError(
                "[osc] %s: %d out of range 1..65535" % (option, port))
        setattr(config, attr, port)


def load_config(path: Optional[Path]) -> Config:
    """Load routing.conf. ``path=None`` returns built-in defaults."""
    config = Config()
    if path is None:
        return config
    if not path.is_file():
        raise ConfigError("config file not found: %s" % path)

    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=("#", ";")
    )
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except (configparser.Error, OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError: a config
        # saved in a single-byte encoding used to leave a traceback
        # instead of the line that names the file.
        raise ConfigError("cannot read %s: %s" % (path, exc)) from None

    pending: List[str] = []
    pending_globals: List[str] = []
    pending_nested: List[str] = []
    _dispatch(parser, config, pending, pending_globals, pending_nested)
    device = device_for_name(config.device_name)
    for section in pending_globals:
        config.globals.extend(_parse_global_section(parser, section, device))
    for section in pending_nested:
        config.channels.extend(_parse_nested_section(parser, section, device))
    for section in pending:
        family = section.split(":", 1)[0]
        config.channels.extend(
            _parse_channel_section(parser, section, family, device,
                                   config.device_name))

    _check_device_channels(config)
    _check_link_agreement(config.routes)
    return config


def _dispatch(parser: "configparser.ConfigParser", config: "Config",
              pending: List[str], pending_globals: List[str],
              pending_nested: List[str]) -> None:
    """Route each section to its parser, or warn that we do not know it.

    Channel and nested sections are only *collected* here: both need the
    device, and `[device]` may appear anywhere in the file.
    """
    for section in parser.sections():
        if section == "device":
            _check_options(section, "device", parser.options(section))
            config.device_name = parser.get(section, "name",
                                            fallback=config.device_name).strip()
            usb_id = parser.get(section, "usb-id", fallback=config.usb_id).strip()
            if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", usb_id):
                raise ConfigError(
                    "[device] usb-id: expected 'vvvv:pppp' hex format, got %r" % usb_id
                )
            config.usb_id = usb_id.lower()
            serial = parser.get(section, "serial",
                                fallback=config.serial).strip()
            if serial and not re.fullmatch(r"[0-9A-Za-z]+", serial):
                raise ConfigError(
                    "[device] serial: expected the number printed on the "
                    "box, got %r" % serial
                )
            config.serial = serial
        elif section == "osc":
            _parse_osc(parser, section, config)
        elif section.startswith("route:"):
            config.routes.append(_parse_route(parser, section))
        elif section.startswith(("input:", "output:")):
            pending.append(section)
        elif section == "pin":
            _parse_pin(parser, section, config)
        elif section in global_families(device_for_name(config.device_name)):
            pending_globals.append(section)
        elif _is_nested_section(section, config):
            pending_nested.append(section)
        else:
            _warn_unknown_section(section, config)


def _modelled_names() -> str:
    """The devices whose register table has rows, for the warnings."""
    return ", ".join(d.name for d in DEVICES if d.registers)


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
            section, config.device_name, _modelled_names())
        return
    log.warning(
        "ignoring unknown section [%s] -- this config may have been "
        "written by a newer version of oscmix-desk (known: %s)",
        section, _known_sections(config))


def _check_device_channels(config: Config) -> None:
    """Reject channels the configured device does not have.

    ``CHANNEL_MIN..CHANNEL_MAX`` is 1..64 and says nothing about any
    particular interface, so ``output = 40/41`` parsed cleanly on a
    20-channel UCX II, was applied, and did nothing -- the exact shape of
    failure this project exists to prevent, since at message level the
    routing is perfect.

    Deliberately a separate pass rather than a check inside
    ``_parse_channels``: ``[device]`` may appear after the routes in the
    file, so the device is only known once every section has been read.
    It also keeps syntax ("is this a channel number") apart from
    capability ("does this device have it").

    An unmodelled device is *no opinion*, not an error. The 802 has never
    been tested here, and a model that rejected its channels would be
    guessing at hardware nobody can check.
    """
    device = device_for_name(config.device_name)
    if device is None:
        return
    for route in config.routes:
        kind, source = route.source
        for option, channels, capability in (
                (kind, source, kind),
                ("output", route.output, "output")):
            valid = device.channels_for(capability)
            if not valid:
                # Modelled, but this capability was never recorded --
                # the 802 is listed so the device dimension is real, and
                # declares nothing because guessing is how a model
                # becomes a lie. Being in the table is not an opinion.
                continue
            for channel in channels:
                if channel not in valid:
                    raise ConfigError(
                        "[route:%s] %s: channel %d does not exist on a %s "
                        "(it has %s %d..%d)"
                        % (route.name, option, channel, device.name,
                           capability, min(valid), max(valid))
                    )


def _check_link_agreement(routes: Sequence[Route]) -> None:
    """Reject routes that disagree on whether an output pair is linked.

    The stereo link is a property of the hardware pair, not of a route, so
    two routes feeding the same outputs cannot each have their own. Left
    unchecked the last link message wins while both routes still write
    their own mix shape, and the mismatched one silently loses a channel:
    a linked pair fed by the hard-panned pair of an unlinked route folds
    both messages onto the same register, and one output goes dead.
    """
    seen: Dict[Tuple[int, ...], Route] = {}
    for route in routes:
        if len(route.output) != 2:
            continue
        previous = seen.get(route.output)
        if previous is None:
            seen[route.output] = route
        elif previous.stereo != route.stereo:
            raise ConfigError(
                "[route:%s] and [route:%s] both drive output pair %s but "
                "disagree on 'stereo' (%s vs %s); the link is a property of "
                "the hardware pair, so it has to be the same for both"
                % (previous.name, route.name,
                   "/".join(map(str, route.output)),
                   str(previous.stereo).lower(), str(route.stereo).lower())
            )


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
                          device: object) -> List[ChannelSetting]:
    """Parse ``[eq:input:3]`` and the families that follow it.

    Produces ``ChannelSetting`` like a flat section does, with the option
    carrying the rest of the path -- ``eq/band1freq``, or ``eq`` for the
    sub-family's own switch. One settings type rather than two, because
    the plan consumes them identically and a second type would be a
    second loop to forget.
    """
    sub, family, raw = section.split(":")
    channel = int(raw)
    known = settable_nested(device, sub, family)  # type: ignore[arg-type]
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
                device, family):  # type: ignore[arg-type]
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


def _has_channel(device: object, family: str, sub: str, channel: int) -> bool:
    """Whether this device has that channel in that sub-family."""
    known = settable_nested(device, sub, family)  # type: ignore[arg-type]
    for register in known.values():
        return channel in device.channels_for(  # type: ignore[attr-defined]
            register.channels)
    return False


def _parse_global_section(parser: "configparser.ConfigParser", section: str,
                          device: object) -> List[GlobalSetting]:
    """Parse ``[echo]`` and the other channel-less families.

    Same rule as a channel section and for the same reason: which
    options exist and what values they take comes from the register
    model, not from a list kept here. A second list is a second place to
    disagree with the device.
    """
    known = settable_globals(device, section)  # type: ignore[arg-type]
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
                           family: str, device: object,
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

    known = settable_options(device, family)  # type: ignore[arg-type]
    if not known:
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
            "(modelled: %s)", section, device_name, family, _modelled_names())
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
        register = option_register(device, family, option,  # type: ignore[arg-type]
                                   channel)
        if register is None:
            valid = option_channels(device, family, option)  # type: ignore[arg-type]
            raise ConfigError(
                "[%s] %s: channel %d does not have it on a %s (it has %s "
                "on %d..%d)"
                % (section, option, channel,
                   device.name, option, min(valid), max(valid)))  # type: ignore[attr-defined]
        settings.append(ChannelSetting(
            family, channel, option,
            _parse_domain(parser.get(section, option), section, option,
                          register)))
    return settings


def _parse_domain(raw: str, section: str, option: str,
                  register: object) -> object:
    """Read a value according to the register's declared domain."""
    domain = register.domain           # type: ignore[attr-defined]
    if domain == BOOL:
        return 1 if _parse_bool(raw, section, option) else 0
    if domain == ENUM:
        choices = register.choices     # type: ignore[attr-defined]
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
                  register: object) -> float:
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


def profiles_dir(config_path: Optional[Path] = None) -> Optional[Path]:
    """Where profiles live: ``profiles/`` beside the routing config.

    Beside it rather than inside it, because a profile *is* a
    ``routing.conf`` -- complete, parsed by the same code, subject to
    the same compatibility rule (ADR 0006). A new section type for them
    would have meant a second format with a second set of promises, and
    ``--dump-config > profiles/tracking.conf`` would not compose.
    """
    base = config_path or discover_config_path()
    if base is None:
        return None
    return base.parent / "profiles"


def list_profiles(config_path: Optional[Path] = None) -> List[str]:
    """Profile names, sorted. Missing directory is empty, not an error."""
    directory = profiles_dir(config_path)
    if directory is None or not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.conf") if p.is_file())


def profile_path(name: str, config_path: Optional[Path] = None) -> Path:
    """The file a profile name refers to.

    Refuses a name that is not a plain identifier: profiles are selected
    on a command line and a path separator would let one escape the
    directory. Checked here rather than at each call site.
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ConfigError(
            "%r is not a profile name -- letters, digits, dot, dash and "
            "underscore, and it may not start with punctuation" % name)
    directory = profiles_dir(config_path)
    if directory is None:
        raise ConfigError("no config directory, so no profiles either")
    return directory / ("%s.conf" % name)
