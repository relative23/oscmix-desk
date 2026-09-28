"""Read-only association of one local endpoint with its actual ALSA bridge."""

import os
import shutil
import socket
from pathlib import Path

import pytest

from oscmix_desk import diagnostics, locking, process
from oscmix_desk.model import Config

UNIX_HEADER = "Num       RefCount Protocol Flags    Type St Inode Path\n"


def command(entry, words):
    (entry / "cmdline").write_bytes(b"\0".join(os.fsencode(w) for w in words) + b"\0")


def test_status_resolves_exact_listening_owner_without_connecting(endpoint, monkeypatch):
    config, path, proc = endpoint
    def forbidden(*_a, **_k):
        pytest.fail("read-only status attempted I/O to the backend or filesystem mutation")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    state = diagnostics.backend_status(config, proc)
    assert state.state == "ready"
    assert state.pid == 101
    assert (state.uid, state.gid) == (os.getuid(), os.getgid())
    assert state.endpoint == str(path)
    assert state.device.serial == "24216011"


@pytest.mark.parametrize("args", [
    ["another", "-x", "24:1", "oscmix"],
    ["alsaseqio", "24:1", "oscmix"],
    ["alsaseqio", "-x", "24:0", "oscmix"],
    ["alsaseqio", "-x", "24:1", "another"],
    ["alsaseqio", "-x", "24:1"],
    ["alsaseqio", "some-argument", "-x", "24:1", "oscmix"],
])
def test_unrelated_or_nonexclusive_bridge_never_qualifies(endpoint, args):
    config, _path, proc = endpoint
    command(proc / "102", args)
    assert diagnostics.backend_status(config, proc).state == "unknown"


@pytest.mark.parametrize("tail", [[], ["-r", "udp!127.0.0.1!7222"],
                                  ["-c", "/another"], ["extra"], ["-q"]])
def test_wrong_backend_arguments_refuse(endpoint, tail):
    config, _path, proc = endpoint
    command(proc / "101", ["oscmix", *tail])
    assert diagnostics.backend_status(config, proc).state == "conflict"


@pytest.mark.parametrize("pid", [101, 102])
def test_argv_name_does_not_replace_executable_identity(endpoint, pid):
    config, _path, proc = endpoint
    (proc / str(pid) / "exe").unlink()
    (proc / str(pid) / "exe").symlink_to("/usr/bin/another")
    assert diagnostics.backend_status(config, proc).state != "ready"


def test_other_interface_refused_even_with_same_model(endpoint):
    config, _path, proc = endpoint
    command(proc / "102", ["alsaseqio", "-x", "25:1", "oscmix"])
    result = diagnostics.backend_status(config, proc)
    assert result.state == "conflict"
    assert result.detail == "backend drives another interface"


def test_missing_serial_is_not_a_match(endpoint):
    config, _path, proc = endpoint
    (proc / "asound/seq/clients").write_text('Client  24 : "Fireface UCX II" [Kernel]\n')
    assert diagnostics.backend_status(config, proc).state == "unknown"


@pytest.mark.parametrize("fields", ["00000000 0005 01", "00010000 0002 01",
                                    "00010000 0005 03"])
def test_nonlistening_or_other_socket_types_do_not_identify_owner(endpoint, fields):
    _config, path, proc = endpoint
    (proc / "net/unix").write_text(UNIX_HEADER + "0: 2 0 " + fields + " 501 " + str(path))
    assert process.control_socket_owner(path, proc) is None


@pytest.mark.parametrize("table", ["", "bad header\n", UNIX_HEADER + "malformed\n"])
def test_unreadable_or_malformed_table_is_not_absence(endpoint, table):
    config, _path, proc = endpoint
    (proc / "net/unix").write_text(table)
    assert diagnostics.backend_status(config, proc).state == "unknown"


def test_no_owner_is_unknown_not_ready_or_absent(endpoint):
    config, _path, proc = endpoint
    (proc / "101/fd/3").unlink()
    assert diagnostics.backend_status(config, proc).state == "unknown"


def test_shared_listening_descriptor_does_not_choose_an_arbitrary_owner(endpoint):
    config, _path, proc = endpoint
    (proc / "103/fd").mkdir(parents=True)
    (proc / "103/fd/8").symlink_to(os.readlink(proc / "101/fd/3"))
    assert diagnostics.backend_status(config, proc).state == "unknown"


