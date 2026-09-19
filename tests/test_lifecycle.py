"""The service contract: exit codes and the readiness protocol.

systemd acts on both. The exit code decides whether the unit restarts, and
under ``Type=notify`` a process that exits 0 without ever sending
``READY=1`` counts as a protocol failure -- which puts the unit into
exactly the restart loop the exit codes are chosen to avoid.

Until now these rules were stated in a module docstring and exercised by
one integration test. Here they are the assertion.
"""


import pytest
from session_doubles import RunningChild, ready_count


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
    from oscmix_desk.model import GlobalSetting

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
# The start-up apply and its verifier are one transaction (ADR 0019).
# --------------------------------------------------------------------------


def _machine(tmp_path, monkeypatch, boxes):
    from conftest import fake_proc

    proc = fake_proc(tmp_path / "proc", boxes=boxes)
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    return proc


def test_the_serial_is_pinned_once_the_device_is_found(lifecycle, tmp_path,
                                                       monkeypatch):
    """The key must not move under a running writer (ADR 0023).

    It is recomputed on every write, and the card list empties the moment
    the interface is unplugged. Measured across a real unplug in 0.6.7:
    the key went from `2a39-3fd9-24216011` to `2a39-3fd9-unknown` and a
    second lock file appeared beside the first. The pinned serial is the
    one in the name of the client the unit bound (ADR 0024).
    """
    proc = _machine(tmp_path, monkeypatch, boxes=[(42, "24216011")])
    lifecycle()
    assert lifecycle.config.serial == "24216011"

    # And once the machine forgets the interface, the pinned key stands.
    (proc / "asound" / "cards").write_text("")
    (proc / "asound" / "seq" / "clients").write_text("")
    from oscmix_desk.discovery import lock_key
    assert lock_key(lifecycle.config.usb_id, lifecycle.config.serial) == \
        "2a39-3fd9-24216011"


def test_a_configured_serial_is_never_overwritten(lifecycle, tmp_path,
                                                  monkeypatch):
    """`[device] serial` names the box outright, and always wins."""
    _machine(tmp_path, monkeypatch, boxes=[(42, "99887766")])
    lifecycle(config_fields={"serial": "24216011"})
    assert lifecycle.config.serial == "24216011"


def test_a_device_that_shows_no_serial_pins_an_empty_one(lifecycle, tmp_path,
                                                         monkeypatch):
    """Empty, not a placeholder: it is the key the card list gives anyway."""
    _machine(tmp_path, monkeypatch, boxes=[])
    lifecycle()
    assert lifecycle.config.serial == ""


def test_a_start_without_the_lock_fails_and_never_signals_ready(
        session_mod, lifecycle):
    """READY=1 only after the routing was applied (ADR 0024).

    0.6.7 and 0.6.8 refused to write without the lock -- and then sent
    READY=1 anyway, so systemd reported a started desk that had never
    been written, and nothing retried. ADR 0022 said the start fails.
    It does now: EXIT_FAILURE, the backend stopped, and Restart=on-failure
    tries again after RestartSec.
    """
    code = lifecycle(alive=True, lock_unavailable=True)
    assert code == session_mod.EXIT_FAILURE
    assert ready_count(lifecycle.notifications) == 0
    assert lifecycle.children[-1].terminated, "the backend it spawned is stopped"


def test_an_ambiguous_interface_is_a_config_error_before_anything_starts(
        session_mod, lifecycle):
    """Two identical interfaces and no `[device] serial`: exit 2, not a guess.

    Exit 2 is in RestartPreventExitStatus, so a config that cannot say
    which box it is for does not become a restart loop (ADR 0024).
    """
    code = lifecycle(alive=True, ambiguous=True)
    assert code == session_mod.EXIT_CONFIG
    assert ready_count(lifecycle.notifications) == 0
    assert lifecycle.children == [], "no backend is started for a guess"


def test_a_start_beside_a_running_session_refuses_with_a_config_error(
        session_mod, lifecycle):
    """Two sessions on one port took turns killing each other's backend."""
    code = lifecycle(alive=True, session_running=True)
    assert code == session_mod.EXIT_CONFIG
    assert lifecycle.children == [], "no backend is started into that port"
    assert "READY=1" not in lifecycle.notifications


def test_a_backend_that_cannot_be_written_to_fails_the_start_cleanly(
        session_mod, lifecycle):
    """An OSError from the socket was a traceback out of run_session."""
    code = lifecycle(alive=True, backend_unreachable=True)
    assert code == session_mod.EXIT_FAILURE
    assert ready_count(lifecycle.notifications) == 0
    assert lifecycle.children[-1].terminated


def test_a_start_that_applied_says_ready_exactly_and_hands_on_its_config(
        monkeypatch, tmp_path):
    """The mutation run of 0.6.10 found no test that read the READY line
    itself, nor one that saw the config path reach the verifier -- the
    path its lock is keyed beside when there is no shared directory."""
    from oscmix_desk import session

    notices = []
    handed = []
    monkeypatch.setattr(session, "sd_notify", notices.append)
    monkeypatch.setattr(session, "_apply_and_verify",
                        lambda child, config, stop, path: handed.append(path)
                        or "verifier")
    path = tmp_path / "routing.conf"
    assert session._apply_or_fail(None, session.Config(), {"stop": False},
                                  path) == ("verifier", None)
    assert notices == ["READY=1"]
    assert handed == [path]
