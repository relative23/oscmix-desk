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
from typing import Dict, Optional, Tuple

from .config import Config, discover_config_path, profile_path
from .constants import (
    CHILD_STOP_GRACE,
    EXIT_CONFIG,
    EXIT_FAILURE,
    EXIT_OK,
    PORT_READY_TIMEOUT,
    RECONCILE_WAIT_FOR_VERIFIER,
    VERIFIER_STOP_GRACE,
    VERIFY_SETTLE,
)
from .discovery import (
    device_serials,
    lock_key,
    resolve_binary,
    udp_port_listening,
    usb_device_present,
    wait_for_device,
)
from .errors import ConfigError, DeviceAmbiguous, DeviceLockUnavailable
from .log import log
from .notify import sd_notify
from .process import _cleanup_stale_backend, socket_owner, supervise
from .profiles import DeviceLock, effective_config, take_device_lock
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
                        proc_root: Path) -> bool:
    """Wait until oscmix binds its OSC port; False when it never did.

    A living backend is not a listening one, and this is UDP: every
    datagram sent to a port nobody bound is accepted by the kernel and
    dropped. The apply would look like it worked, the verifier would
    confirm nothing, and `Type=notify` would have told systemd the desk
    is set. The caller fails the start instead (0.6.6).
    """
    deadline = time.monotonic() + PORT_READY_TIMEOUT
    announced = False
    while time.monotonic() < deadline:
        if udp_port_listening(config.osc_port, proc_root):
            # Bound is not enough: bound *by this backend* is. A
            # stranger holding the port is not readiness, and the
            # cleanup deliberately leaves strangers alone (ADR 0021).
            #
            # An owner that cannot be resolved is not this backend
            # either. The cleanup already reads that uncertainty as
            # "touch nobody"; reading it here as "ready" was the same
            # doubt answered two opposite ways (0.6.7).
            owner = socket_owner(config.osc_port, proc_root)
            if owner == child.pid:
                log.info("oscmix is listening on UDP %d", config.osc_port)
                return True
            if not announced:
                if owner is None:
                    log.warning("UDP %d is bound, but the process holding it "
                                "cannot be identified", config.osc_port)
                else:
                    log.warning("UDP %d is held by pid %d, not by this "
                                "backend", config.osc_port, owner)
                announced = True
        if child.poll() is not None:
            return False
        time.sleep(0.25)
    log.error("oscmix is not listening on UDP %d after %.0fs",
              config.osc_port, PORT_READY_TIMEOUT)
    return False


def _stop_child(child: "subprocess.Popen[bytes]") -> None:
    """Stop a backend this process started and will not use."""
    try:
        child.terminate()
        child.wait(timeout=CHILD_STOP_GRACE)
    except subprocess.TimeoutExpired:
        child.kill()
    except OSError:
        pass


