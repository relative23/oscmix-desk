"""The start-up apply and its verifier are one transaction (ADR 0019).

One lock from the first write to the verifier's last, the desk read
under that lock rather than before it, and a desk for somewhere else not
applied here (ADR 0026).
"""


import pytest
from session_doubles import FakeChild, RunningChild, ready_count
from support import device_key

from oscmix_desk import locking
from oscmix_desk import reload as reload_mod


def _lock_probe(path):

    return locking.take_device_lock(path, device_key(path), wait=0.1)

def test_the_start_holds_the_device_lock_until_the_verifier_is_done(
        tmp_path, monkeypatch, session_mod):
    """One hold, not one per write.

    The verifier re-applies the routing after the start, so a switch
    that landed between the apply and the retry was overwritten -- the
    defect 0.6.3 measured. Holding the lock across both makes the switch
    wait for the whole transaction instead.
    """
    from support import write_config

    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    during = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *a, **k: during.append(("apply",
                                                       _lock_probe(path))))
    monkeypatch.setattr(session_module, "verify_and_repair",
                        lambda *a, **k: during.append(("verify",
                                                       _lock_probe(path))))
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)

    config = session_mod.load_config(path)
    verifier = session_module._apply_and_verify(RunningChild(), config,
                                                {"stop": False}, path)
    assert verifier is not None
    verifier.join(timeout=5)
    assert [tag for tag, _lock in during] == ["apply", "verify"]
    assert [lock for _tag, lock in during] == [None, None], \
        "the lock is held from the first write to the verifier's last"
    after = _lock_probe(path)
    assert after is not None, "and released when the verifier finishes"
    after.release()

def test_a_start_that_cannot_take_the_lock_writes_nothing(
        tmp_path, monkeypatch, session_mod, caplog):
    """No lock, no write (ADR 0022).

    Until 0.6.7 the start applied anyway, on the grounds that a desk
    with no routing is worse than a re-apply. It also made the whole
    guarantee conditional: a write nobody serialised is the one thing
    the lock exists to prevent, and systemd can restart a unit.
    """
    from support import write_config

    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    applied = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *a, **k: applied.append(a))
    monkeypatch.setattr(session_module, "verify_and_repair",
                        lambda *a, **k: None)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    held = locking.take_device_lock(path, device_key(path))
    assert held is not None
    from oscmix_desk.errors import DeviceLockUnavailable

    try:
        with caplog.at_level("ERROR"), pytest.raises(DeviceLockUnavailable):
            session_module._apply_and_verify(
                RunningChild(), session_mod.load_config(path),
                {"stop": False}, path)
    finally:
        held.release()
    # Raised, not returned as None: None also means "nothing declared"
    # and "a stop arrived", and run_session sent READY=1 for all three.
    assert applied == []
    assert "device lock is not available" in caplog.text

def test_a_backend_that_never_binds_its_port_fails_the_start(session_mod,
                                                             lifecycle):
    """Alive but deaf, which READY=1 must not paper over.

    OSC here is UDP: every datagram sent to a port nobody bound is
    accepted by the kernel and dropped. The routing would be "applied"
    into nothing, the verifier would confirm nothing, and systemd would
    have been told the desk is set. The start fails instead, and the
    backend it started does not outlive it.
    """
    assert lifecycle(alive=True, port_ready=False) == session_mod.EXIT_FAILURE
    assert ready_count(lifecycle.notifications) == 0, \
        "Type=notify means the routing is on the device"
    assert lifecycle.children[-1].terminated is True, \
        "a backend this start will not use must not be left running"

_LATER = "[route:y]\nplayback = 5/6\noutput = 5/6\n"

# A profile that names another box was pinned to this process's interface
# and applied here until 0.6.11, and not applied in 0.6.11. Since 0.7.0
# it is no desk at all -- refused where it is loaded (ADR 0026) -- so the
# desk in effect under the lock is routing.conf's.
@pytest.mark.parametrize(("profile", "applied_output"), [
    (_LATER, (5, 6)),
    ("[device]\nusb-id = 1234:5678\nserial = 99887766\n" + _LATER, (1, 2)),
], ids=["a desk for this interface", "a desk for another interface"])
def test_a_start_reads_the_desk_under_the_lock(tmp_path, monkeypatch,
                                               session_mod, profile,
                                               applied_output):
    """A switch that commits while the start waits for the lock wins.

    The config this process parsed at startup is a snapshot older than
    that switch. Serialising the writers is not enough if the loser
    writes a desk nobody asked for any more (0.6.6).
    """
    import threading

    from support import write_config

    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "later.conf", profile)
    applied = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda config, *a, **k: applied.append(config))
    monkeypatch.setattr(session_module, "verify_and_repair",
                        lambda *a, **k: None)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
    started = session_mod.load_config(path)

    held = locking.take_device_lock(path, device_key(path))
    assert held is not None

    def commit_then_release():
        (tmp_path / "active-profile").write_text("later\n")
        held.release()

    threading.Timer(0.3, commit_then_release).start()
    verifier = session_module._apply_and_verify(RunningChild(), started,
                                                {"stop": False}, path)
    assert verifier is not None
    verifier.join(timeout=5)
    assert [r.output for r in applied[0].routes] == [applied_output], \
        "the start applied the wrong desk"
    assert applied[0].osc_port == started.osc_port, \
        "the ports belong to the running process, not to the desk"
    assert applied[0].osc_recv_port == started.osc_recv_port
    assert applied[0].device_name == started.device_name
    assert (applied[0].usb_id, applied[0].serial) == \
        (started.usb_id, started.serial), \
        "the interface belongs to the running process too"

