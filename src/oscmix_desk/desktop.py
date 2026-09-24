"""Inspect the existing upstream GTK mixer and its saved OSC connection."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Dict, Optional

from .diagnostics import query
from .discovery import resolve_binary
from .model import Config


@dataclass(frozen=True)
class DesktopStatus:
    """Desktop prerequisites; no settings are changed by inspection."""

    binary: Optional[str]
    problem: Optional[str]
    connection: Dict[str, object] = field(default_factory=dict)


def inspect_desktop(config: Optional[Config] = None,
                    binary: Optional[str] = None) -> DesktopStatus:
    """Use GSettings itself so the compiled schema and saved values agree."""
    binary = binary or resolve_binary("oscmix-gtk", "OSCMIX_BIN_GTK")
    if binary is None:
        return DesktopStatus(None, "oscmix-gtk is not installed; install the GTK companion")
    try:
        connection = _connection(query(["gsettings", "list-recursively", "oscmix"]))
    except (OSError, ValueError) as exc:
        return DesktopStatus(binary, "cannot read GTK schema/connection: %s" % exc)
    if config is not None:
        expected = {"send-host": "127.0.0.1", "send-port": config.osc_port,
                    "recv-host": "127.0.0.1", "recv-port": config.osc_recv_port}
        different = [key for key in expected if connection[key] != expected[key]]
        if different:
            corrections = "; ".join("gsettings set oscmix %s %s" % (key, expected[key])
                                    for key in different)
            return DesktopStatus(binary, "GTK connection differs from this desk: " +
                                 corrections + " (review and run explicitly)", connection)
    return DesktopStatus(binary, None, connection)


def _connection(text: str) -> Dict[str, object]:
    values: Dict[str, object] = {}
    keys = {"send-host", "recv-host", "send-port", "recv-port"}
    for line in text.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3 or parts[0] != "oscmix" or parts[1] not in keys:
            continue
        _schema, key, raw = parts
        if key in values:
            raise ValueError("duplicate GTK connection key: " + key)
        if key.endswith("port"):
            raw = raw.removeprefix("uint32 ")
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError) as exc:
            raise ValueError("invalid GTK value: " + key) from exc
        if key.endswith("host"):
            if not isinstance(value, str) or not value:
                raise ValueError("invalid GTK host: " + key)
        elif type(value) is not int or not 1 <= value <= 65535:
            raise ValueError("invalid GTK port: " + key)
        values[key] = value
    if set(values) != keys:
        raise ValueError("GTK connection schema is missing required keys")
    return values
