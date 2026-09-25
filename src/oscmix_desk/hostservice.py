"""Identity and bounded control of an explicitly registered OpenRC/runit desk.

Systemd user units keep their native interface. These two system supervisors
are selected by a root-owned registration, never by guessing the distribution.
Their mixer process still belongs to the ordinary audio user.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

REGISTRATION = Path("/etc/oscmix-desk/service.json")
STATE = Path("/var/lib/oscmix-desk")
MAINTENANCE_FILES = (("service", "service-update"), ("core", "package-update"),
                     ("gtk", "gtk-package-update"))
SUPERVISOR_PID = Path("/run/oscmix-desk-supervisor.pid")
RUNIT_SERVICE = Path("/var/service/oscmix-desk")
OPENRC_ENABLED = Path("/etc/runlevels/default/oscmix-desk")


@dataclass(frozen=True)
class HostService:
    manager: str
    user: str
    uid: int
    home: Path
    config: Path
    command: Path


def _root_file(path: Path) -> str:
    """Reject user-written or redirected manager identity files."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise OSError("service identity is not a root-owned regular file: " + str(path))
    for parent in path.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise OSError("service identity has an untrusted parent: " + str(parent))
    return path.read_text()


def registered() -> Optional[HostService]:
    try:
        data = json.loads(_root_file(REGISTRATION))
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise OSError("invalid host service registration") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise OSError("unsupported host service registration")
    if data.get("installed") is not True:
        raise OSError("host service registration is incomplete; rerun its installer")
    for key in ("manager", "user", "home", "config", "command"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise OSError("incomplete host service registration: " + key)
    if (data["manager"] not in ("openrc", "runit") or type(data.get("uid")) is not int
            or data["uid"] <= 0 or any(not Path(data[key]).is_absolute()
                                      for key in ("home", "config", "command"))):
        raise OSError("invalid host service manager/user/paths")
    return HostService(data["manager"], data["user"], data["uid"], Path(data["home"]),
                       Path(data["config"]), Path(data["command"]))


def _matches(pid: int, service: HostService, proc_root: Path) -> bool:
    entry = proc_root / str(pid)
    try:
        if entry.stat().st_uid != service.uid:
            return False
        argv = entry.joinpath("cmdline").read_bytes().split(b"\0")[:-1]
        expected = [os.fsencode(service.command), b"--config", os.fsencode(service.config)]
        if argv != expected and not (len(argv) == 4 and argv[1:] == expected
                                     and Path(os.fsdecode(argv[0])).name.startswith("python3")):
            return False
        environment = entry.joinpath("environ").read_bytes().split(b"\0")
        return (b"OSCMIX_SERVICE_MANAGER=" + service.manager.encode() in environment
                and b"HOME=" + os.fsencode(service.home) in environment)
    except OSError:
        return False


def main_pid(service: HostService, proc_root: Path) -> Optional[int]:
    """Resolve the native manager's child, then verify its user and exact command."""
    if service.uid != os.getuid():
        return None
    if service.manager == "openrc":
        try:
            supervisor = _root_file(SUPERVISOR_PID).strip()
        except FileNotFoundError:
            return None
        if not supervisor.isdigit() or int(supervisor) <= 1:
            raise OSError("invalid OpenRC supervisor PID")
        parent = proc_root / supervisor
        try:
            # /proc/<root supervisor>/exe is deliberately unreadable to an
            # ordinary user. Its root-owned PID file and root-only command
            # identity are readable without granting privilege to the desk.
            argv = (parent / "cmdline").read_bytes().split(b"\0")[:-1]
            pairs = [argv[index:index + 2] for index in range(len(argv) - 1)]
            if (parent.stat().st_uid != 0
                    or len(argv) < 3 or Path(os.fsdecode(argv[0])).name != "supervise-daemon"
                    or argv[1:3] != [b"oscmix-desk", b"--start"]
                    or [b"--pidfile", os.fsencode(SUPERVISOR_PID)] not in pairs
                    or [b"--user", service.user.encode()] not in pairs):
                raise OSError("OpenRC supervisor identity changed")
            children = (parent / "task" / supervisor / "children").read_text().split()
        except FileNotFoundError:
            return None
        candidates = [int(pid) for pid in children if pid.isdigit()]
    else:
        if not RUNIT_SERVICE.exists():
            return None
        try:
            shown = _root_file(RUNIT_SERVICE / "supervise/pid").strip()
        except FileNotFoundError:
            return None
        if not shown:
            return None
        if not shown.isdigit() or int(shown) <= 1:
            raise OSError("invalid runit child PID")
        pid = int(shown)
        if not _matches(pid, service, proc_root):
            return None
        try:
            state = (proc_root / shown / "status").read_text()
            match = re.search(r"^PPid:\s+([1-9][0-9]*)$", state, re.MULTILINE)
            if match is None:
                raise OSError("runit child has no identifiable parent")
            parent = proc_root / match[1]
            argv = (parent / "cmdline").read_bytes().split(b"\0")[:-1]
            names = (b"oscmix-desk", os.fsencode(RUNIT_SERVICE),
                     os.fsencode(RUNIT_SERVICE.resolve()))
            if (parent.stat().st_uid != 0 or len(argv) != 2
                    or Path(os.fsdecode(argv[0])).name != "runsv" or argv[1] not in names):
                raise OSError("runit supervisor identity changed")
        except FileNotFoundError:
            return None
        return pid
    matches = [pid for pid in candidates if _matches(pid, service, proc_root)]
    if len(matches) > 1:
        raise OSError("host supervisor has multiple matching desk processes")
    return matches[0] if matches else None


def reload(service: HostService, proc_root: Path) -> str:
    """Signal only the identified unprivileged desk supervisor, pinned by pidfd."""
    pid = main_pid(service, proc_root)
    if pid is None:
        return "not running"
    fd = None
    try:
        fd = os.pidfd_open(pid)
        if main_pid(service, proc_root) != pid or not _matches(pid, service, proc_root):
            return "failed"
        signal.pidfd_send_signal(fd, signal.SIGHUP)
    except (OSError, AttributeError):
        return "failed"
    finally:
        if fd is not None:
            os.close(fd)
    return "reloaded"


def report(service: HostService, proc_root: Path) -> Dict[str, str]:
    if service.uid != os.getuid():
        return {"state": "unavailable", "manager": service.manager,
                "detail": "the registered host desk belongs to another user"}
    pid = main_pid(service, proc_root)
    enabled = OPENRC_ENABLED if service.manager == "openrc" else RUNIT_SERVICE
    allowed = (STATE / "service-allowed").is_file()
    maintenance = (STATE / "service-update").exists()
    return {"state": "observed", "manager": service.manager, "LoadState": "loaded",
            "ActiveState": "active" if pid else "inactive",
            "UnitFileState": "enabled" if enabled.exists() and allowed else "disabled",
            "MainPID": str(pid or 0), "FragmentPath": str(REGISTRATION),
            "ConfiguredHome": str(service.home), "Configuration": str(service.config),
            "UserID": str(service.uid), "Maintenance": str(maintenance).lower(),
            "StatusText": ("maintenance incomplete" if maintenance else
                           "process inspection; no hardware verification")}


def maintenance_problem() -> Optional[str]:
    """Persistent fences block activation through both services and manual clients."""
    for _, name in MAINTENANCE_FILES:
        try:
            (STATE / name).stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return "cannot establish installation maintenance state: " + str(exc)
        return "installation maintenance incomplete: " + str(STATE / name)
    return None
