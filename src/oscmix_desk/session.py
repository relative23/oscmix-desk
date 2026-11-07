"""The session lifecycle: discover, launch, apply, signal, supervise.

Startup retains one Desk operation through apply and background repair.
Supervision handles signals only after that ownership has been established.
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

from .constants import (
    CHILD_STOP_GRACE,
    EXIT_CONFIG,
    EXIT_FAILURE,
    EXIT_OK,
    PORT_READY_TIMEOUT,
    VERIFIER_STOP_GRACE,
    VERIFY_SETTLE,
)
from .desk import Desk, _operation
from .devices import device_for_name
from .discovery import Device as Interface
from .discovery import (
    device_serials,
    lock_key,
    resolve_binary,
    usb_device_authorized,
    usb_device_present,
    usb_revision,
    wait_for_device,
)
from .errors import (
    BackendControlError,
    ConfigError,
    DeviceAmbiguous,
    DeviceLockUnavailable,
    WriteFailed,
)
from .locking import control_path
from .log import log
from .model import Config
from .notices import log_desk_notices
from .notify import sd_notify
from .process import _cleanup_stale_backend, control_socket_owner, supervise
from .reconcile import ApplyIntent, application_plan, desired
from .reload import _config_path, _desk_under_the_lock, _reconcile, watched_desk
from .watch import Watch, reader


def _print_dry_run(client: int | None, config: Config) -> None:
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
    print("would run: alsaseqio -x %s:1 oscmix -c <device control socket>"
          % ("<client>" if client is None else client))
    for path, types, values in application_plan(config, ApplyIntent.INITIAL).messages():
        print("would send: %s ,%s %s"
              % (path, types, " ".join(map(str, values))))


def _start_backend(client: int, config: Config, config_path: Path | None = None
                    ) -> subprocess.Popen[bytes] | None:
    """Launch ``alsaseqio <client>:1 oscmix``, or None if a binary is missing."""
    alsaseqio = resolve_binary("alsaseqio", "OSCMIX_BIN_ALSASEQIO")
    backend = resolve_binary("oscmix", "OSCMIX_BIN_BACKEND")
    if alsaseqio is None or backend is None:
        log.error("alsaseqio/oscmix not found -- run install.sh first")
        return None

    endpoint = control_path(config_path, lock_key(config.usb_id, config.serial))
    endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [alsaseqio, "-x", "%d:1" % client, backend, "-c", str(endpoint)]
    log.info("starting: %s", " ".join(command))
    return subprocess.Popen(command)


def _install_stop_handlers(child: subprocess.Popen[bytes],
                           stop_requested: dict[str, bool]) -> None:
    """Turn SIGTERM/SIGINT into an orderly backend shutdown."""
    def handle_stop(signum: int, _frame: object) -> None:
        stop_requested["stop"] = True
        log.info("received %s; stopping backend", signal.Signals(signum).name)
        sd_notify("STOPPING=1")
        if child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)


def _install_reload_handler(reload_requested: dict[str, bool]) -> None:
    """Turn SIGHUP into a request to reconcile, not into a reconcile.

    The handler sets a flag and returns. Everything a reconcile does --
    reading a config off disk, acquiring a writer lease, waiting out the link
    barrier -- is forbidden here: a signal handler runs between two
    bytecodes of whatever was executing, and doing that work in place
    means doing it *inside* the apply it was meant to follow.

    The supervise loop picks the flag up within its poll interval, which
    is the only place in this process where nothing else is half-done.
    ``sighup`` tells it from a request the watcher made (`_serve_reload`).
    """
    def handle_reload(_signum: int, _frame: object) -> None:
        reload_requested["reload"] = reload_requested["sighup"] = True

    signal.signal(signal.SIGHUP, handle_reload)


def _await_backend_port(child: subprocess.Popen[bytes], endpoint: Path,
                        proc_root: Path) -> bool:
    """Wait for this child's listening control socket, without connecting."""
    deadline = time.monotonic() + PORT_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            owner = control_socket_owner(endpoint, proc_root)
            if owner == child.pid:
                log.info("oscmix is listening at %s", endpoint)
                return True
            if owner is not None:
                log.error("control endpoint %s is held by pid %d, not started backend %d",
                          endpoint, owner, child.pid)
                return False
        except (OSError, ValueError) as exc:
            log.error("cannot inspect backend readiness: %s", exc)
            return False
        if child.poll() is not None:
            return False
        time.sleep(0.25)
    log.error("oscmix did not bind %s within %.0fs", endpoint, PORT_READY_TIMEOUT)
    return False


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    """Stop a backend this process started and will not use."""
    try:
        child.terminate()
        child.wait(timeout=CHILD_STOP_GRACE)
    except subprocess.TimeoutExpired:
        child.kill()
    except OSError:
        pass


