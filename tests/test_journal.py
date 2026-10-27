"""The write journal: what desk last wrote per register, beside the device lock.

The device reports neither the playback matrix nor playback mute, so the only
record of them is what desk itself sent. These tests hold the journal to the
same rules as the lock it lives beside: one file per interface and user, in the
shared or runtime lock directory only, replaced atomically, read with
suspicion, and never the reason an operation fails.
"""

import errno
import math
import os
import stat

import pytest
from control_peer import BUSY
from two_boxes import B

from oscmix_desk import ApplyIntent, ChannelSetting, Config, open_desk
from oscmix_desk import journal as journal_mod
from oscmix_desk.discovery import lock_key
from oscmix_desk.errors import WriteFailed
from oscmix_desk.journal import journal_path, read_journal, record_written

KEY = lock_key("2a39:3fd9", B[1])
VOLUME = "/output/1/volume"


def test_writes_round_trip_with_their_types_and_time(tmp_path):
    path = tmp_path / "k.written.json"
    assert record_written(path, {"/mix/1/playback/1": ("fi", (float("-inf"), 0)),
                                 "/playback/1/mute": ("i", (1,)),
                                 "/hardware/opticalout": ("s", ("ADAT",))}, now=0)
    journal = read_journal(path)
    level, pan = journal["/mix/1/playback/1"][1]
    assert math.isinf(level)
    assert level < 0
    assert pan == 0
    assert journal["/playback/1/mute"] == ("i", (1,), "1970-01-01T00:00:00.000000Z")
    assert journal["/hardware/opticalout"][1] == ("ADAT",)
    assert stat.S_IMODE(path.stat().st_mode) == 0o660


def test_a_later_operation_merges_and_updates(tmp_path):
    path = tmp_path / "k.written.json"
    record_written(path, {"/a": ("i", (1,)), "/b": ("i", (2,))}, now=0)
    record_written(path, {"/b": ("i", (3,))}, now=60)
    journal = read_journal(path)
    assert journal["/a"] == ("i", (1,), "1970-01-01T00:00:00.000000Z")
    assert journal["/b"] == ("i", (3,), "1970-01-01T00:01:00.000000Z")


def test_the_journal_lives_beside_the_lock_and_nowhere_else(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    own = "%s.written.%d.json" % (KEY, os.getuid())
    assert journal_path(None, KEY) == shared / own
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    assert journal_path(None, KEY) == tmp_path / "rt/oscmix-desk" / own
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert journal_path(tmp_path / "routing.conf", KEY) is None
    assert journal_path(None, "") is None


def _hostile(path, kind):
    if kind == "symlink":
        target = path.with_name("target.json")
        target.write_text('{"schema": 1, "registers": {}}')
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversize":
        path.write_bytes(b" " * (journal_mod.MAX_BYTES + 1))
    else:
        path.write_text({
            "json": "{",
            "schema": '{"schema": 2, "registers": {}}',
            "table": '{"schema": 1, "registers": []}',
            "name": '{"schema": 1, "registers": {"x": {"tags": "i", "args": [1], "at": "t"}}}',
            "stamp": '{"schema": 1, "registers": {"/x": {"tags": "i", "args": [1]}}}',
            "type": '{"schema": 1, "registers": {"/x": {"tags": "i", "args": [1.5], "at": "t"}}}',
            "count": '{"schema": 1, "registers": {"/x": {"tags": "ii", "args": [1], "at": "t"}}}',
            "bool": '{"schema": 1, "registers": {"/x": {"tags": "f", "args": [true], "at": "t"}}}',
        }[kind])


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize", "json", "schema", "table",
                                  "name", "stamp", "type", "count", "bool"])
def test_an_unsound_journal_is_ignored_with_a_warning(tmp_path, caplog, kind):
    path = tmp_path / "k.written.json"
    _hostile(path, kind)
    assert read_journal(path) == {}
    assert "ignoring the write journal" in caplog.text


def test_a_missing_journal_is_empty_and_silent(tmp_path, caplog):
    assert read_journal(tmp_path / "none.json") == {}
    assert read_journal(None) == {}
    assert caplog.text == ""


