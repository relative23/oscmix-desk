"""The record beside the journal: the CC reports desk saw and the last connect check.

Held to the journal's rules: one file per interface and user in the runtime lock directory,
replaced atomically, read with suspicion, merged newest-first, never the reason an operation
fails.
"""

import json
import os

import pytest
from two_boxes import B

from oscmix_desk import seen as seen_mod
from oscmix_desk.discovery import lock_key
from oscmix_desk.journal import journal_path, stamp, stamp_seconds
from oscmix_desk.seen import (
    CC_MIX,
    CC_MODE,
    Seen,
    cell_value,
    check_record,
    read_all_seen,
    read_seen,
    record_seen,
)

KEY = lock_key("2a39:3fd9", B[1])
MISMATCH = {"route": "mic", "path": "/mix/1/input/1",
            "device": cell_value(-12.0, 0), "declared": cell_value(float("-inf"), 0)}


def test_reports_and_a_check_round_trip(tmp_path):
    path = tmp_path / "k.seen.json"
    check = check_record(60.0, 2, [MISMATCH], ["/mix/3/input/3"], [])
    assert record_seen(path, {CC_MIX: ("is", (0, "TotalMix App"), 0.0),
                              CC_MODE: ("i", (1,), 1.5), "/input/1/gain": ("f", (3.0,), 2.0)},
                       check)
    seen = read_seen(path)
    assert seen.reports == {CC_MIX: ("is", (0, "TotalMix App"), "1970-01-01T00:00:00.000000Z"),
                            CC_MODE: ("i", (1,), "1970-01-01T00:00:01.500000Z")}, \
        "only the CC registers are kept"
    assert seen.connect == check
    assert check["state"] == "mismatched"
    assert check["at"] == "1970-01-01T00:01:00.000000Z"
    assert json.loads(path.read_text())["connect"]["mismatched"][0]["declared"] == {
        "level_db": "-inf", "pan": 0}, "JSON has no -inf"


def test_a_later_operation_keeps_what_it_did_not_see_again(tmp_path):
    path = tmp_path / "k.seen.json"
    check = check_record(0.0, 1, [], [], [])
    record_seen(path, {CC_MIX: ("is", (3, "20ch"), 0.0)}, check)
    record_seen(path, {CC_MODE: ("i", (1,), 5.0)})
    seen = read_seen(path)
    assert set(seen.reports) == {CC_MIX, CC_MODE}
    assert seen.connect == check, "an operation without a check keeps the last one"
    assert check["state"] == "matched"
    record_seen(path, {CC_MIX: ("is", (0, "TotalMix App"), 9.0)})
    assert read_seen(path).reports[CC_MIX][1] == (0, "TotalMix App")


def test_nothing_to_store_stores_nothing(tmp_path):
    path = tmp_path / "k.seen.json"
    assert not record_seen(path, {})
    assert not record_seen(path, {"/input/1/gain": ("f", (1.0,), 0.0)})
    assert not record_seen(None, {CC_MODE: ("i", (1,), 0.0)})
    assert not path.exists()


def test_the_states_of_a_check():
    assert check_record(0.0, 1, [], ["/mix/1/input/1"], [])["state"] == "incomplete"
    assert check_record(0.0, 1, [], [], ["/mix/1/input/1"])["state"] == "incomplete"
    assert check_record(0.0, 1, [MISMATCH], ["/mix/2/input/1"], [])["state"] == "mismatched"


