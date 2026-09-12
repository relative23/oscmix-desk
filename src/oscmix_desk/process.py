"""Backend process supervision: stale cleanup and stop escalation."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .constants import CHILD_STOP_GRACE, SERVICE_UNIT, STALE_BACKEND_SETTLE
from .discovery import udp_port_listening
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


def _cleanup_stale_backend(port: int, proc_root: Path) -> None:
    """A stale oscmix (e.g. from a manual run) would hold the OSC port."""
    if not udp_port_listening(port, proc_root):
        return
    pids = find_stale_backends(proc_root)
    if not pids:
        log.warning("UDP port %d is in use by an unknown process; "
                    "oscmix may fail to bind it", port)
        return
    log.warning("UDP port %d already in use; terminating stale oscmix (pid %s)",
                port, ", ".join(map(str, pids)))
    for pid in pids:
        _terminate(pid)
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


def reload_service() -> bool:
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
        return False
    return _systemctl("reload", SERVICE_UNIT) == 0
