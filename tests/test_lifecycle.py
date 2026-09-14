"""The service contract: exit codes and the readiness protocol.

systemd acts on both. The exit code decides whether the unit restarts, and
under ``Type=notify`` a process that exits 0 without ever sending
``READY=1`` counts as a protocol failure -- which puts the unit into
exactly the restart loop the exit codes are chosen to avoid.

Until now these rules were stated in a module docstring and exercised by
one integration test. Here they are the assertion.
"""

import argparse
import time

import pytest


def _key(path):
    """The device key the code under test derives for this config."""
    from oscmix_desk.discovery import device_key
    from oscmix_desk.profiles import load_config

    return device_key(load_config(path).usb_id)


@pytest.fixture
def session_module():
    from oscmix_desk import session

    return session


def make_args(**overrides):
    values = dict(timeout=1.0, dry_run=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeChild:
    """A backend that is already finished."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


class TimeoutExpired(Exception):
    """subprocess.TimeoutExpired, for the double that stands in for it."""


@pytest.fixture
def lifecycle(session_module, monkeypatch):
    """run_session with every outside interaction replaced.

    Returns a helper that records the readiness notifications sent, so a
    test can assert not only *that* READY was sent but how often.
    """
    notifications = []
    children = []

    def run(*, seq_client=42, usb_present=True, binaries=True,
            returncode=0, stop_requested=False, routes=(), port_ready=True,
            alive=False, **args):
        monkeypatch.setattr(session_module, "sd_notify", notifications.append)
        monkeypatch.setattr(session_module, "wait_for_seq_client",
                            lambda *a, **k: seq_client)
        monkeypatch.setattr(session_module, "usb_device_present",
                            lambda *a, **k: usb_present)
        monkeypatch.setattr(session_module, "resolve_binary",
                            lambda *a, **k: "/bin/true" if binaries else None)
        monkeypatch.setattr(session_module, "_cleanup_stale_backend",
                            lambda *a, **k: None)
        monkeypatch.setattr(session_module, "_install_stop_handlers",
                            lambda *a, **k: None)
        # True: the port came up. False is the backend that lives but
        # never binds, which since 0.6.6 fails the start (ADR 0021).
        monkeypatch.setattr(session_module, "_await_backend_port",
                            lambda *a, **k: port_ready)
        monkeypatch.setattr(session_module, "_apply_and_verify",
                            lambda *a, **k: None)
        def spawn(*a, **k):
            child = RunningChild() if alive else FakeChild(returncode)
            children.append(child)
            return child

        monkeypatch.setattr(session_module, "subprocess",
                            type("S", (), {"Popen": staticmethod(spawn),
                                           "TimeoutExpired": TimeoutExpired})())

        def fake_supervise(child, stop, on_reload=None,
                           reload_requested=None):
            # Signature mirrors the real one, keywords included: a double
            # that accepts **kwargs would have swallowed the reconcile
            # trigger silently instead of failing here.
            stop["stop"] = stop_requested
            return returncode

        monkeypatch.setattr(session_module, "supervise", fake_supervise)

        from oscmix_desk import Config
        config = Config(routes=list(routes))
        return session_module.run_session(make_args(**args), config)

    run.notifications = notifications
    run.children = children
    return run


def ready_count(notifications):
    return notifications.count("READY=1")


def test_no_device_exits_zero_and_signals_ready_once(session_mod, lifecycle):
    # The unit is pulled in by udev whether or not the interface is on.
    # "Nothing to do" is a successful start, not a failure to restart.
    assert lifecycle(seq_client=None, usb_present=False) == session_mod.EXIT_OK
    assert ready_count(lifecycle.notifications) == 1


def test_device_present_without_a_midi_port_is_a_runtime_failure(session_mod,
                                                                 lifecycle):
    # Plugged in but no ALSA client: a driver problem a restart may fix.
    assert lifecycle(seq_client=None,
                     usb_present=True) == session_mod.EXIT_FAILURE
    assert ready_count(lifecycle.notifications) == 0, (
        "a failing start must not claim readiness")


def test_missing_binaries_fail_without_claiming_readiness(session_mod,
                                                          lifecycle):
    assert lifecycle(binaries=False) == session_mod.EXIT_FAILURE
    assert ready_count(lifecycle.notifications) == 0


def test_a_clean_backend_exit_is_success_with_readiness(session_mod,
                                                        lifecycle):
    assert lifecycle(returncode=0) == session_mod.EXIT_OK
    assert ready_count(lifecycle.notifications) >= 1


def test_a_crashed_backend_is_a_runtime_failure(session_mod, lifecycle):
    assert lifecycle(returncode=1) == session_mod.EXIT_FAILURE


def test_a_crash_after_unplugging_is_not_a_failure(session_mod, lifecycle):
    # The backend dies because the device went away. Restarting cannot
    # help and would loop until the unit hits its start limit.
    assert lifecycle(returncode=1, usb_present=False) == session_mod.EXIT_OK
    assert ready_count(lifecycle.notifications) >= 1


def test_a_requested_stop_is_success(session_mod, lifecycle):
    assert lifecycle(returncode=1,
                     stop_requested=True) == session_mod.EXIT_OK
    assert ready_count(lifecycle.notifications) >= 1


@pytest.mark.parametrize("case", [
    {"seq_client": None, "usb_present": False},
    {"returncode": 0},
    {"returncode": 1, "usb_present": False},
    {"returncode": 1, "stop_requested": True},
])
def test_every_zero_exit_signalled_readiness(session_mod, lifecycle, case):
    # The contract in one line: exit 0 implies READY was sent. Type=notify
    # treats the alternative as a protocol failure.
    assert lifecycle(**case) == session_mod.EXIT_OK
    assert ready_count(lifecycle.notifications) >= 1


def test_a_dry_run_starts_nothing(session_mod, lifecycle, capsys):
    route = session_mod.Route(name="m", playback=(1, 2), output=(5, 6))
    assert lifecycle(dry_run=True, routes=[route]) == session_mod.EXIT_OK
    printed = capsys.readouterr().out
    assert "would run: alsaseqio 42:1" in printed
    assert "/output/5/stereo" in printed
    assert ready_count(lifecycle.notifications) == 0, (
        "a dry run is not a started service")


def test_a_config_error_exits_two_without_restarting(session_mod, tmp_path):
    # RestartPreventExitStatus=2: a broken routing.conf must stop the unit
    # rather than loop, because no restart can fix a typo.
    from oscmix_desk import cli

    path = tmp_path / "routing.conf"
    path.write_text("[route:x]\nplayback = 1/2\noutput = nonsense\n")
    assert cli.main(["--config", str(path)]) == session_mod.EXIT_CONFIG


def test_a_signal_death_is_named_not_just_numbered(session_module, monkeypatch,
                                                   caplog, tmp_path):
    # A bare "status -13" sent the first reader of the midnight crash
    # cluster to the exit-code tables; the log now says SIGPIPE itself.
    monkeypatch.setattr(session_module, "usb_device_present",
                        lambda *_a: True)
    with caplog.at_level("ERROR"):
        code = session_module._exit_code_for(
            -13, session_module.Config(routes=[]), tmp_path, {"stop": False})
    assert code == session_module.EXIT_FAILURE
    assert "status -13 (SIGPIPE)" in caplog.text


# --------------------------------------------------------------------------
# What counts as "nothing to apply"
# --------------------------------------------------------------------------

class RunningChild:
    """A backend that is still up, so a verifier may start."""

    def __init__(self, pid=202):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.waited = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        # Recorded rather than ignored: the grace the caller allows is
        # part of what a stop does.
        self.waited = timeout
        return self.returncode


def test_a_config_without_routes_is_still_applied(session_module, monkeypatch):
    """Since 0.4.0 a file may declare channel or global state and no route.

    `_apply_and_verify` returned before `apply_routing` whenever
    `config.routes` was empty -- a guard from 0.1.0, when routes were all
    a file could say, that survived every release the surface grew in.
    An `[input:3]` or `[clock]` section in such a file parsed, showed in
    `--dry-run`, went out on a SIGHUP reload, and never reached the
    device at start. The check is on what the config *declares* now.

    The rest of the assertions pin what the function hands on, because
    the first version of this test checked only "apply was called" and
    the mutation run showed 19 survivors in the function -- the ports,
    the stop check, the thread -- all of them newly covered and none of
    them looked at.
    """
    from oscmix_desk import ChannelSetting, Config
    from oscmix_desk.config import GlobalSetting

    applied = []
    verified = []
    notices = []
    monkeypatch.setattr(session_module, "sd_notify", notices.append)
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *args, **kwargs: applied.append((args, kwargs)))
    monkeypatch.setattr(session_module, "verify_and_repair",
                        lambda *args, **kwargs: verified.append((args, kwargs)))
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)

    child = RunningChild()
    stop = {"stop": False}
    config = Config(osc_port=7301, osc_recv_port=8301,
                    channels=[ChannelSetting("input", 3, "gain", 12.0)],
                    globals=[GlobalSetting("clock", "source", "Internal")])
    verifier = session_module._apply_and_verify(child, config, stop)

    # Written on the configured ports, not on defaults.
    assert applied == [((config, 7301, 8301), {})]
    # Verified on a thread the session can outlive (ADR 0009): a daemon,
    # named for thread dumps, running the verifier exactly once.
    assert verifier is not None, "a config with state to verify got no verifier"
    assert verifier.daemon is True
    assert verifier.name == "verify"
    verifier.join(timeout=5)
    assert not verifier.is_alive()
    assert len(verified) == 1
    # The phases, as `systemctl status` shows them.
    assert notices[:2] == ["STATUS=applying routing", "STATUS=verifying routing"]
    import re
    assert re.fullmatch(r"STATUS=running; verifier finished at \d\d:\d\d:\d\d",
                        notices[2])
    (verified_config, should_stop), kwargs = verified[0]
    assert verified_config is config
    assert kwargs == {}
    # The stop check carries both reasons to stop writing: a shutdown
    # request, and a backend that is already gone.
    assert should_stop() is False
    stop["stop"] = True
    assert should_stop() is True
    stop["stop"] = False
    child.returncode = 1
    assert should_stop() is True


def test_an_empty_config_leaves_the_desk_alone(session_module, monkeypatch,
                                               caplog):
    # The other half of the rule above: a file that declares nothing
    # writes nothing and starts no verifier, and says so.
    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *a, **k: pytest.fail("nothing to apply"))
    with caplog.at_level("INFO"):
        verifier = session_module._apply_and_verify(RunningChild(), Config(),
                                                    {"stop": False})
    assert verifier is None
    assert "leaving mixer state untouched" in caplog.text


# --------------------------------------------------------------------------
# The two start-up helpers that only subprocess tests reached
#
# The 0.6.1 mutation run left exactly 50 mutants with no covering test:
# 26 in _await_backend_port, 23 in _install_stop_handlers, one in a dead
# function. The integration tests drive both through a real process,
# which loads the checked-out source and never a mutant (ADR 0005), so
# the strongest gate in the repository was structurally blind here.
# --------------------------------------------------------------------------



class PollingChild:
    """A backend whose exit can be scheduled by poll count."""

    def __init__(self, exits_after=None, returncode=None):
        self.pid = 202
        self.polls = 0
        self.exits_after = exits_after
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.exits_after is not None and self.polls >= self.exits_after:
            self.returncode = 1
        return self.returncode

    def terminate(self):
        self.terminated = True


def test_a_strangers_port_is_not_backend_readiness(
        session_module, monkeypatch, tmp_path):
    from test_process import fake_proc

    proc = fake_proc(tmp_path, {"201": ("other", "other"),
                                "202": ("oscmix", "oscmix")},
                     listening_port=7301, owner="201")
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 0.01)
    assert session_module._await_backend_port(
        PollingChild(), session_module.Config(osc_port=7301), proc) is False


def test_a_failed_apply_releases_the_device_lock(
        session_module, session_mod, monkeypatch, tmp_path):
    from conftest import write_config

    from oscmix_desk.profiles import take_device_lock

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")

    def fail(*args, **kwargs):
        raise OSError("send failed")

    monkeypatch.setattr(session_module, "apply_routing", fail)
    with pytest.raises(OSError, match="send failed"):
        session_module._apply_and_verify(
            RunningChild(), session_mod.load_config(path), {"stop": False}, path)
    lock = take_device_lock(path, _key(path), wait=0)
    assert lock is not None, "a failed write leaked its lock descriptor"
    lock.release()


def test_the_port_wait_returns_as_soon_as_the_backend_listens(
        session_module, monkeypatch, caplog, tmp_path):
    from oscmix_desk import Config

    answers = iter([False, False, True])
    asked = []

    def listening(port, root):
        asked.append((port, root))
        return next(answers)

    from test_process import fake_proc

    proc = fake_proc(tmp_path, {"202": ("oscmix", "oscmix")},
                     listening_port=7301, owner="202")
    monkeypatch.setattr(session_module, "udp_port_listening", listening)
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 5.0)
    child = PollingChild()
    started = time.monotonic()
    with caplog.at_level("INFO"):
        session_module._await_backend_port(child, Config(osc_port=7301),
                                           proc)
    assert "listening on UDP 7301" in caplog.text
    # The configured port and the given /proc root, every time -- not
    # whatever a stub that ignores its arguments would accept.
    assert asked == [(7301, proc)] * 3
    assert "not listening" not in caplog.text
    assert time.monotonic() - started < 2.0, "it waited out the timeout"


def test_the_port_wait_stops_when_the_backend_dies(session_module, monkeypatch,
                                                  caplog, tmp_path):
    # A child that exited will never bind the port; waiting on would burn
    # the whole timeout for nothing and hide the exit behind a misleading
    # "not listening" warning.
    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "udp_port_listening",
                        lambda port, root: False)
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 5.0)
    child = PollingChild(exits_after=2)
    started = time.monotonic()
    with caplog.at_level("INFO"):
        session_module._await_backend_port(child, Config(osc_port=7301),
                                           tmp_path)
    assert time.monotonic() - started < 2.0
    assert "not listening" not in caplog.text
    assert child.polls >= 2


def test_the_port_wait_times_out_with_a_warning(session_module, monkeypatch,
                                                caplog, tmp_path):
    from oscmix_desk import Config

    monkeypatch.setattr(session_module, "udp_port_listening",
                        lambda port, root: False)
    monkeypatch.setattr(session_module, "PORT_READY_TIMEOUT", 0.3)
    with caplog.at_level("WARNING"):
        session_module._await_backend_port(PollingChild(), Config(osc_port=7301),
                                           tmp_path)
    assert "not listening on UDP 7301" in caplog.text


def _capture_signal_handlers(session_module, monkeypatch):
    installed = {}
    monkeypatch.setattr(session_module.signal, "signal",
                        lambda signum, handler: installed.__setitem__(signum,
                                                                      handler))
    return installed


def test_stop_handlers_turn_a_signal_into_an_orderly_shutdown(session_module,
                                                              monkeypatch):
    import signal

    installed = _capture_signal_handlers(session_module, monkeypatch)
    notices = []
    monkeypatch.setattr(session_module, "sd_notify", notices.append)
    child = PollingChild()
    stop = {"stop": False}
    session_module._install_stop_handlers(child, stop)
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}

    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert stop["stop"] is True
    assert notices == ["STOPPING=1"], "systemd must hear about the stop first"
    assert child.terminated is True


def test_stop_handlers_do_not_terminate_a_backend_that_is_already_gone(
        session_module, monkeypatch):
    import signal

    installed = _capture_signal_handlers(session_module, monkeypatch)
    monkeypatch.setattr(session_module, "sd_notify", lambda *_a: None)
    child = PollingChild(returncode=0)
    stop = {"stop": False}
    session_module._install_stop_handlers(child, stop)
    installed[signal.SIGINT](signal.SIGINT, None)
    assert stop["stop"] is True
    assert child.terminated is False


# --------------------------------------------------------------------------
# The start-up apply and its verifier are one transaction (ADR 0019).
# --------------------------------------------------------------------------

def _lock_probe(path):
    from oscmix_desk import profiles as profiles_mod

    return profiles_mod.take_device_lock(path, _key(path), wait=0.1)


def test_the_start_holds_the_device_lock_until_the_verifier_is_done(
        tmp_path, monkeypatch, session_mod):
    """One hold, not one per write.

    The verifier re-applies the routing after the start, so a switch
    that landed between the apply and the retry was overwritten -- the
    defect 0.6.3 measured. Holding the lock across both makes the switch
    wait for the whole transaction instead.
    """
    from conftest import write_config

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
    from conftest import write_config

    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    applied = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *a, **k: applied.append(a))
    monkeypatch.setattr(session_module, "verify_and_repair",
                        lambda *a, **k: None)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    monkeypatch.setattr(profiles_mod, "SWITCH_LOCK_WAIT", 0.3)
    held = profiles_mod.take_device_lock(path, _key(path))
    assert held is not None
    try:
        with caplog.at_level("ERROR"):
            verifier = session_module._apply_and_verify(
                RunningChild(), session_mod.load_config(path),
                {"stop": False}, path)
    finally:
        held.release()
    assert verifier is None, "nothing to verify, because nothing was written"
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


def test_a_start_reads_the_desk_under_the_lock(tmp_path, monkeypatch,
                                               session_mod):
    """A switch that commits while the start waits for the lock wins.

    The config this process parsed at startup is a snapshot older than
    that switch. Serialising the writers is not enough if the loser
    writes a desk nobody asked for any more (0.6.6).
    """
    import threading

    from conftest import write_config

    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "later.conf",
                 "[route:y]\nplayback = 5/6\noutput = 5/6\n")
    applied = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda config, *a, **k: applied.append(config))
    monkeypatch.setattr(session_module, "verify_and_repair",
                        lambda *a, **k: None)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    monkeypatch.setattr(profiles_mod, "SWITCH_LOCK_WAIT", 5.0)
    started = session_mod.load_config(path)

    held = profiles_mod.take_device_lock(path, _key(path))
    assert held is not None

    def commit_then_release():
        (tmp_path / "active-profile").write_text("later\n")
        held.release()

    threading.Timer(0.3, commit_then_release).start()
    verifier = session_module._apply_and_verify(RunningChild(), started,
                                                {"stop": False}, path)
    assert verifier is not None
    verifier.join(timeout=5)
    assert [r.output for r in applied[0].routes] == [(5, 6)], \
        "the start applied the desk it read before waiting"
    assert applied[0].osc_port == started.osc_port, \
        "the ports belong to the running process, not to the desk"
    assert applied[0].osc_recv_port == started.osc_recv_port
    assert applied[0].device_name == started.device_name


def test_a_stop_during_the_lock_wait_applies_nothing(tmp_path, monkeypatch,
                                                     session_mod):
    # SIGTERM arrived while another writer had the device. Writing the
    # whole routing on the way out is the opposite of what was asked.
    import threading

    from conftest import write_config

    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    applied = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda *a, **k: applied.append(a))
    monkeypatch.setattr(profiles_mod, "SWITCH_LOCK_WAIT", 5.0)
    stop = {"stop": False}
    held = profiles_mod.take_device_lock(path, _key(path))
    assert held is not None

    def stop_then_release():
        stop["stop"] = True
        held.release()

    threading.Timer(0.3, stop_then_release).start()
    verifier = session_module._apply_and_verify(
        RunningChild(), session_mod.load_config(path), stop, path)
    assert verifier is None
    assert applied == [], "nothing is written on the way out"
    after = profiles_mod.take_device_lock(path, _key(path), wait=0.2)
    assert after is not None, "and the lock is released"
    after.release()


def test_a_config_that_stopped_parsing_keeps_the_desk_of_this_process(
        tmp_path, monkeypatch, session_mod, caplog):
    """Read under the lock, and the file may be mid-edit by then.

    A start that refused to apply anything because somebody was typing
    would leave the desk unrouted. It applies what this process came up
    with, and says which file it could not read.
    """
    from conftest import write_config

    from oscmix_desk import session as session_module

    path = write_config(tmp_path / "routing.conf",
                        "[route:x]\nplayback = 1/2\noutput = 1/2\n")
    started = session_mod.load_config(path)
    path.write_text("[route:x]\noutput = 99\nplayback = 1\n")
    with caplog.at_level("ERROR"):
        applied = session_module._desk_under_the_lock(path, started)
    assert applied is started
    assert "no longer usable" in caplog.text


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
