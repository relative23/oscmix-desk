"""Which profile is in effect, remembered beside the config (ADR 0018).

Read with suspicion, written through a rename so it is never half there,
synced with its directory, and honest about the three ways that can go
wrong.
"""

import os
import stat

import pytest
from profile_desk import GOOD, TRACKING, desk, shared_lock_dir

from oscmix_desk import marker as marker_mod
from oscmix_desk import outcome as outcome_mod
from oscmix_desk import profiles


def test_an_applied_switch_is_remembered_beside_the_config(tmp_path,
                                                          recording_backend):
    path = desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=recording_backend)
    assert outcome.applied
    assert (tmp_path / "active-profile").read_text().strip() == "tracking"
    assert marker_mod.active_profile(path) == "tracking"

def test_a_refused_switch_remembers_nothing(tmp_path, recording_backend):
    path = desk(tmp_path, broken="[route:x]\noutput = 99\nplayback = 1\n")
    assert not profiles.switch_profile("broken", config_path=path,
                                       backend=recording_backend).applied
    assert not (tmp_path / "active-profile").exists()
    assert marker_mod.active_profile(path) is None

def test_the_marker_functions_answer_nothing_without_a_config(tmp_path):
    # No routing.conf, no profiles directory, no marker: None and False,
    # never an AttributeError on a path that does not exist.
    assert marker_mod.active_profile_path(None) is None
    assert marker_mod.active_profile(None) is None
    # In effect and durable, both, each time: `durable` alone went
    # unasserted outside a switch (survivors, 0.6.11).
    assert marker_mod.remember_active_profile("tracking", None) == (False, False)
    assert marker_mod.forget_active_profile(None) == (True, True)
    path = desk(tmp_path, tracking=TRACKING)
    assert marker_mod.remember_active_profile("tracking", path) == (True, True)
    assert (tmp_path / "active-profile").read_text() == "tracking\n"
    assert marker_mod.forget_active_profile(path) == (True, True)
    assert marker_mod.forget_active_profile(path) == (True, True), "twice is fine"
    assert not (tmp_path / "active-profile").exists()

def test_a_marker_write_that_fails_leaves_the_old_marker_whole(tmp_path,
                                                              monkeypatch,
                                                              caplog):
    path = desk(tmp_path, tracking=TRACKING, mixdown=GOOD)
    (tmp_path / "active-profile").write_text("tracking\n")

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(profiles.os, "replace", refuse)
    with caplog.at_level("WARNING"):
        assert marker_mod.remember_active_profile("mixdown", path) == (False,
                                                                     False)
    assert (tmp_path / "active-profile").read_text() == "tracking\n"
    assert list(tmp_path.glob("*.tmp")) == []
    assert "not remembered" in caplog.text

