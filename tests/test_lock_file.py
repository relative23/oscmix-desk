"""What may sit at a lock path, and who may open it (ADR 0023, 0024).

The shared lock directory is writable by a whole group, so a lock file
is opened without following links, without blocking on a FIFO, and only
when it is a regular file -- and a refusal says why.
"""

import errno
import os
import stat
import threading

import pytest
from conftest import repo_file
from two_boxes import lock_dir

from oscmix_desk import locking, profiles


def _take(key, wait=0.2):
    return locking.take_device_lock(None, key, wait=wait)

def test_a_symlink_at_the_lock_path_is_refused_and_its_target_untouched(
        tmp_path, monkeypatch, caplog):
    """0.6.8 followed it and chmod'ed the target to 0666."""
    shared = lock_dir(tmp_path, monkeypatch)
    target = tmp_path / "someone-elses-file"
    target.write_text("secret")
    target.chmod(0o644)
    (shared / "k.lock").symlink_to(target)
    with caplog.at_level("ERROR"):
        assert _take("k") is None
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "symbolic link" in caplog.text

@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a 0444 FIFO read-write")
def test_a_fifo_at_the_lock_path_does_not_block(tmp_path, monkeypatch, caplog):
    """The read-only fallback blocked in open() until a writer appeared.

    Measured in 0.6.8: still hanging after 25 s, the 30 s lock wait never
    reached. 0444 keeps the read-write attempt from opening it, which is
    how a FIFO owned by someone else reaches the fallback.
    """
    shared = lock_dir(tmp_path, monkeypatch)
    os.mkfifo(shared / "k.lock", 0o444)
    result = []
    worker = threading.Thread(target=lambda: result.append(_take("k")),
                              daemon=True)
    with caplog.at_level("ERROR"):
        worker.start()
        worker.join(5)
    assert not worker.is_alive(), "take_device_lock is blocked on a FIFO"
    assert result == [None]
    assert "not a regular file" in caplog.text

def test_a_writable_fifo_or_a_directory_is_not_a_lock(tmp_path, monkeypatch):
    shared = lock_dir(tmp_path, monkeypatch)
    os.mkfifo(shared / "f.lock", 0o666)
    (shared / "d.lock").mkdir()
    assert _take("f") is None
    assert _take("d") is None

@pytest.mark.skipif(os.geteuid() == 0, reason="root opens anything")
def test_a_lock_file_nobody_here_can_open_is_a_refusal_that_says_why(
        tmp_path, monkeypatch, caplog):
    shared = lock_dir(tmp_path, monkeypatch)
    (shared / "k.lock").write_text("")
    (shared / "k.lock").chmod(0)
    with caplog.at_level("ERROR"):
        assert _take("k") is None
    assert "cannot open the device lock" in caplog.text
    assert "this user cannot open it" in caplog.text

def test_a_lock_file_with_another_link_keeps_its_mode(tmp_path, monkeypatch):
    """A hardlink is someone's file under a second name; not ours to chmod."""
    shared = lock_dir(tmp_path, monkeypatch)
    original = shared / "elsewhere"
    original.write_text("")
    original.chmod(0o644)
    os.link(original, shared / "k.lock")
    held = _take("k")
    assert held is not None
    held.release()
    assert stat.S_IMODE(original.stat().st_mode) == 0o644

def test_a_new_lock_file_takes_the_directory_s_group(tmp_path, monkeypatch):
    others = [g for g in os.getgroups() if g != os.getegid()]
    if not others:
        pytest.skip("the test user belongs to one group only")
    shared = lock_dir(tmp_path, monkeypatch)
    try:
        os.chown(shared, -1, others[0])
    except OSError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("cannot hand the directory to a supplementary group")
        raise
    umask = os.umask(0o077)
    try:
        held = _take("k")
    finally:
        os.umask(umask)
    assert held is not None
    held.release()
    info = (shared / "k.lock").stat()
    assert (stat.S_IMODE(info.st_mode), info.st_gid) == (0o660, others[0])

