"""The device's state as a config: ``--dump-config``.

``observed()`` rendered as config -- the inverse of ``mix_messages``, and
only as complete as the device is willing to report. ``/mix/<out>/input/
<in>`` comes back; the playback matrix does not (ADR 0002), so a dump
reproduces monitoring paths and cannot reproduce software routing.
Saying that loudly is the whole difference between a useful tool and one
that silently loses half a config.

Pure, like the reconciler it reads from: what was seen goes in, a
``Config``'s worth of routes and settings and their text come out.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .constants import LEVEL_MAX, LEVEL_MIN, UNLINKED_GAIN_OFFSET
from .model import (
    ChannelSetting,
    Config,
    GlobalSetting,
    Route,
    SettingValue,
)
from .numeric import finite, integer, render_number, report_value
from .osc import Args
from .reconcile import policy_for
from .registers import (
    BOOL,
    ENABLE_OPTION,
    ENUM,
    PIN,
    REESTABLISHED,
    Device,
    Register,
    global_families,
    nested_families,
    register_at,
    settable_globals,
    settable_nested,
    settable_option_rows,
)


def _linked(seen: Mapping[str, Args], family: str,
            channel: int) -> Optional[bool]:
    """A reported link, never an unlinked state inferred from silence."""
    odd = channel - (channel - 1) % 2
    args = seen.get("/%s/%d/stereo" % (family, odd))
    partner = seen.get("/%s/%d/stereo" % (family, odd + 1))
    if not args:
        return None
    try:
        value = integer(args[0])
        if value not in (0, 1) or (partner and integer(partner[0]) != value):
            return None
        return bool(value)
    except (TypeError, ValueError):
        return None


def _omitted(warnings: Optional[List[str]], path: str, reason: str) -> None:
    if warnings is not None:
        warnings.append(" ".join(("%s: %s; omitted" % (path, reason)).splitlines()))


def _mix_cell(parts: List[str], args: Args, device: Optional[Device]
              ) -> Optional[Tuple[int, int, Args]]:
    out, src = int(parts[2]), int(parts[4])
    if (device is None or out not in device.channels["output"]
            or src not in device.channels["input"]):
        raise ValueError("channel is outside the known device map")
    if not args:
        raise ValueError("missing mix level")
    if args[0] == float("-inf"):
        return None
    level = finite(args[0])
    if level <= LEVEL_MIN:
        return None
    # Unlinked stereo compensation can reach 6.0206 dB on OSC.
    if level > max(LEVEL_MAX, UNLINKED_GAIN_OFFSET) + 1e-5:
        raise ValueError("mix level outside the supported range")
    if len(args) < 2:
        raise ValueError("missing pan")
    pan = integer(args[1])
    if not -100 <= pan <= 100:
        raise ValueError("invalid pan")
    return out, src, (level, pan)


def _mix_entries(seen: Mapping[str, Args], device: Optional[Device],
                 warnings: Optional[List[str]]) -> Dict[Tuple[int, int], Args]:
    """Every reported input-matrix cell that is not muted."""
    entries: Dict[Tuple[int, int], Args] = {}
    for path, args in seen.items():
        parts = path.split("/")
        if len(parts) != 5 or parts[1] != "mix" or parts[3] != "input":
            continue
        try:
            cell = _mix_cell(parts, args, device)
            if cell is not None:
                out, src, value = cell
                entries[(out, src)] = value
        except (TypeError, ValueError) as exc:
            _omitted(warnings, path, str(exc))
    return entries


def channels_from_observed(seen: Mapping[str, Args],
                           device: Optional[Device] = None,
                           warnings: Optional[List[str]] = None
                           ) -> Tuple[ChannelSetting, ...]:
    """Channel state read back out of a dump, as config would express it.

    Only options a config can actually set: the register model's
    ``settable_options``. Anything else the device reports is state this
    project has no vocabulary for, and inventing one in the dump writer
    is how a config grows options nothing can parse.

    Written because ``render_config`` could format channel sections and
    nothing produced any -- the renderer was reachable only from tests.
    That is the same shape as the two defects 0.3.0 already
    fixed: a capability built, correct, and wired to nothing.
    """
    if device is None:
        return ()
    found: List[ChannelSetting] = []
    for family in ("input", "output"):
        # Rows, not names: one option can have several registers when
        # the device's limits differ per channel, and a dict keyed by
        # name keeps only the last -- which dropped /input/1-2/gain out
        # of every dump, because the surviving row covered 3-4.
        wanted = list(settable_option_rows(device, family))
        for sub in nested_families(device, family):
            for option, register in settable_nested(device, sub, family).items():
                key = sub if option == ENABLE_OPTION else "%s/%s" % (sub, option)
                wanted.append((key, register))
        for option, register in sorted(wanted, key=lambda row: row[0]):
            for channel in device.channels.get(register.channels, ()):
                args = seen.get(register.path(ch=channel))
                if args is not None:
                    try:
                        value = _config_value(register, args)
                        found.append(ChannelSetting(family, channel, option, value))
                    except (TypeError, ValueError) as exc:
                        _omitted(warnings, register.path(ch=channel), str(exc))
    return tuple(found)


def globals_from_observed(seen: Mapping[str, Args],
                          device: Optional[Device] = None,
                          warnings: Optional[List[str]] = None
                          ) -> Tuple[GlobalSetting, ...]:
    """Channel-less settings read back out of a dump.

    The counterpart to `channels_from_observed` for the families with no
    channel dimension. Without it a dump reads as though the device had
    no echo, reverb, control room, clock or hardware settings at all --
    42 registers the device reports and the file does not mention.
    """
    if device is None:
        return ()
    found: List[GlobalSetting] = []
    for family in global_families(device):
        for option, register in sorted(settable_globals(device, family).items()):
            args = seen.get(register.template)
            if args is not None:
                try:
                    value = _config_value(register, args)
                    found.append(GlobalSetting(family, option, value))
                except (TypeError, ValueError) as exc:
                    _omitted(warnings, register.template, str(exc))
    return tuple(found)


def _unnameable(register: "Register", value: SettingValue) -> bool:
    """Whether an enum value is one this backend cannot put a name to.

    Written for `/controlroom/mainout`, which reported -1 for "no main
    out" with no name attached: as `mainout = -1` it produced a file
    that would not load -- a dump of a working device that refuses to be
    a config. The round trip caught it, which is what the round trip is
    for.

    That case is gone: the pin moved to 55802a6 and e8151cd gave enums a
    value list, so -1 now arrives as "None". This stays because the
    situation is not specific to that register -- any enum reporting a
    value the declared names do not cover would produce the same
    unloadable file -- and because the alternative is finding out again
    on somebody's desk. `tests/test_dump_sections.py` constructs the
    case rather than relying on a device to produce it.
    """
    return register.domain == ENUM and value not in register.choices


def _config_value(register: "Register", args: Args) -> SettingValue:
    """One reported register as the value a config would carry.

    Enums report ``(index, name)`` and a config writes the name; booleans
    report an int and a config writes true/false. Getting this wrong is
    silent -- the dump looks fine and the file it produces sets something
    else -- so the round trip is asserted in tests/test_pin_remember.py.
    """
    if not args:
        raise ValueError("missing scalar value")
    report_value(args[0], register)
    if register.domain == ENUM:
        wire = register.values or tuple(range(len(register.choices)))
        name = register.choices[wire.index(integer(args[0]))]
        if len(args) > 1 and args[1] != name:
            raise ValueError("enum name disagrees with its wire value")
        return name if len(args) > 1 else args[0]
    if register.domain == BOOL:
        return bool(args[0])
    return args[0]


def routes_from_observed(seen: Mapping[str, Args],
                         device: Optional[Device] = None,
                         warnings: Optional[List[str]] = None) -> Tuple[Route, ...]:
    """Reconstruct the routes a device's reported state implies.

    Deterministic in name and order, because the round trip has to be a
    fixed point: dumping, applying and dumping again must produce the
    same file, and a name derived from anything but the channels would
    not survive that.

    Only what the dump carries. See ``unrecoverable`` for the rest.
    """
    entries = _mix_entries(seen, device, warnings)
    routes = []
    claimed = set()
    for (out, src) in sorted(entries):
        if (out, src) in claimed:
            continue
        cell = entries[(out, src)]
        level = finite(cell[0])
        pan = integer(cell[1])
        out_linked = _linked(seen, "output", out)
        in_linked = _linked(seen, "input", src)
        path = "/mix/%d/input/%d" % (out, src)
        if out_linked is None or in_linked is None:
            _omitted(warnings, path, "missing or contradictory stereo-link report")
            continue
        pair = (device is not None and out % 2 == src % 2 == 1
                and out + 1 in device.channels["output"]
                and src + 1 in device.channels["input"])

        if pair and out_linked and in_linked and pan == 0 and level <= LEVEL_MAX:
            # A linked pair folds onto its odd channel, and the register
            # is the pair's. pan 0 is a plain stereo pass-through.
            routes.append(Route(name="in%d-%d-out%d-%d" % (src, src + 1, out, out + 1),
                                input=(src, src + 1), output=(out, out + 1),
                                level=round(level, 1)))
            claimed.add((out, src))
        elif (pair and not out_linked and in_linked and pan == -100
              and (out + 1, src) in entries
              and integer(entries[(out + 1, src)][1]) == 100
              and abs(finite(entries[(out + 1, src)][0]) - level) < .0001
              and LEVEL_MIN < round(level - UNLINKED_GAIN_OFFSET, 1) <= 0):
            # The hard-panned pair an unlinked route writes. oscmix
            # halved the gain on the way in, so the 6 dB compensation
            # comes back off to recover the `level` the config asked for.
            routes.append(Route(name="in%d-%d-out%d-%d-split" % (src, src + 1, out, out + 1),
                                input=(src, src + 1), output=(out, out + 1),
                                level=round(level - UNLINKED_GAIN_OFFSET, 1),
                                stereo=False))
            claimed.update({(out, src), (out + 1, src)})
        elif pan == 0 and not out_linked and not in_linked and level <= LEVEL_MAX:
            routes.append(Route(name="in%d-out%d" % (src, out),
                                input=(src,), output=(out,),
                                level=round(level, 1)))
            claimed.add((out, src))
        else:
            _omitted(warnings, path,
                     "pan, balance or channel pairing is not expressible as a route")
    return tuple(routes)


def unrecoverable(device: Optional[Device] = None) -> Tuple[str, ...]:
    """Register families a dump cannot reproduce, and why it matters.

    Read from the register model rather than listed here, so a family
    that becomes reportable stops being an excuse the moment the
    recording says so.
    """
    if device is None:
        return ()
    return tuple(r.template for r in device.registers
                 if r.verify == REESTABLISHED)


def render_config(config: Config, device: Optional[Device] = None,
                  warnings: Sequence[str] = ()) -> str:
    """Render the representable subset, with all observed omissions visible.

    PIN settings are active; REMEMBER settings are comments so a dump
    does not claim ownership of live controls. Uncommenting declares an
    initial value without changing that register's reconciliation policy.
    """
    missing = unrecoverable(device)
    lines = [
        "# Generated by oscmix-session --dump-config.",
        "#",
        "# This is what the device reported, not everything it is doing.",
    ]
    if warnings:
        lines += ["#", "# INCOMPLETE EXPORT -- reported state omitted:"]
        lines += ["#   " + warning for warning in warnings]
        lines += ["# Keep --snapshot for the raw state. Merge, do not replace."]
    if missing:
        lines += [
            "#",
            "# NOT IN HERE, because the device does not report it:",
        ]
        lines += ["#   %s" % t for t in missing]
        lines += [
            "#",
            "# A `/mix` write to the playback matrix draws no reply and the",
            "# state dump omits it, so software routing cannot be read back",
            "# -- only re-established from a config. If you had playback",
            "# routes, they are not below and this file will not restore",
            "# them. Merge, do not replace.",
        ]
    lines += [
        "#",
        "# Values are emitted as config where the register model pins them,",
        "# and commented out where it remembers them -- a dump cannot tell",
        "# 'I meant this' from 'this is where I left it', so for anything a",
        "# person turns during a session it does not decide. Uncommenting",
        "# declares an initial value; it does not change the policy. Only",
        "# flat input/output options support overrides in [pin]. See",
        "# docs/decisions/0012-pin-and-remember.md.",
        "",
        "[device]",
        "name = %s" % config.device_name,
        "usb-id = %s" % config.usb_id,
        "",
        "[osc]",
        "port = %d" % config.osc_port,
        "",
    ]
    if not config.routes:
        lines += ["# No representable input route was recovered. This does not",
                  "# prove that direct monitoring is absent; see --snapshot."]
    for route in config.routes:
        kind, source = route.source
        lines += [
            "[route:%s]" % route.name,
            "%s = %s" % (kind, "/".join(map(str, source))),
            "output = %s" % "/".join(map(str, route.output)),
            "level = %.1f" % route.level,
        ]
        if not route.stereo:
            lines.append("stereo = false")
        lines.append("")
    lines += _global_sections(config, device)
    lines += _channel_sections(config, device)
    return "\n".join(lines).rstrip() + "\n"


def _channel_sections(config: Config,
                      device: Optional[Device]) -> List[str]:
    """``[input:N]`` / ``[output:N]`` blocks for the observed channel state.

    Pinned options become config lines, remembered ones become comments
    carrying the same value. A section whose every option is remembered
    is still emitted: seeing what the device holds is most of why anyone
    runs a dump, and hiding it would make the file look like the channel
    had no state at all.
    """
    # Grouped by the section a setting belongs in, not by channel: a
    # nested option goes to `[eq:input:3]` and a flat one to `[input:3]`
    # (ADR 0014), so one channel produces several sections and they must
    # not be run together.
    by_section: Dict[Tuple[str, str, int], List[ChannelSetting]] = {}
    for setting in config.channels:
        sub = setting.option.split("/", 1)[0] if "/" in setting.option else ""
        if not sub and setting.option in nested_families(device,
                                                         setting.family):
            sub = setting.option          # the sub-family's own switch
        by_section.setdefault((sub, setting.family, setting.channel),
                              []).append(setting)
    lines: List[str] = []
    for (sub, family, channel), settings in sorted(by_section.items()):
        header = ("[%s:%s:%d]" % (sub, family, channel) if sub
                  else "[%s:%d]" % (family, channel))
        lines.append(header)
        for setting in sorted(settings, key=lambda s: s.option):
            path = "/%s/%d/%s" % (family, channel, setting.option)
            name = (ENABLE_OPTION if setting.option == sub
                    else setting.option.split("/", 1)[-1])
            lines.append(_setting_line(name, setting.value, path, device))
        lines.append("")
    return lines


def _global_sections(config: Config, device: Optional[Device]) -> List[str]:
    """`[echo]` and the other channel-less families."""
    by_family: Dict[str, List[GlobalSetting]] = {}
    for setting in config.globals:
        by_family.setdefault(setting.family, []).append(setting)
    lines: List[str] = []
    for family, settings in sorted(by_family.items()):
        lines.append("[%s]" % family)
        for setting in sorted(settings, key=lambda s: s.option):
            lines.append(_setting_line(setting.option, setting.value,
                                       setting.path, device))
        lines.append("")
    return lines


def _setting_line(name: str, value: SettingValue, path: str,
                  device: Optional[Device]) -> str:
    """One config line, live or commented, with the reason for commenting."""
    register = register_at(device, path)
    try:
        rendered = _render_value(value, register)
    except (TypeError, ValueError) as exc:
        return "# %s omitted: invalid reported value (%s)" % (name, exc)
    entry = "%s = %s" % (name, rendered)
    if register is not None and _unnameable(register, value):
        return ("# %s   # this backend reports no name for it; see "
                "michaelforney/oscmix#30" % entry)
    if policy_for(path, device) == PIN:
        return entry
    return "# %s   # remembered: the device's value wins" % entry


def _render_value(value: SettingValue,
                  register: "Optional[Register]" = None) -> str:
    """A value as the config file spells it.

    Keyed off the declared domain rather than the Python type, for the
    same reason `_encode` is: a bool arrives from the device as `True`
    and from the parser as `1` (the wire form), and a renderer that
    asked `isinstance` wrote `true` the first time and `1` the second.
    Both parse, so nothing failed -- the dump of a dump just quietly
    stopped matching the dump. The fixed-point test caught it.
    """
    if register is not None and register.domain == BOOL:
        return "true" if value else "false"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return render_number(value, register)
    return str(value)
