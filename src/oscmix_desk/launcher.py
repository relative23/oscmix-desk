"""The desktop launcher: check, start the backend, exec the GTK mixer.

Lives in the package for the same reason everything else does -- it
was the one file outside the architecture test, the mutation scope and
the coverage most of the repository is held to, and it duplicated the
sysfs and procfs helpers to stay standalone. The package is installed
beside it now, so that duplication bought nothing.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .constants import DEFAULT_OSC_PORT, DEFAULT_USB_ID, SERVICE_UNIT
from .discovery import resolve_binary, udp_port_listening, usb_device_present

BACKEND_WAIT = float(os.environ.get("OSCMIX_BACKEND_WAIT", "5"))
SERVICE = SERVICE_UNIT

log = logging.getLogger("oscmix-launch")


def load_settings() -> "tuple[str, int]":
    """Read usb-id and OSC port from routing.conf; fall back to defaults.

    The launcher must never fail because of a config problem -- the
    backend reports those properly -- so parse errors only log a warning.
    """
    usb_id, port = DEFAULT_USB_ID, DEFAULT_OSC_PORT
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates = [Path(xdg) / "oscmix" / "routing.conf",
                  Path("/etc/oscmix/routing.conf")]
    env = os.environ.get("OSCMIX_CONFIG")
    if env:
        candidates.insert(0, Path(env))
    for path in candidates:
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(
            interpolation=None, inline_comment_prefixes=("#", ";")
        )
        try:
            parser.read(path, encoding="utf-8")
            raw_id = parser.get("device", "usb-id", fallback=usb_id).strip()
            if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", raw_id):
                usb_id = raw_id.lower()
            port = parser.getint("osc", "port", fallback=port)
            port = _active_profile_port(path, port)
        except (configparser.Error, ValueError) as exc:
            log.warning("ignoring unreadable config %s: %s", path, exc)
        break
    return usb_id, port


def _active_profile_port(config_path: Path, port: int) -> int:
    """The port the active profile states, if it states one.

    A profile inherits `[osc]` from routing.conf unless it says otherwise
    (ADR 0018), and the backend runs on the profile's port while one is
    active. The launcher polled routing.conf's port only, and warned
    about a backend that was reachable all along (0.6.10).
    """
    marker = config_path.with_name("active-profile")
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return port
    # The same rule as config.profile_path, kept in step by a test.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return port
    profile = config_path.with_name("profiles") / ("%s.conf" % name)
    if not profile.is_file():
        return port
    parser = configparser.ConfigParser(interpolation=None,
                                       inline_comment_prefixes=("#", ";"))
    try:
        parser.read(profile, encoding="utf-8")
        return parser.getint("osc", "port", fallback=port)
    except (configparser.Error, ValueError, UnicodeDecodeError):
        return port


def notify(summary: str, body: str, urgency: str = "normal") -> None:
    if os.environ.get("OSCMIX_NO_NOTIFY"):
        return
    try:
        subprocess.run(
            ["notify-send", "--urgency", urgency, "--icon", "oscmix",
             summary, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def systemctl_user(*verb: str) -> int:
    try:
        return subprocess.run(
            ["systemctl", "--user", *verb], check=False
        ).returncode
    except OSError:
        return 1


def ensure_backend(port: int, proc_root: Path) -> bool:
    """Start the backend service if needed; True if it accepts OSC."""
    if systemctl_user("is-active", "--quiet", SERVICE) != 0:
        log.info("starting %s", SERVICE)
        systemctl_user("reset-failed", SERVICE)
        # --no-block: the unit is Type=notify, a plain start would block
        # until READY or TimeoutStartSec; the port poll below bounds the
        # wait instead.
        systemctl_user("start", "--no-block", SERVICE)
    deadline = time.monotonic() + BACKEND_WAIT
    while time.monotonic() < deadline:
        if udp_port_listening(port, proc_root):
            return True
        time.sleep(0.25)
    return udp_port_listening(port, proc_root)


def resolve_gtk_binary() -> Optional[str]:
    """The GUI binary, resolved exactly like the backend pair.

    This used to ask PATH first with its own copy of the lookup, which
    left the GUI open to the same shadowing the backend was measured to
    hit on 2026-08-26: a stale root-owned copy in /usr/local/bin wins
    whenever the session's PATH lacks ~/.local/bin. One rule, one
    place -- see resolve_binary for the ordering and the measurement.
    """
    return resolve_binary("oscmix-gtk", "OSCMIX_BIN_GTK")


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(levelname)s: %(message)s")
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    sysfs_usb = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))

    usb_id, port = load_settings()

    if not usb_device_present(usb_id, sysfs_usb):
        log.error("RME Fireface (%s) is not connected", usb_id)
        notify("RME Fireface Mixer",
               "The Fireface is not connected. Plug it in and switch it on.",
               urgency="critical")
        return 1

    if not ensure_backend(port, proc_root):
        log.warning("backend not reachable on UDP %d; starting mixer anyway "
                    "(check: journalctl --user -u %s)", port, SERVICE)
        notify("RME Fireface Mixer",
               "The mixer backend did not start. "
               "Check 'journalctl --user -u %s'." % SERVICE)

    gtk = resolve_gtk_binary()
    if gtk is None:
        log.error("oscmix-gtk not found -- was it built with GTK support?")
        notify("RME Fireface Mixer", "oscmix-gtk is not installed.",
               urgency="critical")
        return 1
    try:
        os.execv(gtk, [gtk])  # noqa: S606 -- exec is the point
    except OSError as exc:
        # execv only returns by failing -- a stale binary, a bad
        # interpreter, a full exec format mismatch. Report it the same way
        # as the other startup failures instead of dumping a traceback on
        # a user who launched this from a desktop icon.
        # Deliberately not log.exception (TRY400): this runs from a
        # desktop icon, and a traceback is the thing 0.1.3 removed. The
        # message carries the OSError text, which is the actionable part.
        log.error("could not execute %s: %s", gtk, exc)  # noqa: TRY400
        notify("RME Fireface Mixer", "The mixer could not be started: %s"
               % exc, urgency="critical")
        return 1
    return 0  # unreachable: a successful execv never returns
