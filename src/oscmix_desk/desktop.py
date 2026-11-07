"""Inspect the matching upstream GTK companion without opening its mixer."""

from __future__ import annotations

from dataclasses import dataclass, field

from .diagnostics import query
from .discovery import resolve_binary


@dataclass(frozen=True)
class DesktopStatus:
    """Desktop prerequisites; inspection changes no connection settings."""

    binary: str | None
    problem: str | None
    connection: dict[str, object] = field(default_factory=dict)


def inspect_desktop(binary: str | None = None) -> DesktopStatus:
    """The companion's version-only command opens no GTK display or socket."""
    binary = binary or resolve_binary("oscmix-gtk", "OSCMIX_BIN_GTK")
    if binary is None:
        return DesktopStatus(None, "oscmix-gtk is not installed; install the GTK companion")
    try:
        protocol = query([binary, "--control-version"])
    except OSError as exc:
        return DesktopStatus(binary, "cannot identify the coordinated GTK companion: %s" % exc)
    if protocol != "ODK1":
        return DesktopStatus(binary, "GTK companion has an incompatible control protocol")
    return DesktopStatus(binary, None, {"protocol": protocol,
                         "endpoint": "selected and checked by oscmix-launch"})