def _apply_and_verify(child: "subprocess.Popen[bytes]", config: Config,
                      stop_requested: Dict[str, bool],
                      config_path: Optional[Path] = None
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
    # One transaction, from the first write to the verifier's last: a
    # switch that landed between them would be overwritten by the retry
    # that follows it, which is what 0.6.3 measured (ADR 0019).
    lock = take_device_lock(config_path,
                            lock_key(config.usb_id, config.serial))
    if lock is None:
        # Until 0.6.7 this wrote anyway, on the grounds that a desk with
        # no routing is worse than a re-apply. It also made "every writer
        # holds one lock" conditional on nothing going wrong, which is
        # the opposite of what a guarantee is. systemd restarts the unit;
        # a write nobody serialised cannot be taken back (ADR 0022).
        log.error("the device lock is not available; not applying routing")
        raise DeviceLockUnavailable(config.usb_id)

    # The lock may have taken a while. A stop that arrived during the
    # wait means this process is going away, and writing the whole
    # routing on the way out is the opposite of what was asked (0.6.6).
    if stop_requested["stop"] or child.poll() is not None:
        log.info("stop requested while waiting for the device lock; "
                 "nothing applied")
        lock.release()
        return None

    config = _desk_under_the_lock(config_path, config)
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
        lock.release()
        return None

    sd_notify("STATUS=applying routing")
    try:
        apply_routing(config, config.osc_port, config.osc_recv_port)
    except Exception:
        # The lock is this process's promise that nobody else writes
        # while it does. A write that raised must not keep it: the next
        # switch would wait out the full timeout and then refuse.
        lock.release()
        raise
    return _verify_in_background(child, config, stop_requested, lock)


def _verify_in_background(child: "subprocess.Popen[bytes]", config: Config,
                          stop_requested: Dict[str, bool],
                          lock: DeviceLock) -> threading.Thread:
    """Read the routing back on a thread, and free the lock when done.

    The lock spans the apply and this, because the verifier re-applies:
    a switch that landed in between would be overwritten by the retry.
    """
    def should_stop() -> bool:
        # A dead backend counts as a stop: its port is gone, so every
        # write from here would go nowhere and the log would claim a
        # re-apply that never landed anywhere.
        return stop_requested["stop"] or child.poll() is not None

    def deferred_verify() -> None:
        # STATUS= is what `systemctl --user status` shows: the phase the
        # session is in, and when the last one ended. It says what the
        # unit is doing; the lock is what keeps another writer out.
        try:
            if wait_unless_stopped(VERIFY_SETTLE, should_stop):
                return
            sd_notify("STATUS=verifying routing")
            verify_and_repair(config, should_stop)
            sd_notify("STATUS=running; verifier finished at %s"
                      % time.strftime("%H:%M:%S"))
        finally:
            lock.release()

    thread = threading.Thread(target=deferred_verify, name="verify",
                              daemon=True)
    thread.start()
    return thread


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
        fresh, _active = effective_config(config_path)
    except ConfigError as exc:
        log.error("%s is no longer usable (%s); applying the desk this "
                  "process started with", config_path, exc)
        return running
    fresh.osc_port = running.osc_port
    fresh.osc_recv_port = running.osc_recv_port
    fresh.device_name = running.device_name
    # And the interface: the lock was taken and the backend bound for
    # this process's usb id and pinned serial, and a profile naming
    # another box does not move either of them (ADR 0024).
    fresh.usb_id = running.usb_id
    fresh.serial = running.serial
    return fresh


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


def _find_client(args: argparse.Namespace, config: Config, proc_root: Path,
                 sysfs_usb: Path) -> Tuple[Optional[int], int]:
    """The sequencer client of this desk's interface, with its serial pinned.

    Returns the client, or None and the exit code the start ends with.
    Client and serial come from one resolution: `[device] serial` selects
    the box among identical ones, and without it more than one candidate
    is a configuration error, exit 2, which `RestartPreventExitStatus=2`
    keeps from becoming a restart loop. Until 0.6.9 the first matching
    client was bound and the serial worked out separately (ADR 0024).

    The serial is pinned because it is read again on every write, and the
    card list empties the moment the interface is unplugged: a reconcile
    in that window would otherwise take a different lock (ADR 0023).
    """
    log.info("waiting for %r (ALSA sequencer, timeout %.0fs)",
             config.device_name, args.timeout)
    try:
        device = wait_for_device(config.usb_id, config.device_name,
                                 config.serial, args.timeout, proc_root)
    except DeviceAmbiguous as exc:
        log.error("%s -- set [device] serial to the number on the box", exc)
        return None, EXIT_CONFIG
    if device is None:
        return None, _no_client(args, config, proc_root, sysfs_usb)
    config.serial = device.serial
    log.info("found %r as ALSA sequencer client %d", config.device_name,
             device.client)
    return device.client, EXIT_OK


def _no_client(args: argparse.Namespace, config: Config, proc_root: Path,
               sysfs_usb: Path) -> int:
    """The exit code for a start whose interface never showed a client.

    Not connected is the clean no-op it has always been. That includes a
    configured serial the machine does not show while another box of the
    model is plugged in: USB presence alone said "connected" there, and the
    start failed and was restarted for ever (found by review, 0.6.9).
    """
    absent = not usb_device_present(config.usb_id, sysfs_usb) or (
        bool(config.serial) and config.serial not in device_serials(
            proc_root / "asound" / "cards", config.device_name))
    if absent:
        log.info("device %s%s not connected; nothing to do", config.usb_id,
                 " with serial %s" % config.serial if config.serial else "")
        sd_notify("READY=1")  # Type=notify: a clean no-op start
        return EXIT_OK
    log.error(
        "USB device %s is connected but no ALSA sequencer client named %r%s "
        "appeared within %.0fs -- is snd-usb-audio loaded?",
        config.usb_id, config.device_name,
        " with serial %s" % config.serial if config.serial else "",
        args.timeout,
    )
    return EXIT_FAILURE


def run_session(args: argparse.Namespace, config: Config) -> int:
    """Discover the device, run the backend, and supervise it."""
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    sysfs_usb = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))

    client, code = _find_client(args, config, proc_root, sysfs_usb)
    if client is None:
        return code

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
    if not _await_backend_port(child, config, proc_root) \
            and usb_device_present(config.usb_id, sysfs_usb):
        # The port never came up while the device is still there.
        # Routing written into a port nobody bound is dropped by the
        # kernel without a word, and READY=1 would tell systemd the desk
        # is set. A backend that exited cleanly before binding used to
        # reach that READY through the exit mapping (0.6.7); the device
        # being gone is still the clean no-op it always was.
        log.error("backend never bound UDP %d; failing the start",
                  config.osc_port)
        if child.poll() is None:
            _stop_child(child)
        return EXIT_FAILURE

    verifier = None
    if child.poll() is None:
        try:
            verifier = _apply_and_verify(child, config, stop_requested,
                                         _config_path(args))
            # The service is "started": backend up, routing applied --
            # and only then. READY=1 used to follow a lock refusal too,
            # telling systemd the desk was set while nothing had been
            # written (0.6.7, 0.6.8).
            sd_notify("READY=1")
        except DeviceLockUnavailable:
            # A failed start is retried after RestartSec; a held lock is
            # a wait, not a desk (ADR 0022, ADR 0024).
            log.error("failing the start so systemd tries again")
            _stop_child(child)
            return EXIT_FAILURE

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
    the state somebody is listening to.

    The read happens **inside** the device lock. Outside it, a profile
    switch committed between the read and the write would be undone by
    a reconcile that had already decided what to write (0.6.6).

    ``verifier`` is the start-up thread, when it exists: the reconcile
    is serialised behind it (``_verifier_finished``).
    """
    if not _verifier_finished(verifier, stop_requested):
        return
    path = _config_path(args)
    # The same lock a switch takes: a reconcile that started while one
    # was writing used to interleave with it (ADR 0019).
    lock = take_device_lock(path, lock_key(config.usb_id, config.serial))
    if lock is None:
        log.warning("SIGHUP: the device lock is not available; reconcile "
                    "skipped -- send the reload again")
        return
    try:
        fresh = config
        if path is not None:
            try:
                # The active profile if one is remembered, routing.conf
                # otherwise -- the same answer the start gives (ADR 0018),
                # which is what makes the resume hook's reload re-apply the
                # desk that was chosen rather than the default one.
                fresh, active = effective_config(path)
            except ConfigError as exc:
                log.error("SIGHUP: %s is not usable (%s); keeping the running "
                          "configuration", path, exc)
                return
            # Name what was actually reloaded. On the first live run this
            # line said routing.conf while the profile above it was in
            # effect -- true of the file read, misleading about the desk.
            log.info("SIGHUP: reloaded %s (%d route(s), %d channel setting(s))",
                     profile_path(active, path) if active else path,
                     len(fresh.routes), len(fresh.channels))
            # The backend is already bound and already talking to a device.
            # A reload reconciles the *desk*; the ports and the device name
            # belong to the process that is running, and changing them here
            # would mean writing to a port nobody is listening on -- with no
            # error, because OSC over UDP has no delivery guarantee.
            fresh.osc_port = config.osc_port
            fresh.osc_recv_port = config.osc_recv_port
            fresh.device_name = config.device_name
            fresh.usb_id = config.usb_id
            fresh.serial = config.serial
        sd_notify("STATUS=reconciling (SIGHUP)")
        wrote = reconcile_now(fresh, "SIGHUP", lambda: stop_requested["stop"])
    finally:
        lock.release()
    # What it did, not what it was asked to do: a reconcile that stood
    # down because the receive port is held used to report success
    # (0.6.6).
    sd_notify("STATUS=running; %s at %s"
              % ("reconciled" if wrote else "reconcile skipped",
                 time.strftime("%H:%M:%S")))


def _config_path(args: argparse.Namespace) -> Optional[Path]:
    """The config this session runs, for the lock and for the reload."""
    return getattr(args, "config", None) or discover_config_path()


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
