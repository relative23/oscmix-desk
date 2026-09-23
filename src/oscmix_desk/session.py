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
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional, Tuple

from .constants import (
    CHILD_STOP_GRACE,
    EXIT_CONFIG,
    EXIT_FAILURE,
    EXIT_OK,
    PORT_READY_TIMEOUT,
    VERIFIER_STOP_GRACE,
    VERIFY_SETTLE,
)
from .devices import device_for_name
from .discovery import Device as Interface
from .discovery import (
    device_serials,
    lock_key,
    resolve_binary,
    udp_port_listening,
    usb_device_authorized,
    usb_device_present,
    usb_revision,
    wait_for_device,
)
from .errors import (
    DeviceAmbiguous,
    DeviceLockUnavailable,
    ReceivePortError,
)
from .locking import DeviceLock, take_device_lock
from .log import log
from .model import Config
from .notices import log_desk_notices
from .notify import sd_notify
from .process import _cleanup_stale_backend, socket_owner, supervise
from .reconcile import desired, plan
from .reload import _config_path, _desk_under_the_lock, _reconcile
from .routing import apply_routing, wait_unless_stopped
from .verify import verify_and_repair


def _print_dry_run(client: Optional[int], config: Config) -> None:
    """Show what would be started and sent, in the order it would happen.

    Without the interface too (``client`` is None): what would be sent
    does not depend on it, and until 0.7.0 a dry run on a machine whose
    box was switched off printed nothing at all -- on the machine where
    one writes a config before plugging anything in.

    Reads the same plan ``apply_routing`` sends, so the printed sequence
    *is* the sent sequence. It used to walk route by route and print
    link, mix, link, mix -- an order the apply never uses, and the only
    thing CI inspected to guard this project's most expensive bug.

    Both sides moved to ``reconcile.plan`` together, for the same
    reason: the guarantee is that they read one source, not that they
    read a particular one.
    """
    log.info("dry run: live USB playback mode is not validated; "
             "the active hardware stream is checked before writes")
    print("would run: alsaseqio %s:1 oscmix"
          % ("<client>" if client is None else client))
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
        except ReceivePortError as exc:
            # Not "skipped": this port can never be read, so the desk
            # stays unverified until somebody changes `[osc] recv-port`
            # or whatever keeps it from being bound (0.6.11).
            log.error("routing cannot be verified: %s", exc)
            sd_notify("STATUS=running; verifier failed at %s"
                      % time.strftime("%H:%M:%S"))
        except OSError as exc:
            # The socket, not the desk: a thread traceback said nothing a
            # person could act on, and the lock's release was all that
            # happened (0.6.10).
            log.error("verifier could not reach the backend on UDP %d (%s)",
                      config.osc_port, exc)
            sd_notify("STATUS=running; verifier failed at %s"
                      % time.strftime("%H:%M:%S"))
        finally:
            lock.release()

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


