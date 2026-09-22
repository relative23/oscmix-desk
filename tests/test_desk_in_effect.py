"""The desk in effect: the remembered profile, else `routing.conf`
(ADR 0018).

What a start and a reload apply, what `--no-profile` goes back to, and
what a switch says when it could not remember or forget.
"""


import pytest
from profile_desk import GOOD, TRACKING, desk, retargeting_desk, shared_lock_dir
from support import write_config

from oscmix_desk import marker as marker_mod
from oscmix_desk import outcome as outcome_mod
from oscmix_desk import profiles


def test_effective_config_is_the_remembered_profile(tmp_path):
    path = desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    config, name = profiles.effective_config(path)
    assert name == "tracking"
    assert [r.output for r in config.routes] == [(5, 6)]
    # Machine settings still come from routing.conf (ADR 0011).
    assert config.osc_port != 7222

def test_without_a_marker_the_effective_config_is_routing_conf(tmp_path):
    path = desk(tmp_path, tracking=TRACKING)
    config, name = profiles.effective_config(path)
    assert name is None
    assert [r.output for r in config.routes] == [(1, 2)]

@pytest.mark.parametrize(("marker", "why"), [
    ("gone\n", "names a profile that does not exist"),
    ("broken\n", "names a profile that does not parse"),
    ("../../etc/passwd\n", "is not a profile name"),
])
def test_a_marker_that_cannot_be_honoured_falls_back_with_a_warning(
        tmp_path, caplog, marker, why):
    # The desk must come up; a refused start over a file nobody edited is
    # the failure ADR 0006 exists to prevent. The marker stays, so the
    # warning stays until somebody decides.
    path = desk(tmp_path, tracking=TRACKING,
                 broken="[route:x]\noutput = 99\nplayback = 1\n")
    (tmp_path / "active-profile").write_text(marker)
    with caplog.at_level("WARNING"):
        config, name = profiles.effective_config(path)
    assert name is None, why
    assert [r.output for r in config.routes] == [(1, 2)]
    assert "ignoring" in caplog.text or "not usable" in caplog.text
    assert (tmp_path / "active-profile").exists(), "the choice is kept"

def test_a_broken_routing_conf_still_refuses_the_start(tmp_path):
    path = write_config(tmp_path / "routing.conf", "[route:x]\nplayback = 1\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    with pytest.raises(profiles.ConfigError):
        profiles.effective_config(path)

def test_restore_main_applies_routing_conf_and_forgets(tmp_path,
                                                       confirming_backend):
    # A device that answers: with one that only echoes the link flags the
    # read-back waited out its whole 10 s window, twice per run of the
    # suite and once per covering mutant (0.6.10).
    path = desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=confirming_backend)
    assert outcome.applied
    assert outcome.name == "routing.conf"
    assert outcome.reason != outcome_mod.NOT_CHECKED, "a restore checks by default"
    assert confirming_backend.dumps == 1, \
        "the read-back asks the backend it was given, not a socket of its own"
    assert not (tmp_path / "active-profile").exists()
    written = {p for p, _t, _a in confirming_backend.sent}
    assert "/output/1/stereo" in written
    assert "/output/5/stereo" not in written

