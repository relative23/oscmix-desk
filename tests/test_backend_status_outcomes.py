"""Every backend association outcome names its state, cause and evidence exactly.

The /proc parsing behind each input is tested in test_control_identity.py;
here the resolved device, endpoint and holder are given, so each decision
and each field of the result is checked on its own.
"""

import os
import socket
from pathlib import Path

import pytest

from oscmix_desk import diagnostics
from oscmix_desk.diagnostics import BackendStatus
from oscmix_desk.discovery import Device
from oscmix_desk.errors import DeviceAmbiguous
from oscmix_desk.model import Config
from oscmix_desk.process import BackendOwner

DEVICE = Device("2a39:3fd9", "24216011", 24)
PID = 4321


@pytest.fixture
def status(tmp_path, monkeypatch):
    """backend_status with a given device and holder; returns (call, path, proc)."""
    path = tmp_path / "control"
    proc = tmp_path / "proc"
    (proc / str(PID)).mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(path))
    state = {"device": DEVICE, "holder": BackendOwner(PID, True, 24, "24216011")}
    seen = []

    def resolve(usb_id, name, serial, proc_root):
        seen.append(("resolve", usb_id, name, serial, proc_root))
        if isinstance(state["device"], Exception):
            raise state["device"]
        return state["device"]

    def control_path(config_path, key):
        seen.append(("path", config_path, key))
        return path

    def holder(endpoint, proc_root):
        seen.append(("holder", endpoint, proc_root))
        return state["holder"]

    monkeypatch.setattr(diagnostics, "resolve_device", resolve)
    monkeypatch.setattr(diagnostics, "control_path", control_path)
    monkeypatch.setattr(diagnostics, "control_holder", holder)

    def call(config_path=None, **changes):
        state.update(changes)
        return diagnostics.backend_status(Config(serial="24216011"), proc, config_path)

    call.seen = seen
    yield call, path, proc
    listener.close()


def test_the_ready_association_carries_the_owner_process_identity(status):
    call, path, proc = status
    info = (proc / str(PID)).stat()
    assert call() == BackendStatus(
        "ready", "coordinated backend belongs to the selected interface",
        DEVICE, PID, str(path), info.st_uid, info.st_gid)


def test_the_endpoint_is_resolved_for_the_desk_and_its_device(status, tmp_path):
    call, path, proc = status
    call(tmp_path / "routing.conf")
    assert call.seen == [("resolve", "2a39:3fd9", "Fireface UCX II", "24216011", proc),
                         ("path", tmp_path / "routing.conf", DEVICE.key),
                         ("holder", path, proc)]


def test_an_absent_endpoint_names_where_it_was_expected(status):
    call, path, _proc = status
    path.unlink()
    assert call() == BackendStatus("absent", "no coordinated backend endpoint", DEVICE,
                                   endpoint=str(path))


def test_a_file_in_place_of_the_socket_is_a_conflict(status):
    call, path, _proc = status
    path.unlink()
    path.write_text("")
    assert call() == BackendStatus("conflict", "control endpoint is not a real socket",
                                   DEVICE, endpoint=str(path))


@pytest.mark.parametrize("failure", [OSError("unix table unreadable"),
                                     DeviceAmbiguous("two interfaces match")])
def test_an_unresolvable_association_keeps_only_its_cause(status, failure):
    call, _path, _proc = status
    assert call(device=failure) == BackendStatus("unknown", str(failure))


def test_an_unidentified_listener_is_unknown_not_absent(status):
    call, path, _proc = status
    assert call(holder=None) == BackendStatus(
        "unknown", "control endpoint has no identified listening owner", DEVICE,
        endpoint=str(path))


def test_another_program_on_the_endpoint_is_a_conflict(status):
    call, path, _proc = status
    assert call(holder=BackendOwner(PID, False, None, None)) == BackendStatus(
        "conflict", "endpoint belongs to an incompatible program", DEVICE, PID, str(path))


@pytest.mark.parametrize(("device", "holder"), [
    (Device("2a39:3fd9", "24216011", None), BackendOwner(PID, True, 24, "24216011")),
    (DEVICE, BackendOwner(PID, True, None, "24216011")),
])
def test_either_missing_bridge_client_leaves_the_association_unknown(status, device, holder):
    call, path, _proc = status
    assert call(device=device, holder=holder) == BackendStatus(
        "unknown", "cannot establish the exclusive ALSA bridge", device, PID, str(path))


@pytest.mark.parametrize(("device", "holder"), [
    (Device("2a39:3fd9", "", 24), BackendOwner(PID, True, 24, "24216011")),
    (DEVICE, BackendOwner(PID, True, 24, None)),
])
def test_either_missing_serial_leaves_the_association_unknown(status, device, holder):
    call, path, _proc = status
    assert call(device=device, holder=holder) == BackendStatus(
        "unknown", "device/backend serial identity is unavailable", device, PID, str(path))


@pytest.mark.parametrize("holder", [
    BackendOwner(PID, True, 25, "24216011"),   # another client reporting this serial
    BackendOwner(PID, True, 24, "99887766"),   # this client with another serial
])
def test_one_differing_identity_component_is_already_another_interface(status, holder):
    call, path, _proc = status
    assert call(holder=holder) == BackendStatus(
        "conflict", "backend drives another interface", DEVICE, PID, str(path))


def test_an_endpoint_owned_by_another_user_is_a_conflict(status, monkeypatch):
    call, path, proc = status
    original = Path.stat

    def stat(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self == proc / str(PID):
            fields = list(result)
            fields[4] = result.st_uid + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", stat)
    assert call() == BackendStatus("conflict", "endpoint and backend have different owners",
                                   DEVICE, PID, str(path))


def test_an_owner_that_vanished_is_unknown_with_the_cause(status):
    call, path, proc = status
    (proc / str(PID)).rmdir()
    result = call()
    assert result.state == "unknown"
    assert result.detail.startswith("[Errno 2] No such file or directory")
    assert (result.device, result.pid, result.endpoint) == (DEVICE, PID, str(path))
    assert (result.uid, result.gid) == (None, None)
