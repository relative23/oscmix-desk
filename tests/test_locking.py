"""The lock every writer of one interface holds (ADR 0019, 0022-0024).

Where it lives, how it is opened, who may hold it, how long it is waited
for, and that a switch, a restore and the unit each refuse rather than
write without it.
"""

import os
import shutil
import stat

import pytest
from conftest import device_key
from profile_desk import GOOD, TRACKING, desk, shared_lock_dir

from oscmix_desk import locking, profiles
from oscmix_desk import outcome as outcome_mod


def test_a_switch_refuses_when_another_holds_the_lock_too_long(
        tmp_path, recording_backend, monkeypatch, caplog):
    path = desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    umask = os.umask(0o022)
    try:
        with locking._switch_lock(path, device_key(path)) as held:
            assert held
            with caplog.at_level("INFO"):
                outcome = profiles.switch_profile("tracking", config_path=path,
                                                  backend=recording_backend)
    finally:
        os.umask(umask)
    lock = locking.device_lock_path(path, device_key(path))
    # A plain file every writer of the interface can open -- owner and
    # group, the group being the shared directory's (ADR 0024).
    assert stat.S_IMODE(lock.stat().st_mode) == 0o660
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "tracking"
    assert "holds the device lock" in outcome.reason
    assert recording_backend.sent == [], "a refused switch writes nothing"
    assert not (tmp_path / "active-profile").exists()
    # Once, not once per poll: the loop checks every 0.1 s for up to
    # SWITCH_LOCK_WAIT, and a line per check would be 300 of them.
    assert caplog.text.count(
        "another writer holds the device lock; waiting") == 1
    # And once the lock is free, the same switch goes through.
    assert profiles.switch_profile("tracking", config_path=path,
                                   backend=recording_backend).applied

def test_without_a_config_there_is_nothing_to_lock():
    with locking._switch_lock(None) as held:
        assert held is True

def test_no_profile_refuses_when_another_switch_holds_the_lock(
        tmp_path, recording_backend, monkeypatch):
    path = desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    with locking._switch_lock(path, device_key(path)):
        outcome = profiles.restore_main(path, backend=recording_backend)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "routing.conf"
    assert "holds the device lock" in outcome.reason
    assert recording_backend.sent == []
    assert (tmp_path / "active-profile").read_text().strip() == "tracking", \
        "a refused restore keeps the profile in effect"

def test_the_device_lock_is_exclusive_and_released(tmp_path):
    path = desk(tmp_path, tracking=TRACKING)
    lock = locking.take_device_lock(path, device_key(path))
    assert lock is not None
    assert locking.device_lock_path(path, device_key(path)).exists()
    assert locking.take_device_lock(path, device_key(path), wait=0.2) is None, \
        "a second writer must not hold it at the same time"
    lock.release()
    second = locking.take_device_lock(path, device_key(path), wait=0.2)
    assert second is not None
    second.release()
    lock.release()          # releasing twice is not an error

def test_a_writer_without_a_config_gets_a_stand_in(tmp_path):
    # Nothing to lock, and no caller should have to branch on that.
    lock = locking.take_device_lock(None)
    assert lock is not None
    lock.release()

@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_the_unit_locks_a_file_it_cannot_open_for_writing(tmp_path):
    """`ProtectHome=read-only` is the unit's world: flock needs no write."""
    path = desk(tmp_path, tracking=TRACKING)
    lock_file = tmp_path / "active-profile.lock"
    lock_file.write_text("")
    lock_file.chmod(0o444)
    try:
        lock = locking.take_device_lock(path, device_key(path))
        assert lock is not None
        assert locking.take_device_lock(path, device_key(path), wait=0.2) is None
        lock.release()
    finally:
        lock_file.chmod(0o644)

