"""`--profile` and `--list-profiles` as commands.

The outcome logic is covered in tests/test_profiles.py against a
recording backend. This is the part that turns an outcome into an exit
code and a line of stdout -- the only part a shell script can see.

Written at the same time as the feature rather than after the coverage
ratchet complained, which is the difference from --dump-config: that one
landed at 53% on cli.py and was found by the gate, on a push.
"""

import pytest
from conftest import free_udp_port, proc_with_ports, write_config

from oscmix_desk import cli
from oscmix_desk.constants import (
    EXIT_CONFIG,
    EXIT_NOT_PERSISTED,
    EXIT_OK,
    EXIT_RELOAD_FAILED,
)

GOOD = """
[route:main]
output = 1/2
playback = 1/2
level = 0.0

[output:1]
volume = -10.0
"""


@pytest.mark.parametrize("selection", [("--profile", "tracking"), ("--no-profile",)])
def test_a_dry_run_never_switches_or_forgets_a_profile(
        tmp_path, capsys, monkeypatch, selection):
    from oscmix_desk import session

    path = _config_with(tmp_path, {"tracking": GOOD}, monkeypatch)
    marker = tmp_path / "active-profile"
    marker.write_text("tracking\n")
    writes = []
    _quick_wire(monkeypatch)
    monkeypatch.setattr(cli, "reload_service", lambda: writes.append("reload"))
    monkeypatch.setattr(session, "wait_for_seq_client", lambda *a: 42)
    assert cli.main(["--config", str(path), *selection, "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "would run:" in out
    assert ("would send:" in out) == (selection[0] == "--profile")
    assert marker.read_text() == "tracking\n"
    assert not (tmp_path / "active-profile.lock").exists()
    assert writes == []


@pytest.mark.parametrize("extra", ["--diff", "--dump-config", "--no-profile"])
def test_conflicting_profile_actions_are_rejected_before_writing(
        tmp_path, monkeypatch, extra):
    path = _config_with(tmp_path, {"tracking": GOOD}, monkeypatch)
    _quick_wire(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(path), "--profile", "tracking", extra])
    assert exc.value.code == EXIT_CONFIG
    assert not (tmp_path / "active-profile").exists()


def _config_with(tmp_path, profiles, monkeypatch=None):
    """A routing.conf plus a profiles/ directory beside it.

    The ports are free ones, and the profiles inherit them from here
    rather than restating them. That inheritance is not a convenience:
    without it these profiles fall back to the compiled-in 7222, and on
    a developer machine with a Fireface attached that is the live
    backend. This suite moved a fader on real hardware exactly once.
    """
    for name, text in profiles.items():
        write_config(tmp_path / "profiles" / ("%s.conf" % name), text)
        assert "[osc]" not in text, (
            "a profile fixture must not state its own port -- inheriting "
            "it from the tmp_path config is what keeps this off the "
            "hardware")
    send_port, recv_port = free_udp_port(), free_udp_port()
    if monkeypatch is not None:
        # Since 0.6.8 a switch that opens its own socket refuses when
        # nothing holds the send port, because a write nobody receives
        # must not be reported as applied (ADR 0023). These tests drive
        # the real CLI against no backend on purpose -- what they assert
        # is the outcome-to-exit-code translation, not reachability,
        # which has its own test. So the port is shown as bound.
        monkeypatch.setenv("OSCMIX_PROC_ROOT",
                           str(proc_with_ports(tmp_path / "proc", send_port)))
    return write_config(tmp_path / "routing.conf",
                        "[osc]\nport = %d\nrecv-port = %d\n"
                        % (send_port, recv_port))


def test_listing_profiles_prints_one_line_each(tmp_path, capsys, monkeypatch):
    path = _config_with(tmp_path, {"tracking": GOOD, "mixdown": GOOD}, monkeypatch)
    assert cli.main(["--config", str(path), "--list-profiles"]) == EXIT_OK
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("mixdown")
    assert lines[1].startswith("tracking")


def test_listing_with_no_profiles_prints_nothing_and_succeeds(tmp_path,
                                                              capsys):
    path = write_config(tmp_path / "routing.conf", "[osc]\nport = 7222\n")
    assert cli.main(["--config", str(path), "--list-profiles"]) == EXIT_OK
    assert capsys.readouterr().out == ""


def test_a_refused_profile_exits_config_and_says_nothing_was_written(
        tmp_path, capsys, monkeypatch):
    """The exit code a script branches on.

    EXIT_CONFIG rather than EXIT_FAILURE because it is the same failure
    as a bad routing.conf at startup: it did not parse, so nothing
    happened. A script that retries on failure must not retry this.
    """
    path = _config_with(tmp_path, {
        "bad": "[route:x]\noutput = 99\nplayback = 1\n"}, monkeypatch)
    assert cli.main(["--config", str(path), "--profile", "bad"]) == EXIT_CONFIG
    out = capsys.readouterr().out
    assert "refused" in out
    assert "nothing written" in out
    assert "bad" in out


def test_a_missing_profile_exits_config(tmp_path, capsys, monkeypatch):
    path = _config_with(tmp_path, {}, monkeypatch)
    assert cli.main(["--config", str(path),
                     "--profile", "nosuch"]) == EXIT_CONFIG
    assert "nosuch" in capsys.readouterr().out


def test_an_applied_but_unverifiable_switch_still_exits_ok(tmp_path, capsys,
                                                          monkeypatch):
    """No backend is running, so the read-back confirms nothing.

    This is the desktop case in miniature: applied, unconfirmable, and
    that is EXIT_OK because the registers did go out. Reporting failure
    here would make every switch on a machine with the mixer GUI open
    look broken.
    """
    # This runs the real CLI against nothing, so both real waits apply:
    # the link barrier, and then the full verify window with no device
    # to answer it. At their shipped values that is 11.5 s -- the
    # slowest single test in the suite, and mutmut re-runs it per
    # mutant, which is what pushed the mutation job past a 90-minute
    # CI timeout. The outcome is what is under test here, not the
    # durations; ADR 0010's timing tests own those.
    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import routing as routing_mod
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.05)
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.05)
    monkeypatch.setattr(profiles_mod, "VERIFY_TIMEOUT", 0.3)

    path = _config_with(tmp_path, {"tracking": GOOD}, monkeypatch)
    assert cli.main(["--config", str(path),
                     "--profile", "tracking"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "applied" in out
    assert "unconfirmed" in out
    assert "/output/1/volume" in out, (
        "the channel section has to appear in the unconfirmed list -- "
        "it was missing from the read-back entirely until 0.3.0, and "
        "then present but structurally unreachable because the window "
        "closed as soon as the stereo flags matched")


# --------------------------------------------------------------------------
# --no-profile, and the listing's mark (ADR 0018)
# --------------------------------------------------------------------------

def _quick_wire(monkeypatch):
    # Nothing listens on the free ports: shorten the barrier and the
    # read-back window so the transaction runs in well under a second.
    from oscmix_desk import profiles as profiles_mod
    from oscmix_desk import routing as routing_mod

    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.05)
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.05)
    monkeypatch.setattr(profiles_mod, "VERIFY_TIMEOUT", 0.2)