def test_the_marker_goes_through_a_temporary_file_and_a_rename(tmp_path,
                                                              monkeypatch):
    path = desk(tmp_path, tracking=TRACKING)
    import os

    renames = []
    real_replace = marker_mod.os.replace

    def record(src, dst):
        assert os.path.dirname(src) == str(tmp_path), \
            "beside the marker: a rename does not cross file systems"
        renames.append((os.path.basename(src), os.path.basename(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(profiles.os, "replace", record)
    umask = os.umask(0o022)
    try:
        assert marker_mod.remember_active_profile("tracking", path).in_effect is True
    finally:
        os.umask(umask)
    # A temporary name of its own, beside the marker. It was the fixed
    # `active-profile.tmp` until 0.6.11, which two switches holding
    # different device locks shared: one could rename the file the other
    # was still writing.
    (source, target), = renames
    assert target == "active-profile"
    assert source.startswith("active-profile.")
    assert source.endswith(".tmp")
    assert source != "active-profile.tmp"
    assert (tmp_path / "active-profile").read_text() == "tracking\n"
    # World-readable like before, not mkstemp's 0600: the launcher reads it.
    assert stat.S_IMODE((tmp_path / "active-profile").stat().st_mode) == 0o644
    assert list(tmp_path.glob("*.tmp")) == []

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_marker_is_ignored_with_a_warning(tmp_path, caplog):
    path = desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.write_text("tracking\n")
    marker.chmod(0)
    try:
        with caplog.at_level("WARNING"):
            assert marker_mod.active_profile(path) is None
    finally:
        marker.chmod(0o600)
    assert "ignoring" in caplog.text

def test_a_marker_that_cannot_be_removed_is_a_warning_not_a_crash(tmp_path,
                                                                 caplog):
    path = desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.mkdir()
    (marker / "child").write_text("")               # unlink raises
    with caplog.at_level("WARNING"):
        assert marker_mod.forget_active_profile(path) == (False, False)
    assert "cannot remove %s (" % marker in caplog.text

def test_a_marker_change_that_could_not_be_synced_names_the_directory(
        tmp_path, monkeypatch, caplog):
    path = desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(marker_mod, "_fsync_directory", lambda _d: False)
    with caplog.at_level("WARNING"):
        assert marker_mod.remember_active_profile("tracking", path) == (True,
                                                                      False)
        assert marker_mod.forget_active_profile(path) == (True, False)
    assert ("profile 'tracking' remembered, but %s could not be synced"
            % tmp_path) in caplog.text
    assert ("marker removed, but %s could not be synced" % tmp_path) \
        in caplog.text

def test_fsync_of_the_directory_reports_what_it_did(tmp_path, monkeypatch):
    # Never raises, because some filesystems refuse it. It says so
    # instead, and the caller turns that into a warning (0.6.6).
    assert marker_mod._fsync_directory(tmp_path) is True
    assert marker_mod._fsync_directory(tmp_path / "does-not-exist") is False

    def refuse(fd):
        raise OSError("fsync unsupported")

    monkeypatch.setattr(profiles.os, "fsync", refuse)
    assert marker_mod._fsync_directory(tmp_path) is False

def test_an_empty_marker_means_no_profile(tmp_path):
    path = desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("\n")
    assert marker_mod.active_profile(path) is None

@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_config_directory_that_cannot_be_written_is_a_warning(tmp_path,
                                                               caplog):
    # The temporary file never exists, so the clean-up has nothing to
    # remove; that has to be as quiet as the write failing was loud.
    path = desk(tmp_path, tracking=TRACKING)
    tmp_path.chmod(0o500)
    try:
        with caplog.at_level("WARNING"):
            assert marker_mod.remember_active_profile("tracking", path).in_effect is False
    finally:
        tmp_path.chmod(0o700)
    assert not (tmp_path / "active-profile").exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert "not remembered" in caplog.text

def test_forgetting_reports_whether_the_marker_is_gone(tmp_path):
    path = desk(tmp_path, tracking=TRACKING)
    assert marker_mod.forget_active_profile(path).in_effect is True, "nothing to remove"
    (tmp_path / "active-profile").write_text("tracking\n")
    assert marker_mod.forget_active_profile(path).in_effect is True
    assert marker_mod.forget_active_profile(None).in_effect is True

def test_a_short_write_is_finished_rather_than_truncated(tmp_path,
                                                         monkeypatch):
    """write(2) may write less than it was given without failing.

    The marker is renamed over a correct one, so a truncated name would
    replace a good desk with a profile that does not exist.
    """
    real_write = marker_mod.os.write

    def one_byte_at_a_time(fd, data):
        return real_write(fd, data[:1])

    path = desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(profiles.os, "write", one_byte_at_a_time)
    assert marker_mod.remember_active_profile("tracking", path).in_effect is True
    assert (tmp_path / "active-profile").read_text() == "tracking\n"

def test_a_directory_that_cannot_be_synced_warns_on_both_paths(
        tmp_path, monkeypatch, caplog):
    # The marker is in effect either way; what is not guaranteed is that
    # it survives a power cut, and that has to be said rather than
    # swallowed.
    path = desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(marker_mod, "_fsync_directory", lambda _d: False)
    with caplog.at_level("WARNING"):
        assert marker_mod.remember_active_profile("tracking", path).in_effect is True
    assert "may not survive a power cut" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert marker_mod.forget_active_profile(path).in_effect is True
    assert "may come back after a power cut" in caplog.text

def test_a_switch_to_an_absent_interface_writes_nothing(tmp_path, monkeypatch):
    """0.6.7 reported `applied`, exited 0 and recorded the marker.

    Measured with the UCX II unplugged: eight registers unconfirmed, a
    marker naming a profile that had never been at the device, and the
    next start applying it. No backend is handed in here, because the
    check exists for the caller that opens its own socket.
    """
    shared_lock_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("OSCMIX_SYSFS_USB", str(tmp_path / "no-usb"))
    path = desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "tracking", "a refusal says what it refused"
    assert outcome.reason == "2a39:3fd9 is not connected"
    assert not marker_mod.active_profile_path(path).exists(), \
        "and it remembers nothing"

def test_a_switch_refuses_when_no_backend_holds_the_port(tmp_path, monkeypatch):
    """Presence in sysfs is not reachability.

    Measured on the desk: `authorized=0` emptied the ALSA card list and
    the sequencer clients while `/sys/bus/usb/devices/5-2` stayed in
    place with `idVendor` readable -- so a check on sysfs alone still
    said the device was there, and the switch still reported `applied`
    for datagrams the kernel dropped. udev stops the unit the moment the
    device goes, and a stopped unit does the same thing by itself.
    """
    shared_lock_dir(tmp_path, monkeypatch)
    sysfs = tmp_path / "sysfs"
    (sysfs / "5-2").mkdir(parents=True)
    (sysfs / "5-2" / "idVendor").write_text("2a39\n")
    (sysfs / "5-2" / "idProduct").write_text("3fd9\n")
    monkeypatch.setenv("OSCMIX_SYSFS_USB", str(sysfs))
    # A /proc where the interface is visible to ALSA and nothing is bound:
    # the interface is there, the backend is not.
    from conftest import fake_proc

    proc = fake_proc(tmp_path / "proc", boxes=[(24, "24216011")])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    path = desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path)
    assert outcome.state == outcome_mod.REFUSED
    assert "nothing is listening" in outcome.reason
    assert not marker_mod.active_profile_path(path).exists()