def test_the_record_lives_beside_the_journal_and_nowhere_else(tmp_path, monkeypatch):
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(tmp_path))
    own = journal_path(None, KEY, kind="seen")
    assert own == tmp_path / ("%s.seen.%d.json" % (KEY, os.getuid()))
    assert journal_path(None, KEY) == tmp_path / ("%s.written.%d.json" % (KEY, os.getuid()))
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(tmp_path / "absent"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert journal_path(tmp_path / "routing.conf", KEY, kind="seen") is None
    assert read_all_seen(tmp_path / "routing.conf", KEY) == Seen()


def _good():
    return {"schema": 1, "reports": {CC_MODE: {"tags": "i", "args": [1], "at": stamp(0)}},
            "connect": check_record(0.0, 1, [MISMATCH], [], [])}


def _broken(kind):
    data = _good()
    connect = data["connect"]
    if kind == "schema":
        data["schema"] = 2
    elif kind == "table":
        data["reports"] = []
    elif kind == "foreign":
        data["reports"]["/input/1/gain"] = {"tags": "f", "args": [1.0], "at": stamp(0)}
    elif kind == "args":
        data["reports"][CC_MODE]["args"] = ["1"]
    elif kind == "state":
        connect["state"] = "fine"
    elif kind == "count":
        connect["compared"] = "1"
    elif kind == "list":
        connect["unreported"] = "all"
    elif kind == "long":
        connect["invalid"] = ["/mix/1/input/1"] * (seen_mod.MAX_CELLS + 1)
    elif kind == "path":
        connect["invalid"] = [1]
    elif kind == "item":
        connect["mismatched"] = [dict(MISMATCH, extra=1)]
    elif kind == "where":
        connect["mismatched"] = [dict(MISMATCH, path="/input/1/gain")]
    elif kind == "level":
        connect["mismatched"] = [dict(MISMATCH, device={"level_db": "loud", "pan": 0})]
    elif kind == "pan":
        connect["mismatched"] = [dict(MISMATCH, device={"level_db": 0.0, "pan": 101})]
    elif kind == "boolean":
        connect["mismatched"] = [dict(MISMATCH, device={"level_db": True, "pan": 0})]
    elif kind == "at":
        connect["at"] = 0
    elif kind == "keys":
        connect["mismatched"] = [dict(MISMATCH, device={"level_db": 0.0})]
    elif kind == "value":
        connect["mismatched"] = [dict(MISMATCH, declared=[0.0, 0])]
    elif kind == "shape":
        data["connect"] = []
    return data


@pytest.mark.parametrize("kind", ["schema", "table", "foreign", "args", "state", "count", "list",
                                  "long", "path", "item", "where", "level", "pan", "boolean",
                                  "at", "keys", "value", "shape"])
def test_an_unsound_record_is_ignored_with_a_warning(tmp_path, caplog, kind):
    path = tmp_path / "k.seen.json"
    path.write_text(json.dumps(_broken(kind)))
    assert read_seen(path) == Seen()
    assert "ignoring the record" in caplog.text


def test_a_sound_record_is_read(tmp_path):
    path = tmp_path / "k.seen.json"
    path.write_text(json.dumps(_good()))
    assert read_seen(path).connect["compared"] == 1
    assert read_seen(tmp_path / "absent.json") == Seen()
    assert read_seen(None) == Seen()


def test_a_record_that_cannot_be_stored_is_a_warning(tmp_path, caplog):
    assert not record_seen(tmp_path / "absent" / "k.seen.json", {CC_MODE: ("i", (1,), 0.0)})
    assert "could not store what desk saw" in caplog.text


def test_every_user_keeps_a_record_and_a_reader_takes_the_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(tmp_path))
    mine = journal_path(None, KEY, kind="seen")
    theirs = journal_path(None, KEY, uid=os.getuid() + 1, kind="seen")
    record_seen(theirs, {CC_MIX: ("is", (3, "20ch"), 60.0), CC_MODE: ("i", (1,), 60.0)},
                check_record(90.0, 1, [], [], []))
    record_seen(mine, {CC_MIX: ("is", (0, "TotalMix App"), 120.0)},
                check_record(30.0, 2, [], [], []))
    later = journal_path(None, KEY, uid=os.getuid() + 2, kind="seen")
    record_seen(later, {CC_MODE: ("i", (0,), 10.0)}, check_record(20.0, 3, [], [], []))
    (tmp_path / ("%s.written.%d.json" % (KEY, os.getuid()))).write_text("{}")
    merged = read_all_seen(None, KEY)
    assert merged.reports[CC_MIX][1] == (0, "TotalMix App"), "the later report, whoever saw it"
    assert merged.reports[CC_MODE][1] == (1,)
    assert merged.connect["compared"] == 1, "the later check"


def test_the_stamp_reads_back_and_other_formats_do_not():
    assert stamp_seconds(stamp(1.25)) == 1.25
    assert stamp_seconds("yesterday") is None


def test_the_backend_hands_only_device_reports_to_the_record(wire_peer):
    from support import osc_bundle

    from oscmix_desk.osc import encode_osc

    reported = []
    device_reports = [osc_bundle([encode_osc(CC_MIX, "is", 3, "20ch")])]
    derived = [osc_bundle([encode_osc(CC_MODE, "i", 0)])]
    with wire_peer(reports=device_reports, derived=derived) as (device, _peer):
        device.on_reported = reported.append
        device.request_dump()
        for _ in range(8):
            list(device.messages(0.25))
            if reported:
                break
    assert reported == [(CC_MIX, "is", (3, "20ch"))], "a derived report, sent first, is none"
