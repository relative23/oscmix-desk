"""`--snapshot`, `--diff`, `--dump-config` and `--capture`: what each prints and logs.

The CLI tests run these over a scripted backend. These replace the device read with a fixed one
and pin the rest: the provenance line, the counts each command logs for the person at the
terminal, the always-rewritten note, the journal section of a dump and the warnings about it,
and the read itself -- its deadline, its quiet period, and what a failed read says.
"""

import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from oscmix_desk import reads as reads_mod
from oscmix_desk.config import load_config
from oscmix_desk.journal import journal_path, record_written
from oscmix_desk.model import Config
from oscmix_desk.reads import DUMP_QUIET_SECONDS, DUMP_READ_SECONDS, DeviceRead

SERIAL = "24216011"
KEY = "2a39-3fd9-" + SERIAL
LOGGER = "oscmix-session"
CONFIG = ("[device]\nname = Fireface UCX II\n\n"
          "[route:main]\nplayback = 1/2\noutput = 5/6\nlevel = 0.0\n\n"
          "[input:3]\ngain = 12.0\n")
IN_SYNC = {"/output/5/stereo": (1,), "/playback/1/stereo": (1,), "/input/3/gain": (12.0,),
           "/output/5/volume": (0.0,), "/output/6/volume": (0.0,)}


def lines(caplog, level=None):
    return [record.getMessage() for record in caplog.records
            if record.name == LOGGER and (level is None or record.levelno == level)]


@pytest.fixture
def reading(monkeypatch):
    def install(registers, serial=SERIAL):
        read = DeviceRead(dict(registers), serial, "00ff")
        monkeypatch.setattr(reads_mod, "_read_device", lambda *_a, **_k: read)
        return read
    return install


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(CONFIG)
    return load_config(path)