def test_no_profile_applies_routing_conf_and_forgets(tmp_path, capsys,
                                                     monkeypatch):
    _quick_wire(monkeypatch)
    path = _config_with(tmp_path, {"tracking": GOOD}, monkeypatch)
    (tmp_path / "active-profile").write_text("tracking\n")
    assert cli.main(["--config", str(path), "--no-profile"]) == EXIT_OK
    assert "routing.conf" in capsys.readouterr().out
    assert not (tmp_path / "active-profile").exists()


def test_no_profile_with_a_broken_routing_conf_exits_config(tmp_path, capsys):
    path = write_config(tmp_path / "routing.conf", "[route:x]\nplayback = 1\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    # routing.conf itself does not parse, so the start-up load refuses
    # before --no-profile is reached: exit 2 like any bad routing.conf,
    # and the profile stays in effect for the next start.
    assert cli.main(["--config", str(path), "--no-profile"]) == EXIT_CONFIG
    assert capsys.readouterr().out == ""
    assert (tmp_path / "active-profile").read_text().strip() == "tracking"


def test_a_switch_is_remembered_and_the_listing_shows_it(tmp_path, capsys,
                                                          monkeypatch):
    _quick_wire(monkeypatch)
    path = _config_with(tmp_path, {"tracking": GOOD, "mixdown": GOOD}, monkeypatch)
    assert cli.main(["--config", str(path), "--profile", "tracking"]) == EXIT_OK
    capsys.readouterr()
    assert cli.main(["--config", str(path), "--list-profiles"]) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("tracking") and "(active)" in line
               for line in lines)
    assert not any("(active)" in line for line in lines
                   if line.startswith("mixdown"))


