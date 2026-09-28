"""Backend process supervision: stale cleanup and stop escalation."""

from __future__ import annotations

import getopt
import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import hostservice
from .constants import CHILD_STOP_GRACE, SERVICE_UNIT, STALE_BACKEND_SETTLE
from .discovery import (
    Device,
    parse_seq_clients,
    serial_in,
)
from .log import log


def find_stale_backends(proc_root: Path) -> List[int]:
    """PIDs of oscmix processes owned by this user.

    Matches the kernel comm name (what ``pkill -x`` used) as well as the
    argv0 basename, so a rewritten or empty cmdline cannot hide a stale
    backend.
    """
    pids = []
    uid = os.getuid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            owned = entry.stat().st_uid == uid
        except OSError:
            continue  # the process is gone, or its ownership is unreadable
        if not owned:
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip()
        except OSError:
            comm = ""
        try:
            argv0 = (entry / "cmdline").read_bytes().split(b"\x00", 1)[0]
        except OSError:
            argv0 = b""
        if comm == "oscmix" or \
                os.path.basename(argv0.decode("utf-8", "replace")) == "oscmix":
            pids.append(int(entry.name))
    return sorted(pids)


def _owner_of_sockets(inodes: Set[str], proc_root: Path) -> Optional[int]:
    if not inodes:
        return None
    owners: Set[int] = set()
    for entry in sorted(proc_root.iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            handles = list((entry / "fd").iterdir())
        except OSError:
            continue  # not ours to read, or gone since the scan
        for handle in handles:
            try:
                target = os.readlink(handle)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                owners.add(int(entry.name))
    if len(owners) > 1:
        raise OSError("control socket has multiple process owners")
    return next(iter(owners), None)


def control_socket_owner(path: Path, proc_root: Path) -> Optional[int]:
    """Read the listening SEQPACKET inode; never connect or create a path.

    A missing/unreadable/malformed table is uncertainty, not an absent
    backend. An established connection at the same pathname is not the
    listening endpoint. Names may contain spaces, so only the seven fixed
    fields are split before comparing the complete pathname.
    """
    lines = (proc_root / "net" / "unix").read_text().splitlines()
    if not lines or lines[0].split()[:7] != [
            "Num", "RefCount", "Protocol", "Flags", "Type", "St", "Inode"]:
        raise OSError("Unix socket table is missing its header")
    inodes: Set[str] = set()
    for line in lines[1:]:
        fields = line.split(maxsplit=7)
        if len(fields) < 7:
            raise OSError("Unix socket table contains a malformed row")
        if (len(fields) == 8 and fields[7] == str(path)
                and fields[3:6] == ["00010000", "0005", "01"]):
            if not fields[6].isdigit():
                raise OSError("Unix control socket has an invalid inode")
            inodes.add(fields[6])
    if len(inodes) > 1:
        raise OSError("multiple listening control endpoints have the same pathname")
    return _owner_of_sockets(inodes, proc_root)


def control_holder(path: Path, proc_root: Path) -> Optional[BackendOwner]:
    """Associate this endpoint with its exact coordinated backend and bridge."""
    owner = control_socket_owner(path, proc_root)
    if owner is None:
        return None
    entry = proc_root / str(owner)
    args = (entry / "cmdline").read_bytes().split(b"\0")
    if args[-1] == b"":
        args.pop()
    executable = Path(os.readlink(entry / "exe"))
    if not args or executable.name != "oscmix" or Path(os.fsdecode(args[0])).name != "oscmix":
        return BackendOwner(owner, False, None, None)
    try:
        options, rest = getopt.getopt([os.fsdecode(arg) for arg in args[1:]], "dlp:c:")
    except getopt.GetoptError:
        return BackendOwner(owner, False, None, None)
    endpoints = [value for option, value in options if option == "-c"]
    if rest or len(endpoints) != 1 or endpoints[0] != str(path):
        return BackendOwner(owner, False, None, None)
    client = _bridged_client(entry, proc_root)
    return BackendOwner(owner, True, client, _client_serial(client, proc_root))


@dataclass(frozen=True)
class BackendOwner:
    """Who holds the coordinated endpoint, as far as /proc can tell.

    ``oscmix`` requires the executable, command line and endpoint to match.
    ``client`` is the sequencer client its alsaseqio
    parent bridges -- the unit starts ``alsaseqio -x <client>:1 oscmix`` --
    and ``serial`` the number in that client's name. Either is None when
    the chain cannot be followed, which is not the same as a mismatch.
    """

    pid: int
    oscmix: bool
    client: Optional[int]
    serial: Optional[str]


def _bridged_client(entry: Path, proc_root: Path) -> Optional[int]:
    """The sequencer client the alsaseqio beside ``entry`` bridges.

    The unit runs ``alsaseqio -x 24:1 oscmix ...``. alsaseqio forks, the
    original process execs oscmix and binds the port, and the child stays
    alsaseqio with the client in its argv -- measured on the desk, where
    the port holder is the *parent* of the alsaseqio. Children are looked
    at first; a parent that carries the argument is accepted too, for a
    bridge that runs the other way round.
    """
    holder = entry.name
    clients = []
    for candidate in _children_of(holder, proc_root) + _parent_of(entry, proc_root):
        try:
            args = (candidate / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        try:
            executable = Path(os.readlink(candidate / "exe"))
        except OSError:
            continue
        if (executable.name != "alsaseqio" or not args
                or Path(os.fsdecode(args[0])).name != "alsaseqio"
                or len(args) < 4 or args[1] != b"-x"
                or re.fullmatch(rb"\d+:1", args[2]) is None
                or Path(os.fsdecode(args[3])).name != "oscmix"):
            continue
        clients.append(int(args[2].split(b":", 1)[0]))
    return clients[0] if len(clients) == 1 else None


def _children_of(pid: str, proc_root: Path) -> List[Path]:
    children = []
    for candidate in sorted(proc_root.iterdir()):
        if not candidate.name.isdigit():
            continue
        if _ppid(candidate) == pid:
            children.append(candidate)
    return children


def _parent_of(entry: Path, proc_root: Path) -> List[Path]:
    parent = _ppid(entry)
    return [proc_root / parent] if parent is not None else []


def _ppid(entry: Path) -> Optional[str]:
    """Field four of /proc/<pid>/stat, read past the parenthesised comm."""
    try:
        return (entry / "stat").read_text(errors="replace").rsplit(
            ")", 1)[1].split()[1]
    except (OSError, IndexError):
        return None


def _client_serial(client: Optional[int], proc_root: Path) -> Optional[str]:
    if client is None:
        return None
    try:
        text = (proc_root / "asound" / "seq" / "clients").read_text(
            errors="replace")
    except OSError:
        return None
    name = dict(parse_seq_clients(text)).get(client)
    return serial_in(name) if name is not None else None


def _cleanup_stale_backend(path: Path, device: Device,
                            proc_root: Path) -> Optional[int]:
    """Terminate only this user's orphaned, exactly associated control owner.

    The backend itself handles an abandoned socket under its owner lock.
    A live session, another interface or uncertain ownership is a refusal;
    never terminate an old UDP session or make room by killing a stranger.
    """
    try:
        endpoint = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(endpoint.st_mode):
        log.error("control endpoint %s is not a real socket; leaving it alone", path)
        return -1
    try:
        holder = control_holder(path, proc_root)
    except (OSError, ValueError) as exc:
        log.error("cannot identify the control owner at %s: %s", path, exc)
        return -1
    if holder is None:
        return None
    if (not holder.oscmix or holder.client != device.client
            or holder.serial != device.serial
            or holder.pid not in find_stale_backends(proc_root)):
        log.error("control endpoint %s belongs to another backend/user; leaving it alone", path)
        return holder.pid
    session = _supervising_session(proc_root / str(holder.pid), proc_root)
    if session is not None:
        log.error("backend pid %d belongs to running oscmix-session pid %d; stop that "
                  "session explicitly before starting another", holder.pid, session)
        return session
    log.warning("terminating orphaned backend pid %d at %s", holder.pid, path)
    if not _terminate(holder.pid, lambda: _still_stale(holder.pid, path, device, proc_root)):
        return holder.pid
    time.sleep(STALE_BACKEND_SETTLE)
    return None


def _supervising_session(entry: Path, proc_root: Path) -> Optional[int]:
    """The pid of the live oscmix-session that spawned ``entry``, or None.

    The session runs ``alsaseqio -x <client>:1 oscmix``; alsaseqio forks
    and execs oscmix in the original process, so the backend's parent is
    the session itself. A parent that is gone -- the backend reparented
    to init or a subreaper -- or that is not an oscmix-session leaves
    the backend stale.
    """
    parent = _ppid(entry)
    if parent is None or parent == "1":
        return None
    try:
        argv = (proc_root / parent / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    names = [os.path.basename(arg.decode(errors="replace")) for arg in argv[:2]]
    return int(parent) if "oscmix-session" in names else None


def _still_stale(pid: int, path: Path, device: Device, proc_root: Path) -> bool:
    """Recheck all association facts after a pidfd pins the process."""
    try:
        holder = control_holder(path, proc_root)
    except (OSError, ValueError):
        return False
    return (holder is not None and holder.pid == pid and holder.oscmix
            and holder.client == device.client and holder.serial == device.serial
            and pid in find_stale_backends(proc_root)
            and _supervising_session(proc_root / str(pid), proc_root) is None)


def _terminate(pid: int, still_stale: Callable[[], bool]) -> bool:
    """SIGTERM to a process that was identified a moment ago, by pidfd.

    Between scanning /proc and signalling, that process may exit and the
    kernel may hand its number to something else. Revalidate the holder,
    user, executable and absence of a supervising session AFTER opening
    the pidfd: opening it after a /proc scan alone still leaves a reuse
    window. From then on the fd pins that process; signalling a dead
    one fails instead of hitting its successor.

    True when it was signalled or is already gone. False when no pidfd
    can be had -- os.pidfd_open is Linux-only and a seccomp policy can
    block it -- and then nothing is signalled. Until 0.7.0 that case fell
    back to os.kill, which is the race this function exists to avoid, and
    it took the fallback for a process that had merely exited as well
   .
    """
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True                 # gone already: the ordinary race
    except (AttributeError, OSError) as exc:
        log.error("the stale oscmix (pid %d) was not signalled: this system "
                  "gives no pidfd (%s), and a plain kill could hit whatever "
                  "has that number by now -- stop it by hand", pid, exc)
        return False
    try:
        if not still_stale():
            log.error("pid %d no longer identifies the stale backend; "
                      "nothing was signalled", pid)
            return False
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    except ProcessLookupError:
        pass                        # exited between the two calls
    except (AttributeError, OSError) as exc:
        log.error("the stale oscmix (pid %d) was not signalled (%s) -- stop "
                  "it by hand", pid, exc)
        return False
    finally:
        os.close(fd)
    return True


def supervise(child: "subprocess.Popen[bytes]",
              stop_requested: Dict[str, bool],
              on_reload: Optional[Callable[[], None]] = None,
              reload_requested: Optional[Dict[str, bool]] = None) -> int:
    """Wait for the child; escalate SIGTERM -> SIGKILL on shutdown.

    Also the one place a reconcile can safely run. ``reload_requested``
    is set by the SIGHUP handler, which does nothing else -- a signal
    handler runs between two bytecodes of whatever was executing, so the
    work has to happen somewhere nothing is half-done, and this loop is
    that place.

    The flag is cleared *before* the callback runs, so a SIGHUP arriving
    during a reconcile queues one more rather than being swallowed by
    it: holding a key down should not lose the last request.
    """
    kill_deadline: Optional[float] = None
    while True:
        try:
            return child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        if (reload_requested is not None and reload_requested["reload"]
                and on_reload is not None and not stop_requested["stop"]):
            reload_requested["reload"] = False
            on_reload()
        if stop_requested["stop"]:
            now = time.monotonic()
            if kill_deadline is None:
                kill_deadline = now + CHILD_STOP_GRACE
            elif now >= kill_deadline:
                log.warning("backend ignored SIGTERM; sending SIGKILL")
                child.kill()
                return child.wait()


def _systemctl(*verb: str) -> int:
    """``systemctl --user <verb...>``: its exit status, or 1 without systemctl.

    One of the two places this package runs systemctl outside the
    launcher (``_systemctl_output`` is the other), so a single autouse
    fixture in the tests can stub both. The
    integration suite once started the developer's own oscmix.service
    through a launcher that reached the real binary (0.6.1); nothing
    in-process may be able to do the same.
    """
    try:
        return subprocess.run(["systemctl", "--user", *verb], check=False,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    except OSError:
        return 1


@dataclass(frozen=True)
class UnitProcess:
    """What the unit's main process was started with (0.6.10)."""

    argv: Tuple[str, ...]
    environ: Dict[str, str]
    cwd: Path


def unit_process(proc_root: Path) -> Optional[UnitProcess]:
    """The unit's main process as it was started, or None.

    A switch reloads the unit only when it changed the unit's own desk,
    and the unit's desk is what the unit itself resolved: `--config` on
    its command line, else `discover_config_path` in *its* environment,
    a relative path against *its* working directory -- none of which is
    always the shell's that runs the switch (0.6.10). Read from
    ``/proc/<MainPID>/``, which is exactly that: ``systemctl show -p
    Environment`` lists only the unit file's own lines, not the
    manager's XDG_CONFIG_HOME or HOME, and quotes values with spaces.
    None when the unit is not running, when it has exited but is not
    reaped yet (its files read empty), or when the read fails.
    """
    try:
        host = hostservice.registered()
        shown: Optional[str]
        if host:
            shown = str(hostservice.main_pid(host, proc_root) or 0)
        else:
            shown = _systemctl_output("show", "-p", "MainPID", "--value", SERVICE_UNIT)
    except OSError:
        return None
    pid = shown.strip() if shown is not None else ""
    if not pid.isdigit() or pid == "0":
        return None
    entry = proc_root / pid
    try:
        argv = entry.joinpath("cmdline").read_bytes()
        environ = entry.joinpath("environ").read_bytes()
        cwd = os.readlink(entry / "cwd")
    except OSError:
        return None
    if not argv or not environ:
        return None
    pairs = (item.split(b"=", 1) for item in environ.split(b"\0") if item)
    # Each argument ends in NUL, so only the last one is a terminator: an
    # empty final argument (`--device ''`) is the byte before it.
    return UnitProcess(
        argv=tuple(os.fsdecode(arg) for arg in argv.split(b"\0")[:-1]),
        environ={os.fsdecode(name): os.fsdecode(value)
                 for name, value in (pair for pair in pairs if len(pair) == 2)},
        cwd=Path(cwd))


def _systemctl_output(*verb: str) -> Optional[str]:
    """``systemctl --user <verb...>``'s stdout, or None on any failure.

    The second of the two places this package runs systemctl (see
    ``_systemctl``), stubbed by the same test fixture.
    """
    try:
        result = subprocess.run(["systemctl", "--user", *verb], check=False,
                                capture_output=True, text=True)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


#: What a reload attempt did. "not running" is not a failure: there is
#: no verifier that could revert the switch. A refused reload is one,
#: because the unit is running on a desk it has not re-read (0.6.6).
RELOAD_NOT_RUNNING = "not running"
RELOAD_DONE = "reloaded"
RELOAD_FAILED = "failed"


def reload_service() -> str:
    """Ask the running unit to reconcile now, and say what came of it.

    One of RELOAD_NOT_RUNNING, RELOAD_DONE or RELOAD_FAILED. The middle
    of those three is not a failure and the last one is: a unit that is
    up and refused the reload is acting on a desk it has not re-read.

    A profile switch writes the device from a second process. For up to
    about 22 s after a start the unit's own verifier is still re-applying
    the config it started with, and it overwrote the switch: measured on
    the desk, a switch sent right after a restart read back at the old
    fader value fifteen seconds later. The reload makes the unit re-read
    the desk in effect -- the new profile (ADR 0018) -- and its
    reconcile is serialised behind the verifier (ADR 0013), so whichever
    of the two writes last, it is the profile.
    """
    try:
        host = hostservice.registered()
        if host:
            return hostservice.reload(host, Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc")))
    except OSError:
        return RELOAD_FAILED
    if _systemctl("is-active", "--quiet", SERVICE_UNIT) != 0:
        return RELOAD_NOT_RUNNING
    if _systemctl("reload", SERVICE_UNIT) != 0:
        return RELOAD_FAILED
    return RELOAD_DONE
