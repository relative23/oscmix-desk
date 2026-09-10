"""The session lifecycle: discover, launch, apply, signal, supervise.

This is the only module that composes the others; everything it calls
is testable on its own.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from .config import Config, discover_config_path, load_config
from .constants import (
    EXIT_FAILURE,
    EXIT_OK,
    PORT_READY_TIMEOUT,
    RECONCILE_WAIT_FOR_VERIFIER,
    VERIFIER_STOP_GRACE,
    VERIFY_SETTLE,
)
from .discovery import resolve_binary, udp_port_listening, usb_device_present, wait_for_seq_client
from .errors import ConfigError
from .log import log
from .notify import sd_notify
from .process import _cleanup_stale_backend, supervise
from .reconcile import desired, plan
from .routing import apply_routing, wait_unless_stopped
from .verify import reconcile_now, verify_and_repair


def _print_dry_run(client: int, config: Config) -> None:
    """Show what would be started and sent, in the order it would happen.

    Reads the same plan ``apply_routing`` sends, so the printed sequence
    *is* the sent sequence. It used to walk route by route and print
    link, mix, link, mix -- an order the apply never uses, and the only
    thing CI inspected to guard this project's most expensive bug.

    Both sides moved to ``reconcile.plan`` together, for the same
    reason: the guarantee is that they read one source, not that they
    read a particular one.
    """
    print("would run: alsaseqio %d:1 oscmix" % client)
    for path, types, values in plan(desired(config)).messages():
        print("would send: %s ,%s %s"
              % (path, types, " ".join(map(str, values))))


def _start_backend(client: int, config: Config) -> Optional["subprocess.Popen[bytes]"]:
    """Launch ``alsaseqio <client>:1 oscmix``, or None if a binary is missing."""
    alsaseqio = resolve_binary("alsaseqio", "OSCMIX_BIN_ALSASEQIO")
    backend = resolve_binary("oscmix", "OSCMIX_BIN_BACKEND")
    if alsaseqio is None or backend is None:
        log.error("alsaseqio/oscmix not found -- run install.sh first")
        return None

    # Pass the configured ports through to oscmix -- its compiled-in
    # defaults are 7222/8222 and would silently diverge from routing.conf
    # otherwise. (oscmix-gtk keeps its own port settings; users changing
    # these also need to adjust the GUI's connection settings.)
    command = [alsaseqio, "%d:1" % client, backend,
               "-r", "udp!127.0.0.1!%d" % config.osc_port,
               "-s", "udp!127.0.0.1!%d" % config.osc_recv_port]
    log.info("starting: %s", " ".join(command))
    return subprocess.Popen(command)


def _install_stop_handlers(child: "subprocess.Popen[bytes]",
                           stop_requested: Dict[str, bool]) -> None:
    """Turn SIGTERM/SIGINT into an orderly backend shutdown."""
    def handle_stop(signum: int, _frame: object) -> None:
        stop_requested["stop"] = True
        log.info("received %s; stopping backend", signal.Signals(signum).name)
        sd_notify("STOPPING=1")
        if child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)


def _install_reload_handler(reload_requested: Dict[str, bool]) -> None:
    """Turn SIGHUP into a request to reconcile, not into a reconcile.

    The handler sets a flag and returns. Everything a reconcile does --
    reading a config off disk, binding a port, waiting out the link
    barrier -- is forbidden here: a signal handler runs between two
    bytecodes of whatever was executing, and doing that work in place
    means doing it *inside* the apply it was meant to follow.

    The supervise loop picks the flag up within its poll interval, which
    is the only place in this process where nothing else is half-done.
    """
    def handle_reload(_signum: int, _frame: object) -> None:
        reload_requested["reload"] = True

    signal.signal(signal.SIGHUP, handle_reload)


def _await_backend_port(child: "subprocess.Popen[bytes]", config: Config,
                        proc_root: Path) -> None:
    """Wait until oscmix binds its OSC port, or the child dies trying."""
    deadline = time.monotonic() + PORT_READY_TIMEOUT
    while time.monotonic() < deadline:
        if udp_port_listening(config.osc_port, proc_root):
            log.info("oscmix is listening on UDP %d", config.osc_port)
            return
        if child.poll() is not None:
            return
        time.sleep(0.25)
    log.warning("oscmix not listening on UDP %d after %.0fs; continuing",
                config.osc_port, PORT_READY_TIMEOUT)


def _apply_and_verify(child: "subprocess.Popen[bytes]", config: Config,
                      stop_requested: Dict[str, bool]
                      ) -> Optional[threading.Thread]:
    """Apply the routing, then verify it in the background.

    One background dump does both jobs: it syncs oscmix's link state
    (which triggers the mix re-apply) and verifies the routing, without
    holding up the readiness signal.

    Returns the verifier thread so ``run_session`` can wait for it to
    stop before exiting. It used to read ``stop_requested`` and
    ``child.poll()`` exactly once, here, before starting -- and then run
    for two verification windows plus a blind delay, writing routing at
    three points, with nobody looking again. See
    docs/decisions/0009-verifier-stop-contract.md.
    """
    if not desired(config):
        # Everything the config declares, not only its routes. Since
        # 0.4.0 a file may consist of `[input:3]`, `[eq:input:3]` or
        # `[clock]` sections alone, and a check on `config.routes` skipped
        # every such file at start -- while `--dry-run` printed its
        # writes and a SIGHUP reload sent them. The same shape as the
        # two half-config defects fixed in 0.3.0: a function that looked
        # at one part of the config and treated the rest as empty.
        log.info("nothing declared in the config; leaving mixer state "
                 "untouched")
        return None

    apply_routing(config, config.osc_port, config.osc_recv_port)

    def should_stop() -> bool:
        # A dead backend counts as a stop: its port is gone, so every
        # write from here would go nowhere and the log would claim a
        # re-apply that never landed anywhere.
        return stop_requested["stop"] or child.poll() is not None

    def deferred_verify() -> None:
        if wait_unless_stopped(VERIFY_SETTLE, should_stop):
            return
        verify_and_repair(config, should_stop)

    thread = threading.Thread(target=deferred_verify, name="verify",
                              daemon=True)
    thread.start()
    return thread


def _exit_code_for(returncode: int, config: Config, sysfs_usb: Path,
                   stop_requested: Dict[str, bool]) -> int:
    """Translate the backend's exit into the service's exit contract.

    Every clean exit must have signalled READY under Type=notify --
    exiting 0 without it counts as a protocol failure and would put the
    unit into a restart loop. Re-sending READY is harmless.
    """
    if stop_requested["stop"]:
        log.info("backend stopped (shutdown requested)")
    elif not usb_device_present(config.usb_id, sysfs_usb):
        log.info("backend exited (%d): device was disconnected", returncode)
    elif returncode == 0:
        log.info("backend exited cleanly")
    else:
        suffix = ""
        if returncode < 0:
            try:
                suffix = " (%s)" % signal.Signals(-returncode).name
            except ValueError:
                pass
        log.error("backend exited with status %d%s", returncode, suffix)
        return EXIT_FAILURE
    sd_notify("READY=1")
    return EXIT_OK


def run_session(args: argparse.Namespace, config: Config) -> int:
    """Discover the device, run the backend, and supervise it."""
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    sysfs_usb = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))

    log.info("waiting for %r (ALSA sequencer, timeout %.0fs)",
             config.device_name, args.timeout)
    client = wait_for_seq_client(config.device_name, args.timeout, proc_root)
    if client is None:
        if not usb_device_present(config.usb_id, sysfs_usb):
            log.info("device %s not connected; nothing to do", config.usb_id)
            sd_notify("READY=1")  # Type=notify: a clean no-op start
            return EXIT_OK
        log.error(
            "USB device %s is connected but no ALSA sequencer client named %r "
            "appeared within %.0fs -- is snd-usb-audio loaded?",
            config.usb_id, config.device_name, args.timeout,
        )
        return EXIT_FAILURE
    log.info("found %r as ALSA sequencer client %d", config.device_name, client)

    if args.dry_run:
        _print_dry_run(client, config)
        return EXIT_OK

    _cleanup_stale_backend(config.osc_port, proc_root)
    child = _start_backend(client, config)
    if child is None:
        return EXIT_FAILURE

    stop_requested = {"stop": False}
    reload_requested = {"reload": False}
    _install_stop_handlers(child, stop_requested)
    _install_reload_handler(reload_requested)
    _await_backend_port(child, config, proc_root)

    verifier = None
    if child.poll() is None:
        verifier = _apply_and_verify(child, config, stop_requested)
        # The service is "started": backend up, routing applied.
        sd_notify("READY=1")

    returncode = supervise(child, stop_requested,
                           on_reload=lambda: _reconcile(args, config,
                                                        stop_requested,
                                                        verifier),
                           reload_requested=reload_requested)
    _await_verifier(verifier)
    return _exit_code_for(returncode, config, sysfs_usb, stop_requested)


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
    the state somebody is listening to. Exiting here would take the
    routing down over a typo in a file nobody was forced to edit.

    ``verifier`` is the start-up thread, when it exists: the reconcile
    is serialised behind it (``_verifier_finished``).
    """
    if not _verifier_finished(verifier, stop_requested):
        return
    path = getattr(args, "config", None) or discover_config_path()
    fresh = config
    if path is not None:
        try:
            fresh = load_config(path)
        except ConfigError as exc:
            log.error("SIGHUP: %s is not usable (%s); keeping the running "
                      "configuration", path, exc)
            return
        log.info("SIGHUP: reloaded %s (%d route(s), %d channel setting(s))",
                 path, len(fresh.routes), len(fresh.channels))
        # The backend is already bound and already talking to a device.
        # A reload reconciles the *desk*; the ports and the device name
        # belong to the process that is running, and changing them here
        # would mean writing to a port nobody is listening on -- with no
        # error, because OSC over UDP has no delivery guarantee.
        fresh.osc_port = config.osc_port
        fresh.osc_recv_port = config.osc_recv_port
        fresh.device_name = config.device_name
    reconcile_now(fresh, "SIGHUP", lambda: stop_requested["stop"])


def _await_verifier(verifier: Optional[threading.Thread]) -> None:
    """Do not exit while the verifier may still be writing routing.

    "The mix is never left half-applied" is the property the two-phase
    design exists for, and on the shutdown path nobody used to state it:
    the process exited when ``supervise`` returned and cut the daemon
    thread wherever it happened to be, possibly between two mix writes.

    The verifier checks for a stop between every phase and before every
    write, so once the backend is gone this returns within one socket
    timeout. The grace period is the bound on being wrong about that,
    and it fits inside ``TimeoutStopSec`` alongside ``CHILD_STOP_GRACE``
    -- asserted in ``tests/test_unit_file.py``.
    """
    if verifier is None or not verifier.is_alive():
        return
    verifier.join(timeout=VERIFIER_STOP_GRACE)
    if verifier.is_alive():
        log.warning("verifier still running after %.1fs; exiting anyway",
                    VERIFIER_STOP_GRACE)