def _apply_and_verify(child: subprocess.Popen[bytes], config: Config,
                      stop_requested: dict[str, bool],
                      config_path: Path | None = None, *, strict: bool = False
                      ) -> threading.Thread | None:
    """Transfer one checked operation from initial apply to its verifier."""
    operation = _operation(
        config, config_path, expected_pid=child.pid,
        reread=lambda running: _desk_under_the_lock(config_path, running, strict=strict),
        should_stop=lambda: _interrupted(child, stop_requested) is not None)
    try:
        operation._open()
        if not desired(operation.config):
            # Even an empty desk must identify the launched backend before
            # signalling readiness; it needs no write lease or verifier.
            log.info("nothing declared in the config; leaving mixer state untouched")
            operation._close()
            return None
        sd_notify("STATUS=applying routing")
        if not operation.apply(ApplyIntent.INITIAL):
            operation._close(finish=False)
            return None
        return _verify_in_background(child, stop_requested, operation)
    except DeviceLockUnavailable as exc:
        if _interrupted(child, stop_requested) is not None:
            log.info("%s while waiting for the device lock; nothing applied",
                     _interrupted(child, stop_requested))
            return None
        log.error("the device lock is not available (%s); not applying routing", exc)
        raise
    except BackendControlError:
        operation._close(finish=False)
        if _interrupted(child, stop_requested) is not None:
            log.info("%s; routing not completed", _interrupted(child, stop_requested))
            return None
        raise
    except BaseException:
        operation._close(finish=False)
        raise


def _interrupted(child: subprocess.Popen[bytes],
                 stop_requested: dict[str, bool]) -> str | None:
    """Why the start is not to go on writing, or None.

    Two reasons, told apart: until 0.8.1 a backend that died during the
    lock wait was logged as a stop request.
    """
    if stop_requested["stop"]:
        return "stop requested"
    if child.poll() is not None:
        return "the backend exited"
    return None


def _verify_in_background(child: subprocess.Popen[bytes],
                          stop_requested: dict[str, bool],
                          operation: Desk) -> threading.Thread:
    """Retain startup ownership until observation, repair and END finish."""
    def should_stop() -> bool:
        # A dead backend counts as a stop: its port is gone, so every
        # write from here would go nowhere and the log would claim a
        # re-apply that never landed anywhere.
        return stop_requested["stop"] or child.poll() is not None

    def deferred_verify() -> None:
        # STATUS= is what `systemctl --user status` shows: the phase the
        # session is in, and when the last one ended. It says what the
        # unit is doing; the lock is what keeps another writer out.
        verified = False
        try:
            with operation:
                if operation._wait(VERIFY_SETTLE):
                    return
                sd_notify("STATUS=verifying routing")
                complete = operation.repair()
                verified = True
                if should_stop():
                    return
                operation._finish()
            sd_notify("STATUS=running; verifier %s at %s"
                      % ("finished" if complete else "incomplete", time.strftime("%H:%M:%S")))
        except OSError as exc:
            if should_stop():
                # The stop cancels the operation (ECANCELED), or the backend
                # is gone; supervise() reports which. Logged as a verifier
                # failure until 0.8.1.
                log.info("verifier ended by the stop (%s)", exc)
                return
            _log_verifier_failure(exc, verified)
            sd_notify("STATUS=running; verifier failed at %s"
                      % time.strftime("%H:%M:%S"))
    thread = threading.Thread(target=deferred_verify, name="verify",
                              daemon=True)
    thread.start()
    return thread


