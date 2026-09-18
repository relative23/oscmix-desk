"""Switching the whole desk to a named config, with a stated outcome.

A profile is a complete ``routing.conf`` in ``profiles/`` beside the
main one. Not a new section type: a profile *is* a config, parsed by the
same code, subject to the same compatibility rule (ADR 0006), and
``--dump-config > profiles/tracking.conf`` composes for free.

The design constraint is the desk, not the file format. Switching
happens while someone is listening, and there is no rollback for a
mixer: once ``/output/1/volume`` is on the wire, the monitors are loud.
So the order is fixed and the whole module is arranged around it --
**parse and validate everything, then write, then check.** A config that
cannot be understood costs an error message and not one datagram.

That is why this states an outcome rather than raising. Three, and only
three:

``APPLIED_VERIFIED``
    Written, and the device reported the values back.

``APPLIED_UNVERIFIED``
    Written, and the read-back could not confirm it -- normally because
    the mixer GUI holds UDP 8222, which is the common desktop case, not
    a fault. Carries the list of what went unconfirmed, because
    "unverified" without the list is not an outcome a person can act on.

``REFUSED``
    Nothing was written. The config did not parse, the profile does not
    exist, or the name was not a name.

There is deliberately no fourth. "Partly applied, and here is a
traceback" is the state this module exists to make unrepresentable.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import grp
import os
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from .backend import Backend, loopback
from .config import Config, list_profiles, load_config, profile_path
from .constants import SWITCH_LOCK_WAIT, VERIFY_TIMEOUT
from .discovery import (
    Device,
    resolve_device,
    udp_port_listening,
    usb_device_present,
)
from .errors import ConfigError, DeviceAmbiguous, ReceivePortError
from .log import log
from .process import port_holder
from .registers import device_for_name
from .routing import apply_routing
from .verify import expected_registers, register_ever_reported, verify_routing


def _short(paths: List[str], limit: int = 6) -> str:
    """A register list a person can read at the end of a sentence."""
    shown = ", ".join(paths[:limit])
    return shown if len(paths) <= limit else "%s and %d more" % (
        shown, len(paths) - limit)

#: Written, and the device reported it back.
APPLIED_VERIFIED = "applied-verified"
#: Written; the read-back could not confirm it. ``unverified`` says what.
APPLIED_UNVERIFIED = "applied-unverified"
#: Nothing was written. ``reason`` says why.
REFUSED = "refused"

#: The ``reason`` on an outcome where the read-back was never attempted,
#: because the caller asked for none. Distinct wording from a read-back
#: that ran and came up short: the register list means "unknown" here
#: and "looked for and absent" there, and the state cannot say so.
NOT_CHECKED = "verification not requested"

#: The complete set. A fourth member is a design change, and
#: ``tests/test_profiles.py`` asserts this is exhaustive so it cannot
#: arrive by accretion.
STATES = (APPLIED_VERIFIED, APPLIED_UNVERIFIED, REFUSED)


@dataclass(frozen=True)
class Outcome:
    """What a switch did. Every field answerable without a traceback."""

    state: str
    name: str
    reason: str = ""
    #: Everything not confirmed at its expected value.
    unverified: List[str] = field(default_factory=list)
    #: The subset of ``unverified`` this backend never reports at all --
    #: the playback mix matrix, and anything write-only. Kept separate
    #: because "I could not check it" and "it cannot be checked" are
    #: different facts, and on a real routing the second is the normal
    #: case: every switch leaves /mix/<out>/playback/<pb> unconfirmed,
    #: measured, by design (backend.Traits.dumps_playback_matrix).
    unverifiable: List[str] = field(default_factory=list)
    #: Whether the marker now says what the device does. False when the
    #: switch landed but the marker could not be written, or the restore
    #: could not remove it: the desk holds only until the next reload or
    #: start, and the caller must not send that reload itself (ADR 0019).
    persisted: bool = True
    #: Whether a read-back ran, for an applied outcome (a refusal wrote
    #: nothing, so there was nothing to read). False when the caller
    #: asked for none, or the receive port was held or could not be
    #: bound: ``unverified`` then means "unknown" rather than "looked for
    #: and absent", and ``reason`` says why nobody looked. Not derivable
    #: from ``reason``, which is free text for an unbindable port. Until
    #: 0.6.11 a held port was worded "N register(s) unconfirmed", which
    #: is what a read-back that ran and came up short says.
    read_back: bool = True

    @property
    def applied(self) -> bool:
        """Whether anything reached the device.

        The field a script branches on, derived from the state rather
        than stored beside it: two sources for one fact is how "applied
        but the flag says otherwise" happens.
        """
        return self.state != REFUSED

    def describe(self) -> str:
        """One line, for a person."""
        line = self._describe_state()
        if self.persisted:
            return line
        return line + "; not remembered, so the next reload or start undoes it"

    def _describe_state(self) -> str:
        if self.state == REFUSED:
            return "refused %r, nothing written: %s" % (self.name, self.reason)
        if self.state == APPLIED_VERIFIED:
            return "applied %r and verified it at the device" % self.name
        if self.reason == NOT_CHECKED:
            return ("applied %r; not checked, so none of its %d register(s) "
                    "is confirmed" % (self.name, len(self.unverified)))
        if not self.read_back:
            return ("applied %r; not read back (%s), so none of its %d "
                    "register(s) is confirmed"
                    % (self.name, self.reason, len(self.unverified)))
        missed = [p for p in self.unverified if p not in self.unverifiable]
        if not missed:
            return ("applied %r; %d register(s) this backend cannot report: %s"
                    % (self.name, len(self.unverifiable),
                       _short(self.unverifiable)))
        return ("applied %r; %d register(s) unconfirmed: %s%s"
                % (self.name, len(missed), _short(missed),
                   "" if not self.unverifiable
                   else " (plus %d this backend cannot report)"
                        % len(self.unverifiable)))


def load_profile(name: str, config_path: Optional[Path] = None) -> Config:
    """Parse a profile, or raise ``ConfigError``.

    Separate from :func:`switch_profile` so the refusal path can be
    tested, and used, without a device anywhere near it -- that is what
    makes ``--dry-run`` on a profile honest.

    **Transport settings come from the main config, not the profile.**
    A profile describes the desk: what is routed where, and at what
    level. The OSC ports and the device name describe the *machine*, and
    a profile that had to restate them would be wrong the moment it was
    copied between two machines -- or, worse, silently right. That is
    not hypothetical: a profile with no ``[osc]`` section fell back to
    the compiled-in default 7222 during development and wrote to a live
    Fireface from a unit test, because the default happened to match.

    A profile may still set them, and then it wins: the machine that
    needs a second backend on another port is exactly the machine whose
    profiles are per-backend.
    """
    path = profile_path(name, config_path)
    if not path.is_file():
        raise ConfigError("no profile %r (looked in %s)" % (name, path.parent))
    # Read *onto* the machine settings rather than patched with them
    # afterwards. Patched, the profile was validated while it still named
    # the default device: on a desk for another interface its channels
    # were checked against the UCX II's twenty, and a channel section the
    # main config has ignored with a warning since 0.6.2 was accepted
    # through the UCX II's table and written (0.6.11). The parser takes
    # every machine setting with the value it finds as the fallback, so a
    # profile that states one still wins.
    base = None
    if config_path is not None and Path(config_path).is_file():
        base = keep_machine_settings(Config(), load_config(config_path))
    return load_config(path, base)


#: Everything in a config that describes the *machine* rather than the
#: desk, as (section, option, Config attribute). A profile inherits each
#: of these unless it states it itself.
#:
#: A table rather than a list of ifs, because the failure mode is
#: forgetting one: `usb-id` was left out of the first version and would
#: have silently reverted to the compiled-in default on any machine that
#: sets it. `tests/test_profiles.py` holds this table against the set of
#: fields on Config that no `[route]` or channel section can write, so a
#: new machine-level setting cannot be added without landing here too.
MACHINE_SETTINGS = (
    ("osc", "port", "osc_port"),
    ("osc", "recv-port", "osc_recv_port"),
    ("device", "name", "device_name"),
    ("device", "usb-id", "usb_id"),
    ("device", "serial", "serial"),
)


def keep_machine_settings(desk: Config, running: Config) -> Config:
    """``desk``, with the machine-level settings of the process that runs.

    The backend is bound and talking to one interface; a desk re-read
    under it -- by the start once it holds the lock, by a SIGHUP -- changes
    the routing and nothing in ``MACHINE_SETTINGS``. The session spelled the five
    assignments out twice until 0.6.11, which is the way one gets
    forgotten: the table is what a test holds against ``Config``.
    """
    for _section, _option, attr in MACHINE_SETTINGS:
        setattr(desk, attr, getattr(running, attr))
    return desk


#: Where the active profile's name is kept: one line, beside
#: routing.conf. Written by the CLI after an applied switch, removed by
#: `--no-profile`, and only ever *read* by the session -- which is what
#: keeps the unit's ProtectHome=read-only true. Beside the config rather
#: than in a state directory, so that `--config` selects the profiles
#: and the marker together (ADR 0018).
ACTIVE_MARKER = "active-profile"


def active_profile_path(config_path: Optional[Path]) -> Optional[Path]:
    """The marker file for this config, or None without a config."""
    if config_path is None:
        return None
    return Path(config_path).parent / ACTIVE_MARKER


def active_profile(config_path: Optional[Path] = None) -> Optional[str]:
    """The remembered profile name, or None.

    A marker whose content is not a profile name is ignored with a
    warning rather than trusted: the name is used to build a path.
    """
    path = active_profile_path(config_path)
    if path is None or not path.is_file():
        return None
    try:
        name = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("ignoring %s: %s", path, exc)
        return None
    if not name:
        return None
    try:
        profile_path(name, config_path)
    except ConfigError as exc:
        log.warning("ignoring %s: %s", path, exc)
        return None
    return name


def remember_active_profile(name: str, config_path: Optional[Path]) -> bool:
    """Record a switch that was applied. False, and a warning, if it cannot.

    Not an outcome state: the device already has the profile, and a
    fourth state for "applied but forgotten" would be the "applied, but
    the flag says otherwise" case ADR 0011 forbids.
    """
    path = active_profile_path(config_path)
    if path is None:
        return False
    # Written beside and renamed over, never in place: a crash or a
    # power loss between open and close would otherwise leave an empty
    # marker, and an empty marker reads as "no profile" -- the choice
    # silently gone on the next start. The old marker stays whole until
    # the new one is complete on disk, and the rename is atomic.
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            data = (name + "\n").encode("utf-8")
            written = 0
            while written < len(data):
                # write(2) may write less than it was given without
                # failing. A short write here would fsync and rename a
                # truncated profile name over a correct marker.
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        if not _fsync_directory(path.parent):
            log.warning("profile %r remembered, but %s could not be synced: "
                        "the marker is in effect now and may not survive a "
                        "power cut", name, path.parent)
    except OSError as exc:
        log.warning("profile %r applied but not remembered: cannot write "
                    "%s (%s); the desk holds until the next reload or "
                    "start, which applies routing.conf", name, path, exc)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def _fsync_directory(directory: Path) -> bool:
    """Make a rename or an unlink durable; False when it could not be.

    Some filesystems refuse the fsync of a directory, which is why this
    never raises. It says so now rather than swallowing it: without it
    the rename is visible but not durable, and a power cut can bring
    the previous marker back (0.6.6).
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


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


