"""One resolved interface, from the serial to the socket (ADR 0024).

0.6.8 worked out the interface four times: the unit bound the first
matching sequencer client, pinned the first serial in the card list, a
switch keyed its lock on a rule of its own, and accepted the OSC port
from whoever held it. With one interface the four agreed. The probes of
the review after 0.6.8 showed where they did not, and each of them is a
test here, together with the architecture test the review asked for:
two identical UCX II, `[device] serial` naming the second, and every
path -- the unit's start, a switch, a restore, a reconcile -- taking
that box's lock and reaching that box's backend, and nothing else.
"""

import argparse
import errno
import io
import os
import socket
import stat
import threading
from pathlib import Path

import pytest
from conftest import fake_proc, free_udp_port, repo_file, write_config

from oscmix_desk import profiles
from oscmix_desk import session as session_module
from oscmix_desk.discovery import (
    Device,
    lock_key,
    resolve_device,
    select_seq_client,
    serial_in,
)
from oscmix_desk.errors import DeviceAmbiguous
from oscmix_desk.process import port_holder

A = (24, "24216011")
B = (28, "99887766")
KEY_B = "2a39-3fd9-99887766"
DESK = "[route:main]\nplayback = 1/2\noutput = 1/2\n"


class _Child:
    """A backend that is up for as long as the test needs it."""

    pid = 4242
    returncode = None

    def poll(self):
        return None

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):  # noqa: ARG002 -- Popen's signature
        return 0


def _desk(tmp_path, port, serial=""):
    device = "[device]\nserial = %s\n" % serial if serial else ""
    path = write_config(tmp_path / "desk" / "routing.conf",
                        "%s[osc]\nport = %d\nrecv-port = %d\n%s"
                        % (device, port, free_udp_port(), DESK))
    write_config(tmp_path / "desk" / "profiles" / "b.conf", DESK)
    return path


def _lock_dir(tmp_path, monkeypatch):
    shared = tmp_path / "locks"
    shared.mkdir(mode=0o770, exist_ok=True)
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    return shared


def _record_keys(monkeypatch, module, keys):
    real = module.take_device_lock

    def take(config_path, key=None, wait=None):
        keys.append((module.__name__.rsplit(".", 1)[1], key))
        return real(config_path, key, wait)

    monkeypatch.setattr(module, "take_device_lock", take)


def _run_the_unit(tmp_path, monkeypatch, path, started, reconciled):
    """run_session for real, with only the process and the wire replaced."""
    notified = []
    monkeypatch.setattr(session_module, "sd_notify", notified.append)
    monkeypatch.setattr(session_module, "usb_device_present", lambda *a: True)
    monkeypatch.setattr(session_module, "_cleanup_stale_backend", lambda *a: None)
    monkeypatch.setattr(session_module, "_install_stop_handlers", lambda *a: None)
    monkeypatch.setattr(session_module, "_install_reload_handler", lambda *a: None)
    monkeypatch.setattr(session_module, "_await_backend_port", lambda *a: True)
    monkeypatch.setattr(session_module, "apply_routing", lambda *a, **k: None)
    monkeypatch.setattr(session_module, "verify_and_repair", lambda *a, **k: None)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)

    def start(client, config):
        started.append(client)
        return _Child()

    def reconcile(config, reason, should_stop):
        reconciled.append(config.osc_port)
        return True

    def supervise(child, stop, on_reload=None, reload_requested=None):
        on_reload()                      # one SIGHUP while the unit runs
        return 0

    monkeypatch.setattr(session_module, "_start_backend", start)
    monkeypatch.setattr(session_module, "reconcile_now", reconcile)
    monkeypatch.setattr(session_module, "supervise", supervise)
    config = profiles.load_config(path)
    args = argparse.Namespace(timeout=0.5, dry_run=False, config=path)
    code = session_module.run_session(args, config)
    return code, config, notified


# --------------------------------------------------------------------------
# The architecture test: two identical boxes, the desk for B.
# --------------------------------------------------------------------------

def test_every_path_takes_b_s_lock_and_reaches_only_b(
        tmp_path, monkeypatch, recording_backend):
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A, B],
                     bound=[(port, "oscmix", B[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port, serial=B[1])
    keys, started, reconciled, wired = [], [], [], []
    _record_keys(monkeypatch, session_module, keys)
    _record_keys(monkeypatch, profiles, keys)
    monkeypatch.setattr(profiles, "loopback",
                        lambda send, recv: wired.append(send) or recording_backend)

    code, unit, notified = _run_the_unit(tmp_path, monkeypatch, path,
                                         started, reconciled)
    switched = profiles.switch_profile("b", config_path=path, verify=False)
    restored = profiles.restore_main(config_path=path, verify=False)

    assert code == session_module.EXIT_OK
    assert "READY=1" in notified
    assert started == [B[0]], "the unit bridges B's client, not the first one"
    assert unit.serial == B[1]
    assert [who for who, _key in keys] == ["session", "session",
                                           "profiles", "profiles"], \
        "apply, reconcile, switch and restore each took the lock"
    assert {key for _who, key in keys} == {KEY_B}, keys
    assert switched.applied
    assert restored.applied
    assert wired == [port, port], "the switch and the restore wrote to B's port"
    assert reconciled == [port]


def test_a_switch_refuses_a_backend_that_drives_the_other_box(
        tmp_path, monkeypatch, recording_backend):
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A, B],
                     bound=[(port, "oscmix", A[0])])     # A's backend, on B's port
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port, serial=B[1])
    wired = []
    monkeypatch.setattr(profiles, "loopback",
                        lambda send, recv: wired.append(send) or recording_backend)

    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert outcome.state == profiles.REFUSED
    assert outcome.reason == ("the backend on UDP %d drives the interface "
                              "%s, not %s" % (port, A[1], B[1]))
    assert wired == []
    assert recording_backend.sent == []
    assert not profiles.active_profile_path(path).exists()