def test_a_refused_restore_keeps_the_profile(tmp_path, recording_backend):
    path = write_config(tmp_path / "routing.conf", "[route:x]\nplayback = 1\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=recording_backend)
    assert not outcome.applied
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "routing.conf"
    with pytest.raises(profiles.ConfigError) as parsed:
        profiles.load_config(path)
    assert outcome.reason == str(parsed.value), "the reason is the parse error"
    assert recording_backend.sent == []
    assert (tmp_path / "active-profile").read_text().strip() == "tracking"

def test_the_listing_marks_the_active_profile(tmp_path):
    path = desk(tmp_path, tracking=TRACKING, mixdown=GOOD)
    (tmp_path / "active-profile").write_text("mixdown\n")
    lines = profiles.describe_profiles(path)
    assert [line.startswith("mixdown") and line.endswith("(active)")
            for line in lines] == [True, False]

def test_a_marker_that_cannot_be_written_does_not_change_the_outcome(
        tmp_path, recording_backend, caplog, monkeypatch):
    # Not a fourth state: the device has the profile, ADR 0011.
    path = desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.mkdir()          # a directory where the file should be
    with caplog.at_level("WARNING"):
        outcome = profiles.switch_profile("tracking", config_path=path,
                                          backend=recording_backend)
    assert outcome.applied
    assert outcome.name == "tracking"
    assert "not remembered" in caplog.text

def test_restore_main_can_be_asked_not_to_check(tmp_path, recording_backend):
    # verify=False is the switch's contract too (NOT_CHECKED): everything
    # expected goes in the list, and the marker is still forgotten.
    path = desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=recording_backend,
                                    verify=False)
    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert outcome.name == "routing.conf"
    assert outcome.reason == outcome_mod.NOT_CHECKED
    assert outcome.read_back is False
    assert outcome.persisted is True, "the marker was removed either way"
    assert outcome.unverified == sorted(
        profiles.expected_registers(profiles.load_config(path)))
    assert not (tmp_path / "active-profile").exists()

def test_a_switch_that_cannot_remember_says_so_in_the_outcome(
        tmp_path, recording_backend, caplog):
    path = desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").mkdir()
    with caplog.at_level("WARNING"):
        outcome = profiles.switch_profile("tracking", config_path=path,
                                          backend=recording_backend)
    assert outcome.applied
    assert outcome.persisted is False, \
        "the caller decides about the reload, and needs the fact to do it"
    assert "not remembered" in outcome.describe()
    assert "next reload or start" in outcome.describe()

def test_a_restore_that_cannot_forget_says_so_in_the_outcome(
        tmp_path, confirming_backend):
    path = desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.mkdir()
    (marker / "child").write_text("")
    outcome = profiles.restore_main(path, backend=confirming_backend)
    assert outcome.applied
    assert outcome.persisted is False

def test_an_applied_switch_that_was_remembered_stays_persisted(
        tmp_path, recording_backend):
    path = desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=recording_backend)
    assert outcome.persisted is True
    assert "not remembered" not in outcome.describe()

def test_a_restore_to_an_absent_interface_writes_nothing(tmp_path, monkeypatch):
    shared_lock_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("OSCMIX_SYSFS_USB", str(tmp_path / "no-usb"))
    path = desk(tmp_path, tracking=TRACKING)
    outcome = profiles.restore_main(config_path=path)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "routing.conf"
    assert outcome.reason == "2a39:3fd9 is not connected"

def test_a_marker_that_is_not_utf8_is_ignored_with_a_warning(tmp_path, caplog):
    """It raised UnicodeDecodeError past `except OSError` -- on every start."""
    path = desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_bytes(b"\xff\xfe\n")
    with caplog.at_level("WARNING"):
        _config, active = profiles.effective_config(path)
    assert active is None
    assert "ignoring" in caplog.text

def test_a_marker_that_may_not_survive_a_power_cut_says_so_in_the_outcome(
        tmp_path, monkeypatch, recording_backend):
    """The marker is in effect, so the unit is reloaded as usual; what is
    not known is whether the directory entry reached the disk. That was a
    log line only, and `persisted=True` read as more than it meant."""
    path = retargeting_desk(tmp_path)
    durable = profiles.switch_profile("here", config_path=path,
                                      backend=recording_backend, verify=False)
    assert (durable.persisted, durable.durable) == (True, True)
    assert "power cut" not in durable.describe()
    monkeypatch.setattr(marker_mod, "_fsync_directory", lambda _d: False)
    for outcome in (
            profiles.switch_profile("here", config_path=path,
                                    backend=recording_backend, verify=False),
            profiles.restore_main(config_path=path, backend=recording_backend,
                                  verify=False)):
        assert (outcome.persisted, outcome.durable) == (True, False)
        assert outcome.describe().endswith(
            "; remembered, but it may not survive a power cut")