def test_a_stop_during_the_lock_wait_applies_nothing(tmp_path, monkeypatch,
                                                     session_mod):
    # SIGTERM arrived while another writer had the device. Writing the
    # whole routing on the way out is the opposite of what was asked.
    import threading

    from support import write_config

    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    applied = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *a, **k: applied.append(a))
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 5.0)
    stop = {"stop": False}
    held = locking.take_device_lock(path, device_key(path))
    assert held is not None

    def stop_then_release():
        stop["stop"] = True
        held.release()

    threading.Timer(0.3, stop_then_release).start()
    verifier = session_module._apply_and_verify(
        RunningChild(), session_mod.load_config(path), stop, path)
    assert verifier is None
    assert applied == [], "nothing is written on the way out"
    after = locking.take_device_lock(path, device_key(path), wait=0.2)
    assert after is not None, "and the lock is released"
    after.release()

def test_a_config_that_stopped_parsing_keeps_the_desk_of_this_process(
        tmp_path, monkeypatch, session_mod, caplog):
    """Read under the lock, and the file may be mid-edit by then.

    A start that refused to apply anything because somebody was typing
    would leave the desk unrouted. It applies what this process came up
    with, and says which file it could not read.
    """
    from support import write_config


    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    started = session_mod.load_config(path)
    path.write_text("[route:x]\noutput = 99\nplayback = 1\n")
    with caplog.at_level("ERROR"):
        applied = reload_mod._desk_under_the_lock(path, started)
    assert applied is started
    assert "%s is no longer usable (" % path in caplog.text
    assert "); applying the desk this process started with" in caplog.text
    assert "[route:x]" in caplog.text, "the reason is the parser's"

def test_a_backend_that_ignores_the_stop_is_killed(session_module,
                                                   monkeypatch):
    # SIGTERM, then the grace, then SIGKILL: the same escalation the
    # supervisor uses, for the backend a failed start will not use.
    class Stubborn(RunningChild):
        def __init__(self):
            super().__init__()
            self.killed = False

        def terminate(self):
            self.terminated = True          # and keeps running

        def wait(self, timeout=None):
            self.waited = timeout
            raise session_module.subprocess.TimeoutExpired("oscmix", timeout)

        def kill(self):
            self.killed = True

    child = Stubborn()
    session_module._stop_child(child)
    assert child.terminated is True
    assert child.killed is True
    assert child.waited == session_module.CHILD_STOP_GRACE

def test_a_backend_that_is_already_gone_is_not_an_error(session_module):
    class Gone(RunningChild):
        def terminate(self):
            raise OSError("no such process")

    session_module._stop_child(Gone())      # no raise

def test_the_port_wait_answers_whether_the_port_came_up(session_module,
                                                        monkeypatch, tmp_path):
    """Four answers, and the caller fails the start on three of them.

    Before 0.6.6 this returned nothing at all, so a backend that was
    alive and deaf reached READY=1 (ADR 0021). Since 0.6.7 a bound port
    counts only when its owner can be shown to be this backend: the
    cleanup already reads an unresolvable owner as "touch nobody", and
    reading it here as "ready" answered one doubt two opposite ways.
    """
    from test_process import fake_proc

    from oscmix_desk import Config

    config = Config(osc_port=7301)
    proc = fake_proc(tmp_path, {"202": ("oscmix", "oscmix")},
                     listening_port=7301, owner="202")
    assert session_module._await_backend_port(RunningChild(pid=202), config,
                                              proc) is True

    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 0.2)
    assert session_module._await_backend_port(RunningChild(pid=999), config,
                                              proc) is False, \
        "a port held by somebody else is not this backend listening"

    nobody = fake_proc(tmp_path / "b", {"202": ("oscmix", "oscmix")},
                       listening_port=7301)
    assert session_module._await_backend_port(RunningChild(pid=202), config,
                                              nobody) is False, \
        "an owner nobody can resolve is not this backend either"

    monkeypatch.setattr(session_module, "udp_port_listening", lambda *a: False)
    assert session_module._await_backend_port(FakeChild(0), config,
                                              proc) is False, \
        "a backend that exited is not a port that came up"

def test_a_backend_that_exits_before_it_binds_fails_the_start(session_mod,
                                                              lifecycle):
    """Clean exit is not readiness either.

    The start-failure branch used to ask whether the child was still
    alive, so a backend that bound nothing and then exited 0 skipped it
    and collected READY=1 from the exit mapping instead (0.6.7).
    """
    assert lifecycle(port_ready=False, returncode=0) == session_mod.EXIT_FAILURE
    assert ready_count(lifecycle.notifications) == 0

def test_a_device_unplugged_during_the_start_is_still_a_clean_no_op(
        session_mod, lifecycle):
    # The port never came up because the interface went away. That is
    # the one case where a start without routing is the right answer,
    # and systemd must not be told it failed.
    assert lifecycle(port_ready=False, returncode=0,
                     usb_present=False) == session_mod.EXIT_OK
    assert ready_count(lifecycle.notifications) == 1

def test_a_stranger_on_the_port_is_reported_once_not_every_poll(
        session_module, monkeypatch, tmp_path, caplog):
    """Once, not once per 0.25 s, and not never.

    The announcement flag is the whole difference between a line that
    explains a stalled start and either silence or a hundred copies of
    the same sentence.
    """
    from test_process import fake_proc

    from oscmix_desk import Config

    proc = fake_proc(tmp_path, {"201": ("other", "other")},
                     listening_port=7301, owner="201")
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 0.6)
    with caplog.at_level("WARNING"):
        assert session_module._await_backend_port(
            RunningChild(pid=202), Config(osc_port=7301), proc) is False
    assert caplog.text.count("is held by pid 201") == 1