def test_two_boxes_and_no_serial_refuse_everywhere(tmp_path, monkeypatch):
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A, B],
                     bound=[(port, "oscmix", A[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port)
    started = []

    code, _unit, notified = _run_the_unit(tmp_path, monkeypatch, path,
                                          started, [])
    switched = profiles.switch_profile("b", config_path=path, verify=False)
    restored = profiles.restore_main(config_path=path, verify=False)

    assert code == session_module.EXIT_CONFIG
    assert notified == []
    assert started == []
    for outcome in (switched, restored):
        assert outcome.state == profiles.REFUSED
        assert "2 interfaces match" in outcome.reason


def test_one_box_gives_the_unit_and_a_switch_the_same_key(
        tmp_path, monkeypatch, recording_backend):
    """The 0.6.8 split in its simplest form: one resolution for both."""
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A],
                     bound=[(port, "oscmix", A[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port)
    keys = []
    _record_keys(monkeypatch, session_module, keys)
    _record_keys(monkeypatch, profiles, keys)
    monkeypatch.setattr(profiles, "loopback", lambda *a: recording_backend)

    _run_the_unit(tmp_path, monkeypatch, path, [], [])
    profiles.switch_profile("b", config_path=path, verify=False)
    assert {key for _who, key in keys} == {lock_key("2a39:3fd9", A[1])}


# --------------------------------------------------------------------------
# The port: bound is not reachable (review probe C, against a real socket).
# --------------------------------------------------------------------------

def test_a_stranger_s_socket_on_the_port_is_refused_and_receives_nothing(
        tmp_path, monkeypatch):
    """A real socket, a real /proc for who holds it, a simulated interface.

    The interface is simulated so the test does not depend on a Fireface
    being switched on: it passed for days on a desk with the UCX II up and
    failed the first time the box was off -- which is every CI runner.
    The port and its owner are the machine's own, which is the point.
    """
    stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stranger.bind(("127.0.0.1", 0))
    stranger.setblocking(False)
    port = stranger.getsockname()[1]
    try:
        monkeypatch.setenv("OSCMIX_PROC_ROOT", "/proc")
        box = fake_proc(tmp_path / "box", boxes=[A])
        resolve = profiles.resolve_device
        monkeypatch.setattr(
            profiles, "resolve_device",
            lambda usb, name, serial, _proc: resolve(usb, name, serial, box))
        _lock_dir(tmp_path, monkeypatch)
        path = _desk(tmp_path, port)
        outcome = profiles.switch_profile("b", config_path=path, verify=False)
        assert outcome.state == profiles.REFUSED, outcome.reason
        assert outcome.reason == (
            "UDP %d is held by pid %d, not by an oscmix backend of this user"
            % (port, os.getpid()))
        with pytest.raises(BlockingIOError):
            stranger.recv(65535)
        assert not profiles.active_profile_path(path).exists()
    finally:
        stranger.close()


def test_the_holder_is_followed_to_the_client_it_bridges(tmp_path):
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", B[0])])
    holder = port_holder(port, proc)
    assert holder is not None
    assert (holder.oscmix, holder.client, holder.serial) == (True, B[0], B[1])


def test_a_bridge_above_the_holder_is_found_too(tmp_path):
    """alsaseqio forks and oscmix is the parent on a real desk; not always."""
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", None)])
    holder_entry = next(p for p in proc.iterdir() if p.name.isdigit())
    parent = proc / "39000"
    (parent / "fd").mkdir(parents=True)
    (parent / "comm").write_text("alsaseqio\n")
    (parent / "stat").write_text("39000 (alsaseqio) S 1 0 0\n")
    (parent / "cmdline").write_bytes(b"alsaseqio\x0028:1\x00oscmix\x00")
    (holder_entry / "stat").write_text("%s (oscmix) S 39000 0 0\n"
                                       % holder_entry.name)
    holder = port_holder(port, proc)
    assert (holder.client, holder.serial) == (B[0], B[1])


def test_a_holder_that_is_not_oscmix_and_one_without_a_bridge(tmp_path):
    port, other = free_udp_port(), free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B],
                     bound=[(port, "python3", None), (other, "oscmix", None)])
    stranger = port_holder(port, proc)
    assert stranger is not None
    assert stranger.oscmix is False
    lone = port_holder(other, proc)
    assert (lone.oscmix, lone.client, lone.serial) == (True, None, None)
    assert port_holder(free_udp_port(), proc) is None


# --------------------------------------------------------------------------
# The lock file: only a regular file, never followed (review probes A).
# --------------------------------------------------------------------------

def _take(key, wait=0.2):
    return profiles.take_device_lock(None, key, wait=wait)


def test_a_symlink_at_the_lock_path_is_refused_and_its_target_untouched(
        tmp_path, monkeypatch, caplog):
    """0.6.8 followed it and chmod'ed the target to 0666."""
    shared = _lock_dir(tmp_path, monkeypatch)
    target = tmp_path / "someone-elses-file"
    target.write_text("secret")
    target.chmod(0o644)
    (shared / "k.lock").symlink_to(target)
    with caplog.at_level("ERROR"):
        assert _take("k") is None
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "symbolic link" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a 0444 FIFO read-write")
def test_a_fifo_at_the_lock_path_does_not_block(tmp_path, monkeypatch, caplog):
    """The read-only fallback blocked in open() until a writer appeared.

    Measured in 0.6.8: still hanging after 25 s, the 30 s lock wait never
    reached. 0444 keeps the read-write attempt from opening it, which is
    how a FIFO owned by someone else reaches the fallback.
    """
    shared = _lock_dir(tmp_path, monkeypatch)
    os.mkfifo(shared / "k.lock", 0o444)
    result = []
    worker = threading.Thread(target=lambda: result.append(_take("k")),
                              daemon=True)
    with caplog.at_level("ERROR"):
        worker.start()
        worker.join(5)
    assert not worker.is_alive(), "take_device_lock is blocked on a FIFO"
    assert result == [None]
    assert "not a regular file" in caplog.text


def test_a_writable_fifo_or_a_directory_is_not_a_lock(tmp_path, monkeypatch):
    shared = _lock_dir(tmp_path, monkeypatch)
    os.mkfifo(shared / "f.lock", 0o666)
    (shared / "d.lock").mkdir()
    assert _take("f") is None
    assert _take("d") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens anything")
def test_a_lock_file_nobody_here_can_open_is_a_refusal_that_says_why(
        tmp_path, monkeypatch, caplog):
    shared = _lock_dir(tmp_path, monkeypatch)
    (shared / "k.lock").write_text("")
    (shared / "k.lock").chmod(0)
    with caplog.at_level("ERROR"):
        assert _take("k") is None
    assert "cannot open the device lock" in caplog.text
    assert "this user cannot open it" in caplog.text


def test_a_lock_file_with_another_link_keeps_its_mode(tmp_path, monkeypatch):
    """A hardlink is someone's file under a second name; not ours to chmod."""
    shared = _lock_dir(tmp_path, monkeypatch)
    original = shared / "elsewhere"
    original.write_text("")
    original.chmod(0o644)
    os.link(original, shared / "k.lock")
    held = _take("k")
    assert held is not None
    held.release()
    assert stat.S_IMODE(original.stat().st_mode) == 0o644


def test_a_new_lock_file_takes_the_directory_s_group(tmp_path, monkeypatch):
    others = [g for g in os.getgroups() if g != os.getegid()]
    if not others:
        pytest.skip("the test user belongs to one group only")
    shared = _lock_dir(tmp_path, monkeypatch)
    try:
        os.chown(shared, -1, others[0])
    except OSError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("cannot hand the directory to a supplementary group")
        raise
    umask = os.umask(0o077)
    try:
        held = _take("k")
    finally:
        os.umask(umask)
    assert held is not None
    held.release()
    info = (shared / "k.lock").stat()
    assert (stat.S_IMODE(info.st_mode), info.st_gid) == (0o660, others[0])


# --------------------------------------------------------------------------
# The resolution itself.
# --------------------------------------------------------------------------

def test_the_serial_is_read_from_a_device_name():
    assert serial_in("Fireface UCX II (24216011)") == "24216011"
    assert serial_in("Fireface UCX II") is None
    assert serial_in("Fireface UCX II (123)") is None


def test_the_client_is_selected_by_serial_and_never_guessed():
    text = ('Client  24 : "Fireface UCX II (24216011)" [Kernel Legacy]\n'
            'Client  28 : "Fireface UCX II (99887766)" [Kernel Legacy]\n')
    assert select_seq_client(text, "Fireface UCX II", B[1]) == B[0]
    assert select_seq_client(text, "Fireface UCX II", "11111111") is None
    assert select_seq_client(text[:text.index("Client  28")],
                             "Fireface UCX II") == A[0]
    with pytest.raises(DeviceAmbiguous) as raised:
        select_seq_client(text, "Fireface UCX II")
    assert ("Fireface UCX II (24216011) (client 24), "
            "Fireface UCX II (99887766) (client 28)") in str(raised.value)


def test_the_card_list_decides_when_no_client_is_up(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A, B])
    (proc / "asound" / "seq" / "clients").write_text("")
    with pytest.raises(DeviceAmbiguous) as raised:
        resolve_device("2a39:3fd9", "Fireface UCX II", "", proc)
    assert "24216011, 99887766" in str(raised.value)
    assert resolve_device("2a39:3fd9", "Fireface UCX II", B[1], proc) == \
        Device(usb_id="2a39:3fd9", serial=B[1], client=None)

    one = fake_proc(tmp_path / "one", boxes=[A])
    (one / "asound" / "seq" / "clients").write_text("")
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", one).serial == A[1]
    assert Device("2a39:3fd9", "", None).key == "2a39-3fd9-unknown"


# --------------------------------------------------------------------------
# What ships: the directory's trust circle, and the tools that write.
# --------------------------------------------------------------------------

