"""The lock every writer of one interface holds.

A switch, ``--no-profile``, and the unit's own apply, verifier and
reconcile (ADR 0019). Two writers at once would interleave their link
phases and mix writes on the wire, which is the ordering ADR 0001 exists
to guarantee. Keyed by the interface rather than by a config directory
(ADR 0022), in one directory that is the same for every writer on the
machine (ADR 0023), and refused rather than skipped when it cannot be
held.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import grp
import os
import stat
import time
from pathlib import Path
from typing import Iterator, Optional

from .constants import SWITCH_LOCK_WAIT
from .log import log

#: The lock every writer of this desk takes, beside the marker: a
#: switch, `--no-profile`, and the unit's own apply, verifier and
#: reconcile (ADR 0019). Two writers at once would interleave their link
#: phases and mix writes on the wire -- the ordering ADR 0001 exists to
#: guarantee. This name is the last fallback, beside the marker, for a
#: session with neither the shared lock directory nor a runtime
#: directory; every other writer keys on the interface (ADR 0023).
SWITCH_LOCK = "active-profile.lock"

#: The one path that is the same for every writer on the machine,
#: created 3770 root:audio by tmpfiles.d. Overridable for tests through
#: OSCMIX_LOCK_DIR; a directory that is absent means the root steps of
#: the installer never ran, and the search falls through to the ones
#: that depend on the caller (ADR 0023).
SHARED_LOCK_DIR = "/run/oscmix-desk"


class DeviceLock:
    """A held device lock, or a stand-in for "there was nothing to lock".

    The stand-in is for the one case with no path to lock at: no shared
    directory, no runtime directory and no config directory, so no other
    writer could find a lock either. It is not a way to write without a
    lock that exists: a lock that cannot be *taken* is ``None`` from
    ``take_device_lock``, and every caller refuses on it (since 0.6.7).
    """

    def __init__(self, fd: Optional[int] = None) -> None:
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def device_lock_path(config_path: Optional[Path],
                     key: Optional[str] = None) -> Optional[Path]:
    """Where the lock for this device lives.

    `/run/oscmix-desk/<key>.lock` whenever that directory exists, which
    the installer creates through tmpfiles.d. It is the only candidate
    that depends on neither the environment nor the user nor the config
    directory, so every writer of one interface computes it identically
    -- including one under sudo, cron or a bare ssh command, which have
    no `$XDG_RUNTIME_DIR` and used to walk straight past a holder
    (ADR 0023).

    `$XDG_RUNTIME_DIR/oscmix-desk/` second, for a machine whose
    installer never ran the root steps. Beside the config last, for a
    session with neither. None when there is no config either: nothing
    to lock, nothing to contend with.
    """
    if key:
        shared = Path(os.environ.get("OSCMIX_LOCK_DIR", SHARED_LOCK_DIR))
        if shared.is_dir():
            # It exists, so every other writer on this machine is using
            # it. Falling back from here would put this process on a
            # different path from the holder, which is the hole the
            # runtime directory had (ADR 0023). Unusable is a refusal.
            return shared / ("%s.lock" % key)
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            directory = Path(runtime) / "oscmix-desk"
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("cannot use %s (%s); falling back to the config "
                            "directory", directory, exc)
            else:
                return directory / ("%s.lock" % key)
    if config_path is None:
        return None
    return Path(config_path).parent / SWITCH_LOCK


#: How every lock file is opened. The shared directory is writable by a
#: whole group, so what sits at a lock path may have been put there by
#: someone else. O_NOFOLLOW refuses a symbolic link instead of following
#: it -- 0.6.8 followed one and chmod'ed its target to 0666, stopped only
#: by fs.protected_symlinks. O_NONBLOCK keeps a FIFO from blocking open()
#: until a writer appears -- a planted one hung every writer, the 30 s
#: wait included (ADR 0024).
_LOCK_OPEN = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: Owner and group: the shared directory belongs to `audio`, and every
#: writer of the interface is in it. Nobody else may hold the lock.
LOCK_FILE_MODE = 0o660


def _open_lock(path: Path) -> Optional[int]:
    """Open the lock file read-write, or read-only, or not at all.

    The unit runs with `ProtectHome=read-only`, so under a config
    directory it can neither create the file nor open it for writing.
    `flock` needs neither: the lock lives on the open file description,
    and a read-only one holds it exactly as well. None means there is no
    descriptor to lock, which the caller turns into a refusal rather
    than into an unlocked write (ADR 0022).

    Only a regular file is a lock. A directory, FIFO, socket or device
    at the path is refused, whoever put it there (ADR 0024).
    """
    fd = _open_regular(path, os.O_RDWR | os.O_CREAT)
    if fd is not None:
        _share_with_the_directory_group(fd, path)
        return fd
    return _open_regular(path, os.O_RDONLY)


def _open_regular(path: Path, flags: int) -> Optional[int]:
    try:
        fd = os.open(path, flags | _LOCK_OPEN, LOCK_FILE_MODE)
    except OSError:
        return None
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        regular = False
    if not regular:
        os.close(fd)
        return None
    return fd


def _share_with_the_directory_group(fd: int, path: Path) -> None:
    """Give a lock file this process owns the directory's group and 0660.

    The mode passed to open() is masked by the umask, so a unit with
    umask 077 would create a file no other writer can open. Only a file
    this process owns, with exactly one link, is touched: anything else
    at that path is not this process's to change.

    The unit itself can chmod but not regroup: a sandboxed user service
    runs in a user namespace that maps only the user's own group, and
    fchown to `audio` fails there with EINVAL. It does not need to -- a
    file created in the setgid directory has the group already, and the
    tmpfiles.d `z` line regroups one that predates it (ADR 0024).
    """
    try:
        info = os.fstat(fd)
        group = os.stat(path.parent).st_gid
    except OSError:
        return
    if info.st_uid != os.geteuid() or info.st_nlink != 1:
        return
    try:
        os.fchmod(fd, LOCK_FILE_MODE)
        if info.st_gid != group:
            os.fchown(fd, -1, group)
    except OSError:
        pass


def _why_unopenable(path: Path) -> str:
    """What a person needs to know to clear a lock that cannot be opened."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _why_not_creatable(path.parent)
    except OSError as exc:
        return "%s; %s belongs to group %s" % (
            exc.strerror or exc, path.parent, _group_of(path.parent))
    if stat.S_ISLNK(info.st_mode):
        return "it is a symbolic link"
    if not stat.S_ISREG(info.st_mode):
        return "it is not a regular file"
    return "it belongs to uid %d and this user cannot open it" % info.st_uid


