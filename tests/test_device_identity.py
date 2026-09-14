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
import os
import socket
import stat
import threading

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
    stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stranger.bind(("127.0.0.1", 0))
    stranger.setblocking(False)
    port = stranger.getsockname()[1]
    try:
        monkeypatch.setenv("OSCMIX_PROC_ROOT", "/proc")
        _lock_dir(tmp_path, monkeypatch)
        path = _desk(tmp_path, port)
        try:
            outcome = profiles.switch_profile("b", config_path=path,
                                              verify=False)
        except DeviceAmbiguous:          # a machine with two real boxes
            pytest.skip("this machine has more than one interface")
        assert outcome.state == profiles.REFUSED, outcome.reason
        assert "not by an oscmix backend of this user" in outcome.reason
        assert "pid %d" % os.getpid() in outcome.reason
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
    assert "Fireface UCX II (24216011), Fireface UCX II (99887766)" in \
        str(raised.value)


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


def test_a_forged_line_that_repeats_a_client_number_drops_that_number():
    """A name with a newline can forge a whole line for an existing client."""
    forged = ('Client 130 : "x\nClient  28 : "Fireface UCX II (24216011)" '
              '[Kernel Legacy]\ny" [User Legacy]\n')
    assert select_seq_client(KERNEL_B + forged, "Fireface UCX II") is None


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