class _Refused(Exception):
    """Why a writer will not write, carried to the outcome."""


def _proc_root() -> Path:
    return Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))


def _target(config: Config, reach: bool) -> Device:
    """The interface this write is for, or _Refused with the reason.

    One resolution for the lock key and for the reachability check, so
    the two cannot describe different boxes (ADR 0024). ``reach`` is
    False when the caller hands in its own backend and so owns the other
    end of the socket -- the test seam, and the only caller that uses it.
    Ambiguity refuses either way: it is about which lock to take.
    """
    try:
        device = resolve_device(config.usb_id, config.device_name,
                                config.serial, _proc_root())
    except DeviceAmbiguous as exc:
        raise _Refused(str(exc)) from exc
    if reach:
        reason = _unreachable(config, device)
        if reason is not None:
            raise _Refused(reason)
    return device


def _still_the_target(config: Config, target: Device) -> Optional[str]:
    """Why a write must not go ahead after the lock wait, or None.

    The wait can take 30 s, and the backend on the port can change in
    that time: checked only before it, a switch wrote to whatever held the
    port afterwards and recorded the marker (found by review, 0.6.9). A
    window of milliseconds remains between this and the first datagram;
    it is the one a UDP write cannot close.
    """
    try:
        now = _target(config, reach=True)
    except _Refused as refusal:
        return str(refusal)
    if now.key != target.key:
        return ("the interface changed while waiting for the device lock "
                "(%s, now %s)" % (target.key, now.key))
    return None


