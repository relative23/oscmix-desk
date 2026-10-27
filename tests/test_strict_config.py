"""Strict validation refuses ignored declarations before a device operation."""

import argparse
import re
import threading
from pathlib import Path

import pytest
from session_doubles import RunningChild
from support import device_key, fake_proc, write_config

from oscmix_desk import cli, locking, profiles, session
from oscmix_desk import desk as desk_mod
from oscmix_desk import reload as reload_mod
from oscmix_desk.config import load_config
from oscmix_desk.errors import ConfigError
from oscmix_desk.model import CommandLine
from oscmix_desk.outcome import REFUSED

DEVICE = "[device]\nserial=24216011\n"
GOOD = "[output:5]\nvolume=-20\n"
TYPO = "[ouput:5]\nvolume=-30\n"


@pytest.mark.parametrize(("header", "section", "body"), [
    (DEVICE, "ouput:5", "volume=-30"),
    (DEVICE, "eqx:output:5", "enabled=true"),
    (DEVICE, "reverbb", "room=1"),
    ("[device]\nname=Some Box\n", "output:5", "volume=-30"),
    ("[device]\nname=Fireface 802\n", "input:3", "gain=10"),
    ("[device]\nname=Fireface 802\n", "eq:output:5", "enabled=true"),
    ("[device]\nname=Fireface 802\n", "reverb", "room=1"),
])
@pytest.mark.parametrize("device_first", [True, False])
def test_ignored_sections_warn_by_default_and_refuse_in_strict_mode(
        tmp_path, caplog, header, section, body, device_first):
    declaration = "[%s]\n%s\n" % (section, body)
    path = write_config(tmp_path / "routing.conf",
                        header + declaration if device_first else declaration + header)
    with caplog.at_level("WARNING"):
        load_config(path)
    assert "ignoring" in caplog.text
    assert "[%s]" % section in caplog.text
    with pytest.raises(ConfigError, match=re.escape(section)):
        load_config(path, strict=True)


def test_strict_validation_uses_the_overridden_device_model(tmp_path):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    assert load_config(path, strict=True).channels
    said = CommandLine(device_name="Fireface 802")
    assert load_config(path, said=said).channels == ()
    with pytest.raises(ConfigError, match="no register model"):
        load_config(path, said=said, strict=True)


def test_strict_mode_preserves_defaults_and_valid_supported_configs(tmp_path):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    assert load_config(None, strict=True) == load_config(None)
    assert load_config(path, strict=True) == load_config(path)


@pytest.mark.parametrize('strict', [False, True])
def test_running_session_passes_its_validation_mode_to_the_start(
        lifecycle, monkeypatch, strict):
    modes = []
    monkeypatch.setattr(session, '_apply_or_fail',
                        lambda *_a, strict: (modes.append(strict) or None, None))
    assert lifecycle(alive=True, strict=strict) == 0
    assert modes == [strict]


@pytest.mark.parametrize("selection", ["switch", "restore"])
@pytest.mark.parametrize("bad_main", [False, True])
def test_strict_profile_api_refuses_before_lock_connection_and_marker_changes(
        tmp_path, monkeypatch, recording_backend, selection, bad_main):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD + (TYPO if bad_main else ""))
    write_config(tmp_path / "profiles/tracking.conf", GOOD + ("" if bad_main else TYPO))
    marker = path.with_name("active-profile")
    marker.write_text("previous\n")
    if selection == "restore" and not bad_main:
        path.write_text(DEVICE + GOOD + TYPO)
    monkeypatch.setattr(desk_mod, "device_lock", lambda *_a, **_k: pytest.fail("took a lock"))
    monkeypatch.setattr(desk_mod, "connect_backend", lambda *_a, **_k: pytest.fail("connected"))
    monkeypatch.setattr(profiles, "reload_service", lambda: pytest.fail("reloaded"))
    outcome = (profiles.switch_profile("tracking", path, recording_backend,
                                       verify=False, strict=True) if selection == "switch"
               else profiles.restore_main(path, recording_backend, verify=False, strict=True))
    assert outcome.state == REFUSED
    assert not outcome.applied
    assert not outcome.persisted
    assert not outcome.read_back
    assert "ouput:5" in outcome.reason
    assert recording_backend.sent == []
    assert recording_backend.operations == []
    assert marker.read_text() == "previous\n"


@pytest.mark.parametrize("bad_profile", [None, "[output:5]\nvolum=-10\n", TYPO,
                                          "[device]\nserial=99887766\n"])
