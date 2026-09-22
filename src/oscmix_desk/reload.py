"""A desk read again by a session that is already running.

Twice: under the device lock at the start, since the file the start read
is older than the wait for the device and the lock, and on SIGHUP, which
is how a switch, a resume and a person tell the unit to reconcile (ADR
0013). Both keep the machine the session was started for and apply only
a desk that is for it -- what a restart with the same command line would
apply here (ADR 0024, ADR 0026) -- and a reconcile says on the status
line whether it wrote.
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from .constants import RECONCILE_WAIT_FOR_VERIFIER, SERVICE_UNIT
from .discovery import lock_key
from .errors import ConfigError, ReceivePortError
from .locking import take_device_lock
from .log import log
from .model import Config, Machine
from .notices import log_desk_notices
from .notify import sd_notify
from .paths import discover_config_path, profile_path
from .profiles import effective_config, keep_machine_settings
from .verify import reconcile_now


def _desk_under_the_lock(config_path: Optional[Path],
                         running: Config) -> Config:
    """Re-read the desk now that nothing else may write it.

    The config this process started with was read before the lock, and
    a profile switch committed in between would otherwise be overwritten
    by a snapshot older than it: serialised writers, wrong result. So
    the desired state is read inside the lock that writes it (0.6.6).

    Machine settings stay with the running process. The ports and the
    device name belong to the process, not to the desk, which is the
    same rule the reconcile follows.
    """
    if config_path is None:
        return running
    try:
        fresh, _active = effective_config(config_path, running.overrides)
    except ConfigError as exc:
        log.error("%s is no longer usable (%s); applying the desk this "
                  "process started with", config_path, exc)
        return running
    kept = _kept_for_this_process(fresh, running)
    if kept is None:
        return running
    # The start spoke about the desk as it read it, before the wait for
    # the device and the lock. Another desk by now -- a file edited, a
    # profile switched in between -- is spoken about here; the same one
    # is not spoken about twice.
    if kept != running:
        log_desk_notices(kept)
    return kept


def _kept_for_this_process(fresh: Config, running: Config
                           ) -> Optional[Config]:
    """``fresh`` with this process's machine settings, or None when it is a
    desk for somewhere else.

    The lock was taken and the backend bound for this process's ports,
    usb id and pinned serial, and nothing re-read under it moves them
    (ADR 0024). Until 0.6.11 that meant the re-read desk was applied
    *here* whatever it named: a `routing.conf` edited to name a box with
    42 outputs reached a UCX II, which has twenty (ADR 0026).

    The re-read file is resolved the way a restart would resolve it --
    read and validated under the command line the start was given
    (``running.overrides``) -- and then held against what this session
    runs (``Machine.elsewhere``, which also says where the two still
    differ). So a reload applies what a restart
    would apply here, and refuses what a restart would take somewhere
    else. The first cuts compared the bare file: they refused the
    session's own desk under ``--osc-port``, with advice to restart that
    a restart did not follow (found by review, 0.6.11).

    Only ``routing.conf`` can name another machine since 0.7.0 -- a
    profile that does is refused where it is loaded, and the desk in
    effect falls back to ``routing.conf`` (ADR 0018) -- so the advice is
    the one a moved ``routing.conf`` gets: a restart follows it.
    """
    live = Machine(running.device_name, running.usb_id, running.serial,
                   running.osc_port, running.osc_recv_port)
    said = fresh.loaded
    moved = "" if said is None else \
        said.under(running.overrides).elsewhere(live)
    if said is None or not moved:
        return keep_machine_settings(fresh, running)
    log.error("the desk now in effect is for another backend or interface "
              "-- %s -- and a running session keeps the one it was started "
              "for, so it was not applied; restart the session to follow it "
              "(systemctl --user restart %s)", moved, SERVICE_UNIT)
    return None


def _verifier_finished(verifier: Optional[threading.Thread],
                       stop_requested: Dict[str, bool]) -> bool:
    """Wait for the start-up verifier before a reconcile writes anything.

    Both write the whole routing to one device. The verifier holds the
    receive port for stretches and releases it between phases, and a
    SIGHUP landing in one of those gaps used to start a second
    ``apply_routing`` on the main thread while the verifier's retry was
    about to start its own: two link phases, two barriers and two mix
    writes interleaved on the wire, which is exactly the ordering the
    two-phase apply exists to guarantee. Not theoretical: after a
    suspend the device re-enumerates, udev restarts the unit, and the
    resume hook sends its reload into that same window.

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
    """Re-read the config and re-apply it, on SIGHUP.

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
    wrote = _reconcile_once(args, config, stop_requested, verifier)
    if stop_requested["stop"]:
        return
    # What it did, not what it was asked to do: a reconcile that stood
    # down because the receive port is held used to report success
    # (0.6.6). Every other way of standing down -- the lock held
    # elsewhere, a verifier that outlived the wait, a config that no
    # longer parses -- left the previous line standing until 0.6.10.
    sd_notify("STATUS=running; %s at %s"
              % ("reconciled" if wrote else "reconcile skipped",
                 time.strftime("%H:%M:%S")))


def _reconcile_once(args: argparse.Namespace, config: Config,
                    stop_requested: Dict[str, bool],
                    verifier: Optional[threading.Thread]) -> bool:
    """The reconcile itself; True when it wrote the device."""
    if not _verifier_finished(verifier, stop_requested):
        return False
    path = _config_path(args)
    # The same lock a switch takes: a reconcile that started while one
    # was writing used to interleave with it (ADR 0019).
    lock = take_device_lock(path, lock_key(config.usb_id, config.serial))
    if lock is None:
        log.warning("SIGHUP: the device lock is not available; reconcile "
                    "skipped -- send the reload again")
        return False
    try:
        fresh = _reloaded_desk(config, path)
        if fresh is None:
            return False
        sd_notify("STATUS=reconciling (SIGHUP)")
        try:
            return bool(reconcile_now(fresh, "SIGHUP",
                                      lambda: stop_requested["stop"]))
        except ReceivePortError as exc:
            log.error("SIGHUP: %s; reconcile skipped -- with no dump there "
                      "is no way to tell what to leave alone", exc)
            return False
        except OSError as exc:
            # Out of `supervise` and `run_session` as a traceback until
            # 0.6.10, with the backend left to systemd.
            log.error("SIGHUP: cannot reach the backend on UDP %d (%s); "
                      "reconcile skipped", config.osc_port, exc)
            return False
    finally:
        lock.release()


def _reloaded_desk(running: Config, path: Optional[Path]) -> Optional[Config]:
    """The desk a SIGHUP re-reads, or None when the file is not usable.

    The active profile if one is remembered, routing.conf otherwise --
    the same answer the start gives (ADR 0018), which is what makes the
    resume hook's reload re-apply the desk that was chosen rather than
    the default one. A config that no longer parses keeps the running
    configuration, which is the state somebody is listening to.
    """
    if path is None:
        return running
    try:
        fresh, active = effective_config(path, running.overrides)
    except ConfigError as exc:
        log.error("SIGHUP: %s is not usable (%s); keeping the running "
                  "configuration", path, exc)
        return None
    # The backend is already bound and already talking to a device. A
    # reload reconciles the *desk*; the ports, the device name and the
    # interface belong to the process that is running, and changing them
    # here would mean writing to a port nobody is listening on -- with no
    # error, because OSC over UDP has no delivery guarantee (ADR 0024).
    kept = _kept_for_this_process(fresh, running)
    if kept is None:
        return None
    # Name what was actually reloaded. On the first live run this line
    # said routing.conf while the profile above it was in effect -- true
    # of the file read, misleading about the desk. And only once it is
    # the desk that will be applied: "reloaded X" above "X was not
    # applied" read as a contradiction on the desk (0.6.11).
    log.info("SIGHUP: reloaded %s (%d route(s), %d channel setting(s))",
             profile_path(active, path) if active else path,
             len(kept.routes), len(kept.channels))
    log_desk_notices(kept)
    return kept


def _config_path(args: argparse.Namespace) -> Optional[Path]:
    """The config this session runs, for the lock and for the reload."""
    return getattr(args, "config", None) or discover_config_path()