def test_an_applied_switch_reloads_the_unit_and_a_refused_one_does_not(
        tmp_path, capsys, monkeypatch, caplog):
    # Measured: a switch sent right after a restart was reverted by the
    # unit's start-up verifier fifteen seconds later. The reload makes
    # the unit re-read the desk in effect (ADR 0018).
    _quick_wire(monkeypatch)
    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    path = _config_with(
        tmp_path,
        {"tracking": GOOD,
         "broken": "[route:x]\noutput = 99\nplayback = 1\n"},
        monkeypatch)
    assert cli.main(["--config", str(path), "--profile", "broken"]) == EXIT_CONFIG
    assert reloads == [], "a refused switch has nothing to hand over"
    capsys.readouterr()
    with caplog.at_level("INFO"):
        assert cli.main(["--config", str(path), "--profile", "tracking"]) == EXIT_OK
    assert reloads == [1]
    assert capsys.readouterr().out.endswith("\n")
    assert ("oscmix.service reloaded, so its own reconcile follows the new "
            "desk") in caplog.text
    assert cli.main(["--config", str(path), "--no-profile"]) == EXIT_OK
    assert reloads == [1, 1]
    # A stopped unit is not an error: there is nothing whose verifier
    # could revert the switch, and the log says so rather than nothing.
    monkeypatch.setattr(cli, "reload_service", lambda: cli.RELOAD_NOT_RUNNING)
    caplog.clear()
    with caplog.at_level("INFO"):
        assert cli.main(["--config", str(path), "--no-profile"]) == EXIT_OK
    assert "oscmix.service is not running; nothing to reload" in caplog.text
    # A running unit that refuses the reload is neither of those: it is
    # up, on a desk it has not re-read, and the exit code says so.
    from oscmix_desk.process import RELOAD_FAILED

    monkeypatch.setattr(cli, "reload_service", lambda: RELOAD_FAILED)
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert cli.main(["--config", str(path),
                         "--no-profile"]) == EXIT_RELOAD_FAILED
    assert "refused the reload" in caplog.text


# --------------------------------------------------------------------------
# A switch the marker did not record must not be handed to the unit
# (ADR 0019).
# --------------------------------------------------------------------------

def test_a_switch_whose_marker_cannot_be_written_does_not_reload(
        tmp_path, capsys, monkeypatch, caplog):
    """Measured on the desk: the reload undid the switch two seconds later.

    With the marker unwritten the unit re-reads routing.conf, and its
    reconcile re-applies it over the profile that just landed. The
    switch is still applied; what it must not do is send the reload that
    undoes it, and the exit code has to say that the desk is temporary
    so a provisioning script cannot read it as done (0.6.6).
    """
    _quick_wire(monkeypatch)
    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    path = _config_with(tmp_path, {"tracking": GOOD}, monkeypatch)
    (tmp_path / "active-profile").mkdir()      # a directory: the write fails
    with caplog.at_level("WARNING"):
        assert cli.main(["--config", str(path),
                         "--profile", "tracking"]) == EXIT_NOT_PERSISTED
    assert reloads == [], "the reload would undo the switch"
    assert "not remembered" in capsys.readouterr().out
    assert "oscmix.service not reloaded" in caplog.text


def test_no_profile_that_cannot_forget_the_marker_does_not_reload(
        tmp_path, capsys, monkeypatch, caplog):
    # The mirror image: the marker still names the profile, so a reload
    # would bring the profile back over the restore.
    _quick_wire(monkeypatch)
    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    path = _config_with(tmp_path, {"tracking": GOOD}, monkeypatch)
    marker = tmp_path / "active-profile"
    marker.mkdir()
    (marker / "child").write_text("")          # a non-empty directory
    with caplog.at_level("WARNING"):
        assert cli.main(["--config", str(path),
                         "--no-profile"]) == EXIT_NOT_PERSISTED
    assert reloads == []
    assert "oscmix.service not reloaded" in caplog.text