def _why_not_creatable(directory: Path) -> str:
    """Why no lock file could be created in ``directory``.

    A read-only mount says so by name: under a sandbox that applies
    ProtectSystem=strict, /run is read-only unless the unit declares the
    directory writable, and "no such file" was the message that case
    produced (measured, 0.6.9).
    """
    try:
        read_only = bool(os.statvfs(directory).f_flag & os.ST_RDONLY)
    except OSError:
        read_only = False
    if read_only:
        return ("%s is read-only for this process; a service needs "
                "ReadWritePaths=-%s" % (directory, directory))
    return "it cannot be created in %s, which belongs to group %s" % (
        directory, _group_of(directory))


def _group_of(directory: Path) -> str:
    try:
        return grp.getgrgid(os.stat(directory).st_gid).gr_name
    except (OSError, KeyError):
        return "an unknown group"


def take_device_lock(config_path: Optional[Path],
                     key: Optional[str] = None,
                     wait: Optional[float] = None) -> Optional[DeviceLock]:
    """Take the lock every writer of this device holds, or None.

    Every writer: a switch, `--no-profile`, and the unit's own apply,
    verifier and reconcile (ADR 0019). None means it is not held, for
    any reason -- contention that outlasted the wait, a lock file that
    cannot be opened, a filesystem that cannot lock. Every caller
    refuses on None since 0.6.7: a write nobody serialised is the thing
    the lock exists to prevent, and "apply anyway" made the guarantee
    conditional on nothing having gone wrong (ADR 0022).
    """
    path = device_lock_path(config_path, key)
    if path is None:
        return DeviceLock()          # no config, so nothing to contend with
    fd = _open_lock(path)
    if fd is None:
        log.error("cannot open the device lock at %s: %s", path,
                  _why_unopenable(path))
        return None
    deadline = time.monotonic() + (SWITCH_LOCK_WAIT if wait is None else wait)
    announced = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return DeviceLock(fd)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                # Not contention: a filesystem that cannot lock, or a
                # descriptor that is gone. Waiting 30 s to say "somebody
                # else has it" would be the wrong answer to both.
                log.error("cannot lock %s (%s)", path, exc)
                os.close(fd)
                return None
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            if not announced:
                log.info("another writer holds the device lock; waiting "
                         "for it")
                announced = True
            time.sleep(0.1)


def held_elsewhere() -> str:
    """What a writer that gave up says: who has it is not known, how long
    it was waited for is."""
    return ("another writer still holds the device lock after %.0fs"
            % SWITCH_LOCK_WAIT)


def _switch_lock_held(config_path: Optional[Path],
                      key: Optional[str] = None) -> Iterator[bool]:
    """Hold the device lock for this config, or yield False after the wait.

    Taken *after* the profile parsed: a refusal for a bad config needs
    no lock and costs nothing, as ADR 0011 promises. Without a config
    there is no directory to lock in and nothing to contend with.
    """
    lock = take_device_lock(config_path, key)
    if lock is None:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


#: Wrapped here rather than with the decorator, on purpose: mutmut
#: leaves decorated functions unmutated, and this loop is what the
#: no-interleave guarantee rests on (ADR 0018). A decorator kept it out
#: of the 0.6.4 mutation run entirely; as a plain generator it is in.
_switch_lock = contextlib.contextmanager(_switch_lock_held)