def _unreachable(config: Config, device: Device) -> Optional[str]:
    """Why a write would not reach this interface, or None when it would.

    A write to an unreachable device is not a write: the datagrams land
    in a port nobody bound, or in the wrong one, and a switch that
    reports `applied` for that records a desired state that was never at
    the device.

    Presence in sysfs is not reachability -- a logical disconnect leaves
    the sysfs entry in place (0.6.8) -- and a bound port is not either:
    in 0.6.8 a stranger's socket on the OSC port took a whole routing
    and the marker was set. The port has to be held by an oscmix of this
    user, and when its alsaseqio parent says which client it bridges,
    that has to be the interface resolved above (ADR 0024).
    """
    sysfs = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))
    if not usb_device_present(config.usb_id, sysfs):
        return "%s is not connected" % config.usb_id
    if device.client is None:
        # The backend bridges a sequencer client; no client, no backend for
        # this box -- whatever holds the port. Checked here and not only by
        # the serial comparison below, which has nothing to compare once
        # the card list and the clients are gone: in that state a switch
        # keyed on `<usb id>-unknown` and wrote beside the unit's lock
        # (found by review, 0.6.9).
        return ("%s%s is not visible to ALSA, so no backend can be driving it"
                % (config.usb_id,
                   " with serial %s" % device.serial if device.serial else ""))
    port = config.osc_port
    if not udp_port_listening(port, _proc_root()):
        return ("nothing is listening on UDP %d, so the backend is not "
                "running" % port)
    holder = port_holder(port, _proc_root())
    if holder is None or not holder.oscmix:
        return ("UDP %d is held by %s, not by an oscmix backend of this user"
                % (port, "pid %d" % holder.pid if holder is not None
                   else "a process that cannot be identified"))
    if device.serial and holder.serial and holder.serial != device.serial:
        return ("the backend on UDP %d drives the interface %s, not %s"
                % (port, holder.serial, device.serial))
    if (device.client is not None and holder.client is not None
            and holder.client != device.client):
        return ("the backend on UDP %d bridges sequencer client %d, not %d"
                % (port, holder.client, device.client))
    return None