def test_strict_effective_config_refuses_an_unusable_active_profile(
        tmp_path, caplog, bad_profile):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    if bad_profile is not None:
        write_config(tmp_path / "profiles/tracking.conf", bad_profile)
    marker = path.with_name("active-profile")
    marker.write_text("tracking\n")
    with pytest.raises(ConfigError, match="active profile 'tracking'"):
        profiles.effective_config(path, strict=True)
    with caplog.at_level("WARNING"):
        config, active = profiles.effective_config(path)
    # Unknown sections remain usable in the default compatibility mode.
    assert active == ("tracking" if bad_profile == TYPO else None)
    assert config == (profiles.load_profile("tracking", path) if active else load_config(path))
    assert marker.read_text() == "tracking\n"


@pytest.mark.parametrize("selection", [[], ["--profile", "tracking"], ["--no-profile"]])
@pytest.mark.parametrize("dry_run", [False, True])
def test_strict_cli_and_preview_refuse_the_same_ignored_target(
        tmp_path, monkeypatch, selection, dry_run):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    write_config(tmp_path / "profiles/tracking.conf", GOOD + TYPO)
    if not selection or selection[0] == "--no-profile":
        path.write_text(DEVICE + GOOD + TYPO)
    marker = path.with_name("active-profile")
    marker.write_text("previous\n")
    monkeypatch.setattr(cli, "run_session", lambda *_: pytest.fail("started device discovery"))
    monkeypatch.setattr(desk_mod, "device_lock", lambda *_a, **_k: pytest.fail("took a lock"))
    monkeypatch.setattr(desk_mod, "connect_backend", lambda *_a, **_k: pytest.fail("connected"))
    monkeypatch.setattr(profiles, "reload_service", lambda: pytest.fail("reloaded"))
    assert cli.main(["--config", str(path), "--strict", *selection,
                     *(["--dry-run"] if dry_run else [])]) == 2
    assert marker.read_text() == "previous\n"


@pytest.mark.parametrize("selection", [["--profile", "tracking"], ["--no-profile"]])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize('previous', [None, TYPO, '[output:5]\nvolum=-10\n'])
def test_strict_explicit_selection_can_recover_an_unusable_active_profile(
        tmp_path, monkeypatch, confirming_backend, selection, dry_run, previous):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    write_config(tmp_path / "profiles/tracking.conf", GOOD)
    if previous is not None:
        write_config(tmp_path / 'profiles/missing.conf', previous)
    marker = path.with_name("active-profile")
    marker.write_text("missing\n")
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(fake_proc(
        tmp_path / "proc", boxes=[(24, "24216011")])))
    monkeypatch.setattr(desk_mod, "connect_backend", lambda *_a, **_k: confirming_backend)
    previews = []
    monkeypatch.setattr(cli, "run_session", lambda args, config: previews.append(config) or 0)
    assert cli.main(["--config", str(path), "--strict", *selection,
                     *(["--dry-run"] if dry_run else [])]) == 0
    if dry_run:
        assert previews
        assert marker.read_text() == "missing\n"
        assert confirming_backend.operations == []
    else:
        assert not previews
        assert confirming_backend.sent
        expected = "tracking" if selection[0] == "--profile" else None
        assert profiles.active_profile(path) == expected


@pytest.mark.parametrize("change", ["main", "profile", "missing-profile"])
def test_strict_start_rereads_after_lock_wait_and_never_reports_ready_for_a_bad_desk(
        tmp_path, monkeypatch, change):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    running = load_config(path, strict=True)
    stopped, notices = [], []
    monkeypatch.setattr(session, "_stop_child", stopped.append)
    monkeypatch.setattr(session, "sd_notify", notices.append)
    monkeypatch.setattr(desk_mod, "connect_backend", lambda *_a, **_k: pytest.fail("connected"))
    held = locking.take_device_lock(path, device_key(path))
    assert held is not None

    def invalidate_then_release():
        try:
            if change == "main":
                path.write_text(DEVICE + GOOD + TYPO)
            else:
                if change == "profile":
                    write_config(tmp_path / "profiles/later.conf", GOOD + TYPO)
                path.with_name("active-profile").write_text("later\n")
        finally:
            held.release()

    timer = threading.Timer(0.05, invalidate_then_release)
    timer.start()
    child = RunningChild()
    try:
        assert session._apply_or_fail(child, running, {"stop": False}, path,
                                       strict=True) == (None, 2)
    finally:
        timer.join(timeout=2)
        held.release()
    assert stopped == [child]
    assert not any("READY=1" in notice for notice in notices)
    after = locking.take_device_lock(path, device_key(path), wait=0.1)
    assert after is not None
    after.release()
    if change != "main":
        assert path.with_name("active-profile").read_text() == "later\n"


