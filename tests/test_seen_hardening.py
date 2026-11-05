"""The record beside the journal read with suspicion: each unsound shape, and what is said.

test_seen.py pins the round trip and the merge. These pin the warning that names the file and
the reason for every way a record can be unsound, the pan and size bounds of a stored connect
check, and how two users' records that saw something at the same moment merge.
"""

import json
import logging

import pytest

from oscmix_desk.journal import journal_path, stamp
from oscmix_desk.seen import (
    CC_MIX,
    CC_MODE,
    MAX_CELLS,
    cell_value,
    check_record,
    read_all_seen,
    read_seen,
    record_seen,
)

KEY = "2a39-3fd9-12345678"


def warnings(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING]


def stored(path, data):
    path.write_text(json.dumps(data))
    return path


def mismatch(pan):
    return {"route": "mic", "path": "/mix/1/input/1", "device": cell_value(-12.0, pan),
            "declared": cell_value(-6.0, 0)}


@pytest.mark.parametrize(("data", "reason"), [
    ({"schema": 2, "reports": {}}, "unknown record schema"),
    ([], "unknown record schema"),
    ({"schema": 1, "reports": []}, "malformed report table"),
    ({"schema": 1, "reports": {"/input/1/gain": {"tags": "f", "args": [1.0], "at": "x"}}},
     "malformed report '/input/1/gain'"),
    ({"schema": 1, "reports": {CC_MIX: {"tags": "i", "args": [1], "at": 5}}},
     "malformed report '/hardware/ccmix'"),
    ({"schema": 1, "reports": {CC_MIX: ["i", [1], "x"]}}, "malformed report '/hardware/ccmix'"),
    ({"schema": 1, "reports": {}, "connect": {"at": 5}}, "malformed connect check"),
])
def test_an_unsound_record_is_ignored_with_the_file_and_the_reason(tmp_path, caplog, data,
                                                                   reason):
    path = stored(tmp_path / "k.seen.1000.json", data)
    seen = read_seen(path)
    assert (seen.reports, seen.connect) == ({}, None)
    assert warnings(caplog) == ["ignoring the record %s: %s" % (path, reason)]


@pytest.mark.parametrize("pan", [-100, 100])
def test_a_check_with_a_pan_at_either_end_of_the_range_is_sound(tmp_path, caplog, pan):
    check = check_record(60.0, 1, [mismatch(pan)], [], [])
    path = stored(tmp_path / "k.seen.1000.json", {"schema": 1, "reports": {}, "connect": check})
    assert read_seen(path).connect == check
    assert warnings(caplog) == []


@pytest.mark.parametrize("pan", [-101, 101])
def test_a_check_with_a_pan_beyond_the_range_is_not(tmp_path, caplog, pan):
    check = check_record(60.0, 1, [mismatch(pan)], [], [])
    path = stored(tmp_path / "k.seen.1000.json", {"schema": 1, "reports": {}, "connect": check})
    assert read_seen(path).connect is None
    assert warnings(caplog) == ["ignoring the record %s: malformed connect check" % path]


def test_a_check_listing_exactly_the_cell_limit_is_sound(tmp_path):
    unreported = ["/mix/%d/input/%d" % (index // 20 + 1, index % 20 + 1)
                  for index in range(MAX_CELLS)]
    check = check_record(60.0, MAX_CELLS, [], unreported, [])
    path = stored(tmp_path / "k.seen.1000.json", {"schema": 1, "reports": {}, "connect": check})
    assert read_seen(path).connect == check
    over = check_record(60.0, MAX_CELLS + 1, [], [*unreported, "/mix/1/input/1"], [])
    stored(path, {"schema": 1, "reports": {}, "connect": over})
    assert read_seen(path).connect is None


def test_a_record_that_cannot_be_stored_costs_a_warning(tmp_path, caplog):
    path = tmp_path / "gone" / "k.seen.1000.json"
    assert not record_seen(path, {CC_MODE: ("i", (1,), 0.0)})
    [warning] = warnings(caplog)
    assert warning.startswith("could not store what desk saw in %s: [Errno 2] " % path)


def test_users_who_saw_at_the_same_moment_merge_by_file_order(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    first = check_record(60.0, 1, [], [], [])
    second = check_record(60.0, 2, [], ["/mix/1/input/1"], [])
    record_seen(journal_path(None, KEY, uid=1001, kind="seen"),
                {CC_MIX: ("is", (0, "TotalMix App"), 5.0)}, first)
    record_seen(journal_path(None, KEY, uid=1002, kind="seen"),
                {CC_MIX: ("is", (3, "20ch"), 5.0)}, second)
    merged = read_all_seen(None, KEY)
    assert merged.reports == {CC_MIX: ("is", (3, "20ch"), stamp(5.0))}, (
        "a tie goes to the file read last, the same on every reader")
    assert merged.connect == second