@pytest.mark.parametrize("client", [24, 25])
def test_multiple_matching_bridges_leave_device_association_unknown(endpoint, client):
    config, _path, proc = endpoint
    shutil.copytree(proc / "102", proc / "103", symlinks=True)
    command(proc / "103", ["alsaseqio", "-x", "%s:1" % client, "oscmix"])
    assert diagnostics.backend_status(config, proc).state == "unknown"


def test_socket_symlink_is_refused(endpoint):
    config, path, proc = endpoint
    renamed = path.with_suffix(".moved")
    path.rename(renamed)
    path.symlink_to(renamed)
    assert diagnostics.backend_status(config, proc).state == "conflict"


def test_endpoint_path_with_spaces_keeps_its_full_name(endpoint):
    _config, path, proc = endpoint
    spaced = path.with_name("a space.control")
    table = (proc / "net/unix").read_text().replace(str(path), str(spaced))
    (proc / "net/unix").write_text(table)
    assert process.control_socket_owner(spaced, proc) == 101


def test_status_of_absent_runtime_does_not_create_it(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("absent")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    config = Config(serial="24216011")
    result = diagnostics.backend_status(config, root / "proc")
    assert result.state == "absent"
    assert not (root / "oscmix-desk").exists()


def test_relative_runtime_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative")
    config = tmp_path / "routing.conf"
    assert locking.control_path(config, "key") == tmp_path / "key.control"


def test_control_requires_a_runtime_directory(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(OSError, match="no runtime"):
        locking.control_path(None, "key")


def test_too_long_endpoint_is_refused_before_socket_io(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("long" * 40)))
    with pytest.raises(OSError, match="Unix socket limit"):
        locking.control_path(None, "key")


def test_two_listening_sockets_with_one_pathname_refuse_association(endpoint):
    _config, path, proc = endpoint
    table = (proc / "net/unix").read_text()
    (proc / "net/unix").write_text(table + "1: 2 0 00010000 0005 01 100502 " + str(path) + "\n")
    with pytest.raises(OSError, match="multiple listening control endpoints"):
        process.control_socket_owner(path, proc)


def test_the_shortest_bridge_command_qualifies(endpoint):
    config, _path, proc = endpoint
    command(proc / "102", ["alsaseqio", "-x", "24:1", "oscmix"])
    assert diagnostics.backend_status(config, proc).state == "ready"


def test_a_bridge_without_the_exclusive_option_never_qualifies(endpoint):
    config, _path, proc = endpoint
    command(proc / "102", ["alsaseqio", "-y", "24:1", "oscmix"])
    assert diagnostics.backend_status(config, proc).state == "unknown"


@pytest.mark.parametrize("missing", ["cmdline", "exe"])
def test_an_unreadable_process_beside_the_backend_does_not_hide_its_bridge(endpoint, missing):
    config, path, proc = endpoint
    # Sorted before the bridge (102) and also a child of the backend (101).
    shutil.copytree(proc / "102", proc / "100", symlinks=True)
    (proc / "100/stat").write_text("100 (alsaseqio) S 101 0 0\n")
    (proc / "100" / missing).unlink()
    assert process.control_holder(path, proc) == process.BackendOwner(101, True, 24, "24216011")
    assert diagnostics.backend_status(config, proc).state == "ready"


def test_an_unreadable_descriptor_does_not_hide_the_socket_owner(endpoint):
    _config, path, proc = endpoint
    for number in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12):
        (proc / "101/fd" / str(number)).write_text("")  # readlink fails on a file
    assert process.control_socket_owner(path, proc) == 101


@pytest.mark.parametrize("words", [
    ["another", "-c", "PATH"],
    ["oscmix", "-z", "-c", "PATH"],
    ["oscmix", "-c", "/another.control"],
    ["oscmix", "-c", "PATH", "extra"],
])
def test_a_listener_that_is_not_this_backend_is_named_but_not_coordinated(endpoint, words):
    _config, path, proc = endpoint
    command(proc / "101", [word.replace("PATH", str(path)) for word in words])
    assert process.control_holder(path, proc) == process.BackendOwner(101, False, None, None)