def _refused_for_the_device(name: str, reason: str) -> "Outcome":
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


class DeviceLock:
    """A held device lock, or a stand-in for "there was nothing to lock".

    The stand-in exists so no caller has to branch: a session without a
    config directory, and an install whose lock file predates ADR 0019,
    both write without one and say so once, rather than refusing to
    drive the device at all.
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
    marker = active_profile_path(config_path)
    return None if marker is None else marker.with_name(SWITCH_LOCK)


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


def _refused_for_the_lock(name: str) -> Outcome:
    reason = ("another writer still holds the device lock after %.0fs"
              % SWITCH_LOCK_WAIT)
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


def forget_active_profile(config_path: Optional[Path]) -> bool:
    """Remove the marker; nothing to remove is not an error.

    False when it is still there afterwards, which the caller carries in
    the outcome: a reload sent then would re-apply the profile the
    marker still names and undo the restore (ADR 0019).
    """
    path = active_profile_path(config_path)
    if path is None:
        return True
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("cannot remove %s (%s); the desk holds until the next "
                    "reload or start, which applies the profile it names",
                    path, exc)
        return False
    if not _fsync_directory(path.parent):
        log.warning("marker removed, but %s could not be synced: the profile "
                    "is out of effect now and may come back after a power "
                    "cut", path.parent)
    return True


def effective_config(config_path: Optional[Path]) -> Tuple[Config, Optional[str]]:
    """The desk a start applies, and the profile it came from.

    The active profile when one is remembered and loads, `routing.conf`
    otherwise. Every path that asks "what desk did you declare" -- the
    start, SIGHUP, --dry-run, --diff -- asks here, so they cannot
    disagree about it.

    Raises ConfigError only for `routing.conf` itself: a main config
    that does not parse is exit 2 as it always was. A remembered profile
    that does not load is a warning and a fallback, because a refused
    start over a file nobody edited is the failure ADR 0006 exists to
    prevent; the marker stays, so the warning stays until somebody
    decides (ADR 0018).
    """
    main = load_config(config_path)
    name = active_profile(config_path)
    if name is None:
        return main, None
    try:
        profile = load_profile(name, config_path)
    except ConfigError as exc:
        log.warning("active profile %r is not usable (%s); applying %s "
                    "instead -- fix the file, or `--no-profile`",
                    name, exc, config_path)
        return main, None
    log.info("active profile %r (%s) is in effect; the routes and channel "
             "state in %s are not", name, profile_path(name, config_path),
             config_path)
    return profile, name


def switch_profile(name: str, config_path: Optional[Path] = None,
                   backend: Optional[Backend] = None,
                   verify: bool = True) -> Outcome:
    """Switch the desk to a profile and say what happened.

    Never raises for a bad config: an unparseable profile is an
    ``Outcome``, because the caller has to distinguish "your typo cost
    you nothing" from "it is applied but I could not check" and an
    exception collapses those into the same thing.
    """
    try:
        config = load_profile(name, config_path)
    except ConfigError as exc:
        # Before any socket exists. The ordering is the promise.
        # The message names the profile: on a desk with five of them,
        # "channel 99 out of range" without a name is a search.
        log.error("profile %r refused, nothing written: %s", name, exc)
        return Outcome(state=REFUSED, name=name, reason=str(exc))

    try:
        target = _target(config, reach=backend is None)
    except _Refused as refusal:
        return _refused_for_the_device(name, str(refusal))

    with _switch_lock(config_path, target.key) as held:
        if not held:
            return _refused_for_the_lock(name)
        changed = _still_the_target(config, target) if backend is None else None
        if changed is not None:
            return _refused_for_the_device(name, changed)
        device = backend if backend is not None else loopback(
            config.osc_port, config.osc_recv_port)
        _write(config, device)
        # Applied, so remembered: from here on every start and reload is
        # this profile's, until --no-profile (ADR 0018). A marker that
        # could not be written travels in the outcome rather than only
        # in the log: the caller must then not reload the unit, whose
        # reconcile would undo what just landed (ADR 0019).
        remembered = remember_active_profile(name, config_path)

        if not verify:
            # Not confirmed, because nobody looked -- which is a different
            # fact from "looked and did not see it", and the state is the
            # same either way. Everything expected goes in the list.
            return Outcome(state=APPLIED_UNVERIFIED, name=name,
                           reason=NOT_CHECKED, persisted=remembered,
                           unverified=sorted(expected_registers(config)),
                           read_back=False)
        return replace(_check(name, config, device), persisted=remembered)


def restore_main(config_path: Optional[Path] = None,
                 backend: Optional[Backend] = None,
                 verify: bool = True) -> Outcome:
    """Apply `routing.conf` again and forget the active profile.

    The same transaction as a switch, with the main config as the desk
    and "routing.conf" as the name the outcome carries: parsed and
    validated in full before the first datagram, then written, then
    checked. The marker goes only once the write is on the wire; a
    refused restore leaves the profile in effect and says so.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("routing.conf refused, nothing written: %s", exc)
        return Outcome(state=REFUSED, name="routing.conf", reason=str(exc))
    try:
        target = _target(config, reach=backend is None)
    except _Refused as refusal:
        return _refused_for_the_device("routing.conf", str(refusal))

    with _switch_lock(config_path, target.key) as held:
        if not held:
            return _refused_for_the_lock("routing.conf")
        changed = _still_the_target(config, target) if backend is None else None
        if changed is not None:
            return _refused_for_the_device("routing.conf", changed)
        device = backend if backend is not None else loopback(
            config.osc_port, config.osc_recv_port)
        _write(config, device)
        forgotten = forget_active_profile(config_path)
        if not verify:
            return Outcome(state=APPLIED_UNVERIFIED, name="routing.conf",
                           reason=NOT_CHECKED, persisted=forgotten,
                           unverified=sorted(expected_registers(config)),
                           read_back=False)
        return replace(_check("routing.conf", config, device),
                       persisted=forgotten)


