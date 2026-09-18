"""A receive port that cannot be bound is not a busy one (0.6.11, ADR 0025).

`Backend.listen` answered None for every OSError, and None means one
thing to every caller: the mixer GUI has the port. Measured: `[osc]
recv-port = 80` fails with EACCES for an ordinary user, and the desk ran
unverified for good under "in use -- close the mixer GUI".

The obvious repair -- let the error out -- would have been worse than the
defect. The link barrier binds the port *after* the links are on the
wire, so an exception from there ends the apply between its phases:
pairs linked, no mix. These tests hold both halves: the error is named
everywhere, and it never tears an apply.
"""

import argparse
import errno
import os
import socket
from pathlib import Path

import pytest
from conftest import free_udp_port, write_config

from oscmix_desk import backend, profiles, routing, verify
from oscmix_desk import session as session_module
from oscmix_desk.errors import ReceivePortError

#: What the double raises, as `str()` and as `strerror`.
DENIED = "cannot bind the receive port UDP 80: Permission denied"
DENIED_STR = "[Errno 13] " + DENIED

DESK = """
[route:main]
output = 1/2
playback = 1/2
level = 0.0

[output:1]
volume = -10.0
"""


class _Socket:
    """A socket whose bind fails the way the test says."""

    def __init__(self, error):
        self.error = error
        self.closed = False

    def bind(self, _address):
        raise self.error

    def close(self):
        self.closed = True


def _failing_with(monkeypatch, error):
    sock = _Socket(error)
    monkeypatch.setattr(backend.socket, "socket", lambda *_a: sock)
    return sock


# --------------------------------------------------------------------------
# The seam: None is EADDRINUSE and nothing else.
# --------------------------------------------------------------------------

def test_only_a_held_port_is_the_normal_state(monkeypatch):
    sock = _failing_with(monkeypatch,
                         OSError(errno.EADDRINUSE, "Address already in use"))
    assert backend.loopback(7222, 8222).listen() is None
    assert sock.closed


@pytest.mark.parametrize("code", [errno.EACCES, errno.EADDRNOTAVAIL,
                                  errno.ENOBUFS])
def test_any_other_bind_failure_carries_its_reason(monkeypatch, code):
    cause = OSError(code, os.strerror(code))
    sock = _failing_with(monkeypatch, cause)
    with pytest.raises(ReceivePortError) as raised:
        backend.loopback(7222, 80).listen()
    assert sock.closed, "the socket leaked"
    assert raised.value.errno == code
    assert raised.value.strerror == (
        "cannot bind the receive port UDP 80: %s" % os.strerror(code))
    assert raised.value.__cause__ is cause
    assert isinstance(raised.value, OSError), \
        "the verifier's and the reconcile's OSError handlers rely on it"


def test_a_socket_that_cannot_be_had_is_the_same_error(monkeypatch):
    def no_socket(*_a):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(backend.socket, "socket", no_socket)
    with pytest.raises(ReceivePortError) as raised:
        backend.loopback(7222, 8222).listen()
    assert raised.value.errno == errno.EMFILE
    assert "UDP 8222: Too many open files" in raised.value.strerror


def _unprivileged_ports_start():
    try:
        return int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
                   .read_text())
    except (OSError, ValueError):
        return 1024


@pytest.mark.skipif(os.geteuid() == 0 or _unprivileged_ports_start() <= 80,
                    reason="this user may bind port 80")
def test_the_measured_case_a_privileged_receive_port():
    """Against the kernel, not a double: the case this was found with."""
    with pytest.raises(ReceivePortError) as raised:
        backend.loopback(free_udp_port(), 80).listen()
    assert raised.value.errno == errno.EACCES


# --------------------------------------------------------------------------
# The apply is never torn.
# --------------------------------------------------------------------------

def test_the_barrier_waits_blind_and_the_apply_finishes(
        tmp_path, unbindable_backend, monkeypatch, caplog):
    monkeypatch.setattr(routing, "LINK_SETTLE", 0.01)
    slept = []
    monkeypatch.setattr(routing.time, "sleep", slept.append)
    config = profiles.load_config(write_config(tmp_path / "routing.conf", DESK))
    with caplog.at_level("INFO"):
        routing.apply_routing(config, 7222, 80, backend=unbindable_backend)
    paths = [path for path, _tags, _args in unbindable_backend.sent]
    assert paths.index("/output/1/stereo") < paths.index("/mix/1/playback/1"), \
        "the mix was never written: the apply ended between its phases"
    assert "/output/1/volume" in paths
    assert slept == [0.01], "the blind wait a held port gets"
    assert "link echo unobservable: " + DENIED_STR in caplog.text
    assert "in use" not in caplog.text


