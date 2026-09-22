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
import os
import socket
import threading

import pytest
from support import fake_proc, free_udp_port, repo_file, write_config
from two_boxes import DESK, A, B, add_clients, lock_dir

from oscmix_desk import locking, profiles
from oscmix_desk import marker as marker_mod
from oscmix_desk import outcome as outcome_mod
from oscmix_desk import reload as reload_mod
from oscmix_desk import session as session_module
from oscmix_desk.discovery import (
    lock_key,
)
from oscmix_desk.errors import DeviceAmbiguous

KEY_B = "2a39-3fd9-99887766"


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


def _record_keys(monkeypatch, module, keys, who=None):
    """Record who took the lock with which key. The start and the reload
    each call `take_device_lock` by the name in their own module; a
    switch and a restore take it through `locking._switch_lock`."""
    real = module.take_device_lock
    who = who or module.__name__.rsplit(".", 1)[1]

    def take(config_path, key=None, wait=None):
        keys.append((who, key))
        return real(config_path, key, wait)

    monkeypatch.setattr(module, "take_device_lock", take)


def _run_the_unit(tmp_path, monkeypatch, path, started, reconciled):
    """run_session for real, with only the process and the wire replaced."""
    notified = []
    monkeypatch.setattr(session_module, "sd_notify", notified.append)
    monkeypatch.setattr(reload_mod, "sd_notify", notified.append)
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
    monkeypatch.setattr(reload_mod, "reconcile_now", reconcile)
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
    lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port, serial=B[1])
    keys, started, reconciled, wired = [], [], [], []
    _record_keys(monkeypatch, session_module, keys)
    _record_keys(monkeypatch, reload_mod, keys)
    _record_keys(monkeypatch, locking, keys, who="switch")
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
    assert [who for who, _key in keys] == ["session", "reload",
                                           "switch", "switch"], \
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
    lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port, serial=B[1])
    wired = []
    monkeypatch.setattr(profiles, "loopback",
                        lambda send, recv: wired.append(send) or recording_backend)

    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.reason == ("the backend on UDP %d drives the interface "
                              "%s, not %s" % (port, A[1], B[1]))
    assert wired == []
    assert recording_backend.sent == []
    assert not marker_mod.active_profile_path(path).exists()


