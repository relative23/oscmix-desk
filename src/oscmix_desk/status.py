"""Read-only runtime diagnosis; metadata and liveness never imply verification."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import DEFAULT_USB_ID, EXIT_CONFIG, EXIT_OK, __version__
from .desktop import inspect_desktop
from .diagnostics import backend_status, port_state, service_status
from .discovery import resolve_binary
from .errors import ConfigError
from .marker import active_profile
from .model import CommandLine, Config
from .paths import profile_path
from .process import socket_owner
from .profiles import effective_config
from .streams import playback_problem, read_playback

Info = Dict[str, object]
MAINTENANCE_DIR = Path("/var/lib/oscmix-desk")


def _configuration(path: Optional[Path], said: CommandLine) -> Tuple[Info, Optional[Config]]:
    info: Info = {"path": str(path) if path else None}
    try:
        stored = active_profile(path)
        config, active = effective_config(path, said)
    except (ConfigError, OSError, ValueError) as exc:
        info.update(state="invalid", detail=str(exc))
        return info, None
    source = profile_path(active, path) if active else path
    info.update(state="fallback" if stored != active else "loaded",
                selected_file=str(source) if source else None,
                stored_profile=stored, effective_profile=active,
                device_name=config.device_name, usb_id=config.usb_id,
                configured_serial=config.serial or None,
                send_port=config.osc_port, receive_port=config.osc_recv_port)
    if stored != active:
        info["detail"] = "stored profile could not be loaded; main configuration selected"
    return info, config


def _installation() -> Info:
    binary = resolve_binary("oscmix", "OSCMIX_BIN_BACKEND")
    info: Info = {"runtime": str(Path(__file__).resolve().parent),
                  "entry_point": sys.argv[0], "python": sys.version.split()[0],
                  "resolved_backend": binary, "backend_sha256": None,
                  "package_metadata": None, "maintenance": _maintenance()}
    if binary:
        try:
            info["backend_sha256"] = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
            metadata = Path(binary).resolve().parent.parent / "share/oscmix-desk/package.json"
            if metadata.is_file():
                data = json.loads(metadata.read_text())
                if isinstance(data, dict):
                    info["package_metadata"] = {
                        key: data[key] if isinstance(data.get(key), str) else None
                        for key in ("version", "source_commit", "backend_commit")}
                    info["metadata_file"] = str(metadata)
                else:
                    info["detail"] = "package metadata must be an object"
        except (OSError, ValueError) as exc:
            info["detail"] = str(exc)
    info["provenance_note"] = (
        "resolved file/metadata; not proof of the running binary or its tests")
    return info


def _maintenance() -> Info:
    info: Info = {}
    for component, filename in (("core", "package-update"), ("gtk", "gtk-package-update")):
        try:
            (MAINTENANCE_DIR / filename).stat()
        except FileNotFoundError:
            info[component] = {"pending": False}
        except OSError as exc:
            info[component] = {"pending": None, "detail": str(exc)}
        else:
            info[component] = {"pending": True,
                "detail": "repair/reinstall this package before starting the service or mixer"}
    return info


def _running_backend(pid: int, proc_root: Path, resolved: Info) -> Info:
    """Hash the kernel's executable reference, including an unlinked old binary."""
    executable = proc_root / str(pid) / "exe"
    try:
        name = os.readlink(executable)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    except OSError as exc:
        return {"state": "unknown", "detail": str(exc)}
    expected = resolved.get("backend_sha256")
    return {"state": "observed", "executable": name, "sha256": digest,
            "matches_resolved": digest == expected if expected else None,
            "note": "file identity at inspection time, not firmware or verification evidence"}


def _playback(config: Config, proc_root: Path) -> Info:
    if config.usb_id != DEFAULT_USB_ID:
        return {"state": "not-applicable", "detail": "mode measurements apply to UCX II"}
    try:
        state = read_playback(config, proc_root)
        if state.card is None:
            return {"state": "unobserved", "detail": "no matching USB playback card"}
        if state.mode is None:
            return {"state": "idle", "card": state.card, "serial": state.serial,
                    "detail": "no active PCM; live playback mode is not validated"}
        problem = playback_problem(config, state.mode)
        return {"state": "unsupported" if problem else "observed", "card": state.card,
                "serial": state.serial, **asdict(state.mode), "problem": problem}
    except (OSError, ValueError) as exc:
        return {"state": "unknown", "detail": str(exc)}


def _receiver(config: Config, proc_root: Path) -> Info:
    try:
        state = port_state(config.osc_recv_port, proc_root)
        owner = socket_owner(config.osc_recv_port, proc_root) if state == "occupied" else None
    except (OSError, ValueError) as exc:
        return {"state": "unknown", "port": config.osc_recv_port, "detail": str(exc)}
    else:
        return {"state": state, "port": config.osc_recv_port, "owner_pid": owner,
                "detail": ("close the owning mixer or wait for the current read-back, then retry"
                           if state == "occupied" else "no listener observed; not a reservation")}


def collect_status(path: Optional[Path], said: CommandLine) -> Info:
    """Only inspect files and query host metadata; no OSC or audio operation."""
    configuration, config = _configuration(path, said)
    service = service_status()
    sections: Dict[str, Info] = {"installation": _installation(),
        "configuration": configuration, "service": {
            key: value for key, value in service.items() if key in (
                "state", "detail", "ActiveState", "UnitFileState", "MainPID",
                "StatusText", "FragmentPath")}}
    if config is not None:
        proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
        backend = backend_status(config, proc_root)
        sections["backend"] = asdict(backend)
        if backend.state == "ready" and backend.pid is not None:
            sections["backend"]["running_file"] = _running_backend(
                backend.pid, proc_root, sections["installation"])
        selected = replace(config, serial=backend.device.serial) if backend.device else config
        sections["playback"] = _playback(selected, proc_root)
        sections["receive_port"] = _receiver(config, proc_root)
    else:
        for name in ("backend", "playback", "receive_port"):
            sections[name] = {"state": "unknown", "detail": "fix the invalid configuration first"}
    sections["desktop"] = asdict(inspect_desktop(config))
    return {"schema_version": 1, "desk_version": __version__, "read_only": True,
            "configuration_valid": config is not None,
            "verification": "not-performed", "sections": sections,
            "note": "point-in-time inspection; service readiness and profile selection "
                    "do not confirm hardware state"}


def _text_lines(value: object, indent: str = "") -> List[str]:
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            label = indent + str(key) + ":"
            if isinstance(item, dict):
                lines.append(label)
                lines.extend(_text_lines(item, indent + "  "))
            else:
                lines.append(label + " " + (str(item) if item is not None else "unknown"))
        return lines
    return [indent + str(value)]


def print_status(path: Optional[Path], said: CommandLine, as_json: bool) -> int:
    report = collect_status(path, said)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) if as_json else
          "\n".join(_text_lines(report)))
    return EXIT_OK if report["configuration_valid"] else EXIT_CONFIG
