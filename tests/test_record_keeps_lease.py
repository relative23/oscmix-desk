"""A slow record cannot cost the operation its lease.

2026-11-14 a CI run failed once: a profile switch applied its write, then stored the write
journal and the seen record -- both fsynced -- while it held the backend lease and sent no
KEEPALIVE. A disk that stalls for some seconds outlasts the backend's five-second idle
limit: the backend ended the lease and the switch's next request met a closed connection
("applied; it cannot be verified", broken pipe). The record is now written beside the
operation while the operation's thread keeps the lease alive.
"""

import errno
import threading
import time

import pytest
from control_peer import KEEPALIVE
from support import signed
from two_boxes import B

from oscmix_desk import ApplyIntent, ChannelSetting, Config, open_desk
from oscmix_desk import backend as backend_mod
from oscmix_desk import desk as desk_mod
from oscmix_desk.discovery import Device
from oscmix_desk.errors import BackendControlError
from oscmix_desk.osc import encode_osc

VOLUME = "/output/1/volume"
STALL = 0.4


def declared():
    return Config(serial=B[1], channels=(ChannelSetting("output", 1, "volume", -12.0),))


@pytest.fixture
def connected(monkeypatch, recording_backend):
    monkeypatch.setattr(desk_mod, "_target",
                        lambda config, reach: Device(config.usb_id, B[1], B[0]))
    monkeypatch.setattr(desk_mod, "connect_backend",
                        signed(desk_mod.connect_backend, recording_backend))
    return recording_backend


def test_a_stalled_record_keeps_sending_keepalives(coordinated, tmp_path, monkeypatch):
    monkeypatch.setattr(backend_mod, "CONTROL_HEARTBEAT", 0.05)
    real = desk_mod.record_written
    records = []

    with coordinated() as peer:
        def stalled(path, written, now=None):
            first = len(peer.requests)
            time.sleep(STALL)
            records.append((threading.current_thread().name, list(peer.requests[first:]),
                            sorted(written)))
            return real(path, written, now)

        monkeypatch.setattr(desk_mod, "record_written", stalled)
        peer.reports = [encode_osc(VOLUME, "f", -12.0)]
        with open_desk(declared(), tmp_path / "routing.conf") as operation:
            assert operation.apply(ApplyIntent.EXPLICIT) is True
            assert len(records) == 1, "the record is complete before apply returns"
            assert operation.verify().confirmed == [VOLUME]
    thread, during, written = records[0]
    assert thread == "desk-record"
    assert written == [VOLUME], "taken on the operation's thread before the writer starts"
    assert during.count(KEEPALIVE) >= 2, during


def test_a_connection_lost_while_recording_still_stores_the_record(
        monkeypatch, connected, tmp_path):
    recording = threading.Event()
    stored = []

    def slow(path, written, now=None):
        recording.set()
        time.sleep(0.1)
        stored.append(dict(written))
        return True

    wait = connected.wait

    def lost(seconds):
        if recording.is_set():
            raise BackendControlError(errno.ECONNRESET, "backend connection lost")
        wait(seconds)

    monkeypatch.setattr(desk_mod, "record_written", slow)
    monkeypatch.setattr(connected, "wait", lost)
    with open_desk(declared(), tmp_path / "routing.conf") as operation:
        assert operation.apply(ApplyIntent.EXPLICIT) is True
        assert len(stored) == 1, "the record is stored although the connection broke"


def test_a_record_failure_reaches_the_operation(monkeypatch, connected, tmp_path):
    def broken(path, written, now=None):
        raise RuntimeError("journal encoder broke")

    monkeypatch.setattr(desk_mod, "record_written", broken)

    def run():
        with open_desk(declared(), tmp_path / "routing.conf") as operation:
            operation.apply(ApplyIntent.EXPLICIT)

    with pytest.raises(RuntimeError, match="journal encoder broke"):
        run()


def test_the_record_thread_hands_its_own_failure_to_the_operation(
        monkeypatch, connected, tmp_path):
    # Only the record apply stores fails: the close's record afterwards succeeds, so the
    # exception apply raises can only be the one the record thread caught.
    failure = RuntimeError("journal encoder broke")
    threads = []

    def broken_once(path, written, now=None):
        threads.append(threading.current_thread().name)
        if len(threads) == 1:
            raise failure
        return True

    monkeypatch.setattr(desk_mod, "record_written", broken_once)
    with (open_desk(declared(), tmp_path / "routing.conf") as operation,
          pytest.raises(RuntimeError) as raised):
        operation.apply(ApplyIntent.EXPLICIT)
    assert raised.value is failure
    assert threads[0] == "desk-record"