def test_the_lock_directory_belongs_to_the_audio_group():
    """1777 let any local user pre-create or hold a lock (0.6.8 probes)."""
    lines = [line for line in repo_file("systemd", "tmpfiles.d",
                                        "oscmix-desk.conf").read_text().splitlines()
             if line and not line.startswith("#")]
    assert lines == ["d /run/oscmix-desk 3770 root audio -",
                     "z /run/oscmix-desk/*.lock 0660 - audio -"]


def test_the_installer_says_when_the_user_is_not_in_audio():
    assert "usermod -aG audio" in repo_file("install.sh").read_text()


def test_the_sweep_resolves_its_device_and_refuses_two():
    source = repo_file("scripts", "sweep-writes.py").read_text()
    assert "resolve_device(" in source
    assert "except DeviceAmbiguous" in source


# --------------------------------------------------------------------------
# The edges of the identity: other users, missing files, the unit sandbox.
# --------------------------------------------------------------------------

def test_another_user_s_oscmix_is_not_this_desk_s_backend(tmp_path, monkeypatch):
    """Name is not ownership; for root, any user's oscmix is still oscmix."""
    from oscmix_desk import process

    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", B[0])])
    someone_else = os.getuid() + 1       # read before os.getuid is replaced
    monkeypatch.setattr(process.os, "getuid", lambda: someone_else)
    assert port_holder(port, proc).oscmix is False
    monkeypatch.setattr(process.os, "getuid", lambda: 0)
    assert port_holder(port, proc).oscmix is True


def test_a_known_client_without_a_readable_client_list_has_no_serial(tmp_path):
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", B[0])])
    (proc / "asound" / "seq" / "clients").unlink()
    holder = port_holder(port, proc)
    assert (holder.client, holder.serial) == (B[0], None)


def test_a_lock_file_that_cannot_be_regrouped_is_still_a_lock(
        tmp_path, monkeypatch):
    """Measured in the unit: fchown to `audio` fails with EINVAL there.

    A sandboxed user service runs in a user namespace that maps only the
    user's own group. The mode still changes, the lock is still held,
    and nothing is raised (ADR 0024).
    """
    _lock_dir(tmp_path, monkeypatch)
    others = [g for g in os.getgroups() if g != os.getegid()]
    if not others:
        # With one group the directory's group is the file's already, so
        # fchown is never reached and this would pass for no reason.
        pytest.skip("the test user belongs to one group only")
    os.chown(tmp_path / "locks", -1, others[0])

    def refuse(*_args):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(profiles.os, "fchown", refuse)
    umask = os.umask(0o077)
    try:
        held = _take("k")
    finally:
        os.umask(umask)
    assert held is not None
    held.release()
    assert stat.S_IMODE((tmp_path / "locks" / "k.lock").stat().st_mode) == 0o660


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens anything")
def test_a_refusal_in_a_directory_of_an_unknown_group_still_says_why(
        tmp_path, monkeypatch, caplog):
    shared = _lock_dir(tmp_path, monkeypatch)
    shared.chmod(0o500)

    def unknown(_gid):
        raise KeyError(_gid)

    monkeypatch.setattr(profiles.grp, "getgrgid", unknown)
    try:
        with caplog.at_level("ERROR"):
            assert _take("k") is None
    finally:
        shared.chmod(0o770)
    assert "belongs to group an unknown group" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root creates anything")
def test_a_read_only_lock_directory_is_named_as_such(tmp_path, monkeypatch,
                                                      caplog):
    """Under a sandbox that applies ProtectSystem=strict, /run is read-only.

    The shipped unit could not create a lock file there -- measured under
    the system manager, which applies the sandbox -- and the refusal said
    "No such file or directory". It names the cause and the fix now.
    """
    shared = _lock_dir(tmp_path, monkeypatch)
    shared.chmod(0o500)
    real = os.statvfs

    class ReadOnly:
        f_flag = os.ST_RDONLY

    monkeypatch.setattr(profiles.os, "statvfs",
                        lambda p: ReadOnly() if str(p) == str(shared) else real(p))
    try:
        with caplog.at_level("ERROR"):
            assert _take("k") is None
    finally:
        shared.chmod(0o770)
    assert ("%s is read-only for this process; a service needs "
            "ReadWritePaths=-%s" % (shared, shared)) in caplog.text


def test_the_unit_declares_the_lock_directory_writable():
    unit = repo_file("systemd", "oscmix.service").read_text()
    assert "\nReadWritePaths=-/run/oscmix-desk\n" in unit


def test_a_snapshot_names_the_resolved_box(tmp_path, monkeypatch):
    from oscmix_desk import Config, cli

    one = fake_proc(tmp_path / "one", boxes=[B])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(one))
    assert cli._snapshot_serial(Config()) == B[1]
    two = fake_proc(tmp_path / "two", boxes=[A, B])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(two))
    assert cli._snapshot_serial(Config()) == "ambiguous"
    assert cli._snapshot_serial(Config(serial=A[1])) == A[1]
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(fake_proc(tmp_path / "none")))
    assert cli._snapshot_serial(Config()) == "?"


def test_hardware_evidence_refuses_to_name_one_of_two_boxes(tmp_path,
                                                             monkeypatch):
    import importlib.util

    from oscmix_desk import Config

    spec = importlib.util.spec_from_file_location(
        "verify_hardware", repo_file("scripts", "verify-hardware.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setenv("OSCMIX_PROC_ROOT",
                       str(fake_proc(tmp_path, boxes=[A, B])))
    with pytest.raises(DeviceAmbiguous):
        tool.device_serial(Config())
    assert tool.device_serial(Config(serial=B[1])) == B[1]


# --------------------------------------------------------------------------
# What an independent review of this release found before it shipped.
# --------------------------------------------------------------------------

def _add_clients(proc, text):
    clients = proc / "asound" / "seq" / "clients"
    clients.write_bytes(clients.read_bytes() + text)


def test_a_backend_that_changes_during_the_lock_wait_is_refused_after_it(
        tmp_path, monkeypatch, recording_backend):
    """Checked before a 30 s wait and never again, the switch wrote anyway."""
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A, B],
                     bound=[(port, "oscmix", B[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles, "SWITCH_LOCK_WAIT", 5.0)
    path = _desk(tmp_path, port, serial=B[1])
    wired = []
    monkeypatch.setattr(profiles, "loopback",
                        lambda send, recv: wired.append(send) or recording_backend)
    held = profiles.take_device_lock(None, KEY_B)
    assert held is not None

    def backend_swapped_then_lock_released():
        bridge = next(p for p in proc.iterdir() if p.name.isdigit()
                      and (p / "comm").read_text().strip() == "alsaseqio")
        (bridge / "cmdline").write_bytes(b"alsaseqio\x0024:1\x00oscmix\x00")
        held.release()

    threading.Timer(0.3, backend_swapped_then_lock_released).start()
    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert outcome.state == profiles.REFUSED
    assert outcome.reason == ("the backend on UDP %d drives the interface "
                              "%s, not %s" % (port, A[1], B[1]))
    assert wired == []
    assert recording_backend.sent == []
    assert not profiles.active_profile_path(path).exists()


def test_names_that_are_not_utf8_do_not_break_a_switch(tmp_path, monkeypatch,
                                                        recording_backend):
    """The kernel cuts comm at 15 bytes, and user space names its clients."""
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[B], bound=[(port, "oscmix", B[0])])
    odd = proc / "45000"
    (odd / "fd").mkdir(parents=True)
    (odd / "comm").write_bytes(b"abc\xce\n")
    (odd / "stat").write_bytes(b"45000 (abc\xce) S 1 0 0\n")
    (odd / "cmdline").write_bytes(b"")
    _add_clients(proc, b'Client 130 : "\xff\xfe" [User Legacy]\n')
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port, serial=B[1])
    monkeypatch.setattr(profiles, "loopback", lambda *a: recording_backend)
    assert profiles.switch_profile("b", config_path=path, verify=False).applied


