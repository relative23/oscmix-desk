"""Connection and receive failures remain distinct from a quiet device.

ODK1 replaces the formerly exclusive UDP receive port. A failed operation
never falls back to blind writes and preserves exact partial results.
"""

import argparse
import errno
import os
import re
import socket
import time
from pathlib import Path

import pytest
from backend_doubles import RecordingBackend
from control_peer import ScriptedControl
from support import write_config

from oscmix_desk import backend, locking, profiles, routing, verify
from oscmix_desk import outcome as outcome_mod
from oscmix_desk import reads as reads_mod
from oscmix_desk import reload as reload_mod
from oscmix_desk import session as session_module
from oscmix_desk.constants import EXIT_FAILURE
from oscmix_desk.errors import ReceivePortError, WriteFailed

DENIED = "cannot read the backend: Permission denied"
DENIED_STR = "[Errno 13] " + DENIED
DESK = "[route:main]\noutput=1/2\nplayback=1/2\nlevel=0.0\n[output:1]\nvolume=-10.0\n"


class _Socket:
    def __init__(self, error):
        self.error = error
        self.closed = False

    def bind(self, _address):
        raise self.error

    def settimeout(self, _timeout):
        pass

    def connect(self, _address):
        raise self.error

    def close(self):
        self.closed = True


@pytest.mark.parametrize("code", [errno.EACCES, errno.ENOENT, errno.ECONNREFUSED, errno.ENOBUFS])
def test_connect_failure_preserves_reason_and_closes_socket(tmp_path, monkeypatch, code):
    peer = ScriptedControl(tmp_path / "c")
    cause = OSError(code, os.strerror(code))
    sock = _Socket(cause)
    try:
        monkeypatch.setattr(backend.socket, "socket", lambda *_args: sock)
        with pytest.raises(OSError, match=re.escape(os.strerror(code))) as failure:
            backend.Control(peer.path, os.getpid())
        assert failure.value is cause
        assert sock.closed
        assert peer.requests == []
    finally:
        peer.close()


def test_a_socket_that_cannot_be_created_preserves_the_reason(monkeypatch):
    def exhausted(*_args):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(backend.socket, "socket", exhausted)
    with pytest.raises(OSError, match="Too many open files") as failure:
        backend.Control(Path("/not-accessed"), os.getpid())
    assert failure.value.errno == errno.EMFILE


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses owner-mode denial")
def test_kernel_endpoint_permissions_refuse_before_handshake(tmp_path):
    peer = ScriptedControl(tmp_path / "c")
    try:
        peer.path.chmod(0)
        with pytest.raises(PermissionError):
            backend.Control(peer.path, os.getpid())
        assert peer.requests == []
    finally:
        peer.close()


def test_the_barrier_reports_partial_writes_when_the_receiver_fails(
        tmp_path, unbindable_backend):
    config = profiles.load_config(write_config(tmp_path / "routing.conf", DESK))
    with pytest.raises(WriteFailed, match=DENIED) as caught:
        routing.apply_routing(config, unbindable_backend)
    paths = [path for path, _tags, _args in unbindable_backend.sent]
    assert paths == ["/playback/1/stereo", "/output/1/stereo"]
    assert caught.value.written == tuple(paths)
    assert caught.value.unwritten == ("/mix/1/playback/1", "/output/1/volume")


def test_the_public_echo_wait_raises_instead_of_claiming_silence(unbindable_backend):
    with pytest.raises(ReceivePortError, match=DENIED):
        routing.await_link_echo({"/output/1/stereo": 1}, unbindable_backend, .1)


def test_the_verifier_does_not_write_after_a_receive_failure(tmp_path, unbindable_backend):
    config = profiles.load_config(write_config(tmp_path / "routing.conf", DESK))
    with pytest.raises(ReceivePortError, match=DENIED):
        verify.verify_and_repair(config, unbindable_backend)
    assert unbindable_backend.sent == []


class _Child:
    pid = 4242
    returncode = None

    def poll(self):
        return None


def test_the_status_says_failed_and_the_journal_says_why(
        tmp_path, monkeypatch, caplog, unbindable_backend):
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.)
    statuses = []
    monkeypatch.setattr(session_module, "sd_notify", statuses.append)
    path = write_config(tmp_path / "routing.conf", DESK)
    lock = locking.take_device_lock(path, "key")
    with caplog.at_level("ERROR"):
        thread = session_module._verify_in_background(
            _Child(), profiles.load_config(path), {"stop": False}, lock, unbindable_backend)
        thread.join(5)
    assert not thread.is_alive()
    assert "routing cannot be verified: " + DENIED_STR in caplog.text
    assert statuses[-1].startswith("STATUS=running; verifier failed at ")
    again = locking.take_device_lock(path, "key", wait=.2)
    assert again is not None
    again.release()
    assert unbindable_backend.operations[-1] == "close"


def test_a_reconcile_stands_down_and_names_receive_failure(
        tmp_path, monkeypatch, caplog, unbindable_backend):
    monkeypatch.setattr(reload_mod, "connect_backend", lambda *_a, **_k: unbindable_backend)
    statuses = []
    monkeypatch.setattr(reload_mod, "sd_notify", statuses.append)
    path = write_config(tmp_path / "routing.conf", DESK)
    with caplog.at_level("ERROR"):
        reload_mod._reconcile(argparse.Namespace(config=path),
                                      profiles.load_config(path), {"stop": False})
    assert DENIED_STR in caplog.text
    assert statuses[-1].startswith("STATUS=running; reconcile skipped")
    assert unbindable_backend.sent == []
    assert unbindable_backend.operations == ["begin", "close"]


