"""Read-only process, port and service inspection shared by CLI and launcher."""

from __future__ import annotations

import getopt
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

from .constants import SERVICE_UNIT
from .discovery import Device, resolve_device, udp_port_listening
from .errors import DeviceAmbiguous
from .model import Config
from .paths import discover_config_path
from .process import port_holder


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


def port_state(port: int, proc_root: Path) -> str:
    """Free/occupied, or an exception when procfs cannot establish either.

    The existing socket helpers tolerate unavailable tables for discovery.
    Diagnosis must not translate a denied read into an available port.
    """
    for name in ("udp", "udp6"):
        try:
            lines = (proc_root / "net" / name).read_text().splitlines()
        except FileNotFoundError:
            if name == "udp6":
                continue
            raise
        if not lines or "local_address" not in lines[0]:
            raise OSError("UDP table %s is missing its header" % name)
        for line in lines[1:]:
            fields = line.split()
            if (len(fields) < 10 or
                    re.fullmatch(r"[0-9A-Fa-f]+:[0-9A-Fa-f]{4}", fields[1]) is None):
                raise OSError("UDP table %s contains a malformed socket row" % name)
    return "occupied" if udp_port_listening(port, proc_root) else "free"


@dataclass(frozen=True)
class BackendStatus:
    """A momentary association, never a lease on a process or device."""

    state: str
    detail: str
    device: Optional[Device] = None
    pid: Optional[int] = None


def backend_status(config: Config, proc_root: Path) -> BackendStatus:
    """Use the same device and socket-owner helpers as the write paths."""
    try:
        device = resolve_device(config.usb_id, config.device_name, config.serial, proc_root)
        bound = port_state(config.osc_port, proc_root)
        if bound == "free":
            return BackendStatus("absent", "no backend on UDP %d" % config.osc_port, device)
        holder = port_holder(config.osc_port, proc_root)
    except (OSError, ValueError, DeviceAmbiguous) as exc:
        return BackendStatus("unknown", str(exc))
    if holder is None:
        return BackendStatus("unknown", "OSC port occupied; owner cannot be identified", device)
    if not holder.oscmix:
        return BackendStatus("conflict", "OSC port belongs to another program/user",
                             device, holder.pid)
    if device.client is None or holder.client is None:
        return BackendStatus("unknown", "cannot associate backend with an ALSA client",
                             device, holder.pid)
    if holder.client != device.client or (device.serial and holder.serial != device.serial):
        return BackendStatus("conflict", "backend drives another interface", device, holder.pid)
    try:
        reply = _reply_destination(holder.pid, proc_root)
    except (OSError, ValueError) as exc:
        return BackendStatus("unknown", str(exc), device, holder.pid)
    if reply != "udp!127.0.0.1!%d" % config.osc_recv_port:
        return BackendStatus("conflict", "backend reply destination differs from the desk: "
                             + reply, device, holder.pid)
    return BackendStatus("ready", "backend belongs to the selected interface", device, holder.pid)


def _reply_destination(pid: int, proc_root: Path) -> str:
    """Pinned main.c accepts -s and -m in order; no flag means loopback 8222."""
    argv = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    if not argv[0]:
        raise ValueError("backend command line is unavailable")
    try:
        if argv[-1] == b"":
            argv.pop()
        options, extra = getopt.getopt(_socket_arguments(argv[1:]), "dlr:s:mp:")
    except getopt.GetoptError as exc:
        raise ValueError("cannot determine backend reply destination: %s" % exc) from exc
    if extra:
        raise ValueError("unexpected backend command arguments")
    reply = "udp!127.0.0.1!8222"
    for key, value in options:
        if key == "-s":
            reply = value
        elif key == "-m":
            reply = "udp!224.0.0.1!8222"
    return reply


def _socket_arguments(arguments: Sequence[bytes]) -> Sequence[str]:
    """Undo pinned socket.c's in-place replacement of '!' by NUL in argv.

    /proc/cmdline observes the modified memory, so a live '-s udp!host!port'
    appears as four fields. Preserve order, including repeated -s and -m.
    Only the known three-part UDP forms are reconstructed.
    """
    words = [os.fsdecode(arg) for arg in arguments]
    result = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in ("-r", "-s") and index + 3 < len(words) and words[index + 1] == "udp":
            result.extend([word, "!".join(words[index + 1:index + 4])])
            index += 4
        elif word in ("-rudp", "-sudp") and index + 2 < len(words):
            result.append("!".join(words[index:index + 3]))
            index += 3
        else:
            result.append(word)
            index += 1
    return result


def service_status() -> Dict[str, str]:
    """Read systemd state without loading, enabling or starting the service."""
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
