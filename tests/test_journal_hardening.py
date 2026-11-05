"""The write journal read with suspicion and written atomically: the edges and what is said.

test_journal.py pins the round trip and the merge. These pin each way a journal file can be
unsound -- and the warning that names the file and the reason -- the size and entry limits at
their exact boundary, the stored number format, and that a failed store only ever warns.
"""

import json
import logging
import os

import pytest

from oscmix_desk import journal as journal_mod
from oscmix_desk.journal import (
    MAX_BYTES,
    MAX_ENTRIES,
    journal_path,
    journal_paths,
    read_journal,
    read_journals,
    record_written,
    stamp,
)

KEY = "2a39-3fd9-12345678"


def warnings(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING]


def document(registers):
    return json.dumps({"schema": 1, "registers": registers})


@pytest.mark.parametrize(("text", "reason"), [
    ('{"schema": 2, "registers": {}}', "unknown journal schema"),
    ('[1]', "unknown journal schema"),
    ('{"schema": 1, "registers": []}', "malformed register table"),
    (document({"mix": {"tags": "i", "args": [1], "at": "x"}}), "malformed entry 'mix'"),
    (document({"/a": {"tags": "i", "args": [1], "at": 5}}), "malformed entry '/a'"),
    (document({"/a": {"tags": "i", "args": [1, 2], "at": "x"}}), "malformed arguments"),
    (document({"/a": {"tags": 5, "args": [1], "at": "x"}}), "malformed arguments"),
    (document({"/a": {"tags": "i", "args": 1, "at": "x"}}), "malformed arguments"),
    (document({"/a": {"tags": "i", "args": [1.5], "at": "x"}}),
     "argument does not match its type tag"),
    (document({"/a": {"tags": "s", "args": [1], "at": "x"}}),
     "argument does not match its type tag"),
])
def test_an_unsound_journal_is_ignored_with_the_file_and_the_reason(tmp_path, caplog, text,
                                                                    reason):
    path = tmp_path / "k.written.1000.json"
    path.write_text(text)
    assert read_journal(path) == {}
    assert warnings(caplog) == ["ignoring the write journal %s: %s" % (path, reason)]


def test_a_journal_that_is_no_regular_file_is_ignored(tmp_path, caplog):
    path = tmp_path / "k.written.1000.json"
    os.mkfifo(path)
    assert read_journal(path) == {}
    assert warnings(caplog) == ["ignoring the write journal %s: not a bounded regular file"
                                % path]


def padded(size):
    """A sound journal of exactly ``size`` bytes, padded in front so a short read breaks it."""
    body = document({"/playback/1/mute": {"tags": "i", "args": [1], "at": stamp(0)}})
    return " " * (size - len(body)) + body


def test_a_journal_of_exactly_the_size_limit_is_read_whole(tmp_path, caplog):
    path = tmp_path / "k.written.1000.json"
    path.write_text(padded(MAX_BYTES))
    assert path.stat().st_size == MAX_BYTES
    assert read_journal(path) == {"/playback/1/mute": ("i", (1,), stamp(0))}
    assert warnings(caplog) == []


def test_a_journal_one_byte_over_the_limit_is_ignored(tmp_path, caplog):
    path = tmp_path / "k.written.1000.json"
    path.write_text(padded(MAX_BYTES + 1))
    assert read_journal(path) == {}
    assert warnings(caplog) == ["ignoring the write journal %s: not a bounded regular file"
                                % path]


def test_finite_floats_are_stored_as_json_numbers(tmp_path):
    path = tmp_path / "k.written.1000.json"
    assert record_written(path, {"/mix/1/playback/1": ("fi", (-12.5, 0)),
                                 "/mix/1/playback/2": ("fi", (float("-inf"), 0))}, now=0)
    stored = json.loads(path.read_text())["registers"]
    assert stored["/mix/1/playback/1"]["args"] == [-12.5, 0]
    assert stored["/mix/1/playback/2"]["args"] == ["-inf", 0], "JSON has no -inf"