def test_a_backend_left_without_its_interface_is_not_written_to(
        tmp_path, monkeypatch, recording_backend):
    """No card, no client, an oscmix still on the port, no serial configured.

    The serial comparisons had nothing to compare, and the switch keyed on
    `2a39-3fd9-unknown` beside the unit's own lock.
    """
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", bound=[(port, "oscmix", B[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port)
    keys = []
    _record_keys(monkeypatch, profiles, keys)
    monkeypatch.setattr(profiles, "loopback", lambda *a: recording_backend)
    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert outcome.state == profiles.REFUSED
    assert outcome.reason == ("2a39:3fd9 is not visible to ALSA, so no backend "
                              "can be driving it")
    assert keys == []


def test_a_fireface_of_another_model_is_not_a_second_candidate(tmp_path):
    """Counting every Fireface line made a UCX II beside an 802 ambiguous."""
    proc = fake_proc(tmp_path, boxes=[A])
    cards = proc / "asound" / "cards"
    cards.write_text(cards.read_text()
                     + " 5 [Fireface802 ]: USB-Audio - Fireface 802 (23456789)\n")
    _add_clients(proc, b'Client  32 : "Fireface 802 (23456789)" [Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])


def test_a_second_box_still_enumerating_is_not_ignored(tmp_path):
    """Its card is listed, its client is not up yet: not a choice either."""
    proc = fake_proc(tmp_path, boxes=[A, B])
    (proc / "asound" / "seq" / "clients").write_text(
        'Client  24 : "Fireface UCX II (24216011)" [Kernel Legacy]\n')
    with pytest.raises(DeviceAmbiguous):
        resolve_device("2a39:3fd9", "Fireface UCX II", "", proc)


def test_a_user_space_client_cannot_pose_as_the_interface(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A])
    _add_clients(proc, b'Client 129 : "Fireface UCX II (99887766)" [User Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])
    assert resolve_device("2a39:3fd9", "Fireface UCX II", B[1], proc).client \
        is None


def test_a_configured_box_that_is_not_plugged_in_is_a_clean_no_op(
        tmp_path, monkeypatch):
    """Another box of the model made USB presence say "connected".

    The start then failed after its wait and was restarted for ever.
    """
    proc = fake_proc(tmp_path / "proc", boxes=[A])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, free_udp_port(), serial=B[1])
    started = []
    code, _unit, notified = _run_the_unit(tmp_path, monkeypatch, path,
                                          started, [])
    assert code == session_module.EXIT_OK
    assert notified == ["READY=1"]
    assert started == []


def test_a_serial_with_letters_is_a_config_error(tmp_path):
    """It passed validation and could never be matched by the selection."""
    from oscmix_desk.errors import ConfigError

    path = write_config(tmp_path / "routing.conf",
                        "[device]\nserial = ABC123\n" + DESK)
    with pytest.raises(ConfigError, match="digits only"):
        profiles.load_config(path)


def test_a_snapshot_names_the_box_its_backend_drives(tmp_path, monkeypatch):
    from oscmix_desk import Config, cli

    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[A, B], bound=[(port, "oscmix", A[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    assert cli._snapshot_serial(Config(serial=B[1], osc_port=port)) == A[1]


def test_the_sweep_refuses_two_boxes_before_it_takes_anything(monkeypatch,
                                                              capsys):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "sweep_writes_identity", repo_file("scripts", "sweep-writes.py"))
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)

    def ambiguous(*_args):
        raise DeviceAmbiguous("2 interfaces match 'Fireface UCX II'")

    taken = []
    monkeypatch.setattr(sweep, "resolve_device", ambiguous)
    monkeypatch.setattr(sweep, "take_device_lock",
                        lambda *a, **k: taken.append(a))
    monkeypatch.setattr(sys, "argv", ["sweep-writes.py"])
    assert sweep.main() == 1
    assert taken == []
    assert "the sweep supports one interface" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The re-review: names that forge lines, models that share a prefix, and a
# misconfigured desk that must not look unplugged.
# --------------------------------------------------------------------------

KERNEL_A = 'Client  24 : "Fireface UCX II (24216011)" [Kernel Legacy]\n'
KERNEL_B = 'Client  28 : "Fireface UCX II (99887766)" [Kernel Legacy]\n'


def test_a_quote_in_a_client_name_does_not_make_it_a_kernel_client():
    spoof = 'Client 130 : "Fireface UCX II (99887766)" [Kernel" [User Legacy]\n'
    assert select_seq_client(KERNEL_A + spoof, "Fireface UCX II", B[1]) is None
    assert select_seq_client(KERNEL_A + spoof, "Fireface UCX II") == A[0]


def test_a_forged_line_that_repeats_a_client_number_is_refused_as_such():
    """A name with a newline can forge a whole line for an existing client.

    Nothing says which of the two lines is real. Dropping both made the
    real interface vanish and the start loop; a refusal names the cause.
    """
    forged = ('Client 130 : "x\nClient  28 : "Fireface UCX II (24216011)" '
              '[Kernel Legacy]\ny" [User Legacy]\n')
    with pytest.raises(DeviceAmbiguous, match="listed more than once"):
        select_seq_client(KERNEL_B + forged, "Fireface UCX II")


def test_a_forged_client_for_a_box_no_card_shows_is_not_an_interface(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A])
    _add_clients(proc, b'Client 140 : "Fireface UCX II (99887766)" '
                       b'[Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", B[1], proc).client \
        is None
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])


def test_a_model_whose_name_starts_another_is_matched_exactly(tmp_path):
    clients = ('Client  24 : "Fireface 802 (11112222)" [Kernel Legacy]\n'
               'Client  28 : "Fireface 802 FS (33334444)" [Kernel Legacy]\n')
    assert select_seq_client(clients, "Fireface 802") == 24
    assert select_seq_client(clients, "Fireface 802 FS") == 28
    # A name with its serial is still a name, and a part of one still
    # matches when nothing matches exactly.
    assert select_seq_client(clients, "Fireface 802 FS (33334444)") == 28
    assert select_seq_client(KERNEL_A, "UCX II") == A[0]
    cards = tmp_path / "cards"
    cards.write_text(" 2 [F802 ]: USB-Audio - Fireface 802 (11112222)\n"
                     " 3 [F802FS ]: USB-Audio - Fireface 802 FS (33334444)\n")
    from oscmix_desk.discovery import device_serials
    assert device_serials(cards, "Fireface 802") == ["11112222"]
    assert device_serials(cards) == ["11112222", "33334444"]


def test_a_desk_named_for_the_wrong_model_fails_instead_of_looking_unplugged(
        tmp_path, monkeypatch):
    """The box is there under another model name: exit 1, not "nothing to do"."""
    proc = fake_proc(tmp_path / "proc")
    (proc / "asound" / "cards").write_text(
        " 2 [UFX ]: USB-Audio - Fireface UFX II (55554444)\n")
    (proc / "asound" / "seq" / "clients").write_text(
        'Client  24 : "Fireface UFX II (55554444)" [Kernel Legacy]\n')
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, free_udp_port(), serial="55554444")
    code, _unit, notified = _run_the_unit(tmp_path, monkeypatch, path, [], [])
    assert code == session_module.EXIT_FAILURE
    assert notified == []

    (proc / "asound" / "cards").unlink()          # and an unreadable list
    code, _unit, notified = _run_the_unit(tmp_path, monkeypatch, path, [], [])
    assert code == session_module.EXIT_FAILURE
    assert notified == []


def test_a_forged_kernel_line_without_a_serial_or_under_a_bare_name_is_ignored(
        tmp_path):
    """Its name is not a card's product name, so it is not an interface."""
    proc = fake_proc(tmp_path, boxes=[A])
    _add_clients(proc, b'Client 140 : "Fireface UCX II" [Kernel Legacy]\n'
                       b'Client 141 : "UCX II" [Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])
    assert resolve_device("2a39:3fd9", "UCX II", "", proc).client == A[0]

    unplugged = fake_proc(tmp_path / "unplugged")
    _add_clients(unplugged, b'Client 140 : "Fireface UCX II" [Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "",
                          unplugged).client is None


def test_a_client_listed_before_its_card_waits_for_the_card(tmp_path):
    """The kernel may show the client a moment before the card line."""
    proc = fake_proc(tmp_path, boxes=[A])
    cards = proc / "asound" / "cards"
    listed = cards.read_text()
    cards.write_text("")
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc).client \
        is None
    cards.write_text(listed)
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc).client \
        == A[0]


