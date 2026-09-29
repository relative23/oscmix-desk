"""One resolved interface, from the serial to the socket.

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

from oscmix_desk import diagnostics, locking, profiles
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

    returncode = None

    @property
    def pid(self):
        # The control peer is served by the process running the test. A PID
        # taken at import names the collecting process instead, which differs
        # under a forking runner and makes the start refuse the endpoint.
        return os.getpid()

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
    each call `device_lock` by the name in their own module; a
    switch and a restore take it through `locking._switch_lock`."""
    real = module.device_lock
    who = who or module.__name__.rsplit(".", 1)[1]

    def take(config_path, key=None, wait=None, should_stop=None):
        keys.append((who, key))
        return real(config_path, key, wait, should_stop)

    monkeypatch.setattr(module, "device_lock", take)


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
    monkeypatch.setattr(session_module, "verify_and_repair", lambda *a, **k: True)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)

    def start(client, config, config_path=None):
        started.append(client)
        return _Child()

    def reconcile(config, reason, backend, should_stop):
        reconciled.append(config.serial)
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

def test_every_path_takes_b_s_lock_and_reaches_only_b(tmp_path, monkeypatch, coordinated):
    from control_peer import BEGIN, END

    with coordinated() as peer:
        path = _desk(tmp_path, 7222, serial=B[1])
        keys, started, reconciled = [], [], []
        _record_keys(monkeypatch, session_module, keys)
        _record_keys(monkeypatch, reload_mod, keys)
        _record_keys(monkeypatch, locking, keys, who="switch")
        code, unit, notified = _run_the_unit(tmp_path, monkeypatch, path,
                                            started, reconciled)
        switched = profiles.switch_profile("b", config_path=path, verify=False)
        restored = profiles.restore_main(config_path=path, verify=False)
        assert code == session_module.EXIT_OK
        assert "READY=1" in notified
        assert started == [B[0]]
        assert unit.serial == B[1]
        assert [who for who, _key in keys] == ["session", "reload", "switch", "switch"]
        assert {key for _who, key in keys} == {KEY_B}
        assert switched.applied
        assert restored.applied
        assert reconciled == [B[1]]
        assert peer.requests.count(BEGIN) == peer.requests.count(END) == 4
        assert peer.writes, "no real control request reached the selected endpoint"


def test_a_switch_refuses_a_backend_that_drives_the_other_box(tmp_path, coordinated):
    with coordinated(client=A[0]) as peer:
        path = _desk(tmp_path, 7222, serial=B[1])
        outcome = profiles.switch_profile("b", config_path=path, verify=False)
        assert outcome.state == outcome_mod.REFUSED
        assert "backend drives another interface" in outcome.reason
        assert peer.requests == []
        assert peer.writes == []
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


def test_one_box_gives_the_unit_and_a_switch_the_same_key(tmp_path, monkeypatch, coordinated):
    with coordinated(serial=A[1], client=A[0], boxes=(A,)):
        path = _desk(tmp_path, 7222)
        keys = []
        _record_keys(monkeypatch, session_module, keys)
        _record_keys(monkeypatch, reload_mod, keys)
        _record_keys(monkeypatch, locking, keys, who="switch")
        code, _, _ = _run_the_unit(tmp_path, monkeypatch, path, [], [])
        assert code == session_module.EXIT_OK
        assert profiles.switch_profile("b", config_path=path, verify=False).applied
        assert {key for _who, key in keys} == {lock_key("2a39:3fd9", A[1])}


# --------------------------------------------------------------------------
# The port: bound is not reachable (review probe C, against a real socket).
# --------------------------------------------------------------------------

def test_a_strangers_socket_is_refused_without_connecting(tmp_path, tmp_path_factory, monkeypatch):
    # Use the real kernel Unix socket table and owner, with a simulated UCX.
    shared = tmp_path_factory.mktemp("stranger")
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    endpoint = locking.control_path(None, lock_key("2a39:3fd9", A[1]))
    stranger = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    stranger.bind(str(endpoint))
    stranger.listen(1)
    stranger.setblocking(False)
    try:
        box = fake_proc(tmp_path / "box", boxes=[A])
        resolve = profiles.resolve_device
        for module in (profiles, diagnostics):
            monkeypatch.setattr(module, "resolve_device",
                                lambda usb, name, serial, _proc: resolve(usb, name, serial, box))
        monkeypatch.setenv("OSCMIX_PROC_ROOT", "/proc")
        path = _desk(tmp_path, 7222, serial=A[1])
        outcome = profiles.switch_profile("b", config_path=path, verify=False)
        assert outcome.state == outcome_mod.REFUSED
        assert "endpoint belongs to an incompatible program" in outcome.reason
        with pytest.raises(BlockingIOError):
            stranger.accept()
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
# Cases found before 0.6.9 shipped.
# --------------------------------------------------------------------------


def test_a_backend_that_changes_during_the_lock_wait_is_refused_after_it(
        tmp_path, monkeypatch, coordinated):
    with coordinated() as peer:
        monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
        path = _desk(tmp_path, 7222, serial=B[1])
        held = locking.take_device_lock(None, KEY_B)
        assert held is not None

        def changed():
            (peer.proc / str(os.getpid() + 1) / "cmdline").write_bytes(
                b"alsaseqio\0-x\0" + b"24:1\0oscmix\0")
            held.release()

        timer = threading.Timer(0.2, changed)
        timer.start()
        try:
            outcome = profiles.switch_profile("b", config_path=path, verify=False)
        finally:
            timer.join()
            held.release()
        assert outcome.state == outcome_mod.REFUSED
        assert "backend drives another interface" in outcome.reason
        assert peer.requests == []
        assert peer.writes == []
        assert not marker_mod.active_profile_path(path).exists()