def test_a_journal_that_cannot_be_stored_is_a_warning(tmp_path, caplog):
    assert not record_written(tmp_path / "absent/k.written.json", {"/a": ("i", (1,))})
    assert "could not record 1 writes" in caplog.text
    assert not record_written(None, {"/a": ("i", (1,))})
    assert not record_written(tmp_path / "k.json", {})


def test_a_failed_replace_leaves_no_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "k.written.json"

    def full(*_args):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(journal_mod.os, "replace", full)
    assert not record_written(path, {"/a": ("i", (1,))})
    assert list(tmp_path.iterdir()) == []


def test_the_entry_bound_keeps_the_previous_journal(tmp_path, monkeypatch, caplog):
    path = tmp_path / "k.written.json"
    record_written(path, {"/a": ("i", (1,))}, now=0)
    monkeypatch.setattr(journal_mod, "MAX_ENTRIES", 1)
    assert not record_written(path, {"/b": ("i", (2,))})
    assert list(read_journal(path)) == ["/a"]
    assert "would exceed 1 entries" in caplog.text


def test_the_backend_reports_only_acknowledged_writes(wire_peer):
    seen = []
    burst = [("/output/%d/volume" % n, "f", (0.,)) for n in (1, 2, 3)]
    with wire_peer(disconnect_after="/output/2/volume") as (device, _peer):
        device.on_written = lambda *write: seen.append(write)
        with pytest.raises(WriteFailed):
            device.send(burst)
    assert seen == [("/output/1/volume", "f", (0.,))], "a lost acknowledgement is not recorded"
    seen.clear()
    with wire_peer(refuse_write=BUSY) as (device, _peer):
        device.on_written = lambda *write: seen.append(write)
        with pytest.raises(WriteFailed):
            device.send(burst)
    assert seen == []


def declared():
    return Config(serial=B[1], channels=(ChannelSetting("output", 1, "volume", -12.0),))


def test_an_operation_records_its_writes_before_it_releases_the_lock(coordinated, tmp_path):
    config_path = tmp_path / "routing.conf"
    with coordinated() as peer:
        peer.reports = []
        with open_desk(declared(), config_path) as operation:
            operation.apply(ApplyIntent.EXPLICIT)
            path = journal_path(config_path, KEY)
            tags, args, _at = read_journal(path)[VOLUME]
            assert (tags, args) == ("f", (-12.0,)), "stored after the apply, under the lock"
        assert VOLUME in read_journal(path)


def test_a_reader_operation_records_nothing(coordinated, tmp_path):
    config_path = tmp_path / "routing.conf"
    with coordinated() as peer:
        peer.reports = []
        with open_desk(Config(serial=B[1]), config_path):
            pass
        assert read_journal(journal_path(config_path, KEY)) == {}


def test_each_user_keeps_a_journal_and_a_reader_takes_the_latest_write(tmp_path, monkeypatch):
    # The shared directory is sticky: a file root wrote cannot be replaced by the unit's user.
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(tmp_path))
    mine, theirs = journal_path(None, KEY), journal_path(None, KEY, uid=0)
    assert mine != theirs or os.getuid() == 0
    record_written(theirs, {"/a": ("i", (1,)), "/b": ("i", (1,))}, now=60)
    record_written(mine, {"/a": ("i", (2,))}, now=120)
    record_written(mine, {"/b": ("i", (2,))}, now=0)
    (tmp_path / ("%s.written.json" % KEY)).write_text("{}")
    (tmp_path / ("other.written.%d.json" % os.getuid())).write_text("{}")
    assert journal_mod.journal_paths(None, KEY) == tuple(sorted({mine, theirs}))
    merged = journal_mod.read_journals(None, KEY)
    assert merged["/a"][:2] == ("i", (2,)), "the later write, whoever made it"
    assert merged["/b"][:2] == ("i", (1,))


def test_no_lock_directory_means_no_journals(tmp_path):
    assert journal_mod.journal_paths(tmp_path / "routing.conf", KEY) == ()
    assert journal_mod.read_journals(tmp_path / "routing.conf", KEY) == {}