def _find_client(args: argparse.Namespace, config: Config, proc_root: Path,
                 sysfs_usb: Path) -> Tuple[Optional[Interface], int]:
    """The interface this desk is for: its sequencer client and its serial.

    Returns it, or None and the exit code the start ends with.
    Client and serial come from one resolution: `[device] serial` selects
    the box among identical ones, and without it more than one candidate
    is a configuration error, exit 2, which `RestartPreventExitStatus=2`
    keeps from becoming a restart loop. Until 0.6.9 the first matching
    client was bound and the serial worked out separately (ADR 0024).

    The start pins the serial it is given here, because it is read again
    on every write, and the card list empties the moment the interface is
    unplugged: a reconcile in that window would otherwise take a
    different lock (ADR 0023). Until 0.7.0 this function wrote it into
    its argument, which is how the caller's config came to change under
    it.
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
    log.info("found %r as ALSA sequencer client %d", config.device_name,
             device.client)
    return device, EXIT_OK


def _firmware_notice(config: Config, sysfs_usb: Path) -> None:
    """Say so when the interface runs another firmware than was measured.

    The register table, the hardware evidence and the write sweep were
    recorded on one USB release. A newer firmware may move or add
    registers, and until 0.7.0 nothing anywhere said that the box on the
    desk was not the box that was measured (second outside review). A
    notice and not a refusal: the read-back still verifies every register
    it can, and a firmware update must not take the desk down.
    """
    model = device_for_name(config.device_name)
    measured = None if model is None else model.firmware
    reported = usb_revision(config.usb_id, sysfs_usb)
    if measured and reported and reported != measured:
        log.warning("the interface reports USB release %s, and the register "
                    "table for %r was recorded on %s: every register is "
                    "still read back, but what this firmware moved or "
                    "added is not known here", reported, config.device_name,
                    measured)


def _no_client(args: argparse.Namespace, config: Config, proc_root: Path,
               sysfs_usb: Path) -> int:
    """The exit code for a start whose interface never showed a client.

    Not connected is the clean no-op it has always been. That includes a
    configured serial the machine does not show while another box of the
    model is plugged in: USB presence alone said "connected" there, and the
    start failed and was restarted for ever (found by review, 0.6.9).
    """
    cards = proc_root / "asound" / "cards"
    # Against every card, not the configured model: a desk whose
    # name is wrong for the box it names must fail loudly, not report the
    # box as unplugged. And only when the list can be read at all.
    absent = not usb_device_present(config.usb_id, sysfs_usb) or (
        bool(config.serial) and cards.is_file()
        and config.serial not in device_serials(cards))
    if absent:
        log.info("device %s%s not connected; nothing to do", config.usb_id,
                 " with serial %s" % config.serial if config.serial else "")
        sd_notify("READY=1")  # Type=notify: a clean no-op start
        return EXIT_OK
    if not usb_device_authorized(config.usb_id, sysfs_usb):
        log.error("USB device %s is connected but the kernel has not "
                  "authorized it (authorized=0 in sysfs -- USBGuard, a "
                  "policy?); no driver binds until it is, and the unit "
                  "retries", config.usb_id)
        return EXIT_FAILURE
    log.error(
        "USB device %s is connected but no ALSA sequencer client named %r%s "
        "appeared within %.0fs -- is snd-usb-audio loaded?",
        config.usb_id, config.device_name,
        " with serial %s" % config.serial if config.serial else "",
        args.timeout,
    )
    return EXIT_FAILURE


def _apply_or_fail(child: "subprocess.Popen[bytes]", config: Config,
                   stop_requested: Dict[str, bool], config_path: Optional[Path]
                   ) -> Tuple[Optional[threading.Thread], Optional[int]]:
    """Apply the desk and signal READY, or stop the backend and say why.

    The service is "started" when the backend is up and the routing is
    applied -- and only then. READY=1 used to follow a lock refusal too,
    telling systemd the desk was set while nothing had been written
    (0.6.7, 0.6.8). Returns the verifier thread, or the exit code of a
    start that failed: a held lock is a wait, not a desk, and systemd
    retries after RestartSec (ADR 0022, ADR 0024); a socket the backend
    cannot be reached through was a traceback until 0.6.10.
    """
    try:
        verifier = _apply_and_verify(child, config, stop_requested, config_path)
    except DeviceLockUnavailable:
        log.error("failing the start so systemd tries again")
    except OSError as exc:
        log.error("cannot write to the backend on UDP %d (%s); failing the "
                  "start", config.osc_port, exc)
    else:
        sd_notify("READY=1")
        return verifier, None
    _stop_child(child)
    return None, EXIT_FAILURE


def run_session(args: argparse.Namespace, config: Config) -> int:
    """Discover the device, run the backend, and supervise it."""
    # The desk a start looks for an interface with, or a dry run shows --
    # after --device. Before the search: a dry run ends there, and a start
    # that finds no interface is no reason to say less.
    log_desk_notices(config)
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    sysfs_usb = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))

    interface, code = _find_client(args, config, proc_root, sysfs_usb)
    if interface is None or interface.client is None:
        if args.dry_run and code != EXIT_CONFIG:
            # Which interface is a question a dry run cannot answer here;
            # what would be written to it is one it can. The exit code
            # stays the one a start would have ended with.
            _print_dry_run(None, config)
        return code
    client, config = interface.client, replace(config, serial=interface.serial)
    _firmware_notice(config, sysfs_usb)

    if args.dry_run:
        _print_dry_run(client, config)
        return EXIT_OK

    if _cleanup_stale_backend(config.osc_port, proc_root) is not None:
        # Two sessions on one port would take turns killing each other's
        # backend (0.6.9, measured). The one that is already running keeps
        # the desk; this one says why it stops. Exit 2 rather than 1: a
        # restart cannot fix it, and RestartPreventExitStatus keeps a
        # unit started beside a manual session from looping on it.
        return EXIT_CONFIG
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
        verifier, failed = _apply_or_fail(child, config, stop_requested,
                                          _config_path(args))
        if failed is not None:
            return failed

    returncode = supervise(child, stop_requested,
                           on_reload=lambda: _reconcile(args, config,
                                                        stop_requested,
                                                        verifier),
                           reload_requested=reload_requested)
    _await_verifier(verifier)
    return _exit_code_for(returncode, config, sysfs_usb, stop_requested)


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