def test_two_boxes_and_no_serial_refuse_everywhere(tmp_path, monkeypatch):
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A, B],
                     bound=[(port, "oscmix", A[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    lock_dir(tmp_path, monkeypatch)
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
        assert outcome.state == outcome_mod.REFUSED
        assert "2 interfaces match" in outcome.reason


def test_one_box_gives_the_unit_and_a_switch_the_same_key(
        tmp_path, monkeypatch, recording_backend):
    """The 0.6.8 split in its simplest form: one resolution for both."""
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A],
                     bound=[(port, "oscmix", A[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port)
    keys = []
    _record_keys(monkeypatch, session_module, keys)
    _record_keys(monkeypatch, reload_mod, keys)
    _record_keys(monkeypatch, locking, keys, who="switch")
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
        lock_dir(tmp_path, monkeypatch)
        path = _desk(tmp_path, port)
        outcome = profiles.switch_profile("b", config_path=path, verify=False)
        assert outcome.state == outcome_mod.REFUSED, outcome.reason
        assert outcome.reason == (
            "UDP %d is held by pid %d, not by an oscmix backend of this user"
            % (port, os.getpid()))
        with pytest.raises(BlockingIOError):
            stranger.recv(65535)
        assert not marker_mod.active_profile_path(path).exists()
    finally:
        stranger.close()


# --------------------------------------------------------------------------
# What ships: the directory's trust circle, and the tools that write.
# --------------------------------------------------------------------------


def test_the_installer_says_when_the_user_is_not_in_audio():
    assert "usermod -aG audio" in repo_file("install.sh").read_text()


# --------------------------------------------------------------------------
# The edges of the identity: other users, missing files, the unit sandbox.
# --------------------------------------------------------------------------


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


def test_a_backend_that_changes_during_the_lock_wait_is_refused_after_it(
        tmp_path, monkeypatch, recording_backend):
    """Checked before a 30 s wait and never again, the switch wrote anyway."""
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[A, B],
                     bound=[(port, "oscmix", B[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    lock_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
    path = _desk(tmp_path, port, serial=B[1])
    wired = []
    monkeypatch.setattr(profiles, "loopback",
                        lambda send, recv: wired.append(send) or recording_backend)
    held = locking.take_device_lock(None, KEY_B)
    assert held is not None

    def backend_swapped_then_lock_released():
        bridge = next(p for p in proc.iterdir() if p.name.isdigit()
                      and (p / "comm").read_text().strip() == "alsaseqio")
        (bridge / "cmdline").write_bytes(b"alsaseqio\x0024:1\x00oscmix\x00")
        held.release()

    threading.Timer(0.3, backend_swapped_then_lock_released).start()
    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.reason == ("the backend on UDP %d drives the interface "
                              "%s, not %s" % (port, A[1], B[1]))
    assert wired == []
    assert recording_backend.sent == []
    assert not marker_mod.active_profile_path(path).exists()


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
    add_clients(proc, b'Client 130 : "\xff\xfe" [User Legacy]\n')
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    lock_dir(tmp_path, monkeypatch)
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
    lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, port)
    keys = []
    _record_keys(monkeypatch, locking, keys, who="switch")
    monkeypatch.setattr(profiles, "loopback", lambda *a: recording_backend)
    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.reason == ("2a39:3fd9 is not visible to ALSA, so no backend "
                              "can be driving it")
    assert keys == []


def test_a_configured_box_that_is_not_plugged_in_is_a_clean_no_op(
        tmp_path, monkeypatch):
    """Another box of the model made USB presence say "connected".

    The start then failed after its wait and was restarted for ever.
    """
    proc = fake_proc(tmp_path / "proc", boxes=[A])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, free_udp_port(), serial=B[1])
    started = []
    code, _unit, notified = _run_the_unit(tmp_path, monkeypatch, path,
                                          started, [])
    assert code == session_module.EXIT_OK
    assert notified == ["READY=1"]
    assert started == []


# --------------------------------------------------------------------------
# The re-review: names that forge lines, models that share a prefix, and a
# misconfigured desk that must not look unplugged.
# --------------------------------------------------------------------------


def test_a_desk_named_for_the_wrong_model_fails_instead_of_looking_unplugged(
        tmp_path, monkeypatch):
    """The box is there under another model name: exit 1, not "nothing to do"."""
    proc = fake_proc(tmp_path / "proc")
    (proc / "asound" / "cards").write_text(
        " 2 [UFX ]: USB-Audio - Fireface UFX II (55554444)\n")
    (proc / "asound" / "seq" / "clients").write_text(
        'Client  24 : "Fireface UFX II (55554444)" [Kernel Legacy]\n')
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    lock_dir(tmp_path, monkeypatch)
    path = _desk(tmp_path, free_udp_port(), serial="55554444")
    code, _unit, notified = _run_the_unit(tmp_path, monkeypatch, path, [], [])
    assert code == session_module.EXIT_FAILURE
    assert notified == []

    (proc / "asound" / "cards").unlink()          # and an unreadable list
    code, _unit, notified = _run_the_unit(tmp_path, monkeypatch, path, [], [])
    assert code == session_module.EXIT_FAILURE
    assert notified == []


# --------------------------------------------------------------------------
# What the mutation run of this release left unobserved.
# --------------------------------------------------------------------------


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
    lock_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
    path = _desk(tmp_path, port, serial=B[1])
    monkeypatch.setattr(profiles, "loopback", lambda *a: recording_backend)
    held = locking.take_device_lock(None, KEY_B)

    def swapped_then_released():
        bridge = next(p for p in proc.iterdir() if p.name.isdigit()
                      and (p / "comm").read_text().strip() == "alsaseqio")
        (bridge / "cmdline").write_bytes(b"alsaseqio\x0024:1\x00oscmix\x00")
        held.release()

    threading.Timer(0.3, swapped_then_released).start()
    outcome = profiles.restore_main(config_path=path, verify=False)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "routing.conf"
    assert outcome.reason == ("the backend on UDP %d drives the interface "
                              "%s, not %s" % (port, A[1], B[1]))
    assert recording_backend.sent == []


def test_the_switch_refusal_after_the_wait_names_the_profile(
        tmp_path, monkeypatch, recording_backend):
    port = free_udp_port()
    proc = fake_proc(tmp_path / "proc", boxes=[B], bound=[(port, "oscmix", B[0])])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    lock_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
    path = _desk(tmp_path, port, serial=B[1])
    held = locking.take_device_lock(None, KEY_B)

    def backend_gone_then_released():
        (proc / "net" / "udp").write_text("  sl  local_address\n")
        held.release()

    threading.Timer(0.3, backend_gone_then_released).start()
    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert (outcome.state, outcome.name) == (outcome_mod.REFUSED, "b")
    assert "nothing is listening on UDP" in outcome.reason


def test_a_restore_that_cannot_reach_its_interface_takes_no_lock(
        tmp_path, monkeypatch, recording_backend):
    port = free_udp_port()
    monkeypatch.setenv("OSCMIX_PROC_ROOT",
                       str(fake_proc(tmp_path / "proc", bound=[(port, "oscmix", 28)])))
    lock_dir(tmp_path, monkeypatch)
    keys = []
    _record_keys(monkeypatch, locking, keys, who="switch")
    outcome = profiles.restore_main(config_path=_desk(tmp_path, port),
                                    verify=False)
    assert outcome.state == outcome_mod.REFUSED
    assert keys == [], "refused before the lock, like a bad config"


# --------------------------------------------------------------------------
# 0.6.10, second round: the unit's desk is what the unit's environment
# resolves; an empty profile name is a switch; socket errors in the
# verifier and the reconcile are log lines.
# --------------------------------------------------------------------------


def test_a_verifier_that_cannot_reach_the_backend_ends_quietly(
        tmp_path, monkeypatch, caplog):
    from oscmix_desk import Config

    def unreachable(*_a, **_k):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(session_module, "verify_and_repair", unreachable)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    statuses = []
    monkeypatch.setattr(session_module, "sd_notify", statuses.append)
    lock_dir(tmp_path, monkeypatch)
    lock = locking.take_device_lock(None, KEY_B)
    with caplog.at_level("ERROR"):
        thread = session_module._verify_in_background(_Child(), Config(),
                                                      {"stop": False}, lock)
        thread.join(5)
    assert not thread.is_alive()
    assert "verifier could not reach the backend" in caplog.text
    assert any("verifier failed" in s for s in statuses)
    assert locking.take_device_lock(None, KEY_B, wait=0.2) is not None, \
        "the lock was released"
