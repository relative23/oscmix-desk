"""How a start connects, what it leaves behind when that fails, and how the
backend's exit becomes the service's exit code.

The complete lifecycle runs in a subprocess (test_session_integration.py),
which the mutation suite cannot observe; these check the same decisions in
process.
"""

import logging

import pytest
from session_doubles import RunningChild
from support import device_key, write_config

from oscmix_desk import locking
from oscmix_desk import session as session_module
from oscmix_desk.config import load_config

DESK = "[route:x]\nplayback = 1/2\noutput = 1/2\n"


@pytest.fixture
def notified(monkeypatch):
    sent = []
    monkeypatch.setattr(session_module, "sd_notify", sent.append)
    return sent


@pytest.mark.parametrize("returncode", [0, 1, -9])
def test_a_requested_stop_is_a_clean_exit(fake_sysfs, notified, returncode):
    code = session_module._exit_code_for(returncode, load_config(None), fake_sysfs,
                                         {"stop": True})
    assert (code, notified) == (session_module.EXIT_OK, ["READY=1"])


def test_a_backend_that_ended_with_its_device_is_a_clean_exit(empty_sysfs, notified):
    code = session_module._exit_code_for(1, load_config(None), empty_sysfs, {"stop": False})
    assert (code, notified) == (session_module.EXIT_OK, ["READY=1"])


def test_a_clean_backend_exit_with_the_device_present_is_clean(fake_sysfs, notified):
    code = session_module._exit_code_for(0, load_config(None), fake_sysfs, {"stop": False})
    assert (code, notified) == (session_module.EXIT_OK, ["READY=1"])


@pytest.mark.parametrize(("returncode", "text"), [
    (1, "backend exited with status 1"),
    (-9, "backend exited with status -9 (SIGKILL)"),
    (-99, "backend exited with status -99"),
])
def test_a_failing_backend_with_its_device_present_fails_the_service(
        fake_sysfs, notified, caplog, returncode, text):
    with caplog.at_level(logging.ERROR):
        code = session_module._exit_code_for(returncode, load_config(None), fake_sysfs,
                                             {"stop": False})
    assert (code, notified) == (session_module.EXIT_FAILURE, [])
    assert [record.getMessage() for record in caplog.records] == [text]


# ---------------------------------------------------------------------------

@pytest.fixture
def start(tmp_path, monkeypatch, recording_backend):
    path = write_config(tmp_path / "routing.conf", DESK)
    monkeypatch.setattr(session_module, "apply_routing", lambda *_a, **_k: None)
    monkeypatch.setattr(session_module, "verify_and_repair", lambda *_a, **_k: None)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    return path


def _free(path):
    lock = locking.take_device_lock(path, device_key(path), wait=0.1)
    if lock is None:
        return False
    lock.release()
    return True


def test_a_start_connects_to_the_backend_it_launched_for_its_desk(
        start, monkeypatch, recording_backend):
    connected = []

    def connect(config, config_path, **options):
        connected.append((config_path, options))
        return recording_backend

    monkeypatch.setattr(session_module, "connect_backend", connect)
    child = RunningChild(pid=4242)
    stop = {"stop": False}
    verifier = session_module._apply_and_verify(child, load_config(start), stop, start)
    verifier.join(timeout=5)
    [(config_path, options)] = connected
    assert config_path == start
    assert (options["expected_pid"], options["reader"]) == (4242, False)
    should_stop = options["should_stop"]
    assert should_stop() is False
    child.returncode = 0
    assert should_stop() is True, "a backend that exited ends the operation"
    child.returncode = None
    stop["stop"] = True
    assert should_stop() is True
    assert _free(start)


def test_an_empty_desk_identifies_the_backend_as_a_reader(
        tmp_path, monkeypatch, recording_backend):
    path = write_config(tmp_path / "routing.conf", "")
    connected = []
    monkeypatch.setattr(session_module, "connect_backend",
                        lambda *_a, **options: connected.append(options) or recording_backend)
    assert session_module._apply_and_verify(RunningChild(), load_config(path),
                                            {"stop": False}, path) is None
    assert [options["reader"] for options in connected] == [True]
    assert recording_backend.operations == ["close"]
    assert _free(path)


def test_a_failed_connection_propagates_and_releases_the_lock(start, monkeypatch):
    failure = OSError(111, "Connection refused")

    def refused(*_a, **_k):
        raise failure

    monkeypatch.setattr(session_module, "connect_backend", refused)
    with pytest.raises(OSError, match="Connection refused") as raised:
        session_module._apply_and_verify(RunningChild(), load_config(start),
                                         {"stop": False}, start)
    assert raised.value is failure
    assert _free(start)


def test_a_failed_operation_closes_its_connection_and_releases_the_lock(
        start, monkeypatch, recording_backend):
    def busy():
        recording_backend.operations.append("begin")
        raise OSError(16, "another client holds the backend operation lease")

    monkeypatch.setattr(session_module, "connect_backend", lambda *_a, **_k: recording_backend)
    monkeypatch.setattr(recording_backend, "begin", busy)
    with pytest.raises(OSError, match="lease"):
        session_module._apply_and_verify(RunningChild(), load_config(start),
                                         {"stop": False}, start)
    assert recording_backend.operations == ["begin", "close"]
    assert _free(start)


def test_a_start_without_a_runtime_directory_locks_beside_its_desk(
        start, monkeypatch, tmp_path, recording_backend):
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(tmp_path / "absent"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(session_module, "connect_backend", lambda *_a, **_k: recording_backend)
    during = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *_a, **_k: during.append(_free(start)))
    verifier = session_module._apply_and_verify(RunningChild(), load_config(start),
                                                {"stop": False}, start)
    verifier.join(timeout=5)
    assert locking.device_lock_path(start, device_key(start)).parent == start.parent
    assert during == [False], "the lock beside the desk is held while writing"
    assert _free(start)
