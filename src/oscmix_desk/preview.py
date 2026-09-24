"""Compare profile declarations without reading or changing hardware state."""

from __future__ import annotations

from typing import List

from .model import Config
from .reconcile import PHASE_LINK, PHASE_MIX, desired


def transition_lines(previous: Config, target: Config) -> List[str]:
    """Describe omissions and link changes between two partial desks.

    Both maps come from the real planner, including duplicate crosspoint
    resolution. They are declarations, never a cached hardware snapshot.
    """
    before = {entry.path: entry for entry in desired(previous)}
    after = {entry.path: entry for entry in desired(target)}
    omitted = sorted(path for path, entry in before.items()
                     if entry.phase == PHASE_MIX and path.startswith("/mix/")
                     and path not in after)
    changed = sorted(path for path, entry in after.items()
                     if entry.phase == PHASE_LINK
                     and (path not in before or before[path].args != entry.args))
    lines = ["Comparing declarations only; this is not a complete hardware snapshot."]
    for path in omitted:
        lines.append("  not overwritten or explicitly muted by target: %s" % path)
    if not omitted:
        lines.append("  no previously declared route crosspoints omitted")
    if changed:
        lines.append("  target link declarations change: %s" % ", ".join(changed))
        lines.append("  link changes may also affect partner channels/crosspoints")
    lines.append("Unknown playback routes cannot be inferred from these files.")
    return lines