# --------------------------------------------------------------------------
# What the mutation run of this release left unobserved.
# --------------------------------------------------------------------------

def test_the_serial_comes_from_the_client_when_the_card_list_is_unreadable(
        tmp_path):
    proc = fake_proc(tmp_path, boxes=[B])
    (proc / "asound" / "cards").unlink()
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=B[1], client=B[0])


def test_waiting_returns_the_device_once_its_client_comes_up(tmp_path):
    from oscmix_desk.discovery import wait_for_device

    proc = fake_proc(tmp_path, boxes=[B])
    clients = proc / "asound" / "seq" / "clients"
    listed = clients.read_text()
    clients.write_text("")
    threading.Timer(0.3, lambda: clients.write_text(listed)).start()
    assert wait_for_device("2a39:3fd9", "Fireface UCX II", "", 5.0, proc) == \
        Device(usb_id="2a39:3fd9", serial=B[1], client=B[0])


def test_a_card_list_that_is_not_utf8_is_still_read(tmp_path):
    from oscmix_desk.discovery import card_products

    cards = tmp_path / "cards"
    cards.write_bytes(b" 0 [X ]: HDA-Intel - HDA \xff\xfe\n"
                      b" 2 [II24216011 ]: USB-Audio - Fireface UCX II (24216011)\n")
    assert card_products(cards)[1] == "Fireface UCX II (24216011)"


def _holder(tmp_path, comm, argv, children=(), stat_comm=None):
    """A /proc with one UDP holder whose comm, argv and children we choose."""
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", None)])
    entry = next(p for p in proc.iterdir() if p.name.isdigit())
    (entry / "comm").write_bytes(comm)
    (entry / "cmdline").write_bytes(argv)
    for pid, child_stat, child_argv in children:
        child = proc / str(pid)
        (child / "fd").mkdir(parents=True)
        if child_stat is not None:
            (child / "stat").write_bytes(child_stat % entry.name.encode())
        if child_argv is not None:
            (child / "cmdline").write_bytes(child_argv)
    return port, proc


def test_an_oscmix_is_known_by_its_name_or_by_its_program(tmp_path):
    by_program = _holder(tmp_path / "a", b"osc-renamed\n",
                         b"/home/u/.local/bin/oscmix\x00-r\x00udp\x00")
    by_name = _holder(tmp_path / "b", b"oscmix\n", b"/usr/bin/python3\x00x\x00")
    neither = _holder(tmp_path / "c", b"python3\n", b"/usr/bin/python3\x00x\x00")
    assert port_holder(*by_program).oscmix is True
    assert port_holder(*by_name).oscmix is True
    assert port_holder(*neither).oscmix is False


def test_an_unreadable_or_undecodable_holder_is_not_an_oscmix(tmp_path):
    port, proc = _holder(tmp_path / "a", b"oscmix\n", b"oscmix\x00")
    entry = next(p for p in proc.iterdir() if p.name.isdigit())
    (entry / "comm").unlink()
    assert port_holder(port, proc).oscmix is False
    odd = _holder(tmp_path / "b", b"\xff\xfe\n", b"/x/\xffoscmix\x00")
    assert port_holder(*odd).oscmix is False


def test_the_bridge_is_found_past_an_unreadable_sibling_and_a_bracket_in_a_name(
        tmp_path):
    port, proc = _holder(
        tmp_path, b"oscmix\n", b"oscmix\x00",
        children=[
            (39999, b"39999 (gone) S %s 0 0\n", None),         # no cmdline
            (40005, b"40005 (als)aseqio) S %s 0 0\n",
             b"alsaseqio\x0028:1\x00oscmix\x00"),
        ])
    holder = port_holder(port, proc)
    assert (holder.client, holder.serial) == (B[0], B[1])


def test_a_start_with_no_client_reads_the_real_usb_presence(tmp_path,
                                                             monkeypatch):
    """The start's no-client path, with sysfs as it is, not a stand-in."""
    import argparse

    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "wait_for_device", lambda *a: None)
    monkeypatch.setattr(session_module, "sd_notify", lambda message: None)
    proc = fake_proc(tmp_path / "proc")
    args = argparse.Namespace(timeout=0.1)
    empty = tmp_path / "no-usb"
    empty.mkdir()
    assert session_module._find_client(args, Config(), proc, empty) == \
        (None, session_module.EXIT_OK)
    present = tmp_path / "usb"
    (present / "5-2").mkdir(parents=True)
    (present / "5-2" / "idVendor").write_text("2a39\n")
    (present / "5-2" / "idProduct").write_text("3fd9\n")
    assert session_module._find_client(args, Config(), proc, present) == \
        (None, session_module.EXIT_FAILURE)


def test_a_start_names_an_interface_the_kernel_has_not_authorized(
        tmp_path, monkeypatch, caplog):
    """Measured with `authorized=0`: every start said "is snd-usb-audio
    loaded?" about a box USBGuard or a hand had held back. Still a
    failure -- the retry is what brings the desk up once it is allowed."""
    import argparse

    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "wait_for_device", lambda *a: None)
    proc = fake_proc(tmp_path / "proc")
    present = tmp_path / "usb"
    (present / "5-2").mkdir(parents=True)
    (present / "5-2" / "idVendor").write_text("2a39\n")
    (present / "5-2" / "idProduct").write_text("3fd9\n")
    (present / "5-2" / "authorized").write_text("0\n")
    with caplog.at_level("ERROR"):
        assert session_module._find_client(
            argparse.Namespace(timeout=0.1), Config(), proc, present) == \
            (None, session_module.EXIT_FAILURE)
    assert "the kernel has not authorized it" in caplog.text
    assert "snd-usb-audio" not in caplog.text


