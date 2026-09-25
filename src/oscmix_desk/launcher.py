"""Open the existing GTK mixer only after checking its target and prerequisites."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .config import load_config
from .constants import SERVICE_UNIT
from .desktop import inspect_desktop
from .diagnostics import BackendStatus, backend_status, service_start_problem, service_status
from .discovery import resolve_binary
from .errors import ConfigError
from .hostservice import maintenance_problem
from .model import Config
from .paths import discover_config_path

BACKEND_WAIT = float(os.environ.get("OSCMIX_BACKEND_WAIT", "5"))
SERVICE = SERVICE_UNIT
SYSTEM_CONFIG = Path("/etc/oscmix/routing.conf")
log = logging.getLogger("oscmix-launch")


def config_file() -> Optional[Path]:
    """Use the session's discovery rule, including its testable system path."""
    return discover_config_path({**os.environ, "OSCMIX_SYSTEM_CONFIG":
        os.environ.get("OSCMIX_SYSTEM_CONFIG") or str(SYSTEM_CONFIG)})


def notify(summary: str, body: str, urgency: str = "normal") -> None:
    if os.environ.get("OSCMIX_NO_NOTIFY"):
        return
    try:
        subprocess.run(
            ["notify-send", "--urgency", urgency, "--icon", "oscmix", summary, body],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def systemctl_user(*verb: str) -> int:
    try:
        return subprocess.run(["systemctl", "--user", *verb], check=False,
                              timeout=3).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def ensure_backend(config: Config, path: Optional[Path], proc_root: Path) -> BackendStatus:
    """Reuse a matching manual/service backend; otherwise start only an enabled desk."""
    status = backend_status(config, proc_root, path)
    if status.state == "ready":
        return status
    if status.state != "absent":
        raise OSError(status.detail)
    if status.device is None or status.device.client is None:
        raise OSError("selected Fireface is not connected or has no ALSA sequencer client")
    service = service_status()
    problem = service_start_problem(path, service)
    if problem:
        raise OSError(problem)
    if service.get("manager") not in ("openrc", "runit"):
        log.info("starting enabled %s", SERVICE)
        systemctl_user("reset-failed", SERVICE)
        if systemctl_user("start", "--no-block", SERVICE):
            raise OSError("could not start %s; inspect its user journal" % SERVICE)
    deadline = time.monotonic() + BACKEND_WAIT
    while True:
        status = backend_status(config, proc_root, path)
        if status.state == "ready":
            return status
        if status.state != "absent" or time.monotonic() >= deadline:
            raise OSError("backend did not become ready: " + status.detail)
        time.sleep(0.25)


def resolve_gtk_binary() -> Optional[str]:
    """Same explicit-override/per-user/PATH ordering as the backend binaries."""
    return resolve_binary("oscmix-gtk", "OSCMIX_BIN_GTK")


def _launch(proc_root: Path) -> None:
    path = config_file()
    config = load_config(path)
    gtk = resolve_gtk_binary()
    if gtk is None:
        raise OSError("oscmix-gtk is not installed; install the GTK companion")
    desktop = inspect_desktop(gtk)
    if desktop.problem:
        raise OSError(desktop.problem)
    problem = maintenance_problem()
    if problem:
        raise OSError(problem + "; finish installation recovery first")
    status = ensure_backend(config, path, proc_root)
    if (status.pid is None or status.endpoint is None or status.device is None
            or not status.device.serial):
        raise OSError("backend identity is incomplete")
    environment = dict(os.environ, OSCMIX_CONTROL_SOCKET=status.endpoint,
                       OSCMIX_BACKEND_PID=str(status.pid),
                       OSCMIX_DEVICE_SERIAL=status.device.serial)
    try:
        os.execve(gtk, [gtk], environment)  # noqa: S606 -- GTK checks kernel peer before HELLO
    except OSError as exc:
        raise OSError("could not execute %s: %s" % (gtk, exc)) from exc


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(levelname)s: %(message)s")
    try:
        _launch(Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc")))
    except (OSError, ConfigError) as exc:
        log.error("%s", exc)  # noqa: TRY400 -- concise desktop notification, no traceback
        notify("RME Fireface Mixer", str(exc), urgency="critical")
        return 1
    return 0