@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
@pytest.mark.skipif(os.geteuid() == 0, reason="root opens anything")
def test_a_lock_that_cannot_be_opened_is_a_refusal(tmp_path, monkeypatch,
                                                   caplog):
    """No lock, no write (ADR 0022).

    Until 0.6.7 this warned and wrote anyway, which made "every writer
    holds one lock" true only while nothing went wrong. Every caller
    refuses now: a switch says so, a reconcile stands down, and a start
    fails so systemd can try again.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = desk(tmp_path, tracking=TRACKING)
    tmp_path.chmod(0o500)
    try:
        with caplog.at_level("ERROR"):
            lock = locking.take_device_lock(path, device_key(path))
    finally:
        tmp_path.chmod(0o700)
    assert lock is None
    assert str(tmp_path / "active-profile.lock") in caplog.text, \
        "the error has to name the lock it could not open"

def test_the_lock_lives_in_the_runtime_directory(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    path = desk(tmp_path, tracking=TRACKING)
    assert locking.device_lock_path(path, "2a39-3fd9-24216011") == \
        runtime / "oscmix-desk" / "2a39-3fd9-24216011.lock"
    assert (runtime / "oscmix-desk").is_dir(), "and it is created"

def test_without_a_runtime_directory_the_lock_stays_beside_the_config(
        tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = desk(tmp_path, tracking=TRACKING)
    assert locking.device_lock_path(path, "2a39-3fd9-24216011") == \
        tmp_path / "active-profile.lock"
    assert locking.device_lock_path(None, "2a39-3fd9-24216011") is None

def test_two_configs_over_one_device_take_the_same_lock(tmp_path, monkeypatch):
    """The point of keying on the hardware.

    Two config directories describing one interface are two desks on one
    device. Until 0.6.7 they held two different lock files and wrote at
    the same time.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    first = desk(tmp_path / "a", tracking=TRACKING)
    second = desk(tmp_path / "b", tracking=TRACKING)
    key = "2a39-3fd9-24216011"
    assert locking.device_lock_path(first, key) == \
        locking.device_lock_path(second, key)
    held = locking.take_device_lock(first, key)
    assert held is not None
    assert locking.take_device_lock(second, key, wait=0.2) is None
    held.release()

def test_without_a_runtime_directory_the_config_path_is_the_lock(
        tmp_path, monkeypatch):
    """The fallback the runtime directory usually hides.

    With `$XDG_RUNTIME_DIR` set, the config path no longer decides where
    the lock lives, so nothing observes it being passed. A session
    without a runtime directory is the case where it still does.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = desk(tmp_path, tracking=TRACKING)
    lock = locking.take_device_lock(path, "2a39-3fd9-24216011")
    assert lock is not None
    assert (tmp_path / "active-profile.lock").exists()
    assert locking.take_device_lock(path, "2a39-3fd9-24216011",
                                     wait=0.2) is None
    lock.release()
    # And with no config either there is nothing to contend over.
    assert locking.take_device_lock(None, "2a39-3fd9-24216011") is not None

def test_a_runtime_directory_that_cannot_be_made_falls_back_and_says_so(
        tmp_path, monkeypatch, caplog):
    # A file where the directory should be: mkdir raises, and the lock
    # has to land beside the config rather than nowhere.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(blocker))
    path = desk(tmp_path, tracking=TRACKING)
    with caplog.at_level("WARNING"):
        where = locking.device_lock_path(path, "2a39-3fd9-24216011")
    assert where == tmp_path / "active-profile.lock"
    assert str(blocker / "oscmix-desk") in caplog.text, \
        "the warning names the directory it could not use"

def test_a_switch_without_a_runtime_directory_locks_beside_the_config(
        tmp_path, recording_backend, monkeypatch):
    """The config path still decides where the lock lives, sometimes.

    With a runtime directory in play it does not, so nothing observes
    the switch passing it down. Without one it does, and a switch must
    then contend with a lock held there.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    path = desk(tmp_path, tracking=TRACKING)
    held = locking.take_device_lock(path, device_key(path))
    assert held is not None
    assert (tmp_path / "active-profile.lock").exists()
    try:
        outcome = profiles.switch_profile("tracking", config_path=path,
                                          backend=recording_backend)
    finally:
        held.release()
    assert outcome.state == outcome_mod.REFUSED
    assert recording_backend.sent == []