def test_a_restore_also_checks_again_after_the_lock_wait(
        tmp_path, monkeypatch, recording_backend):
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A, B],
                     bound=[(port, "oscmix", B[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles, "SWITCH_LOCK_WAIT", 5.0)
    path = _desk(tmp_path, port, serial=B[1])
    monkeypatch.setattr(profiles, "loopback", lambda *a: recording_backend)
    held = profiles.take_device_lock(None, KEY_B)

    def swapped_then_released():
        bridge = next(p for p in proc.iterdir() if p.name.isdigit()
                      and (p / "comm").read_text().strip() == "alsaseqio")
        (bridge / "cmdline").write_bytes(b"alsaseqio\x0024:1\x00oscmix\x00")
        held.release()

    threading.Timer(0.3, swapped_then_released).start()
    outcome = profiles.restore_main(config_path=path, verify=False)
    assert outcome.state == profiles.REFUSED
    assert outcome.name == "routing.conf"
    assert outcome.reason == ("the backend on UDP %d drives the interface "
                              "%s, not %s" % (port, A[1], B[1]))
    assert recording_backend.sent == []


def test_the_switch_refusal_after_the_wait_names_the_profile(
        tmp_path, monkeypatch, recording_backend):
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[B], bound=[(port, "oscmix", B[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    _lock_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles, "SWITCH_LOCK_WAIT", 5.0)
    path = _desk(tmp_path, port, serial=B[1])
    held = profiles.take_device_lock(None, KEY_B)

    def backend_gone_then_released():
        (proc / "net" / "udp").write_text("  sl  local_address\n")
        held.release()

    threading.Timer(0.3, backend_gone_then_released).start()
    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert (outcome.state, outcome.name) == (profiles.REFUSED, "b")
    assert "nothing is listening on UDP" in outcome.reason


def test_a_restore_that_cannot_reach_its_interface_takes_no_lock(
        tmp_path, monkeypatch, recording_backend):
    port = free_udp_port()
    monkeypatch.setenv("OSCMIX_PROC_ROOT",
                       str(fake_proc(tmp_path / "proc", bound=[(port, "oscmix", 28)])))
    _lock_dir(tmp_path, monkeypatch)
    keys = []
    _record_keys(monkeypatch, profiles, keys)
    outcome = profiles.restore_main(config_path=_desk(tmp_path, port),
                                    verify=False)
    assert outcome.state == profiles.REFUSED
    assert keys == [], "refused before the lock, like a bad config"


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches anything")
def test_a_lock_directory_that_cannot_be_searched_says_permission_and_group(
        tmp_path, monkeypatch, caplog):
    """What a user outside `audio` sees for /run/oscmix-desk (measured)."""
    shared = _lock_dir(tmp_path, monkeypatch)
    shared.chmod(0o600)                 # readable, not searchable
    try:
        with caplog.at_level("ERROR"):
            assert _take("k") is None
    finally:
        shared.chmod(0o770)
    record = next(r.getMessage() for r in caplog.records
                  if "cannot open the device lock" in r.getMessage())
    assert record.endswith(": Permission denied; %s belongs to group %s"
                           % (shared, profiles._group_of(shared)))


def test_a_snapshot_reads_the_real_proc_by_default(monkeypatch):
    from pathlib import Path

    from oscmix_desk import Config, cli

    seen = []
    monkeypatch.delenv("OSCMIX_PROC_ROOT", raising=False)
    monkeypatch.setattr(cli, "port_holder",
                        lambda port, proc: seen.append(proc) or None)
    monkeypatch.setattr(cli, "resolve_device",
                        lambda usb, name, serial, proc: seen.append(proc)
                        or Device(usb, B[1], B[0]))
    assert cli._snapshot_serial(Config()) == B[1]
    assert seen == [Path("/proc"), Path("/proc")]


# --------------------------------------------------------------------------
# 0.6.10: two sessions on one port, and a switch of a desk the unit does
# not run.
# --------------------------------------------------------------------------

def _backend_with_parent(tmp_path, parent_argv):
    """A /proc where an oscmix holds a port and its parent is ``parent_argv``."""
    from oscmix_desk.process import _cleanup_stale_backend

    port = free_udp_port()
    proc = fake_proc(tmp_path, bound=[(port, "oscmix", None)])
    holder = next(p for p in proc.iterdir() if p.name.isdigit())
    parent = proc / "39000"
    (parent / "fd").mkdir(parents=True)
    (parent / "comm").write_text("python3\n")
    (parent / "stat").write_text("39000 (python3) S 1 0 0\n")
    (parent / "cmdline").write_bytes(b"\0".join(a.encode() for a in parent_argv) + b"\0")
    (holder / "stat").write_text("%s (oscmix) S 39000 0 0\n" % holder.name)
    return port, proc, holder, _cleanup_stale_backend


def test_a_backend_of_a_live_session_is_not_stale(tmp_path, monkeypatch):
    """A second session by hand terminated the unit's backend (measured)."""
    from oscmix_desk import process

    port, proc, _holder, cleanup = _backend_with_parent(
        tmp_path, ["python3", "/home/u/.local/bin/oscmix-session"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    killed = []
    monkeypatch.setattr(process, "_terminate", killed.append)
    assert cleanup(port, proc) == 39000
    assert killed == []


def test_a_backend_whose_session_is_gone_is_stale(tmp_path, monkeypatch):
    from oscmix_desk import process

    port, proc, holder, cleanup = _backend_with_parent(tmp_path, ["bash"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    monkeypatch.setattr(process, "STALE_BACKEND_SETTLE", 0.0)
    killed = []
    monkeypatch.setattr(process, "_terminate", killed.append)
    assert cleanup(port, proc) is None
    assert killed == [int(holder.name)]
    # Reparented to init after its session died: stale as well.
    (holder / "stat").write_text("%s (oscmix) S 1 0 0\n" % holder.name)
    killed.clear()
    assert cleanup(port, proc) is None
    assert killed == [int(holder.name)]


def _unit(environ=None, argv=("oscmix-session",), cwd="/"):
    """A stubbed `unit_process` answer."""
    from oscmix_desk.process import UnitProcess

    return lambda *a: UnitProcess(argv=tuple(argv), environ=dict(environ or {}),
                                  cwd=Path(cwd))


def test_a_switch_of_another_desk_does_not_reload_the_unit(tmp_path, monkeypatch,
                                                            capsys):
    """The unit re-applied its own routing.conf over the switch (0.6.9)."""
    from oscmix_desk import cli, profiles

    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    outcome = profiles.Outcome(state=profiles.APPLIED_UNVERIFIED, name="x",
                               reason=profiles.NOT_CHECKED, persisted=True)
    unit_desk = tmp_path / "unit" / "routing.conf"
    unit_desk.parent.mkdir()
    unit_desk.write_text(DESK)
    monkeypatch.setattr(cli, "unit_process", _unit())
    monkeypatch.setattr(cli, "discover_config_path", lambda *a: unit_desk)
    assert cli._report_outcome(outcome, tmp_path / "other.conf") == cli.EXIT_OK
    assert reloads == []
    assert cli._report_outcome(outcome, unit_desk) == cli.EXIT_OK
    assert cli._report_outcome(outcome, None) == cli.EXIT_OK
    assert reloads == [1, 1]
    capsys.readouterr()


# --------------------------------------------------------------------------
# 0.6.10, second round: the unit's desk is what the unit's environment
# resolves; an empty profile name is a switch; socket errors in the
# verifier and the reconcile are log lines.
# --------------------------------------------------------------------------

def test_the_reload_follows_the_unit_s_environment_not_the_shell_s(
        tmp_path, monkeypatch, capsys):
    from oscmix_desk import cli, profiles

    unit_desk = tmp_path / "unit" / "routing.conf"
    other = tmp_path / "other" / "routing.conf"
    for path in (unit_desk, other):
        path.parent.mkdir()
        path.write_text(DESK)
    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    outcome = profiles.Outcome(state=profiles.APPLIED_UNVERIFIED, name="x",
                               reason=profiles.NOT_CHECKED, persisted=True)
    # The unit names its desk in its own Environment=.
    monkeypatch.setattr(cli, "unit_process",
                        _unit({"OSCMIX_CONFIG": str(unit_desk)}))
    # The shell's OSCMIX_CONFIG names another file: no --config, and the
    # switch was still for the other desk (the 0.6.9 bug via the env).
    monkeypatch.setenv("OSCMIX_CONFIG", str(other))
    assert cli._report_outcome(outcome, other) == cli.EXIT_OK
    assert reloads == []
    # A --config naming the unit's own file is the unit's desk.
    assert cli._report_outcome(outcome, unit_desk) == cli.EXIT_OK
    assert reloads == [1]
    # No unit process to read: reload as before.
    monkeypatch.setattr(cli, "unit_process", lambda *a: None)
    assert cli._report_outcome(outcome, other) == cli.EXIT_OK
    assert reloads == [1, 1]
    capsys.readouterr()


def test_the_unit_s_desk_is_resolved_in_the_unit_s_environment(tmp_path,
                                                                monkeypatch):
    """XDG_CONFIG_HOME and HOME of the unit, not of this shell.

    The first cut resolved only OSCMIX_CONFIG from the unit and took
    the XDG fallback from the shell, so `XDG_CONFIG_HOME=/x
    oscmix-session --profile Y` reloaded a unit that runs ~/.config.
    """
    from oscmix_desk import cli

    def desk(root):
        (root / "oscmix").mkdir(parents=True)
        (root / "oscmix" / "routing.conf").write_text(DESK)
        return root / "oscmix" / "routing.conf"

    shell = desk(tmp_path / "shell")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(shell.parent.parent))
    monkeypatch.setenv("OSCMIX_CONFIG", str(tmp_path / "elsewhere.conf"))
    by_xdg = desk(tmp_path / "unit-xdg")
    monkeypatch.setattr(cli, "unit_process", _unit({
        "XDG_CONFIG_HOME": str(by_xdg.parent.parent)}))
    assert cli._unit_desk() == by_xdg
    by_home = desk(tmp_path / "unit-home" / ".config")
    monkeypatch.setattr(cli, "unit_process", _unit({
        "HOME": str(tmp_path / "unit-home")}))
    assert cli._unit_desk() == by_home
    # OSCMIX_CONFIG in the unit wins over both, existing or not.
    monkeypatch.setattr(cli, "unit_process", _unit({
        "OSCMIX_CONFIG": str(tmp_path / "named.conf"), "HOME": str(tmp_path)}))
    assert cli._unit_desk() == tmp_path / "named.conf"


def test_the_unit_s_desk_is_its_config_argument_before_its_environment(
        tmp_path, monkeypatch):
    """A unit started with `--config /x` runs /x whatever its environment
    says, as session._config_path reads it; a relative path is against
    the unit's working directory, not this shell's."""
    from oscmix_desk import cli

    environ = {"OSCMIX_CONFIG": str(tmp_path / "env.conf")}
    (tmp_path / "shell").mkdir()
    monkeypatch.chdir(tmp_path / "shell")
    for argv in (("oscmix-session", "--config", "/x/routing.conf"),
                 ("oscmix-session", "--config=/x/routing.conf"),
                 ("oscmix-session", "--conf", "/x/routing.conf", "--timeout", "5")):
        monkeypatch.setattr(cli, "unit_process", _unit(environ, argv, "/unit"))
        assert cli._unit_desk() == Path("/x/routing.conf"), argv
    monkeypatch.setattr(cli, "unit_process",
                        _unit(environ, ("oscmix-session", "--config", "desk/r.conf"),
                              "/unit"))
    assert cli._unit_desk() == Path("/unit/desk/r.conf")
    monkeypatch.setattr(cli, "unit_process",
                        _unit({"OSCMIX_CONFIG": "desk/r.conf"}, cwd="/unit"))
    assert cli._unit_desk() == Path("/unit/desk/r.conf")
    # A command line this parser cannot read: cannot be told, and the
    # parser's usage text is the unit's, not this switch's stderr.
    monkeypatch.setattr(cli, "unit_process",
                        _unit(environ, ("oscmix-session", "--config"), "/unit"))
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())
    assert cli._unit_desk() is None
    assert cli.sys.stderr.getvalue() == ""
    # An empty final argument is an argument, not a terminator: a unit
    # started with `--device ''` runs its --config, not "cannot be told".
    monkeypatch.setattr(cli, "unit_process", _unit(
        environ, ("oscmix-session", "--config", "/x/r.conf", "--device", ""),
        "/unit"))
    assert cli._unit_desk() == Path("/x/r.conf")
    # No desk anywhere: None, and the switch reloads as before.
    monkeypatch.setattr(cli, "unit_process", _unit({"HOME": str(tmp_path)}))
    assert cli._unit_desk() is None


def test_the_unit_s_process_is_read_from_proc(tmp_path, monkeypatch):
    """/proc/<MainPID>/{cmdline,environ,cwd}: no quoting to undo, and the
    manager's XDG_CONFIG_HOME and HOME are there, which `systemctl show
    -p Environment` never lists."""
    from oscmix_desk import process

    entry = tmp_path / "4242"
    entry.mkdir()
    (entry / "cmdline").write_bytes(
        b"python3\0oscmix-session\0--config\0/my desk/r.conf\0--device\0\0")
    (entry / "environ").write_bytes(
        b"HOME=/home/x\0OSCMIX_CONFIG=/home/x/my desk/routing.conf\0"
        b"NOEQUALS\0\0EMPTY=\0")
    (entry / "cwd").symlink_to(tmp_path)
    answers = {"MainPID": "4242\n"}
    monkeypatch.setattr(process, "_systemctl_output",
                        lambda *verb: answers.get(verb[2]))
    unit = process.unit_process(tmp_path)
    assert unit == process.UnitProcess(
        argv=("python3", "oscmix-session", "--config", "/my desk/r.conf",
              "--device", ""),
        environ={"HOME": "/home/x", "EMPTY": "",
                 "OSCMIX_CONFIG": "/home/x/my desk/routing.conf"},
        cwd=tmp_path)
    # Exited but not reaped: cmdline and environ read empty. Not told.
    (entry / "cmdline").write_bytes(b"")
    assert process.unit_process(tmp_path) is None
    (entry / "cmdline").write_bytes(b"x\0")
    (entry / "environ").write_bytes(b"")
    assert process.unit_process(tmp_path) is None
    (entry / "environ").write_bytes(b"A=b\0")
    assert process.unit_process(tmp_path) is not None
    (entry / "cwd").unlink()
    assert process.unit_process(tmp_path) is None
    # Not running: MainPID is 0. Unreadable or absent: None as well.
    answers["MainPID"] = "0\n"
    assert process.unit_process(tmp_path) is None
    answers["MainPID"] = "4243\n"
    assert process.unit_process(tmp_path) is None
    answers["MainPID"] = "garbage"
    assert process.unit_process(tmp_path) is None
    answers.clear()
    assert process.unit_process(tmp_path) is None


def test_an_empty_profile_name_is_a_refused_switch_not_a_start(
        tmp_path, monkeypatch, capsys):
    """`--profile ''` was falsy, fell through every action, and started
    the service; with --list-profiles it listed (0.6.9)."""
    from oscmix_desk import cli

    started = []
    monkeypatch.setattr(cli, "run_session", lambda *a: started.append(1) or 0)
    path = write_config(tmp_path / "routing.conf", DESK)
    assert cli.main(["--config", str(path), "--profile", ""]) == cli.EXIT_CONFIG
    assert started == []
    assert "is not a profile name" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--config", str(path), "--profile", "", "--list-profiles"])


def test_a_verifier_that_cannot_reach_the_backend_ends_quietly(
        tmp_path, monkeypatch, caplog):
    from oscmix_desk import Config

    def unreachable(*_a, **_k):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(session_module, "verify_and_repair", unreachable)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    statuses = []
    monkeypatch.setattr(session_module, "sd_notify", statuses.append)
    _lock_dir(tmp_path, monkeypatch)
    lock = profiles.take_device_lock(None, KEY_B)
    with caplog.at_level("ERROR"):
        thread = session_module._verify_in_background(_Child(), Config(),
                                                      {"stop": False}, lock)
        thread.join(5)
    assert not thread.is_alive()
    assert "verifier could not reach the backend" in caplog.text
    assert any("verifier failed" in s for s in statuses)
    assert profiles.take_device_lock(None, KEY_B, wait=0.2) is not None, \
        "the lock was released"


def test_a_reconcile_that_cannot_reach_the_backend_stands_down(
        tmp_path, monkeypatch, caplog):
    import argparse

    from oscmix_desk import Config

    def unreachable(*_a, **_k):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(session_module, "reconcile_now", unreachable)
    statuses = []
    monkeypatch.setattr(session_module, "sd_notify", statuses.append)
    _lock_dir(tmp_path, monkeypatch)
    path = write_config(tmp_path / "routing.conf", DESK)
    with caplog.at_level("ERROR"):
        session_module._reconcile(argparse.Namespace(config=path), Config(),
                                  {"stop": False})
    assert "reconcile skipped" in caplog.text
    assert statuses[-1].startswith("STATUS=running; reconcile skipped")


def test_the_launcher_and_the_session_agree_on_what_a_profile_name_is():
    import inspect

    from oscmix_desk import config, launcher

    rule = 'r"[A-Za-z0-9][A-Za-z0-9._-]*"'
    assert rule in inspect.getsource(config.profile_path)
    assert rule in inspect.getsource(launcher._active_profile_port)


# --------------------------------------------------------------------------
# 0.6.10 mutation run: what its survivors showed no test observed.
# --------------------------------------------------------------------------

def test_a_session_named_past_the_interpreter_or_under_init_is_not_one(
        tmp_path, monkeypatch):
    """Only argv[0] or argv[1] names the program -- `python3 <script>` or
    the script itself. A later argument that happens to be called
    oscmix-session is an editor's file, and a backend whose parent is
    pid 1 has no session whatever pid 1 runs."""
    from oscmix_desk import process

    port, proc, holder, cleanup = _backend_with_parent(
        tmp_path, ["vim", "-R", "oscmix-session"])
    monkeypatch.setattr(process.os, "getuid", os.getuid)
    monkeypatch.setattr(process, "STALE_BACKEND_SETTLE", 0.0)
    killed = []
    monkeypatch.setattr(process, "_terminate", killed.append)
    assert cleanup(port, proc) is None
    assert killed == [int(holder.name)]
    init = proc / "1"
    init.mkdir()
    (init / "cmdline").write_bytes(b"oscmix-session\0")
    (holder / "stat").write_text("%s (oscmix) S 1 0 0\n" % holder.name)
    assert process._supervising_session(holder, proc) is None
    # An argv that is not UTF-8 is read, not raised on.
    (proc / "39000" / "cmdline").write_bytes(
        b"python3\0/opt/\xff/oscmix-session\0")
    (holder / "stat").write_text("%s (oscmix) S 39000 0 0\n" % holder.name)
    assert process._supervising_session(holder, proc) == 39000


def test_the_unit_s_main_pid_is_asked_for_exactly(tmp_path, monkeypatch):
    """`systemctl --user show -p MainPID --value oscmix.service`: without
    --value the answer is `MainPID=4242`, which is no pid."""
    from oscmix_desk import process

    asked = []
    monkeypatch.setattr(process, "_systemctl_output",
                        lambda *verb: asked.append(verb) or "0\n")
    assert process.unit_process(tmp_path) is None
    assert asked == [("show", "-p", "MainPID", "--value", "oscmix.service")]
    # "0" is not running even where a /proc/0 would answer.
    zero = tmp_path / "0"
    zero.mkdir()
    (zero / "cmdline").write_bytes(b"x\0")
    (zero / "environ").write_bytes(b"A=b\0")
    (zero / "cwd").symlink_to(tmp_path)
    assert process.unit_process(tmp_path) is None
    # A value may contain "=": only the first one separates the name.
    entry = tmp_path / "4242"
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"x\0")
    (entry / "environ").write_bytes(b"OSCMIX_CONFIG=/a=b/routing.conf\0")
    (entry / "cwd").symlink_to(tmp_path)
    monkeypatch.setattr(process, "_systemctl_output", lambda *verb: "4242\n")
    assert process.unit_process(tmp_path).environ == {
        "OSCMIX_CONFIG": "/a=b/routing.conf"}


def test_the_unit_desk_reads_the_real_proc_and_compares_files_safely(
        tmp_path, monkeypatch):
    from oscmix_desk import cli

    roots = []
    monkeypatch.delenv("OSCMIX_PROC_ROOT", raising=False)
    monkeypatch.setattr(cli, "unit_process", lambda root: roots.append(root))
    assert cli._unit_desk() is None
    assert roots == [Path("/proc")]
    assert cli._same_file(tmp_path / "a", None) is False

    def unreadable(self, *a, **k):
        raise OSError("loop")

    monkeypatch.setattr(cli.Path, "resolve", unreadable)
    assert cli._same_file(tmp_path / "a", tmp_path / "a") is False


# --------------------------------------------------------------------------
# 0.6.11: one rule for what a re-read desk may not change.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reread", ["_desk_under_the_lock", "_reloaded_desk"])
def test_a_re_read_desk_keeps_every_machine_setting_of_the_process(
        tmp_path, reread):
    """The start's re-read under the lock and the SIGHUP's each spelled the
    five assignments out by hand; `profiles.MACHINE_SETTINGS` is the list, and
    a setting added to it is kept by both without anybody remembering."""
    from oscmix_desk import Config

    path = write_config(tmp_path / "routing.conf", """
[device]
name = Another Box
usb-id = 1111:2222
serial = 99887766

[osc]
port = 9001
recv-port = 9002

[route:main]
playback = 1/2
output = 3/4
""")
    running = Config(serial="24216011")
    args = (running, path) if reread == "_reloaded_desk" else (path, running)
    fresh = getattr(session_module, reread)(*args)
    assert fresh is not running
    assert [route.output for route in fresh.routes] == [(3, 4)], \
        "the desk itself is the file's"
    for _section, _option, attr in profiles.MACHINE_SETTINGS:
        assert getattr(fresh, attr) == getattr(running, attr), attr
    assert fresh.osc_port != 9001


def _reread(name, running, path):
    """`_desk_under_the_lock` answers with the running desk when it refuses,
    `_reloaded_desk` with None; both mean "not applied"."""
    args = (running, path) if name == "_reloaded_desk" else (path, running)
    fresh = getattr(session_module, name)(*args)
    return None if fresh is running else fresh


@pytest.mark.parametrize("reread", ["_desk_under_the_lock", "_reloaded_desk"])
@pytest.mark.parametrize(("elsewhere", "named"), [
    ("[device]\nname = Some Box\n", "device name 'Some Box'"),
    ("[device]\nserial = 99887766\n", "serial '99887766' (not '')"),
    ("[osc]\nport = 9500\n", "osc port 9500 (not 7222)"),
], ids=["another model", "another box of the model", "another backend"])
def test_a_re_read_desk_for_somewhere_else_is_not_applied_here(
        tmp_path, caplog, reread, elsewhere, named):
    """Until 0.6.11 it was pinned to this session's interface and written.
    Measured and reviewed: `routing.conf` edited to name a box with 42
    outputs reached a UCX II, which has twenty; a profile stating another
    port and serial was written to its own interface by the switch and
    then to this one by the reload the switch sent -- the same persisted
    profile meant one target for a switch, another for a reload, and the
    first again after a restart. A notice that compared models was silent
    for two boxes of one model."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    running = profiles.load_config(path)
    assert _reread(reread, running, path) is not None, "its own desk"

    # As the file itself ...
    path.write_text(elsewhere + "[route:far]\nplayback = 1/2\noutput = 3/4\n")
    with caplog.at_level("INFO"):
        assert _reread(reread, running, path) is None
    assert "the desk now in effect is for another backend or interface" \
        in caplog.text
    assert named in caplog.text
    assert "systemctl --user restart oscmix.service" in caplog.text
    assert "SIGHUP: reloaded" not in caplog.text, "it was not"

    # ... and as an active profile over an untouched routing.conf.
    path.write_text("[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "far.conf", elsewhere
                 + "[route:far]\nplayback = 1/2\noutput = 3/4\n")
    (tmp_path / "active-profile").write_text("far\n")
    assert _reread(reread, running, path) is None


@pytest.mark.parametrize("reread", ["_desk_under_the_lock", "_reloaded_desk"])
def test_what_the_start_replaced_does_not_read_as_a_desk_for_elsewhere(
        tmp_path, caplog, reread):
    """`--device`, `--osc-port` and the serial a start pins change the live
    attributes, not what the file resolved to -- and the file is what a
    re-read compares. A first cut compared models of live names and
    announced a `--device` start again at every SIGHUP."""
    from oscmix_desk import cli

    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    running = profiles.load_config(path)
    with caplog.at_level("WARNING"):
        cli._override_device(running, "Some Box")
    assert "--device replaces [device] name after validation" in caplog.text
    running.osc_port, running.serial = 9000, "24216011"
    caplog.clear()
    with caplog.at_level("WARNING"):
        fresh = _reread(reread, running, path)
    assert fresh is not None
    assert (fresh.device_name, fresh.osc_port, fresh.serial) == (
        "Some Box", 9000, "24216011")
    assert "another backend" not in caplog.text
    assert "checked for" not in caplog.text
