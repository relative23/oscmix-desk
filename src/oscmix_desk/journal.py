"""What desk last wrote to each register of one interface, kept beside its lock.

The device reports most of its state, but not the playback matrix, the playback link flags or
playback mute: those can only be re-established. Every write the backend acknowledges is
collected by the operation that sent it and stored after each apply and when the operation
closes, still under the device lock: ``<lock directory>/<key>.written.<uid>.json``. Normally
that directory is ``/run/oscmix-desk``, so the journal describes this boot; the first apply
after a boot fills it again. Only the shared or runtime lock directory holds a journal --
never the config directory, which the unit cannot write.

One file per user, because the shared directory is sticky: nobody may replace a file another
user owns, and a desk run with sudo would otherwise have frozen the unit's journal. A reader
merges every user's file and takes, per register, the latest write.

An entry says what desk sent and the backend acknowledged, and when. It never says the
hardware holds that value: TotalMix, the GTK mixer or the front panel may have changed it
since. A journal that cannot be read or written costs a warning, never an operation.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .locking import SWITCH_LOCK, device_lock_path
from .log import log
from .osc import Args, Value

SCHEMA = 1
#: Far above one desk (every register of a UCX II is about 2300), small enough to read whole.
MAX_ENTRIES = 20000
MAX_BYTES = 8 * 1024 * 1024
FILE_MODE = 0o660
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"

#: path -> (OSC type tags, arguments)
Written = Dict[str, Tuple[str, Args]]
#: path -> (OSC type tags, arguments, UTC time of the write)
Journal = Dict[str, Tuple[str, Args, str]]


def journal_path(config_path: Optional[Path], key: str,
                 uid: Optional[int] = None, kind: str = "written") -> Optional[Path]:
    """This user's journal beside the interface's lock; None outside a runtime lock directory.

    ``kind`` names the file: ``written`` for the journal, ``seen`` for the record beside it.
    """
    lock = device_lock_path(config_path, key)
    if lock is None or not key or lock.name == SWITCH_LOCK:
        return None
    return lock.with_name("%s.%s.%d.json" % (key, kind, os.getuid() if uid is None else uid))


def journal_paths(config_path: Optional[Path], key: str,
                  kind: str = "written") -> Tuple[Path, ...]:
    """Every user's journal of this interface."""
    own = journal_path(config_path, key, kind=kind)
    if own is None:
        return ()
    try:
        names = os.listdir(own.parent)
    except OSError:
        return ()
    pattern = re.compile(r"%s\.%s\.\d+\.json" % (re.escape(key), re.escape(kind)))
    return tuple(own.parent / name for name in sorted(names) if pattern.fullmatch(name))


def stamp(seconds: float) -> str:
    """A time as the journal stores it: UTC, microseconds, so users' files merge by it."""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(STAMP)


def stamp_seconds(text: str) -> Optional[float]:
    """The time a stored stamp names, or None for one in another format."""
    try:
        return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def read_journals(config_path: Optional[Path], key: str) -> Journal:
    """Every user's recorded writes, merged: per register the latest one."""
    merged: Journal = {}
    for path in journal_paths(config_path, key):
        for name, entry in read_journal(path).items():
            if name not in merged or entry[2] >= merged[name][2]:
                merged[name] = entry
    return merged


def _encode(tags: str, args: Args) -> List[object]:
    """JSON values for the arguments; non-finite floats as their names, which JSON lacks."""
    encoded: List[object] = []
    for tag, value in zip(tags, args):
        if tag == "f" and isinstance(value, float) and not math.isfinite(value):
            encoded.append(repr(value))
        else:
            encoded.append(value)
    return encoded


def _decode(tags: object, values: object) -> Args:
    if (not isinstance(tags, str) or not isinstance(values, list)
            or len(tags) != len(values) or not tags):
        raise ValueError("malformed arguments")
    decoded: List[Value] = []
    for tag, value in zip(tags, values):
        if tag == "f" and isinstance(value, (int, float, str)) and not isinstance(value, bool):
            decoded.append(float(value))
        elif (tag == "i" and type(value) is int) or (tag == "s" and isinstance(value, str)):
            decoded.append(value)
        else:
            raise ValueError("argument does not match its type tag")
    return tuple(decoded)


def _parse(raw: bytes) -> Journal:
    """A sound journal document, or ValueError naming what is wrong with it."""
    data = json.loads(raw)
    if type(data) is not dict or data.get("schema") != SCHEMA:
        raise ValueError("unknown journal schema")
    entries = data.get("registers")
    if type(entries) is not dict or len(entries) > MAX_ENTRIES:
        raise ValueError("malformed register table")
    journal: Journal = {}
    for name, row in entries.items():
        if (not name.startswith("/") or type(row) is not dict
                or not isinstance(row.get("at"), str)):
            raise ValueError("malformed entry %r" % name)
        tags = row.get("tags")
        journal[name] = (str(tags), _decode(tags, row.get("args")), row["at"])
    return journal


def _bounded(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("not a bounded regular file")
        return stream.read(MAX_BYTES + 1)


def read_journal(path: Optional[Path]) -> Journal:
    """Every recorded write; empty, with a warning, for anything but a sound journal."""
    if path is None:
        return {}
    try:
        journal = _parse(_bounded(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring the write journal %s: %s", path, exc)
        return {}
    return journal


def record_written(path: Optional[Path], written: Mapping[str, Tuple[str, Args]],
                   now: Optional[float] = None) -> bool:
    """Add one operation's acknowledged writes, replacing the file atomically.

    Called with the device lock held, so no other writer of this interface merges at the same
    time. False, with a warning, when the journal could not be stored.
    """
    if path is None or not written:
        return False
    # Microseconds: readers merge users' journals by this stamp, and a second is too coarse.
    at = stamp(time.time() if now is None else now)
    journal = read_journal(path)
    journal.update({name: (tags, args, at) for name, (tags, args) in written.items()})
    if len(journal) > MAX_ENTRIES:
        log.warning("not recording %d writes: the journal %s would exceed %d entries",
                    len(written), path, MAX_ENTRIES)
        return False
    document = {"schema": SCHEMA, "registers": {
        name: {"tags": tags, "args": _encode(tags, args), "at": stamp}
        for name, (tags, args, stamp) in sorted(journal.items())}}
    try:
        _replace(path, document)
    except (OSError, ValueError) as exc:
        log.warning("could not record %d writes in %s: %s", len(written), path, exc)
        return False
    return True


def _replace(path: Path, document: object) -> None:
    """Write beside the journal, then rename over it; never leave a half file behind."""
    fd, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), FILE_MODE)
            json.dump(document, stream, indent=1, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