def test_the_shared_directory_wins_over_the_runtime_directory(
        tmp_path, monkeypatch):
    shared = shared_lock_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    path = desk(tmp_path, tracking=TRACKING)
    assert locking.device_lock_path(path, "2a39-3fd9-24216011") == \
        shared / "2a39-3fd9-24216011.lock"

def test_a_writer_without_a_runtime_directory_computes_the_same_path(
        tmp_path, monkeypatch):
    """The hole this release exists for.

    `$XDG_RUNTIME_DIR` is absent from sudo, cron and a bare ssh command.
    Measured on the desk in 0.6.7: with a holder on the runtime path, the
    same switch run without the variable computed a path beside the
    config, took it in two seconds and wrote the whole routing.
    """
    shared = shared_lock_dir(tmp_path, monkeypatch)
    path = desk(tmp_path, tracking=TRACKING)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    with_env = locking.device_lock_path(path, "2a39-3fd9-24216011")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    without_env = locking.device_lock_path(path, "2a39-3fd9-24216011")
    assert with_env == without_env == shared / "2a39-3fd9-24216011.lock"

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    held = locking.take_device_lock(path, "2a39-3fd9-24216011")
    assert held is not None
    try:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        assert locking.take_device_lock(
            path, "2a39-3fd9-24216011", wait=0.2) is None
    finally:
        held.release()

def test_two_user_sessions_over_one_interface_contend(tmp_path, monkeypatch):
    """`/run/user/<uid>` is per user; one piece of hardware is not."""
    shared = shared_lock_dir(tmp_path, monkeypatch)
    path = desk(tmp_path, tracking=TRACKING)
    key = "2a39-3fd9-24216011"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-1000"))
    first = locking.take_device_lock(path, key)
    assert first is not None
    try:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-2000"))
        assert locking.device_lock_path(path, key) == \
            shared / ("%s.lock" % key)
        assert locking.take_device_lock(path, key, wait=0.2) is None
    finally:
        first.release()

def test_a_vanishing_runtime_directory_does_not_free_the_lock(
        tmp_path, monkeypatch):
    """It goes with the last logout of a user without lingering.

    In 0.6.7 the holder's file stopped existing and the next writer
    created a fresh inode and took it.
    """
    shared_lock_dir(tmp_path, monkeypatch)
    runtime = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    path = desk(tmp_path, tracking=TRACKING)
    held = locking.take_device_lock(path, "2a39-3fd9-24216011")
    assert held is not None
    try:
        shutil.rmtree(runtime, ignore_errors=True)
        assert locking.take_device_lock(
            path, "2a39-3fd9-24216011", wait=0.2) is None
    finally:
        held.release()

def test_a_writer_without_a_config_still_takes_the_lock(tmp_path, monkeypatch):
    """The shared path needs no config directory, so neither does a writer.

    `scripts/sweep-writes.py` is the one that has none, and it is the
    loudest writer in the repository.
    """
    shared_lock_dir(tmp_path, monkeypatch)
    held = locking.take_device_lock(None, "2a39-3fd9-24216011")
    assert held is not None
    try:
        assert locking.take_device_lock(
            None, "2a39-3fd9-24216011", wait=0.2) is None
    finally:
        held.release()