def test_a_snapshot_names_its_provenance_and_leaves_out_what_streams(reading, monkeypatch,
                                                                     capsys, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    reading({"/input/1/gain": (3.0,), "/output/1/volume": (-10.25,),
             "/output/1/level": (0.5,), "/input/1/dynamics/meter": (7,)})
    monkeypatch.setattr(reads_mod, "device_firmware",
                        lambda *_a, **_k: {"usb_revision": None, "dsp_version": None})
    assert reads_mod._snapshot(Config()) == 0
    assert capsys.readouterr().out == (
        "# oscmix-session --snapshot: 2 registers; Fireface UCX II serial %s, usb ?, dsp ?; "
        "backend epoch 00ff; device-origin reports\n"
        "/input/1/gain 3.0\n/output/1/volume -10.25\n" % SERIAL)
    assert lines(caplog, logging.INFO) == [
        "read 4 registers; 2 in the snapshot, 2 streaming and left out"]


def test_a_device_in_sync_says_so_and_names_what_is_always_rewritten(reading, config, capsys,
                                                                     caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    reading(IN_SYNC)
    assert reads_mod._diff(config) == 0
    assert capsys.readouterr().out == (
        "the device matches the config\n"
        "2 more would be rewritten regardless: a dump never reports them, so\n"
        "an apply cannot tell whether they are already right.\n")
    assert lines(caplog, logging.INFO) == [
        "read 5 registers; 0 differ, 2 always rewritten, 2 already match"]


def test_a_drifted_device_is_counted_in_the_log(reading, config, capsys, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    reading({**IN_SYNC, "/input/3/gain": (3.0,)})
    assert reads_mod._diff(config) == 3
    assert "1 register(s) differ from the config:" in capsys.readouterr().out
    assert lines(caplog, logging.INFO) == [
        "read 5 registers; 1 differ, 2 always rewritten, 1 already match"]


def test_a_dump_logs_what_it_reconstructed_and_leaves_the_journal_out(reading, config, capsys,
                                                                      caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    reading({"/input/3/gain": (12.0,), "/output/5/volume": (-3.0,)})
    assert reads_mod._dump_config(config) == 0
    out = capsys.readouterr().out
    assert "WRITTEN BY DESK" not in out, "the journal only on request"
    assert lines(caplog) == [("read 2 registers; 0 input route(s), 2 channel setting(s) and 0 "
                              "global setting(s) reconstructed")]


def test_an_unusable_register_is_named_as_an_incomplete_export(reading, config, caplog):
    reading({"/input/3/gain": ("loud",)})
    assert reads_mod._dump_config(config) == 0
    assert [line for line in lines(caplog, logging.WARNING)
            if line.startswith("incomplete export: /input/3/gain: ")]


def test_a_dump_with_the_journal_names_where_it_came_from(reading, config, capsys, caplog,
                                                          monkeypatch, tmp_path):
    caplog.set_level(logging.INFO, logger=LOGGER)
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    first = journal_path(None, KEY, uid=1001)
    second = journal_path(None, KEY, uid=1002)
    record_written(first, {"/mix/5/playback/1": ("fi", (0.0, 0)),
                           "/playback/1/stereo": ("i", (1,)), "/playback/1/mute": ("i", (1,))},
                   now=0)
    record_written(second, {"/playback/2/mute": ("i", (1,))}, now=5)
    reading({"/output/5/stereo": (1,)})
    assert reads_mod._dump_config(config, include_written=True) == 0
    out = capsys.readouterr().out
    assert "# From %s, %s: what desk last sent and the backend" % (first, second) in out
    assert "write journal: 1 playback route(s) and 2 mute flag(s) desk wrote" in lines(caplog)
    assert not [line for line in lines(caplog) if "capture knows no playback" in line], (
        "a dump is no capture")


def test_a_journal_route_whose_output_link_was_not_reported_is_named(reading, config, caplog,
                                                                      monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    record_written(journal_path(None, KEY), {"/mix/5/playback/1": ("fi", (0.0, 0)),
                                             "/playback/1/stereo": ("i", (1,))}, now=0)
    reading({"/output/1/volume": (-3.0,)})
    assert reads_mod._dump_config(config, include_written=True) == 0
    assert lines(caplog, logging.WARNING) == [(
        "incomplete export: /mix/5/playback/1: missing or contradictory stereo-link report; "
        "omitted")]


def test_a_dump_without_any_journal_says_so(reading, config, capsys, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    reading({"/output/5/volume": (-3.0,)})
    assert reads_mod._dump_config(config, include_written=True) == 0
    assert "# From no write journal: what desk last sent" in capsys.readouterr().out
    assert "write journal: 0 playback route(s) and 0 mute flag(s) desk wrote" in lines(caplog)
    assert lines(caplog, logging.WARNING) == []


def test_an_interface_without_a_serial_has_no_journal(reading, config, caplog):
    reading({"/output/5/volume": (-3.0,)}, serial="?")
    reads_mod._dump_config(config, include_written=True)
    assert lines(caplog, logging.WARNING) == [
        "the interface named no serial, so its write journal is unknown"]


def test_without_a_runtime_lock_directory_there_is_no_journal(reading, config, caplog,
                                                              monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    reading({"/output/5/volume": (-3.0,)})
    reads_mod._dump_config(config, include_written=True)
    assert lines(caplog, logging.WARNING) == [
        "no runtime lock directory, so there is no write journal"]


def test_a_capture_without_playback_writes_warns_what_switching_to_it_leaves(reading, config,
                                                                             caplog):
    reading({"/output/5/volume": (-3.0,)})
    assert reads_mod.capture_text(config) is not None
    assert lines(caplog, logging.WARNING) == [(
        "the capture knows no playback routing: desk wrote none since the boot, and the device "
        "does not report it; switching to the capture leaves every playback cell as it is then")]


def test_a_capture_leaves_only_the_gains_autoset_owns_as_comments(reading, config):
    reading({"/input/1/autoset": (1,), "/input/2/autoset": (0,), "/input/1/stereo": (0,),
             "/input/1/gain": (20.0,), "/input/2/gain": (30.0,)})
    text = reads_mod.capture_text(config)
    assert "# gain = 20.0   # AutoSet owns it, so the profile leaves it alone" in text
    assert "# gain = 30.0   # AutoSet owns it" not in text, "unlinked: input 2 is its own"


class Backend:
    """A reader connection: reports in batches, each receive costs its whole timeout."""

    def __init__(self, clock, batches=(), name="Fireface UCX II (%s)" % SERIAL):
        self.clock = clock
        self.batches = list(batches)
        self.device_name = name
        self.epoch = bytes.fromhex("00ff")
        self.timeouts = []

    def request_dump(self):
        pass

    def messages(self, timeout):
        self.timeouts.append(timeout)
        self.clock.now += timeout
        return iter(self.batches.pop(0) if self.batches else ())


@pytest.fixture
def connected(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(reads_mod, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def connect(backend=None, fail=None):
        @contextmanager
        def operation(*_args, **_kwargs):
            if fail is not None:
                raise fail
            yield SimpleNamespace(_control=backend)

        monkeypatch.setattr(reads_mod, "_operation", operation)

    return clock, connect


def test_a_silent_backend_is_given_exactly_the_read_window(connected, caplog):
    clock, connect = connected
    backend = Backend(clock)
    connect(backend)
    assert reads_mod._read_device(Config()) is None
    assert clock.now == DUMP_READ_SECONDS, "it ends at the deadline, not a receive later"
    assert set(backend.timeouts) == {0.25}
    assert lines(caplog, logging.ERROR) == [
        "the backend supplied no device-origin reports in the refresh window"]


def test_the_read_ends_once_the_reports_have_been_quiet_for_longer_than_the_quiet_period(
        connected):
    clock, connect = connected
    backend = Backend(clock, [[("/input/1/gain", "f", (3.0,))]])
    connect(backend)
    read = reads_mod._read_device(Config())
    assert read == DeviceRead({"/input/1/gain": (3.0,)}, SERIAL, "00ff")
    assert clock.now == 0.25 + DUMP_QUIET_SECONDS + 0.25, (
        "the first receive that ends past the quiet period stops it")


def test_a_backend_that_names_no_serial_reads_as_question_mark(connected):
    clock, connect = connected
    connect(Backend(clock, [[("/input/1/gain", "f", (3.0,))]], name="Fireface UCX II"))
    assert reads_mod._read_device(Config()).serial == "?"


def test_a_failed_connection_names_the_reason(connected, caplog):
    _clock, connect = connected
    connect(fail=OSError(111, "Connection refused"))
    assert reads_mod._read_device(Config()) is None
    assert lines(caplog, logging.ERROR) == [
        "cannot read the selected backend: [Errno 111] Connection refused"]
