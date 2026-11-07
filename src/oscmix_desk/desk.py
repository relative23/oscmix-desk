"""One operation owns the checked connection, lease and device lock.

Successful writers acknowledge END before committing state. Failed operations
close without replay; disconnect releases any backend lease. The file lock is
retained until the connection is closed. Reader operations take neither lock
nor write lease and cannot submit writes.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import TracebackType

from .autogain import dependency_paths
from .backend import Control, connect_backend
from .connectcheck import ConnectCheck, check_at_connect
from .constants import VERIFY_TIMEOUT
from .devices import device_for_name
from .discovery import Device as Interface
from .discovery import resolve_device, usb_device_present
from .errors import BackendControlError, ConfigError, DeviceLockUnavailable, WriteFailed
from .journal import Written, journal_path, record_written
from .locking import DeviceLock, device_lock
from .log import log
from .model import Config
from .osc import Args, Message
from .paths import discover_config_path
from .reconcile import ApplyIntent, desired
from .routing import apply_routing, never_stop, wait_unless_stopped
from .seen import NOTED, record_seen
from .verify import (
    VerifyResult,
    expected_registers,
    reconcile_now,
    verify_and_repair,
    verify_routing,
)


class Desk:
    """A single coordinated operation, used with ``open_desk``.

    Config and config_path are read-only. A writer pins its resolved serial
    before acquiring the file lock. The connection is checked under that
    lock; the first apply, verify or repair acquires its write lease.
    Re-entry and reuse after finishing or failing are refused.
    """

    def __init__(self, config: Config, config_path: Path | None = None) -> None:
        self._config = config
        self._config_path = config_path
        self._backend: Control | None = None
        self._lock: DeviceLock | None = None
        self._reader = False
        self._resolved = False
        self._expected_pid: int | None = None
        self._reread: Callable[[Config], Config] | None = None
        self._should_stop: Callable[[], bool] = never_stop
        self._opened = self._entered = self._closed = False
        self._lease = self._finished = self._failed = self._aborted = False
        self._journal: Path | None = None
        self._written: Written = {}
        # What the record beside the journal keeps: CC reports and the start's connect check.
        self._seen: Path | None = None
        self._reported: dict[str, tuple[str, Args, float]] = {}
        self._connected: ConnectCheck | None = None

    @property
    def config(self) -> Config:
        """The desk with this operation's pinned device identity."""
        return self._config

    @property
    def config_path(self) -> Path | None:
        """The selected config path, discovered at most once."""
        return self._config_path

    def __enter__(self) -> Desk:
        if self._entered or self._closed:
            raise RuntimeError("a Desk is one operation and cannot be entered again")
        self._open()
        self._entered = True
        return self

    def __exit__(self, _exc_type: type[BaseException] | None,
                 exc: BaseException | None, _traceback: TracebackType | None) -> None:
        if exc is None:
            self._close()
        else:
            try:
                self._close(finish=False)
            except OSError as cleanup_error:
                log.warning("backend operation cleanup failed: %s", cleanup_error)

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("a closed Desk cannot be reopened")
        if self._opened:
            return
        try:
            self._check_stop()
            desired(self.config)
            if not self._reader:
                self._lock_device()
            if self._backend is None:
                if self._expected_pid is not None and not desired(self.config):
                    self._reader = True
                self._backend = connect_backend(
                    self.config, self.config_path, reader=self._reader,
                    expected_pid=self._expected_pid, should_stop=self._should_stop)
            self._opened = True
        except BaseException:
            try:
                self._close(finish=False)
            except OSError as cleanup_error:
                log.warning("backend operation cleanup failed: %s", cleanup_error)
            raise

    def _lock_device(self) -> None:
        """Pin the writer and re-read its desk inside the acquired lock."""
        # A session has already resolved and pinned the interface
        # before launching this exact backend. Other writers resolve
        # it here; the checked connection verifies it again.
        target = (Interface(self.config.usb_id, self.config.serial, None)
                  if self._resolved else
                  _target(self.config, reach=self._backend is None))
        if target.serial != self.config.serial:
            self._config = replace(self.config, serial=target.serial)
        lock = device_lock(self.config_path, target.key,
                           should_stop=self._should_stop)
        if not isinstance(lock, DeviceLock):
            raise DeviceLockUnavailable(lock)
        self._lock = lock
        self._journal = journal_path(self.config_path, target.key)
        self._seen = journal_path(self.config_path, target.key, kind="seen")
        self._check_stop()
        if self._reread is not None:
            fresh = self._reread(self.config)
            desired(fresh)
            if ((fresh.device_name, fresh.usb_id, fresh.serial) !=
                    (self.config.device_name, self.config.usb_id, self.config.serial)):
                raise ConfigError("a desk re-read cannot change the locked device")
            self._config = fresh

    def _check_stop(self) -> None:
        if self._should_stop():
            self._failed = self._aborted = True
            raise BackendControlError(errno.ECANCELED, "backend operation cancelled")

    @property
    def _control(self) -> Control:
        if (not self._opened or self._closed or self._finished or self._failed
                or self._backend is None):
            raise BackendControlError(errno.ENOTCONN, "Desk operation is no longer usable")
        self._check_stop()
        return self._backend

    def _begin(self) -> Control:
        backend = self._control
        if self._reader:
            raise OSError(errno.EPERM, "a reader Desk cannot acquire a write lease")
        if not self._lease:
            try:
                backend.begin()
            except OSError:
                self._failed = self._aborted = True
                raise
            self._lease = True
            backend.on_written = self._wrote
            backend.on_reported = self._saw
        return backend

    def _wrote(self, path: str, tags: str, args: Args) -> None:
        """One acknowledged write, kept for the journal until it is next stored."""
        self._written[path] = (tags, args)

    def _saw(self, report: Message) -> None:
        """A device report the record keeps (CC Mode, CC Mix), with when it was seen."""
        path, tags, args = report
        if path in NOTED:
            self._reported[path] = (tags, tuple(args), time.time())

    def _record(self) -> None:
        """Store the acknowledged writes so far; the held lock keeps other writers out."""
        written, self._written = self._written, {}
        record_written(self._journal, written)
        reported, self._reported = self._reported, {}
        connected, self._connected = self._connected, None
        record_seen(self._seen, reported, None if connected is None else connected.record())

    def apply(self, intent: ApplyIntent = ApplyIntent.EXPLICIT, *,
              reason: str = "desk") -> bool:
        """Apply the declared state under the selected PIN/REMEMBER intent.

        RECONCILE observes first to check link dependencies. A submission
        failure raises WriteFailed with the sent and pending paths; this
        object cannot replay them. True means the selected plan completed.
        """
        if not isinstance(intent, ApplyIntent):
            raise TypeError("intent must be an ApplyIntent")
        backend = self._begin()
        try:
            if intent == ApplyIntent.RECONCILE:
                complete = reconcile_now(self.config, reason, backend, self._should_stop)
            else:
                self._apply(backend, intent)
                complete = True
        except OSError:
            self._failed = self._aborted = True
            raise
        finally:
            # Now, not only at the end: a start's verifier can be cut off at exit.
            self._record()
        if self._should_stop():
            self._failed = self._aborted = True
            return False
        return complete

    def _apply(self, backend: Control, intent: ApplyIntent) -> None:
        """A start compares the device's input mix first (``connectcheck``), then applies."""
        observed = None
        if intent == ApplyIntent.INITIAL:
            self._connected, observed = check_at_connect(self.config, backend, self._should_stop)
        if observed is None:
            apply_routing(self.config, backend, intent=intent, should_stop=self._should_stop)
        else:
            apply_routing(self.config, backend, intent=intent, observed=observed,
                          should_stop=self._should_stop)

    def verify(self) -> VerifyResult:
        """Read back every declared value, including REMEMBER expectations."""
        try:
            return verify_routing(expected_registers(self.config), self._begin(),
                                  VERIFY_TIMEOUT, should_stop=self._should_stop,
                                  device_model=device_for_name(self.config.device_name),
                                  dependency_paths=dependency_paths(self.config))
        except OSError:
            if not self._reader:
                self._failed = True
            raise

    def repair(self) -> bool:
        """Verify and permit at most one PIN repair, retaining REMEMBER values."""
        try:
            return verify_and_repair(self.config, self._begin(), self._should_stop)
        except OSError as exc:
            if not self._reader:
                self._failed = True
                self._aborted = self._aborted or isinstance(exc, WriteFailed)
            raise

    def _wait(self, seconds: float) -> bool:
        stopped = wait_unless_stopped(seconds, self._should_stop, self._control)
        if stopped:
            self._failed = self._aborted = True
        return stopped

    def _finish(self) -> None:
        """Acknowledge END once, retaining the file lock for marker commit."""
        if self._lease:
            self._lease = False
            self._finished = True
            if self._backend is not None:
                self._backend.finish()

    def _close(self, *, finish: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if finish and not self._aborted:
                self._finish()
        finally:
            try:
                self._disconnect()
            finally:
                try:
                    self._record()
                finally:
                    if self._lock is not None:
                        self._lock.release()

    def _disconnect(self) -> None:
        """Close once; profiles retain the file lock through marker commit."""
        backend, self._backend = self._backend, None
        if backend is not None:
            backend.on_written = backend.on_reported = None
            backend.close()


def open_desk(config: Config, config_path: Path | None = None) -> Desk:
    """Prepare one operation; entering it checks the interface and backend.

    Use ``with open_desk(load_config(path), path) as desk``. Successful exit
    acknowledges the lease end; any path closes the connection and releases
    the file lock. The installed service and profile marker are not changed.
    """
    return Desk(config, config_path or discover_config_path())


def _operation(config: Config, config_path: Path | None = None, *,
               backend: Control | None = None, reader: bool = False,
               resolved: bool = False,
               expected_pid: int | None = None,
               reread: Callable[[Config], Config] | None = None,
               should_stop: Callable[[], bool] = never_stop) -> Desk:
    """The runtime's operation, with an already-owned connection or startup PID."""
    operation = Desk(config, config_path)
    operation._backend = backend
    operation._reader = reader
    operation._resolved = resolved or expected_pid is not None
    operation._expected_pid = expected_pid
    operation._reread = reread
    operation._should_stop = should_stop
    return operation


def _target(config: Config, reach: bool) -> Interface:
    """Resolve a writer's lock identity; the connection checks it again."""
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    device = resolve_device(config.usb_id, config.device_name, config.serial, proc_root)
    if reach:
        sysfs = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))
        if not usb_device_present(config.usb_id, sysfs):
            raise OSError(errno.ENODEV, "%s is not connected" % config.usb_id)
        if device.client is None or not device.serial:
            raise OSError(errno.ENODEV,
                          "the selected interface and its serial are not visible to ALSA")
    return device