def test_names_that_are_not_utf8_do_not_break_a_switch(tmp_path, coordinated):
    with coordinated(boxes=(B,)) as peer:
        odd = peer.proc / "45000"
        (odd / "fd").mkdir(parents=True)
        (odd / "comm").write_bytes(b"abc\xce\n")
        (odd / "stat").write_bytes(b"45000 (abc\xce) S 1 0 0\n")
        (odd / "cmdline").write_bytes(b"")
        add_clients(peer.proc, b'Client 130 : "\xff\xfe" [User Legacy]\n')
        path = _desk(tmp_path, 7222, serial=B[1])
        assert profiles.switch_profile("b", config_path=path, verify=False).applied
        assert peer.writes


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
    outcome = profiles.switch_profile("b", config_path=path, verify=False)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.reason == "the selected interface and its serial are not visible to ALSA"
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


def test_a_restore_also_checks_again_after_the_lock_wait(tmp_path, monkeypatch, coordinated):
    with coordinated() as peer:
        monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
        path = _desk(tmp_path, 7222, serial=B[1])
        held = locking.take_device_lock(None, KEY_B)

        def changed():
            (peer.proc / str(os.getpid() + 1) / "cmdline").write_bytes(
                b"alsaseqio\0-x\0" + b"24:1\0oscmix\0")
            held.release()

        timer = threading.Timer(0.2, changed)
        timer.start()
        try:
            outcome = profiles.restore_main(config_path=path, verify=False)
        finally:
            timer.join()
            held.release()
        assert outcome.state == outcome_mod.REFUSED
        assert outcome.name == "routing.conf"
        assert "backend drives another interface" in outcome.reason
        assert peer.requests == []
        assert peer.writes == []


def test_the_switch_refusal_after_the_wait_names_the_profile(tmp_path, monkeypatch, coordinated):
    with coordinated(boxes=(B,)) as peer:
        monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
        path = _desk(tmp_path, 7222, serial=B[1])
        held = locking.take_device_lock(None, KEY_B)

        def gone():
            peer.path.unlink()
            held.release()

        timer = threading.Timer(0.2, gone)
        timer.start()
        try:
            outcome = profiles.switch_profile("b", config_path=path, verify=False)
        finally:
            timer.join()
            held.release()
        assert (outcome.state, outcome.name) == (outcome_mod.REFUSED, "b")
        assert "no coordinated backend endpoint" in outcome.reason
        assert peer.requests == []


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
        tmp_path, monkeypatch, caplog, recording_backend):
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
                                                      {"stop": False}, lock, recording_backend)
        thread.join(5)
    assert not thread.is_alive()
    assert "verifier lost its backend operation" in caplog.text
    assert any("verifier failed" in s for s in statuses)
    assert locking.take_device_lock(None, KEY_B, wait=0.2) is not None, \
        "the lock was released"


def _verifier_ending(tmp_path, monkeypatch, caplog, backend, verify, stop=None):
    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "verify_and_repair", verify)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    statuses = []
    monkeypatch.setattr(session_module, "sd_notify", statuses.append)
    lock_dir(tmp_path, monkeypatch)
    lock = locking.take_device_lock(None, KEY_B)
    with caplog.at_level("INFO"):
        thread = session_module._verify_in_background(_Child(), Config(),
                                                      stop or {"stop": False}, lock,
                                                      backend)
        thread.join(5)
    assert not thread.is_alive()
    assert locking.take_device_lock(None, KEY_B, wait=0.2) is not None, \
        "the lock was released"
    return statuses


def test_a_repair_the_plan_refused_is_not_a_lost_connection(
        tmp_path, monkeypatch, caplog, recording_backend):
    from oscmix_desk.errors import WriteFailed

    def refused(*_a, **_k):
        raise WriteFailed(OSError("fresh link confirmation required for "
                                  "/output/1/stereo; remaining writes refused"),
                          [], ["/mix/1/playback/1"])

    statuses = _verifier_ending(tmp_path, monkeypatch, caplog, recording_backend, refused)
    assert "verifier did not repair the routing: fresh link confirmation" in caplog.text
    assert "lost its backend operation" not in caplog.text
    assert statuses[-1].startswith("STATUS=running; verifier failed at ")


def test_a_lease_that_does_not_end_after_the_read_back_says_so(
        tmp_path, monkeypatch, caplog, recording_backend):
    from oscmix_desk.errors import ReceivePortError

    def finish():
        raise ReceivePortError(71, "backend operation did not finish")

    monkeypatch.setattr(recording_backend, "finish", finish)
    _verifier_ending(tmp_path, monkeypatch, caplog, recording_backend,
                     lambda *_a, **_k: True)
    assert ("verifier read the routing back, but its backend operation did not "
            "end cleanly") in caplog.text
    assert "routing cannot be verified" not in caplog.text


def test_a_stop_during_the_verifier_is_not_a_verifier_failure(
        tmp_path, monkeypatch, caplog, recording_backend):
    from oscmix_desk.errors import ReceivePortError

    stop = {"stop": False}

    def cancelled(*_a, **_k):
        stop["stop"] = True                  # SIGTERM, mid read-back
        raise ReceivePortError(125, "backend operation cancelled")

    statuses = _verifier_ending(tmp_path, monkeypatch, caplog, recording_backend,
                                cancelled, stop)
    assert "verifier ended by the stop" in caplog.text
    assert [record for record in caplog.records if record.levelname == "ERROR"] == []
    assert not any("verifier failed" in status for status in statuses)
