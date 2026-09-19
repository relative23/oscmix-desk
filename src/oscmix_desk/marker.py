"""Which profile is in effect, remembered beside the config.

One line in ``active-profile``: written by the CLI after an applied
switch, removed by ``--no-profile``, and only ever *read* by the
session, which is what keeps the unit's ``ProtectHome=read-only`` true
(ADR 0018). Written through a temporary file of its own name and a
rename, and synced with its directory, so that a reader sees the old
name or the new one and a power cut cannot bring a half of either back.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional

from .errors import ConfigError
from .log import log
from .paths import profile_path

#: Where the active profile's name is kept: one line, beside
#: routing.conf. Written by the CLI after an applied switch, removed by
#: `--no-profile`, and only ever *read* by the session -- which is what
#: keeps the unit's ProtectHome=read-only true. Beside the config rather
#: than in a state directory, so that `--config` selects the profiles
#: and the marker together (ADR 0018).
ACTIVE_MARKER = "active-profile"


def active_profile_path(config_path: Optional[Path]) -> Optional[Path]:
    """The marker file for this config, or None without a config."""
    if config_path is None:
        return None
    return Path(config_path).parent / ACTIVE_MARKER


def active_profile(config_path: Optional[Path] = None) -> Optional[str]:
    """The remembered profile name, or None.

    A marker whose content is not a profile name is ignored with a
    warning rather than trusted: the name is used to build a path.
    """
    path = active_profile_path(config_path)
    if path is None or not path.is_file():
        return None
    try:
        name = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("ignoring %s: %s", path, exc)
        return None
    if not name:
        return None
    try:
        profile_path(name, config_path)
    except ConfigError as exc:
        log.warning("ignoring %s: %s", path, exc)
        return None
    return name


class Marked(NamedTuple):
    """What a change of the marker achieved.

    ``in_effect`` decides whether the unit may be reloaded (ADR 0019).
    ``durable`` is the smaller promise: the directory was synced, so a
    power cut cannot bring the previous marker back. Until 0.6.11 that
    was a log line and the answer was a single bool that read as both.
    """

    in_effect: bool
    durable: bool


def remember_active_profile(name: str, config_path: Optional[Path]) -> Marked:
    """Record a switch that was applied; a warning when it cannot.

    Not an outcome state: the device already has the profile, and a
    fourth state for "applied but forgotten" would be the "applied, but
    the flag says otherwise" case ADR 0011 forbids.
    """
    path = active_profile_path(config_path)
    if path is None:
        return Marked(False, False)
    # Written beside and renamed over, never in place: a crash or a
    # power loss between open and close would otherwise leave an empty
    # marker, and an empty marker reads as "no profile" -- the choice
    # silently gone on the next start. The old marker stays whole until
    # the new one is complete on disk, and the rename is atomic.
    # A name of its own: two switches that hold different device locks
    # -- two profiles naming two backends -- shared `active-profile.tmp`,
    # and one could rename the file the other was still writing (0.6.11).
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                   dir=path.parent)
        try:
            os.fchmod(fd, 0o644)
            data = (name + "\n").encode("utf-8")
            written = 0
            while written < len(data):
                # write(2) may write less than it was given without
                # failing. A short write here would fsync and rename a
                # truncated profile name over a correct marker.
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("profile %r applied but not remembered: cannot write "
                    "%s (%s); the desk holds until the next reload or "
                    "start, which applies routing.conf", name, path, exc)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return Marked(False, False)
    durable = _fsync_directory(path.parent)
    if not durable:
        log.warning("profile %r remembered, but %s could not be synced: "
                    "the marker is in effect now and may not survive a "
                    "power cut", name, path.parent)
    return Marked(True, durable)


def _fsync_directory(directory: Path) -> bool:
    """Make a rename or an unlink durable; False when it could not be.

    Some filesystems refuse the fsync of a directory, which is why this
    never raises. It says so now rather than swallowing it: without it
    the rename is visible but not durable, and a power cut can bring
    the previous marker back (0.6.6).
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def forget_active_profile(config_path: Optional[Path]) -> Marked:
    """Remove the marker; nothing to remove is not an error.

    Not ``in_effect`` when it is still there afterwards, which the caller
    carries in the outcome: a reload sent then would re-apply the profile the
    marker still names and undo the restore (ADR 0019).
    """
    path = active_profile_path(config_path)
    if path is None:
        return Marked(True, True)
    try:
        path.unlink()
    except FileNotFoundError:
        return Marked(True, True)
    except OSError as exc:
        log.warning("cannot remove %s (%s); the desk holds until the next "
                    "reload or start, which applies the profile it names",
                    path, exc)
        return Marked(False, False)
    durable = _fsync_directory(path.parent)
    if not durable:
        log.warning("marker removed, but %s could not be synced: the profile "
                    "is out of effect now and may come back after a power "
                    "cut", path.parent)
    return Marked(True, durable)
