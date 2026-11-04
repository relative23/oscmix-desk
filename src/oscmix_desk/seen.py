"""What desk last read about one interface that the journal beside it cannot say.

The write journal (``journal``) holds what desk sent. This record holds two things desk read
back, which decide what those writes mean for the playback matrix the device never reports:

- the last CC Mode and CC Mix reports a writing desk operation saw, in any of its dumps: in a
  CC Mix preset the firmware routes playback by a fixed table of its own;
- the last connect check (``connectcheck``): the device's input mix for the input routes the
  desk declares, as a session found it before it overwrote it.

``<lock directory>/<key>.seen.<uid>.json``, beside the journal and for its reasons: one file
per user in the sticky shared directory, stored under the device lock, replaced atomically,
read with suspicion. A reader takes the newest entry of every user's file. Each entry carries
the time desk saw it; whether that was since the interface last appeared is the reader's
question (``playstate``). A record that cannot be read or written costs a warning, never an
operation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .journal import _bounded, _decode, _encode, _replace, journal_paths, stamp
from .log import log
from .osc import Args

SCHEMA = 1
CC_MODE = "/hardware/ccmode"
CC_MIX = "/hardware/ccmix"
#: The device reports kept: the two registers that decide whether the firmware routes playback.
NOTED = (CC_MIX, CC_MODE)
#: What a connect check can come to.
CHECK_STATES = ("matched", "mismatched", "incomplete")
#: More cells than any desk declares (a UCX II has 20 x 20 input cells), small enough to read.
MAX_CELLS = 2000

#: path -> (OSC type tags, arguments, UTC time desk saw the report)
Reports = Dict[str, Tuple[str, Args, str]]
Record = Dict[str, object]


@dataclass(frozen=True)
class Seen:
    """The CC reports and the connect check of one record, or of every user's merged."""

    reports: Reports = field(default_factory=dict)
    #: The last connect check, as ``connectcheck.ConnectCheck.record`` stores it.
    connect: Optional[Record] = None


def cell_value(level: float, pan: object) -> Record:
    """One input-mix value as the record holds it: JSON has no -inf, so silence is a word."""
    return {"level_db": "-inf" if level == -math.inf else level, "pan": pan}


def _sound_value(value: object) -> bool:
    if type(value) is not dict or set(value) != {"level_db", "pan"}:
        return False
    level, pan = value["level_db"], value["pan"]
    number = level == "-inf" or (isinstance(level, (int, float)) and not isinstance(level, bool)
                                 and math.isfinite(level))
    return number and isinstance(pan, int) and not isinstance(pan, bool) and -100 <= pan <= 100


def _sound_mismatch(item: object) -> bool:
    return (type(item) is dict and set(item) == {"route", "path", "device", "declared"}
            and isinstance(item["route"], str) and isinstance(item["path"], str)
            and item["path"].startswith("/mix/")
            and _sound_value(item["device"]) and _sound_value(item["declared"]))


def _sound_check(check: object) -> bool:
    """Whether a stored connect check has the shape ``connectcheck`` writes."""
    if type(check) is not dict or not isinstance(check.get("at"), str):
        return False
    if check.get("state") not in CHECK_STATES or type(check.get("compared")) is not int:
        return False
    lists: List[List[object]] = []
    for name in ("mismatched", "unreported", "invalid"):
        items = check.get(name)
        if not isinstance(items, list) or len(items) > MAX_CELLS:
            return False
        lists.append(items)
    mismatched, unreported, invalid = lists
    return (all(_sound_mismatch(item) for item in mismatched)
            and all(isinstance(path, str) for path in [*unreported, *invalid]))


def _parse(raw: bytes) -> Seen:
    """A sound record, or ValueError naming what is wrong with it."""
    data = json.loads(raw)
    if type(data) is not dict or data.get("schema") != SCHEMA:
        raise ValueError("unknown record schema")
    table = data.get("reports")
    if type(table) is not dict:
        raise ValueError("malformed report table")
    reports: Reports = {}
    for name, row in table.items():
        if name not in NOTED or type(row) is not dict or not isinstance(row.get("at"), str):
            raise ValueError("malformed report %r" % name)
        tags = row.get("tags")
        reports[name] = (str(tags), _decode(tags, row.get("args")), row["at"])
    connect = data.get("connect")
    if connect is not None and not _sound_check(connect):
        raise ValueError("malformed connect check")
    return Seen(reports, connect)


def read_seen(path: Optional[Path]) -> Seen:
    """One user's record; empty, with a warning, for anything but a sound one."""
    if path is None:
        return Seen()
    try:
        return _parse(_bounded(path))
    except FileNotFoundError:
        return Seen()
    except (OSError, ValueError) as exc:
        log.warning("ignoring the record %s: %s", path, exc)
        return Seen()


def read_all_seen(config_path: Optional[Path], key: str) -> Seen:
    """Every user's record of this interface, merged: per report and for the check the newest."""
    reports: Reports = {}
    connect: Optional[Record] = None
    for path in journal_paths(config_path, key, kind="seen"):
        seen = read_seen(path)
        for name, entry in seen.reports.items():
            if name not in reports or entry[2] >= reports[name][2]:
                reports[name] = entry
        if seen.connect is not None and (
                connect is None or str(seen.connect["at"]) >= str(connect["at"])):
            connect = seen.connect
    return Seen(reports, connect)


def record_seen(path: Optional[Path], reports: Mapping[str, Tuple[str, Args, float]],
                connect: Optional[Record] = None) -> bool:
    """Add one operation's CC reports and connect check, replacing the file atomically.

    ``reports`` carry the time each was seen, in seconds. Called with the device lock held.
    False, with a warning, when there was something to store and it could not be stored.
    """
    kept = {name: entry for name, entry in reports.items() if name in NOTED}
    if path is None or (not kept and connect is None):
        return False
    current = read_seen(path)
    merged = dict(current.reports)
    merged.update({name: (tags, tuple(args), stamp(at))
                   for name, (tags, args, at) in kept.items()})
    rows: Dict[str, object] = {name: {"tags": tags, "args": _encode(tags, args), "at": at}
                               for name, (tags, args, at) in sorted(merged.items())}
    document: Record = {"schema": SCHEMA, "reports": rows,
                        "connect": current.connect if connect is None else dict(connect)}
    try:
        _replace(path, document)
    except (OSError, ValueError) as exc:
        log.warning("could not store what desk saw in %s: %s", path, exc)
        return False
    return True


def check_record(at: float, compared: int, mismatched: List[Record],
                 unreported: List[str], invalid: List[str]) -> Record:
    """A connect check in the shape ``read_seen`` accepts."""
    state = ("mismatched" if mismatched else "incomplete" if unreported or invalid
             else "matched")
    return {"at": stamp(at), "state": state, "compared": compared, "mismatched": mismatched,
            "unreported": unreported, "invalid": invalid}