@pytest.mark.parametrize("change", ["main", "profile", "missing-profile"])
def test_strict_sighup_keeps_the_running_desk_without_connecting_or_writing(
        tmp_path, monkeypatch, change):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    running = load_config(path, strict=True)
    if change == "main":
        path.write_text(DEVICE + GOOD + TYPO)
    else:
        if change == "profile":
            write_config(tmp_path / "profiles/later.conf", GOOD + TYPO)
        path.with_name("active-profile").write_text("later\n")
    notices = []
    monkeypatch.setattr(reload_mod, "sd_notify", notices.append)
    monkeypatch.setattr(desk_mod, "connect_backend", lambda *_a, **_k: pytest.fail("connected"))
    reload_mod._reconcile(argparse.Namespace(config=path, strict=True), running, {"stop": False})
    assert "reconcile skipped" in notices[-1]
    after = locking.take_device_lock(path, device_key(path), wait=0.1)
    assert after is not None
    after.release()
    if change != "main":
        assert path.with_name("active-profile").read_text() == "later\n"


@pytest.mark.parametrize("bad", [None, "main", "profile", "missing-active", "broken-entry"])
def test_check_strictly_validates_all_files_offline(
        tmp_path, monkeypatch, capsys, caplog, bad):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    write_config(tmp_path / "profiles/tracking.conf", GOOD)
    write_config(tmp_path / "profiles/unused.conf", GOOD)
    marker = path.with_name("active-profile")
    marker.write_text("tracking\n")
    if bad == "main":
        path.write_text(DEVICE + GOOD + TYPO)
    elif bad == "profile":
        (tmp_path / "profiles/unused.conf").write_text(GOOD + TYPO)
    elif bad == "missing-active":
        marker.write_text("missing\n")
    elif bad == "broken-entry":
        (tmp_path / "profiles/broken.conf").symlink_to("absent")
    original_marker = marker.read_text()
    monkeypatch.setattr(cli, "run_session", lambda *_: pytest.fail("device discovery"))
    monkeypatch.setattr(desk_mod, "device_lock", lambda *_a, **_k: pytest.fail("took a lock"))
    monkeypatch.setattr(desk_mod, "connect_backend", lambda *_a, **_k: pytest.fail("connected"))
    monkeypatch.setattr(profiles, "reload_service", lambda: pytest.fail("reloaded"))
    assert cli.main(["--config", str(path), "--check"]) == (0 if bad is None else 2)
    output = capsys.readouterr().out
    if bad is None:
        assert "3 file(s)" in output
        checked = (path, *sorted((tmp_path / "profiles").iterdir()))
        assert all(str(file) in output for file in checked)
    else:
        assert "configuration check refused" in caplog.text
        assert "configuration check passed" not in output
    assert marker.read_text() == original_marker


def test_check_does_not_report_success_when_the_profile_directory_cannot_be_read(
        tmp_path, monkeypatch, caplog):
    path = write_config(tmp_path / "routing.conf", DEVICE + GOOD)
    original = Path.iterdir

    def unreadable(directory):
        if directory == tmp_path / "profiles":
            raise PermissionError("profile directory denied")
        return original(directory)

    monkeypatch.setattr(Path, "iterdir", unreadable)
    assert cli.main(["--config", str(path), "--check"]) == 2
    assert "cannot list profiles" in caplog.text


def test_check_without_a_main_config_refuses_before_device_discovery(monkeypatch):
    monkeypatch.setattr(cli, "discover_config_path", lambda: None)
    monkeypatch.setattr(cli, "run_session", lambda *_: pytest.fail("device discovery"))
    assert cli.main(["--check"]) == 2


@pytest.mark.parametrize('directory', ['absent', 'empty', 'file'])
def test_check_accepts_no_profiles_and_refuses_an_invalid_profile_directory(
        tmp_path, capsys, directory):
    path = write_config(tmp_path / 'routing.conf', DEVICE + GOOD)
    if directory == 'empty':
        (tmp_path / 'profiles').mkdir()
    elif directory == 'file':
        (tmp_path / 'profiles').write_text('not a directory')
    assert cli.main(['--config', str(path), '--check']) == (2 if directory == 'file' else 0)
    if directory != 'file':
        assert '1 file(s)' in capsys.readouterr().out


@pytest.mark.parametrize('other', [('--profile', 'tracking'), ('--no-profile',),
                                   ('--diff',), ('--status',), ('--list-profiles',),
                                   ('--dry-run',), ('--device', 'Fireface 802'),
                                   ('--osc-port', '7223')])
def test_check_refuses_conflicting_actions_and_device_overrides_before_reading_files(
        monkeypatch, other):
    monkeypatch.setattr(cli, '_check_configurations',
                        lambda *_: pytest.fail('read files before usage validation'))
    with pytest.raises(SystemExit) as stopped:
        cli.main(['--check', *other])
    assert stopped.value.code == 2