def test_an_existing_shared_directory_is_never_fallen_back_from(
        tmp_path, monkeypatch, caplog):
    """Falling back from the directory other writers use is the hole itself.

    A lock that cannot be opened there is a refusal, not a reason to
    compute a different path: the writer that quietly locks somewhere
    else is exactly the one that walks past the holder.
    """
    if os.getuid() == 0:
        pytest.skip("root opens anything")
    shared = shared_lock_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    path = desk(tmp_path, tracking=TRACKING)
    shared.chmod(0o500)                      # no new file may be created
    try:
        with caplog.at_level("ERROR"):
            lock = locking.take_device_lock(path, "2a39-3fd9-24216011")
    finally:
        shared.chmod(0o1777)
    assert lock is None
    assert str(shared) in caplog.text, "the error names the lock it wanted"
    assert not (tmp_path / "active-profile.lock").exists(), \
        "and it must not quietly lock somewhere else"
    assert not (tmp_path / "run" / "oscmix-desk").exists()

def test_the_lock_file_is_openable_by_a_second_user(tmp_path, monkeypatch):
    """A service with umask 077 would otherwise lock everyone else out.

    `flock` holds on a read-only descriptor, so a second writer only
    needs to *open* the file -- but a lock file created 0600 by the unit
    cannot be opened by anyone else at all. Measured on the desk: the
    unit's own lock file came out `-rw-------`. Owner and group since
    0.6.9, the group being the directory's (ADR 0024).
    """
    shared_lock_dir(tmp_path, monkeypatch)
    umask = os.umask(0o077)
    try:
        held = locking.take_device_lock(None, "2a39-3fd9-24216011")
    finally:
        os.umask(umask)
    assert held is not None
    try:
        info = locking.device_lock_path(None, "2a39-3fd9-24216011").stat()
        assert stat.S_IMODE(info.st_mode) == 0o660, \
            "created 0o%o; a second writer cannot open it" % stat.S_IMODE(info.st_mode)
        assert info.st_gid == (tmp_path / "shared").stat().st_gid
    finally:
        held.release()

def test_a_configured_serial_is_the_key_a_switch_and_a_restore_lock_on(
        tmp_path, monkeypatch, recording_backend):
    """`[device] serial` separates two boxes only if a writer uses it.

    Two identical interfaces without it share one lock; with it, each
    desk contends on its own box's key and nothing else. A switch that
    dropped the configured serial would key on the card list instead --
    another box's number, or `ambiguous` -- and walk past a holder of
    its own box (ADR 0023).
    """
    shared_lock_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    path = desk(tmp_path, main=GOOD + "\n[device]\nserial = 99887766\n",
                 tracking=TRACKING)
    held = locking.take_device_lock(None, "2a39-3fd9-99887766")
    assert held is not None
    try:
        switched = profiles.switch_profile("tracking", config_path=path,
                                           backend=recording_backend)
        restored = profiles.restore_main(config_path=path,
                                         backend=recording_backend)
    finally:
        held.release()
    assert switched.state == outcome_mod.REFUSED
    assert restored.state == outcome_mod.REFUSED
    assert "holds the device lock" in switched.reason
    assert recording_backend.sent == []

def test_a_restore_without_a_runtime_directory_locks_beside_the_config(
        tmp_path, recording_backend, monkeypatch):
    """The config path still decides the lock for a bare session.

    The switch has this test already; the restore takes the same lock by
    a separate call, and nothing observed it passing the path down.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    path = desk(tmp_path, tracking=TRACKING)
    held = locking.take_device_lock(path, device_key(path))
    assert held is not None
    assert (tmp_path / "active-profile.lock").exists()
    try:
        outcome = profiles.restore_main(config_path=path,
                                        backend=recording_backend)
    finally:
        held.release()
    assert outcome.state == outcome_mod.REFUSED
    assert recording_backend.sent == []

def test_without_an_override_the_lock_directory_is_the_shared_one(
        tmp_path, monkeypatch):
    """`OSCMIX_LOCK_DIR` is for tests; production reads the real path."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o1777)
    monkeypatch.delenv("OSCMIX_LOCK_DIR", raising=False)
    monkeypatch.setattr(locking, "SHARED_LOCK_DIR", str(shared))
    assert locking.device_lock_path(None, "2a39-3fd9-24216011") == \
        shared / "2a39-3fd9-24216011.lock"