def test_the_public_echo_wait_raises_rather_than_answering_none(
        unbindable_backend):
    """None still means the GUI; callers that want to go on catch this."""
    with pytest.raises(ReceivePortError):
        routing.await_link_echo({"/output/1/stereo": 1}, 80,
                                backend=unbindable_backend)


# --------------------------------------------------------------------------
# The verifier: the mix is made safe first, then the failure is reported.
# --------------------------------------------------------------------------

def test_the_verifier_re_establishes_the_mix_before_it_fails(
        tmp_path, monkeypatch):
    config = profiles.load_config(write_config(tmp_path / "routing.conf", DESK))
    order = []

    def cannot_bind(*_a, **_k):
        order.append("verify")
        raise ReceivePortError(errno.EACCES, DENIED)

    monkeypatch.setattr(verify, "verify_routing", cannot_bind)
    monkeypatch.setattr(
        verify, "blind_reapply_mix",
        lambda cfg, stop, why=None: order.append(("blind", cfg, why)))
    with pytest.raises(ReceivePortError):
        verify.verify_and_repair(config)
    assert order == ["verify", ("blind", config, DENIED)]


def test_the_blind_re_apply_names_the_cause_it_is_given(tmp_path, monkeypatch,
                                                         caplog):
    config = profiles.load_config(write_config(tmp_path / "routing.conf", DESK))
    sent = []
    monkeypatch.setattr(routing, "loopback", lambda *_a: argparse.Namespace(
        request_dump=lambda: sent.append("dump")))
    monkeypatch.setattr(routing, "wait_unless_stopped", lambda *_a: False)
    monkeypatch.setattr(routing, "send_mix", lambda cfg: sent.append("mix"))
    with caplog.at_level("INFO"):
        routing.blind_reapply_mix(config, why="cannot bind UDP 80")
        routing.blind_reapply_mix(config)
    assert sent == ["dump", "mix", "dump", "mix"]
    assert "register sync unobservable (cannot bind UDP 80)" in caplog.text
    assert "register sync unobservable (UDP 8222 in use)" in caplog.text


class _Child:
    pid = 4242
    returncode = None

    def poll(self):
        return None


def _shared_locks(tmp_path, monkeypatch):
    shared = tmp_path / "locks"
    shared.mkdir(mode=0o770)
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))


def test_the_status_says_failed_and_the_journal_says_why(tmp_path, monkeypatch,
                                                         caplog):
    def cannot_bind(*_a, **_k):
        raise ReceivePortError(errno.EACCES, DENIED)

    monkeypatch.setattr(session_module, "verify_and_repair", cannot_bind)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    statuses = []
    monkeypatch.setattr(session_module, "sd_notify", statuses.append)
    _shared_locks(tmp_path, monkeypatch)
    lock = profiles.take_device_lock(None, "2a39-3fd9-99887766")
    with caplog.at_level("ERROR"):
        thread = session_module._verify_in_background(
            _Child(), session_module.Config(), {"stop": False}, lock)
        thread.join(5)
    assert not thread.is_alive()
    assert "routing cannot be verified: " + DENIED_STR in caplog.text
    assert "could not reach the backend" not in caplog.text, \
        "that line names the send port, which is fine"
    assert statuses[-1].startswith("STATUS=running; verifier failed at ")
    assert profiles.take_device_lock(None, "2a39-3fd9-99887766",
                                     wait=0.2) is not None


def test_a_reconcile_stands_down_and_names_the_port(tmp_path, monkeypatch,
                                                    caplog):
    def cannot_bind(*_a, **_k):
        raise ReceivePortError(errno.EACCES, DENIED)

    monkeypatch.setattr(session_module, "reconcile_now", cannot_bind)
    statuses = []
    monkeypatch.setattr(session_module, "sd_notify", statuses.append)
    _shared_locks(tmp_path, monkeypatch)
    path = write_config(tmp_path / "routing.conf", DESK)
    with caplog.at_level("ERROR"):
        session_module._reconcile(argparse.Namespace(config=path),
                                  session_module.Config(), {"stop": False})
    assert "SIGHUP: %s; reconcile skipped" % DENIED_STR in caplog.text
    assert "cannot reach the backend" not in caplog.text
    assert statuses[-1].startswith("STATUS=running; reconcile skipped")


