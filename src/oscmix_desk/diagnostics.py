"""Read-only process, port and service inspection shared by CLI and launcher."""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

from . import hostservice
from .constants import SERVICE_UNIT
from .discovery import Device, resolve_device
from .errors import DeviceAmbiguous
from .locking import control_path
from .model import Config
from .paths import discover_config_path
from .process import control_holder


def query(command: Sequence[str]) -> str:
    """Bound a read-only host query; callers supply a fixed executable/action."""
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                errors="replace", timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError("%s: %s" % (command[0], exc)) from exc
    if result.returncode:
        raise OSError(result.stderr.strip() or "%s exited %d" % (
            command[0], result.returncode))
    return result.stdout.strip()


@dataclass(frozen=True)
class BackendStatus:
    """A momentary association, never a lease on a process or device."""

    state: str
    detail: str
    device: Optional[Device] = None
    pid: Optional[int] = None
    endpoint: Optional[str] = None
    uid: Optional[int] = None
    gid: Optional[int] = None


def backend_status(config: Config, proc_root: Path,
                   config_path: Optional[Path] = None) -> BackendStatus:
    """Inspect the exact coordinated endpoint without connecting to it."""
    try:
        device = resolve_device(config.usb_id, config.device_name, config.serial, proc_root)
        path = control_path(config_path, device.key)
        try:
            endpoint = path.lstat()
        except FileNotFoundError:
            return BackendStatus("absent", "no coordinated backend endpoint", device,
                                 endpoint=str(path))
        if not stat.S_ISSOCK(endpoint.st_mode):
            return BackendStatus("conflict", "control endpoint is not a real socket", device,
                                 endpoint=str(path))
        holder = control_holder(path, proc_root)
    except (OSError, ValueError, DeviceAmbiguous) as exc:
        return BackendStatus("unknown", str(exc))
    if holder is None:
        return BackendStatus("unknown", "control endpoint has no identified listening owner",
                             device, endpoint=str(path))
    if not holder.oscmix:
        return BackendStatus("conflict", "endpoint belongs to an incompatible program",
                             device, holder.pid, str(path))
    if device.client is None or holder.client is None:
        return BackendStatus("unknown", "cannot establish the exclusive ALSA bridge",
                             device, holder.pid, str(path))
    if not device.serial or not holder.serial:
        return BackendStatus("unknown", "device/backend serial identity is unavailable",
                             device, holder.pid, str(path))
    if holder.client != device.client or holder.serial != device.serial:
        return BackendStatus("conflict", "backend drives another interface", device,
                             holder.pid, str(path))
    try:
        process = (proc_root / str(holder.pid)).stat()
        if endpoint.st_uid != process.st_uid:
            return BackendStatus("conflict", "endpoint and backend have different owners",
                                 device, holder.pid, str(path))
    except OSError as exc:
        return BackendStatus("unknown", str(exc), device, holder.pid, str(path))
    return BackendStatus("ready", "coordinated backend belongs to the selected interface",
                         device, holder.pid, str(path), process.st_uid, process.st_gid)


def service_status() -> Dict[str, str]:
    """Read the selected manager without enabling or starting the service."""
    try:
        host = hostservice.registered()
        if host:
            return hostservice.report(host, Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc")))
    except OSError as exc:
        return {"state": "unavailable", "detail": str(exc)}
    properties = ("LoadState", "ActiveState", "UnitFileState", "MainPID",
                  "StatusText", "FragmentPath", "ExecStart", "Environment",
                  "EnvironmentFiles", "UnsetEnvironment")
    try:
        text = query(["systemctl", "--user", "show", "--no-pager",
                      "--property=" + ",".join(properties), SERVICE_UNIT])
    except OSError as exc:
        return {"state": "unavailable", "detail": str(exc)}
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if "LoadState" not in values or "ActiveState" not in values:
        return {"state": "unknown", "detail": "incomplete systemd service report"}
    return {"state": "observed", **values}


def service_start_problem(config_path: Optional[Path], service: Dict[str, str]) -> Optional[str]:
    """Only launch an enabled default session belonging to this desk.

    Custom commands/environment files need an explicit manual start. A
    desktop click must not accidentally apply another session's config.
    """
    if service.get("LoadState") != "loaded":
        return "no installed service; start oscmix-session in a terminal for manual operation"
    if service.get("UnitFileState") not in ("enabled", "enabled-runtime"):
        return "service is not enabled; review the desk and explicitly enable it first"
    if service.get("manager") in ("openrc", "runit"):
        if service.get("Maintenance") != "false":
            return "host service maintenance is incomplete; run oscmix-service maintenance-finish"
        if (service.get("UserID") != str(os.getuid())
                or service.get("ConfiguredHome") != os.environ.get("HOME")
                or config_path is None
                or Path(service.get("Configuration", "")).resolve() != config_path.resolve()):
            return "host service and launcher belong to different user/configuration paths"
        if service.get("ActiveState") != "active":
            return ("no matching host desk process is running; inspect oscmix-service status "
                    "or wait for its supervised restart")
        return None
    if service.get("EnvironmentFiles"):
        return "service uses environment files; start the configured session explicitly"
    if service.get("UnsetEnvironment"):
        return "service unsets environment variables; start the configured session explicitly"
    match = re.search(r"argv\[\]=(.*?) ; ignore_errors=", service.get("ExecStart", ""))
    try:
        argv = shlex.split(match[1]) if match else []
        if len(argv) != 1 or Path(argv[0]).name != "oscmix-session":
            return "custom or unknown service command; start that session explicitly"
        environment = dict(line.split("=", 1) for line in
                           query(["systemctl", "--user", "show-environment"]).splitlines()
                           if "=" in line)
        for item in shlex.split(service.get("Environment", "")):
            if "=" in item:
                key, value = item.split("=", 1)
                environment[key] = value
        theirs = discover_config_path(environment)
        if (environment.get("HOME") != os.environ.get("HOME") or
                (theirs.resolve() if theirs else None) !=
                (config_path.resolve() if config_path else None)):
            return "service and launcher resolve different user/configuration paths"
    except (OSError, ValueError) as exc:
        return "cannot establish service configuration: %s" % exc
    return None