def _write(config: Config, device: Backend) -> None:
    """Apply the profile, barrier and all.

    Delegates to ``routing.apply_routing`` rather than sending the three
    phases itself. The first version did send them itself -- links, mix,
    channel state, one after the other with nothing in between -- and
    dropped the link barrier in the process. On a live UCX II the whole
    switch, apply and read-back, took 48 ms; the barrier alone is
    measured in seconds. That is the stereo-link race the 0.2.0 release
    was about, reintroduced on a new write path.

    Three defects in 0.3.0 had the same shape: a second
    implementation of something that already existed, correct in
    everything it did and missing something the original had. This is
    the third and last of them.
    """
    apply_routing(config, config.osc_port, config.osc_recv_port,
                  backend=device)


def _check(name: str, config: Config, device: Backend) -> Outcome:
    """Read the state back and classify the switch.

    Delegates the whole read-back to ``verify.verify_routing`` against
    the caller's backend. It deliberately does not reimplement the loop:
    the first version of this function read a single datagram and gave
    up, so a register that was demonstrably correct at the device came
    back unconfirmed -- measured on a UCX II, where ``/output/1/volume``
    went 0.0 -> -6.0 and the switch reported it unverified anyway.

    Safe in the direction it failed, and useless: APPLIED_VERIFIED was
    unreachable on real hardware.
    """
    model = device_for_name(config.device_name)
    registers = expected_registers(config)
    try:
        result = verify_routing(registers, config.osc_port,
                                config.osc_recv_port, VERIFY_TIMEOUT,
                                device_model=model, backend=device)
    except ReceivePortError as exc:
        # Applied -- the barrier waited blind -- and unverifiable for a
        # reason that is not the mixer GUI. The outcome carries it, where
        # it used to read "receive port in use" (0.6.11).
        log.error("profile %r applied; it cannot be verified: %s", name, exc)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason=exc.strerror or str(exc),
                       unverified=sorted(registers), read_back=False)
    if result is None:
        # The mixer GUI holds the port. Applied, blind, and said so.
        log.info("profile %r applied; read-back port in use, cannot verify",
                 name)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason="receive port in use (mixer GUI running?)",
                       unverified=sorted(registers), read_back=False)

    unverified = sorted(result.mismatched + result.unobserved)
    if not unverified:
        return Outcome(state=APPLIED_VERIFIED, name=name)
    # Registers this backend never reports are not evidence of a problem,
    # but they are still not confirmation -- so they stay in the list
    # rather than being quietly dropped from it, and are named as the
    # separate thing they are.
    blind = sorted(p for p in unverified
                   if not register_ever_reported(p, model))
    reason = ("%d unconfirmed, %d of them never reported by this backend"
              % (len(unverified), len(blind)))
    return Outcome(state=APPLIED_UNVERIFIED, name=name, reason=reason,
                   unverified=unverified, unverifiable=blind)


def describe_profiles(config_path: Optional[Path] = None) -> Sequence[str]:
    """Profile names with a one-line summary each, for ``--list-profiles``.

    The active one is marked, because "which desk am I on" is the
    question this list is most often asked.
    """
    active = active_profile(config_path)
    lines = []
    for name in list_profiles(config_path):
        mark = "  (active)" if name == active else ""
        try:
            config = load_profile(name, config_path)
        except ConfigError as exc:
            lines.append("%-16s  BROKEN: %s%s" % (name, exc, mark))
            continue
        lines.append("%-16s  %d route(s), %d channel section(s)%s"
                     % (name, len(config.routes), len(config.channels), mark))
    return lines
