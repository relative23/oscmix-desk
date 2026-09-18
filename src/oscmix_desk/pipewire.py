"""Generating named PipeWire sinks from the same routing config."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config, Route
from .errors import ConfigError

# Fallback mapping when the sink's real channel layout is unknown: how
# PipeWire labels the device playback channels in the surround profile.
SURROUND_POSITIONS = {1: "FL", 2: "FR", 3: "RL", 4: "RR",
                      5: "FC", 6: "LFE", 7: "SL", 8: "SR"}


def pipewire_positions(channels: Sequence[int],
                       sink_positions: Optional[Sequence[str]] = None
                       ) -> List[str]:
    """Map device playback channels to the target sink's channel labels.

    With the sink's real ``audio.position`` array (from pw-dump), channel
    N is simply positions[N-1] -- correct for both the surround profile
    (FL FR ... SR) and the pro-audio/Direct profile (AUX0 AUX1 ...).
    Without it, fall back to the 7.1 surround table.
    """
    positions = []
    for channel in channels:
        if sink_positions:
            if not 1 <= channel <= len(sink_positions):
                raise ConfigError(
                    "channel %d exceeds the %d channels of the target sink"
                    % (channel, len(sink_positions))
                )
            positions.append(sink_positions[channel - 1])
        elif channel in SURROUND_POSITIONS:
            positions.append(SURROUND_POSITIONS[channel])
        else:
            raise ConfigError(
                "cannot map channel %d without knowing the sink layout; "
                "channels above 8 need pw-dump auto-detection" % channel
            )
    return positions


def _parse_positions(raw: object) -> Optional[List[str]]:
    """audio.position from pw-dump: either a list or '[ AUX0, AUX1 ]'."""
    if isinstance(raw, list):
        return [str(item) for item in raw] or None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.strip().strip("[]").split(",")]
        return [p for p in parts if p] or None
    return None


def pw_dump_objects(dump_text: Optional[str] = None
                    ) -> Optional[List[Dict[str, Any]]]:
    """``pw-dump``'s objects, or None when they cannot be had.

    None covers pw-dump missing, failing, and printing something that is
    not a JSON list -- everything that is "PipeWire could not be asked"
    rather than "PipeWire has no such sink", which a caller has to tell
    apart (0.6.10). ``dump_text`` stands in for running it.
    """
    if dump_text is None:
        pw_dump = shutil.which("pw-dump")
        if not pw_dump:
            return None
        try:
            dump_text = subprocess.run(
                [pw_dump], capture_output=True, text=True, timeout=10,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        objects = json.loads(dump_text)
    except ValueError:
        return None
    if not isinstance(objects, list):
        return None
    return [obj for obj in objects if isinstance(obj, dict)]


def pw_sink_info(device_name: str, target: Optional[str] = None,
                 dump_text: Optional[str] = None
                 ) -> Optional[Tuple[str, Optional[List[str]]]]:
    """(node.name, channel positions) of the device's sink, via pw-dump.

    With ``target`` given, look that node up; otherwise search for a sink
    matching the configured device name (or "fireface").
    """
    objects = pw_dump_objects(dump_text)
    return None if objects is None else find_sink(objects, device_name,
                                                  target)


def find_sink(objects: List[Dict[str, Any]], device_name: str,
              target: Optional[str] = None
              ) -> Optional[Tuple[str, Optional[List[str]]]]:
    """pw_sink_info over objects already read."""
    needle = device_name.lower()
    for obj in objects:
        # `info` is null for an object pw-dump saw go away (its monitor
        # mode prints those), and nothing promises `props` is a mapping.
        # `.get` on either raised AttributeError until 0.6.11.
        info = obj.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if not isinstance(props, dict):
            continue
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name")
        if not isinstance(name, str):
            continue  # a sink without a node name cannot be a link target
        if target is not None:
            if name != target:
                continue
        else:
            haystack = " ".join(
                str(props.get(key, ""))
                for key in ("node.description", "node.name", "node.nick",
                            "alsa.card_name")
            ).lower()
            if needle not in haystack and "fireface" not in haystack:
                continue
        return name, _parse_positions(props.get("audio.position"))
    return None


_LOOPBACK_TEMPLATE = """\
    {{ name = libpipewire-module-loopback
        args = {{
            node.description = "{description}"
            capture.props = {{
                node.name = "oscmix.{node}"
                media.class = Audio/Sink
                audio.position = [ FL FR ]
            }}
            playback.props = {{
                node.name = "oscmix.{node}.out"
                audio.position = [ {device_positions} ]
                target.object = "{target}"
                stream.dont-remix = true
                node.passive = true
            }}
        }}
    }}"""


def generate_pipewire_conf(config: Config, target: Optional[str],
                           sink_positions: Optional[Sequence[str]] = None
                           ) -> str:
    """One named virtual sink per stereo route, feeding the device
    playback channels of the route's output pair."""
    # One sink per unique output pair; several routes can feed the same
    # outputs (e.g. a default-sink route plus the identity route for the
    # named sink), and the first route's name wins.
    pair_routes: List[Route] = []
    dropped: List[Tuple[Route, str]] = []
    seen_pairs: Dict[Tuple[int, ...], str] = {}
    for route in config.routes:
        if len(route.output) != 2:
            continue
        if route.output in seen_pairs:
            dropped.append((route, seen_pairs[route.output]))
        else:
            pair_routes.append(route)
            seen_pairs[route.output] = route.name
    if not pair_routes:
        raise ConfigError(
            "no stereo pair routes configured; nothing to generate"
        )
    if target is None:
        target = "FIXME-fireface-sink-node-name"
    identity_pairs = {r.output for r in config.routes if r.playback == r.output}
    parts = [
        "# PipeWire named sinks, generated by oscmix-session --pipewire-sinks",
        "# Install to ~/.config/pipewire/pipewire.conf.d/oscmix-sinks.conf,",
        "# then restart: systemctl --user restart pipewire wireplumber",
        "",
    ]
    for route, kept in dropped:
        parts.append(
            "# NOTE: route %r also targets outputs %s; one sink per output "
            "pair,\n# so the sink is named after route %r.\n"
            % (route.name, "/".join(map(str, route.output)), kept)
        )
    for route in pair_routes:
        if route.output not in identity_pairs:
            pair = "/".join(map(str, route.output))
            parts.append(
                "# NOTE: sink %r feeds device playback channels %s, which "
                "need an\n# identity route in routing.conf to reach the "
                "outputs:\n#   [route:%s-direct]\n#   playback = %s\n"
                "#   output = %s\n"
                % (route.name, pair, route.name, pair, pair)
            )
    parts.append("context.modules = [")
    for route in pair_routes:
        parts.append(_LOOPBACK_TEMPLATE.format(
            # The description is a quoted string in the conf; a quote in
            # the route name -- which routing.conf accepts -- broke it.
            description=route.name.replace("\\", "\\\\").replace('"', '\\"'),
            node=re.sub(r"[^\w.-]", "_", route.name),
            device_positions=" ".join(
                pipewire_positions(route.output, sink_positions)),
            target=target,
        ))
    parts.append("]")
    return "\n".join(parts) + "\n"