def test_the_lock_directory_belongs_to_the_audio_group():
    """1777 let any local user pre-create or hold a lock (0.6.8 probes)."""
    lines = [line for line in repo_file("systemd", "tmpfiles.d",
                                        "oscmix-desk.conf").read_text().splitlines()
             if line and not line.startswith("#")]
    assert lines == ["d /run/oscmix-desk 3770 root audio -",
                     "z /run/oscmix-desk/*.lock 0660 - audio -"]

def test_a_lock_file_that_cannot_be_regrouped_is_still_a_lock(
        tmp_path, monkeypatch):
    """Measured in the unit: fchown to `audio` fails with EINVAL there.

    A sandboxed user service runs in a user namespace that maps only the
    user's own group. The mode still changes, the lock is still held,
    and nothing is raised (ADR 0024).
    """
    lock_dir(tmp_path, monkeypatch)
    others = [g for g in os.getgroups() if g != os.getegid()]
    if not others:
        # With one group the directory's group is the file's already, so
        # fchown is never reached and this would pass for no reason.
        pytest.skip("the test user belongs to one group only")
    os.chown(tmp_path / "locks", -1, others[0])

    def refuse(*_args):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(profiles.os, "fchown", refuse)
    umask = os.umask(0o077)
    try:
        held = _take("k")
    finally:
        os.umask(umask)
    assert held is not None
    held.release()
    assert stat.S_IMODE((tmp_path / "locks" / "k.lock").stat().st_mode) == 0o660

@pytest.mark.skipif(os.geteuid() == 0, reason="root opens anything")
def test_a_refusal_in_a_directory_of_an_unknown_group_still_says_why(
        tmp_path, monkeypatch, caplog):
    shared = lock_dir(tmp_path, monkeypatch)
    shared.chmod(0o500)

    def unknown(_gid):
        raise KeyError(_gid)

    monkeypatch.setattr(locking.grp, "getgrgid", unknown)
    try:
        with caplog.at_level("ERROR"):
            assert _take("k") is None
    finally:
        shared.chmod(0o770)
    assert "belongs to group an unknown group" in caplog.text

@pytest.mark.skipif(os.geteuid() == 0, reason="root creates anything")
def test_a_read_only_lock_directory_is_named_as_such(tmp_path, monkeypatch,
                                                      caplog):
    """Under a sandbox that applies ProtectSystem=strict, /run is read-only.

    The shipped unit could not create a lock file there -- measured under
    the system manager, which applies the sandbox -- and the refusal said
    "No such file or directory". It names the cause and the fix now.
    """
    shared = lock_dir(tmp_path, monkeypatch)
    shared.chmod(0o500)
    real = os.statvfs

    class ReadOnly:
        f_flag = os.ST_RDONLY

    monkeypatch.setattr(profiles.os, "statvfs",
                        lambda p: ReadOnly() if str(p) == str(shared) else real(p))
    try:
        with caplog.at_level("ERROR"):
            assert _take("k") is None
    finally:
        shared.chmod(0o770)
    assert ("%s is read-only for this process; a service needs "
            "ReadWritePaths=-%s" % (shared, shared)) in caplog.text

def test_the_unit_declares_the_lock_directory_writable():
    unit = repo_file("systemd", "oscmix.service").read_text()
    assert "\nReadWritePaths=-/run/oscmix-desk\n" in unit

@pytest.mark.skipif(os.geteuid() == 0, reason="root searches anything")
def test_a_lock_directory_that_cannot_be_searched_says_permission_and_group(
        tmp_path, monkeypatch, caplog):
    """What a user outside `audio` sees for /run/oscmix-desk (measured)."""
    shared = lock_dir(tmp_path, monkeypatch)
    shared.chmod(0o600)                 # readable, not searchable
    try:
        with caplog.at_level("ERROR"):
            assert _take("k") is None
    finally:
        shared.chmod(0o770)
    record = next(r.getMessage() for r in caplog.records
                  if "cannot open the device lock" in r.getMessage())
    assert record.endswith(": Permission denied; %s belongs to group %s"
                           % (shared, locking._group_of(shared)))
