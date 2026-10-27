"""A desk read again by a session that is already running.

Twice: under the device lock at the start, since the file the start read
is older than the wait for the device and the lock, and on SIGHUP, which
is how a switch, a resume and a person tell the unit to reconcile.
Both keep the machine the session was started for and apply only
a desk that is for it -- what a restart with the same command line would
apply here -- and a reconcile says on the status
line whether it wrote.
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Dict, Optional, cast

from .constants import RECONCILE_WAIT_FOR_VERIFIER, SERVICE_UNIT
from .desk import _operation
from .errors import ConfigError, DeviceLockUnavailable, WriteFailed
from .log import log
from .model import Config, Machine
from .notices import log_desk_notices
from .notify import sd_notify
from .paths import discover_config_path, profile_path
from .profiles import effective_config, keep_machine_settings
from .reconcile import ApplyIntent


def _desk_under_the_lock(config_path: Optional[Path],
                         running: Config, *, strict: bool = False) -> Config:
    """Re-read the desk now that nothing else may write it.

    The config this process started with was read before the lock, and
    a profile switch committed in between would otherwise be overwritten
    by a snapshot older than it: serialised writers, wrong result. So
    the desired state is read inside the lock that writes it (0.6.6).

    Device identity stays with the running process, as on a reconcile.
    Legacy port values are retained but select no backend.
    """
    fresh = _reread_desk(running, config_path, starting=True, strict=strict)
    if fresh is None and strict:
        raise ConfigError("the desk under the device lock is not usable; strict start refused")
    return fresh or running


def _kept_for_this_process(fresh: Config, running: Config
                           ) -> Optional[Config]:
    """Keep this process's identity, or refuse a desk for another device.

    Re-read and validate under the same overrides as a restart, then
    compare the device name, USB ID and pinned serial with this session.
    Profiles are already checked against their main config by the loader.
    A main config moved to another device requires a deliberate restart.
    """
    live = Machine(running.device_name, running.usb_id, running.serial,
                   running.osc_port, running.osc_recv_port)
    declared_machine = fresh.loaded
    moved = "" if declared_machine is None else \
        declared_machine.under(running.overrides).elsewhere(live)
    if declared_machine is None or not moved:
        return keep_machine_settings(fresh, running)
    log.error("the desk now in effect is for another backend or interface "
              "-- %s -- and a running session keeps the one it was started "
              "for, so it was not applied; restart the session to follow it "
              "(systemctl --user restart %s)", moved, SERVICE_UNIT)
    return None


def _verifier_finished(verifier: Optional[threading.Thread],
                       stop_requested: Dict[str, bool]) -> bool:
    """Wait for the start-up verifier before a reconcile writes anything.

    Startup verification retains the operation's connection and ownership
    through observation and permitted repair. A later selective reconcile
    waits for that worker to end before acquiring its own ownership. Both
    preserve REMEMBER starting values. The historical receive-port gaps
    allowed their link and mix phases to interleave; the worker wait and
    whole-operation ownership retain that ordering across reloads at startup
    or resume.

    Returns False when the reconcile must not proceed: a stop arrived
    while waiting, or the verifier outlived ``RECONCILE_WAIT_FOR_VERIFIER``,
    which is logged so a swallowed reload is never silent.
    """
    if verifier is None or not verifier.is_alive():
        return True
    log.info("SIGHUP: waiting for the start-up verifier before reconciling")
    deadline = time.monotonic() + RECONCILE_WAIT_FOR_VERIFIER
    while verifier.is_alive():
        if stop_requested["stop"]:
            return False
        if time.monotonic() >= deadline:
            log.warning("SIGHUP: verifier still running after %.0fs; reconcile "
                        "skipped -- send the reload again",
                        RECONCILE_WAIT_FOR_VERIFIER)
            return False
        verifier.join(timeout=0.1)
    return True


def _reconcile(args: argparse.Namespace, config: Config,
               stop_requested: Dict[str, bool],
               verifier: Optional[threading.Thread] = None) -> None:
    """Re-read the config and selectively reconcile PIN settings on SIGHUP.

    Re-reading is what makes SIGHUP mean what it means everywhere else.
    A config that no longer parses is *not* applied and not fatal: the
    session keeps running on the configuration it already has, which is
    the state somebody is listening to.

    The read happens **inside** the device lock. Outside it, a profile
    switch committed between the read and the write would be undone by
    a reconcile that had already decided what to write (0.6.6).

    ``verifier`` is the start-up thread, when it exists: the reconcile
    is serialised behind it (``_verifier_finished``).
    """
    outcome = _reconcile_once(args, config, stop_requested, verifier)
    if stop_requested["stop"]:
        return
    # What it did, not what it was asked to do: a reconcile that stood
    # down because the receive port is held used to report success
    # (0.6.6). Every other way of standing down -- the lock held
    # elsewhere, a verifier that outlived the wait, a config that no
    # longer parses -- left the previous line standing until 0.6.10.
    sd_notify("STATUS=running; %s at %s"
              % (outcome, time.strftime("%H:%M:%S")))


def _reconcile_once(args: argparse.Namespace, config: Config,
                    stop_requested: Dict[str, bool],
                    verifier: Optional[threading.Thread]) -> str:
    """Report completion, a refused start, or an incomplete operation."""
    if not _verifier_finished(verifier, stop_requested):
        return "reconcile skipped"
    path = _config_path(args)
    planned = False
    try:
        with _operation(config, path, resolved=True,
                        reread=lambda running: _reload_config(
                            running, path, strict=getattr(args, "strict", False)),
                        should_stop=lambda: stop_requested["stop"]) as desk:
            sd_notify("STATUS=reconciling (SIGHUP)")
            complete = desk.apply(ApplyIntent.RECONCILE, reason="SIGHUP")
            planned = True
            if stop_requested["stop"]:
                return "reconcile incomplete"
            desk._finish()
    except DeviceLockUnavailable as exc:
        if stop_requested["stop"]:
            log.info("SIGHUP: %s; reconcile skipped", exc)
        else:
            log.warning("SIGHUP: %s; reconcile skipped -- send the reload again", exc)
        return "reconcile skipped"
    except ConfigError:
        return "reconcile skipped"
    except OSError as exc:
        return _stood_down(exc, planned, stop_requested["stop"])
    else:
        return "reconciled" if complete else "reconcile incomplete"


def _reload_config(running: Config, path: Optional[Path], *, strict: bool = False) -> Config:
    fresh = _reloaded_desk(running, path, strict=strict)
    if fresh is None:
        raise ConfigError("the reloaded desk is not usable")
    return fresh


def _stood_down(exc: OSError, planned: bool, stopping: bool) -> str:
    """Report a reconcile that ended early: skipped or incomplete.

    Skipped when nothing was submitted, incomplete otherwise.

    Decided by how far the operation came, not by the exception's type:
    until 0.8.1 a receive failure read "skipped" even when it ended the
    lease after the writes, and a refusal the plan made before writing
    anything (a remembered value it cannot keep, a link it may not trust)
    read "incomplete" as if the backend had failed.
    """
    submitted = planned or (isinstance(exc, WriteFailed) and bool(exc.written))
    outcome = "reconcile incomplete" if submitted else "reconcile skipped"
    if stopping:
        log.info("SIGHUP: stop requested; %s (%s)", outcome, exc)
    elif submitted:
        log.error("SIGHUP: %s; %s", exc, outcome)
    else:
        log.error("SIGHUP: %s; %s, nothing written", exc, outcome)
    return outcome


def _reloaded_desk(running: Config, path: Optional[Path], *,
                   strict: bool = False) -> Optional[Config]:
    """The SIGHUP desk, or None to skip an unusable or retargeted file."""
    return _reread_desk(running, path, starting=False, strict=strict)


def _reread_desk(running: Config, path: Optional[Path], *,
                 starting: bool, strict: bool = False) -> Optional[Config]:
    """Validate the selected desk under the lock without retargeting it."""
    if path is None:
        return running
    try:
        fresh, active = effective_config(path, running.overrides, strict=strict)
    except ConfigError as exc:
        if starting and strict:
            log.error("%s is no longer usable (%s); strict start refused, "
                      "nothing written", path, exc)
        elif starting:
            log.error("%s is no longer usable (%s); applying the desk this "
                      "process started with", path, exc)
        else:
            log.error("SIGHUP: %s is not usable (%s); keeping the running "
                      "configuration", path, exc)
        return None
    # A reload reconciles the desk through the running backend's checked
    # device connection. Its machine identity and accepted legacy port
    # values remain fixed for this process; a reload cannot retarget it.
    kept = _kept_for_this_process(fresh, running)
    if kept is not None:
        if not starting:
            log.info("SIGHUP: reloaded %s (%d route(s), %d channel setting(s))",
                     profile_path(active, path) if active else path,
                     len(kept.routes), len(kept.channels))
        if not starting or kept != running:
            log_desk_notices(kept)
    return kept


def _config_path(args: argparse.Namespace) -> Optional[Path]:
    """Pin this invocation's selection, including no discovered config."""
    if not hasattr(args, "_desk_config_path"):
        args._desk_config_path = getattr(args, "config", None) or discover_config_path()
    return cast(Optional[Path], args._desk_config_path)
