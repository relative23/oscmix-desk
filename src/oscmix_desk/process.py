"""Backend process supervision: stale cleanup and stop escalation."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .constants import CHILD_STOP_GRACE, SERVICE_UNIT, STALE_BACKEND_SETTLE
from .discovery import udp_port_listening, udp_socket_inodes
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
            comm = (entry / "comm").read_text().strip()
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


def _cleanup_stale_backend(port: int, proc_root: Path) -> None:
    """A stale oscmix (e.g. from a manual run) would hold the OSC port.

    Only the process that demonstrably holds the port is terminated,
    and only when it is an oscmix of this user. Anything else keeps the
    port and the start fails on the port wait, which is the honest
    outcome: this project does not get to kill a stranger's process to
    make room for itself.
    """
    if not udp_port_listening(port, proc_root):
        return
    owner = socket_owner(port, proc_root)
    if owner is None:
        log.warning("UDP port %d is in use and its owner cannot be "
                    "identified; leaving every process alone", port)
        return
    if owner not in find_stale_backends(proc_root):
        log.warning("UDP port %d is held by pid %d, which is not an oscmix "
                    "of this user; leaving it alone", port, owner)
        return
    log.warning("UDP port %d already in use; terminating the stale oscmix "
                "that holds it (pid %d)", port, owner)
    _terminate(owner)
    # Part of the startup budget; see constants.startup_budget.
    time.sleep(STALE_BACKEND_SETTLE)


def _terminate(pid: int) -> None:
    """Send SIGTERM to a PID that was identified a moment ago.

    Between scanning /proc and signalling, that process may exit and the
    kernel may hand its number to something else; a plain os.kill would
    then signal a stranger. A pidfd refers to the process itself rather
    than to the number, so the race cannot be lost -- signalling a dead
    one fails instead of hitting its successor.

    os.pidfd_open landed in 3.9, the oldest interpreter supported here,
    but it is Linux-only and can be blocked by a seccomp policy, so a
    failure to obtain one falls back rather than skipping the cleanup.
    """
    try:
        fd = os.pidfd_open(pid)
    except (AttributeError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    except (AttributeError, OSError):
        pass
    finally:
        os.close(fd)


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

    The one place this package runs systemctl outside the launcher, so
    a single autouse fixture in the tests can stub the whole of it. The
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


#: What a reload attempt did. "not running" is not a failure: there is
#: no verifier that could revert the switch. A refused reload is one,
#: because the unit is running on a desk it has not re-read (0.6.6).
RELOAD_NOT_RUNNING = "not running"
RELOAD_DONE = "reloaded"
RELOAD_FAILED = "failed"


def reload_service() -> str:
    """Ask the running unit to reconcile now. False when it is not running.

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
