"""Backend process supervision: stale cleanup and stop escalation."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .constants import CHILD_STOP_GRACE, SERVICE_UNIT, STALE_BACKEND_SETTLE
from .discovery import (
    parse_seq_clients,
    serial_in,
    udp_port_listening,
    udp_socket_inodes,
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


def socket_owner(port: int, proc_root: Path) -> Optional[int]:
    """The PID holding the UDP socket on ``port``, when it can be told.

    Name and user are not ownership. Until 0.6.6 the cleanup terminated
    every `oscmix` of this user as soon as *anything* held the port, so
    a second interface's backend on another port could be killed by a
    start whose port was taken by something else entirely.

    The socket inode is the link: `/proc/net/udp` gives it for the bound
    port, and `/proc/<pid>/fd/*` points at `socket:[<inode>]`. None
    means nobody could be shown to hold it, and the caller then leaves
    every process alone.
    """
    inodes = udp_socket_inodes(port, proc_root)
    if not inodes:
        return None
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
                return int(entry.name)
    return None


@dataclass(frozen=True)
class PortHolder:
    """Who holds a UDP port, as far as /proc can tell.

    ``oscmix`` is whether it is an oscmix backend of this user (any
    user's, for root). ``client`` is the sequencer client its alsaseqio
    parent bridges -- the unit starts ``alsaseqio <client>:1 oscmix`` --
    and ``serial`` the number in that client's name. Either is None when
    the chain cannot be followed, which is not the same as a mismatch.
    """

    pid: int
    oscmix: bool
    client: Optional[int]
    serial: Optional[str]


def port_holder(port: int, proc_root: Path) -> Optional[PortHolder]:
    """The process holding ``port`` and the interface it drives, or None.

    "Something is bound" is not "the backend for this box is bound". A
    profile switch that asked only the first question wrote a whole
    routing into a stranger's socket, reported `applied` and recorded
    the marker -- measured against a plain Python socket in 0.6.8
    (ADR 0024). The start already knew better: it accepts the port only
    from the child it spawned.
    """
    owner = socket_owner(port, proc_root)
    if owner is None:
        return None
    entry = proc_root / str(owner)
    client = _bridged_client(entry, proc_root)
    return PortHolder(pid=owner, oscmix=_is_oscmix_of_this_user(entry),
                      client=client, serial=_client_serial(client, proc_root))


def _is_oscmix_of_this_user(entry: Path) -> bool:
    uid = os.getuid()
    try:
        if uid != 0 and entry.stat().st_uid != uid:
            return False
        # errors="replace": the kernel cuts comm at 15 bytes, which can
        # split a multibyte character, and any process on the machine is
        # read here -- a strict decode raised out of every switch (0.6.9).
        comm = (entry / "comm").read_text(errors="replace").strip()
        argv0 = (entry / "cmdline").read_bytes().split(b"\0")[0]
    except OSError:
        return False
    return "oscmix" in (comm, os.path.basename(argv0.decode(errors="replace")))


#: ``alsaseqio 24:1 oscmix ...``: client and port of the bridged device.
_BRIDGE_ARG = re.compile(rb"(\d+):\d+")


def _bridged_client(entry: Path, proc_root: Path) -> Optional[int]:
    """The sequencer client the alsaseqio beside ``entry`` bridges.

    The unit runs ``alsaseqio 24:1 oscmix ...``. alsaseqio forks, the
    original process execs oscmix and binds the port, and the child stays
    alsaseqio with the client in its argv -- measured on the desk, where
    the port holder is the *parent* of the alsaseqio. Children are looked
    at first; a parent that carries the argument is accepted too, for a
    bridge that runs the other way round.
    """
    holder = entry.name
    for candidate in _children_of(holder, proc_root) + _parent_of(entry, proc_root):
        try:
            args = (candidate / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for arg in args[1:]:
            match = _BRIDGE_ARG.fullmatch(arg)
            if match:
                return int(match.group(1))
    return None


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


def _cleanup_stale_backend(port: int, proc_root: Path) -> Optional[int]:
    """A stale oscmix (e.g. from a manual run) would hold the OSC port.

    Only the process that demonstrably holds the port is terminated,
    and only when it is an oscmix of this user *whose session is gone*.
    A backend whose parent is a live oscmix-session is not stale, it is
    someone's running desk: until 0.6.10 a second session started by hand
    terminated the unit's backend, the unit restarted and terminated the
    manual one in turn (measured). The pid of that live session is
    returned instead, and the caller refuses to start. Anything else
    keeps the port and the start fails on the port wait, which is the
    honest outcome: this project does not get to kill a stranger's
    process to make room for itself.
    """
    if not udp_port_listening(port, proc_root):
        return None
    owner = socket_owner(port, proc_root)
    if owner is None:
        log.warning("UDP port %d is in use and its owner cannot be "
                    "identified; leaving every process alone", port)
        return None
    if owner not in find_stale_backends(proc_root):
        log.warning("UDP port %d is held by pid %d, which is not an oscmix "
                    "of this user; leaving it alone", port, owner)
        return None
    session = _supervising_session(proc_root / str(owner), proc_root)
    if session is not None:
        log.error("UDP port %d is held by the backend (pid %d) of a running "
                  "oscmix-session (pid %d); stop that session first -- "
                  "systemctl --user stop oscmix.service if it is the unit",
                  port, owner, session)
        return session
    log.warning("UDP port %d already in use; terminating the stale oscmix "
                "that holds it (pid %d)", port, owner)
    if not _terminate(owner, lambda: _still_stale(owner, port, proc_root)):
        # Not signalled, so the port stays held and a backend started now
        # could not bind it. Exit 2 like a session in the way: a restart
        # cannot change what this machine lets a process do.
        return owner
    # Part of the startup budget; see constants.startup_budget.
    time.sleep(STALE_BACKEND_SETTLE)
    return None


def _supervising_session(entry: Path, proc_root: Path) -> Optional[int]:
    """The pid of the live oscmix-session that spawned ``entry``, or None.

    The session runs ``alsaseqio <client>:1 oscmix``; alsaseqio forks
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


def _still_stale(pid: int, port: int, proc_root: Path) -> bool:
    """Recheck the target after a pidfd pins its process identity."""
    return (socket_owner(port, proc_root) == pid
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
    (second outside review).
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
    shown = _systemctl_output("show", "-p", "MainPID", "--value", SERVICE_UNIT)
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
    if _systemctl("is-active", "--quiet", SERVICE_UNIT) != 0:
        return RELOAD_NOT_RUNNING
    if _systemctl("reload", SERVICE_UNIT) != 0:
        return RELOAD_FAILED
    return RELOAD_DONE
