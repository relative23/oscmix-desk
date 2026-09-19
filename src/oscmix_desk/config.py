"""routing.conf, read: the parser that turns a file into a ``Config``.

Parsing is total -- every input yields a Config or a ConfigError that
names the section and option, never a traceback. ``[device]`` and
``[osc]``, ``[route:*]`` and ``[pin]`` are read here; the sections the
register table declares are read by ``sections``.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .constants import (
    CHANNEL_MAX,
    CHANNEL_MIN,
    DEFAULT_DEVICE_NAME,
    LEVEL_MAX,
    LEVEL_MIN,
)
from .devices import device_for_name
from .errors import ConfigError
from .model import Config, Machine, Route
from .registers import POLICIES, global_families, settable_options
from .sections import (
    _is_nested_section,
    _parse_bool,
    _parse_channel_section,
    _parse_global_section,
    _parse_nested_section,
    _warn_unknown_section,
)


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


def load_config(path: Optional[Path],
                base: Optional[Config] = None) -> Config:
    """Load routing.conf. ``path=None`` returns built-in defaults.

    ``base`` is what the file is read onto, a fresh ``Config`` by
    default. A profile is read onto the machine settings of its main
    config, so that ``[device] name`` is known *while* the profile is
    validated: read onto the defaults and patched afterwards, a profile
    was checked against the UCX II whatever the desk was for (0.6.11).
    """
    config = Config() if base is None else base
    if path is None:
        # No file resolves to the defaults, and that is a record like any
        # other: which interface the (empty) desk was checked for.
        config.loaded = _machine_of(config)
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
    config.loaded = _machine_of(config)
    return config


def _machine_of(config: Config) -> Machine:
    return Machine(config.device_name, config.usb_id, config.serial,
                   config.osc_port, config.osc_recv_port)


def _dispatch(parser: "configparser.ConfigParser", config: "Config",
              pending: List[str], pending_globals: List[str],
              pending_nested: List[str]) -> None:
    """Route each section to its parser, or warn that we do not know it.

    Channel and nested sections are only *collected* here: both need the
    device, and `[device]` may appear anywhere in the file.
    """
    # [device] first, wherever it stands in the file: which model a
    # `[clock]` or a `[pin]` line is checked against depends on it, and
    # until 0.6.10 a section above `[device]` was checked against the
    # default model -- accepted or refused for the wrong reason.
    ordered = sorted(parser.sections(), key=lambda s: s != "device")
    for section in ordered:
        if section == "device":
            _check_options(section, "device", parser.options(section))
            config.device_name = parser.get(section, "name",
                                            fallback=config.device_name).strip()
            if not config.device_name:
                # The name is a substring match, and the empty string is
                # a substring of every name. Measured on the start path:
                # with one MIDI-capable card `name =` selected it, with
                # two -- a USB keyboard beside the interface -- it was
                # "2 interfaces match ''" with `serial` as the remedy, and
                # either way the desk had no model, so nothing in it was
                # checked. It worked by accident; it is refused (0.6.11,
                # ADR 0006).
                raise ConfigError(
                    "[device] name: expected the interface's name, for "
                    "example %r; an empty name matches every card that "
                    "has a MIDI port" % DEFAULT_DEVICE_NAME)
            usb_id = parser.get(section, "usb-id", fallback=config.usb_id).strip()
            if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", usb_id):
                raise ConfigError(
                    "[device] usb-id: expected 'vvvv:pppp' hex format, got %r" % usb_id
                )
            config.usb_id = usb_id.lower()
            serial = parser.get(section, "serial",
                                fallback=config.serial).strip()
            # Digits, as RME prints them and as the sequencer client name
            # carries them: a serial the selection can never match would
            # only turn into a start that waits and fails (0.6.9).
            if serial and not re.fullmatch(r"\d{4,}", serial):
                raise ConfigError(
                    "[device] serial: expected the number printed on the "
                    "box (digits only), got %r" % serial
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