def _log_verifier_failure(exc: OSError, verified: bool) -> None:
    """Say which of four things ended the start-up verifier.

    End this operation in every case; a later explicit operation must
    obtain a new checked connection. Until 0.8.1 the split was by
    exception type alone, so a repair the plan refused read as a lost
    connection, and a lease that did not end after a completed read-back
    read as routing that cannot be verified.
    """
    if verified:
        log.error("verifier read the routing back, but its backend operation "
                  "did not end cleanly (%s)", exc)
    elif isinstance(exc, WriteFailed) and exc.refused:
        log.error("verifier did not repair the routing: %s", exc)
    elif isinstance(exc, BackendControlError):
        # Failed feedback is distinct from silence.
        log.error("routing cannot be verified: %s", exc)
    else:
        # The socket, not the desk: a thread traceback said nothing a
        # person could act on, and the lock's release was all that
        # happened (0.6.10).
        log.error("verifier lost its backend operation (%s)", exc)


def _exit_code_for(returncode: int, config: Config, sysfs_usb: Path,
                   stop_requested: dict[str, bool]) -> int:
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
                 sysfs_usb: Path) -> tuple[Interface | None, int]:
    """The interface this desk is for: its sequencer client and its serial.

    Returns it, or None and the exit code the start ends with.
    Client and serial come from one resolution: `[device] serial` selects
    the box among identical ones, and without it more than one candidate
    is a configuration error, exit 2, which `RestartPreventExitStatus=2`
    keeps from becoming a restart loop. Until 0.6.9 the first matching
    client was bound and the serial worked out separately.

    The start pins the serial it is given here, because it is read again
    on every write, and the card list empties the moment the interface is
    unplugged: a reconcile in that window would otherwise take a
    different lock. Until 0.7.0 this function wrote it into
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
    desk was not the box that was measured. A
    notice and not a refusal: the read-back still verifies every register
    it can, and a firmware update must not take the desk down.
    """
    model = device_for_name(config.device_name)
    measured = None if model is None else model.firmware
    reported = usb_revision(config.usb_id, sysfs_usb, serial=config.serial)
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
    start failed and was restarted for ever.
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


def _apply_or_fail(child: subprocess.Popen[bytes], config: Config,
                   stop_requested: dict[str, bool], config_path: Path | None, *,
                   strict: bool = False
                   ) -> tuple[threading.Thread | None, int | None]:
    """Apply the desk and signal READY, or stop the backend and say why.

    The service is "started" when the backend is up and the routing is
    applied -- and only then. READY=1 used to follow a lock refusal too,
    telling systemd the desk was set while nothing had been written
    (0.6.7, 0.6.8). Returns the verifier thread, or the exit code of a
    start that failed: a held lock is a wait, not a desk, and systemd
    retries after RestartSec; a socket the backend
    cannot be reached through was a traceback until 0.6.10.
    """
    failure = EXIT_FAILURE
    try:
        verifier = _apply_and_verify(child, config, stop_requested, config_path, strict=strict)
    except ConfigError as exc:
        log.error("cannot apply the re-read desk (%s); failing the start", exc)
        failure = EXIT_CONFIG
    except DeviceLockUnavailable:
        log.error("failing the start so systemd tries again")
    except OSError as exc:
        interrupted = _interrupted(child, stop_requested)
        if interrupted is None:
            log.error("cannot complete backend writes (%s); failing the start", exc)
        else:
            # Not a failed start: the stop, or the backend's own exit (an
            # unplug, most often), is the outcome, and supervise() reports
            # it as it would a minute later. Until 0.8.1 both were exit 1.
            log.warning("%s during the start; routing not completed (%s)", interrupted, exc)
            return None, None
    else:
        if _interrupted(child, stop_requested) is None:
            sd_notify("READY=1")
        return verifier, None
    _stop_child(child)
    return None, failure