def test_a_switch_keeps_the_marker_when_its_link_receiver_fails(
        tmp_path, unbindable_backend, caplog):
    path = write_config(tmp_path / "routing.conf", DESK)
    write_config(tmp_path / "profiles" / "tracking.conf", DESK)
    marker = tmp_path / "active-profile"
    marker.write_text("previous\n")
    with caplog.at_level("ERROR"):
        outcome = profiles.switch_profile("tracking", config_path=path, backend=unbindable_backend)
    assert outcome.state == outcome_mod.WRITTEN_IN_PART
    assert DENIED in outcome.reason
    assert outcome.unwritten == ["/mix/1/playback/1", "/output/1/volume"]
    assert outcome.persisted is False
    assert outcome.written == ["/playback/1/stereo", "/output/1/stereo"]
    assert outcome.read_back is False
    assert marker.read_text() == "previous\n"
    assert "written in part" in caplog.text
    assert "the declared desk in effect has not changed" in outcome.describe()


@pytest.mark.parametrize("flag", ["--diff", "--snapshot", "--dump-config"])
def test_a_read_fails_with_the_reason_and_not_with_a_traceback(
        tmp_path, monkeypatch, caplog, flag, unbindable_backend):
    from oscmix_desk import cli

    monkeypatch.setattr(reads_mod, "connect_backend", lambda *_a, **_k: unbindable_backend)
    path = write_config(tmp_path / "routing.conf", DESK)
    with caplog.at_level("ERROR"):
        assert cli.main(["--config", str(path), flag]) == EXIT_FAILURE
    assert DENIED in caplog.text
    assert "close the mixer GUI" not in caplog.text
    assert unbindable_backend.operations == ["close"]


class _BrokenReceive:
    def __init__(self, sock, failure):
        self.sock = sock
        self.failure = failure
        self.reads = 0

    def settimeout(self, timeout):
        self.timeout = timeout
        self.sock.settimeout(timeout)

    def recvmsg(self, _size):
        self.reads += 1
        if isinstance(self.failure, socket.timeout):
            time.sleep(self.timeout)
        raise self.failure

    def close(self):
        self.sock.close()


def test_receive_error_invalidates_the_connection_instead_of_spinning(wire_peer):
    with wire_peer() as (device, _peer):
        sock = _BrokenReceive(device._sock, OSError(errno.ENETDOWN, "Network is down"))
        device._sock = sock
        with pytest.raises(ReceivePortError, match=r"backend receive failed:.*Network is down"):
            list(device.messages(.1))
        with pytest.raises(ReceivePortError, match="no longer valid"):
            list(device.messages(.1))
        assert sock.reads == 1
        assert device._lease is None


def test_socket_timeout_remains_silence(wire_peer):
    with wire_peer() as (device, _peer):
        sock = _BrokenReceive(device._sock, socket.timeout("timed out"))
        device._sock = sock
        assert list(device.messages(.1)) == []
        assert sock.reads == 1


def test_a_connection_closed_by_its_owner_is_no_longer_readable(wire_peer):
    with wire_peer() as (device, _peer):
        device.close()
        with pytest.raises(ReceivePortError, match="no longer valid"):
            list(device.messages(.05))


class _DeafBackend(RecordingBackend):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def messages(self, _timeout):
        self.reads += 1
        raise ReceivePortError(errno.ENETDOWN, "Network is down")


def test_the_read_back_raises_instead_of_spinning_out_its_window():
    device = _DeafBackend()
    with pytest.raises(ReceivePortError, match="Network is down"):
        verify.verify_routing({"/output/1/volume": ("f", (-10.,))}, device, 5.)
    assert device.reads == 1


def _script(name):
    import importlib.util

    from support import repo_file

    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), repo_file("scripts", name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("code", [errno.EBUSY, errno.EACCES, errno.ECONNRESET])
def test_record_dump_connection_failure_is_not_a_hardware_skip(monkeypatch, tmp_path,
                                                              capsys, code):
    from types import SimpleNamespace

    record = _script("record-dump")
    released = []
    monkeypatch.setattr(record.sys, "argv", ["record-dump.py", "--out", str(tmp_path / "out")])
    monkeypatch.setattr(record, "load_config", lambda *_: profiles.Config())
    monkeypatch.setattr(record, "resolve_device", lambda *_: SimpleNamespace(key="device"))
    monkeypatch.setattr(record, "take_device_lock", lambda *_:
                        SimpleNamespace(release=lambda: released.append(True)))

    def failed(*_args, **_kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(record, "connect_backend", failed)
    assert record.main() == 1
    assert released == [True]
    assert os.strerror(code) in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_meter_connection_failure_frees_the_device_lock(monkeypatch):
    from types import SimpleNamespace

    measure = _script("verify-hardware")
    released = []
    monkeypatch.setattr(measure, "resolve_device", lambda *_: SimpleNamespace(key="device"))
    monkeypatch.setattr(measure, "take_device_lock", lambda *_:
                        SimpleNamespace(release=lambda: released.append(True)))

    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(measure, "connect_backend", denied)
    with pytest.raises(PermissionError, match="Permission denied"):
        measure.LevelReader(profiles.Config(), None, None)
    assert released == [True]