def test_a_write_json_cannot_hold_is_not_stored_and_the_journal_stays(tmp_path, caplog):
    path = tmp_path / "k.written.1000.json"
    assert record_written(path, {"/a": ("i", (1,))}, now=0)
    before = path.read_text()
    assert not record_written(path, {"/b": ("i", (float("nan"),))}, now=60)
    assert path.read_text() == before
    [warning] = warnings(caplog)
    assert warning.startswith("could not record 1 writes in %s: " % path)
    assert os.listdir(tmp_path) == [path.name], "no temporary file left behind"


def test_the_journal_is_replaced_from_a_hidden_file_beside_it(tmp_path, monkeypatch):
    path = tmp_path / "k.written.1000.json"
    replaced = []
    real = os.replace

    def replace(source, target):
        replaced.append((os.fspath(source), os.fspath(target)))
        real(source, target)

    monkeypatch.setattr(journal_mod.os, "replace", replace)
    assert record_written(path, {"/a": ("i", (1,))}, now=0)
    [(source, target)] = replaced
    assert target == os.fspath(path)
    assert os.path.dirname(source) == os.fspath(tmp_path), "beside it: the rename is atomic"
    assert os.path.basename(source).startswith(".%s." % path.name)


def test_a_failed_rename_whose_temporary_is_gone_still_only_warns(tmp_path, monkeypatch,
                                                                   caplog):
    path = tmp_path / "k.written.1000.json"

    def replace(source, _target):
        os.unlink(source)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(journal_mod.os, "replace", replace)
    assert not record_written(path, {"/a": ("i", (1,))}, now=0)
    assert warnings(caplog) == ["could not record 1 writes in %s: [Errno 5] Input/output error"
                                % path]


def test_a_journal_in_a_missing_directory_costs_a_warning(tmp_path, caplog):
    path = tmp_path / "gone" / "k.written.1000.json"
    assert not record_written(path, {"/a": ("i", (1,)), "/b": ("i", (2,))}, now=0)
    [warning] = warnings(caplog)
    assert warning.startswith("could not record 2 writes in %s: [Errno 2] " % path)


def test_exactly_the_entry_limit_is_stored(tmp_path, caplog):
    path = tmp_path / "k.written.1000.json"
    written = {"/r/%d" % index: ("i", (index,)) for index in range(MAX_ENTRIES)}
    assert record_written(path, written, now=0)
    assert len(read_journal(path)) == MAX_ENTRIES
    assert warnings(caplog) == []


def test_one_entry_over_the_limit_is_not_stored_and_says_so(tmp_path, caplog):
    path = tmp_path / "k.written.1000.json"
    assert record_written(path, {"/r/0": ("i", (0,))}, now=0)
    more = {"/s/%d" % index: ("i", (index,)) for index in range(MAX_ENTRIES)}
    assert not record_written(path, more, now=60)
    assert warnings(caplog) == ["not recording %d writes: the journal %s would exceed %d entries"
                                % (MAX_ENTRIES, path, MAX_ENTRIES)]
    assert read_journal(path) == {"/r/0": ("i", (0,), stamp(0))}


def test_no_journal_without_a_lock_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(tmp_path / "absent"))
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert journal_path(None, KEY) is None
    assert journal_path(tmp_path / "routing.conf", KEY) is None, "never beside the config"
    assert journal_paths(None, KEY) == ()
    assert read_journals(None, KEY) == {}


def test_users_writing_at_the_same_moment_merge_by_file_order(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    record_written(journal_path(None, KEY, uid=1001), {"/playback/1/mute": ("i", (0,))}, now=5)
    record_written(journal_path(None, KEY, uid=1002), {"/playback/1/mute": ("i", (1,))}, now=5)
    assert [path.name for path in journal_paths(None, KEY)] == [
        "%s.written.1001.json" % KEY, "%s.written.1002.json" % KEY]
    assert read_journals(None, KEY) == {"/playback/1/mute": ("i", (1,), stamp(5))}, (
        "a tie goes to the file read last, the same on every reader")