def run_session(args: argparse.Namespace, config: Config) -> int:
    """Discover the device, run the backend, and supervise it."""
    config_path = _config_path(args)
    # The desk a start looks for an interface with, or a dry run shows --
    # after --device. Before the search: a dry run ends there, and a start
    # that finds no interface is no reason to say less.
    log_desk_notices(config)
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    sysfs_usb = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))

    # Native supervisors can reload while the interface is still absent.
    # Keep that request for the ordinary post-start reconciliation point;
    # the default SIGHUP action would kill the discovery process instead.
    reload_requested = {"reload": False}
    if not args.dry_run:
        _install_reload_handler(reload_requested)
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

    try:
        endpoint = control_path(config_path, interface.key)
        if _cleanup_stale_backend(endpoint, interface, proc_root) is not None:
            return EXIT_CONFIG
        child = _start_backend(client, config, config_path)
    except OSError as exc:
        log.error("cannot start the coordinated backend: %s", exc)
        return EXIT_FAILURE
    if child is None:
        return EXIT_FAILURE

    stop_requested = {"stop": False}
    _install_stop_handlers(child, stop_requested)
    if not _await_backend_port(child, endpoint, proc_root) \
            and usb_device_present(config.usb_id, sysfs_usb):
        # The control endpoint never became ready while the device is
        # still present. Refuse startup instead of mapping an early clean
        # backend exit to a ready desk. Device removal remains a clean
        # no-op; an absent endpoint cannot carry an apply operation.
        log.error("backend never bound its control socket; failing the start")
        if child.poll() is None:
            _stop_child(child)
        return EXIT_FAILURE

    verifier: threading.Thread | None = None
    watch: Watch | None = None
    if child.poll() is None:
        watch = _start_watch(child, config, config_path, args, stop_requested, reload_requested)
        verifier, failed = _apply_or_fail(child, config, stop_requested,
                                          config_path, strict=getattr(args, "strict", False))
        if failed is not None:
            return failed

    returncode = supervise(child, stop_requested,
                           on_reload=lambda: _serve_reload(args, config, stop_requested,
                                                           verifier, reload_requested, watch),
                           reload_requested=reload_requested)
    _await_verifier(verifier)
    return _exit_code_for(returncode, config, sysfs_usb, stop_requested)


def _start_watch(child: subprocess.Popen[bytes], config: Config,
                 config_path: Path | None, args: argparse.Namespace,
                 stop_requested: dict[str, bool],
                 reload_requested: dict[str, bool]) -> Watch | None:
    """Watch the device for `[desk] enforce = watch`, from before the first apply.

    Connected before the start-up operation so the watcher learns the link
    and AutoSet flags from its dump. It never writes: a contradiction sets
    the flags supervise() serves, as SIGHUP does.
    """
    if config.enforce != "watch":
        return None

    def should_stop() -> bool:
        return stop_requested["stop"] or child.poll() is not None

    def request(_paths: tuple[str, ...]) -> None:
        reload_requested["watch"] = True
        reload_requested["reload"] = True

    strict = getattr(args, "strict", False)
    watch = Watch(reader(config, config_path, child.pid),
                  watched_desk(config, config_path, strict=strict), request, should_stop)
    threading.Thread(target=watch.run, name="watch", daemon=True).start()
    log.info("watching the device for changes to what the desk pins ([desk] enforce = watch)")
    return watch


def _serve_reload(args: argparse.Namespace, config: Config,
                  stop_requested: dict[str, bool], verifier: threading.Thread | None,
                  reload_requested: dict[str, bool], watch: Watch | None) -> None:
    """One reconcile for every pending request; a SIGHUP among them wins.

    A reload also watches again what the watcher left to the device.
    """
    sighup = reload_requested.pop("sighup", False)
    watched = reload_requested.pop("watch", False)
    if not sighup and not watched:
        return  # the watcher raised the flag again for a request already served
    if not sighup:
        _reconcile(args, config, stop_requested, verifier, reason="watch")
        return
    if watch is not None:
        watch.forgive()
    _reconcile(args, config, stop_requested, verifier)


def _await_verifier(verifier: threading.Thread | None) -> None:
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