# --------------------------------------------------------------------------
# A switch and the three reads.
# --------------------------------------------------------------------------

def test_a_switch_is_applied_and_says_why_it_could_not_check(
        tmp_path, unbindable_backend, monkeypatch, caplog):
    monkeypatch.setattr(routing, "LINK_SETTLE", 0.01)
    write_config(tmp_path / "routing.conf", DESK)
    write_config(tmp_path / "profiles" / "tracking.conf", DESK)
    with caplog.at_level("ERROR"):
        outcome = profiles.switch_profile(
            "tracking", config_path=tmp_path / "routing.conf",
            backend=unbindable_backend)
    assert outcome.state == profiles.APPLIED_UNVERIFIED
    assert outcome.reason == DENIED
    assert "/output/1/volume" in outcome.unverified
    assert outcome.persisted is True
    written = [path for path, _t, _a in unbindable_backend.sent]
    assert "/mix/1/playback/1" in written
    assert "it cannot be verified" in caplog.text
    assert outcome.read_back is False
    assert outcome.describe() == (
        "applied 'tracking'; not read back (%s), so none of its %d "
        "register(s) is confirmed" % (DENIED, len(outcome.unverified)))


@pytest.mark.parametrize("flag", ["--diff", "--snapshot", "--dump-config"])
def test_a_read_fails_with_the_reason_and_not_with_a_traceback(
        tmp_path, monkeypatch, caplog, flag):
    from oscmix_desk import cli

    monkeypatch.setattr(cli, "loopback", lambda *_a: _unbindable())
    path = write_config(tmp_path / "routing.conf", DESK)
    with caplog.at_level("ERROR"):
        assert cli.main(["--config", str(path), flag]) == cli.EXIT_FAILURE
    assert DENIED in caplog.text
    assert "close the mixer GUI" not in caplog.text


def _unbindable():
    from conftest import _UnbindableBackend
    return _UnbindableBackend()


def test_a_real_listener_still_binds_and_releases():
    """The rewrite of listen() must not have cost the ordinary path."""
    port = free_udp_port()
    with backend.loopback(free_udp_port(), port).listen() as listener:
        assert listener is not None
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with pytest.raises(OSError, match="in use"):
            probe.bind(("127.0.0.1", port))
        probe.close()


# --------------------------------------------------------------------------
# The two scripts that bind the port themselves and skip for a held one.
# --------------------------------------------------------------------------

def _script(name):
    import importlib.util

    from conftest import repo_file

    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), repo_file("scripts", name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_record_dump_skips_for_a_held_port_and_fails_for_any_other_cause(
        monkeypatch, capsys):
    """Exit 77 tells `make` and CI "nothing to measure here". A port that
    cannot be bound for another reason is not that."""
    record = _script("record-dump")
    held = _Socket(OSError(errno.EADDRINUSE, "Address already in use"))
    monkeypatch.setattr(record.socket, "socket", lambda *_a: held)
    with pytest.raises(SystemExit) as skipped:
        record.bind_or_skip(8222)
    assert skipped.value.code == record.EXIT_SKIP
    assert held.closed
    assert "close the mixer GUI" in capsys.readouterr().err

    denied = _Socket(OSError(errno.EACCES, "Permission denied"))
    monkeypatch.setattr(record.socket, "socket", lambda *_a: denied)
    with pytest.raises(SystemExit) as failed:
        record.bind_or_skip(80)
    assert failed.value.code == 1
    assert denied.closed
    err = capsys.readouterr().err
    assert "cannot bind the receive port UDP 80: Permission denied" in err
    assert "mixer GUI" not in err


def test_the_evidence_tool_tells_a_held_port_from_an_unbindable_one():
    """`verify-hardware.py` builds its reader deep inside main(), behind a
    device and a sink, so the rule is held structurally: the skip is
    guarded by EADDRINUSE and the other branch returns 1."""
    from conftest import repo_file

    source = repo_file("scripts", "verify-hardware.py").read_text()
    guard = source.index("reader = LevelReader(config.osc_recv_port)")
    block = source[guard:source.index("sinks = playback_sinks()", guard)]
    assert "exc.errno != errno.EADDRINUSE" in block
    assert block.index("return 1") < block.index("return EXIT_SKIP")
    assert "cannot bind the receive port UDP" in block
