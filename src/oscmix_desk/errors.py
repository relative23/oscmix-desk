"""Exception types shared across the package."""

from __future__ import annotations

from collections.abc import Sequence


class ConfigError(Exception):
    """A problem in routing.conf that the user has to fix."""


class DeviceAmbiguous(ConfigError):
    """More than one interface fits the config, and nothing says which.

    A config error because the remedy is a line in `routing.conf`:
    `[device] serial` names the box. Guessing is not an option -- the
    first match is whichever box the kernel enumerated first, so the desk
    would land on an arbitrary interface and its lock would name the
    other one.
    """


class DeviceLockUnavailable(Exception):
    """The device lock could not be held, so nothing may be written.

    Raised rather than returned as None, because None already means
    "nothing to apply" or "a stop arrived", and the start treated all
    three alike: it sent READY=1 for a desk it never wrote.
    """


class BackendControlError(OSError):
    """The coordinated backend operation cannot continue with valid observations.

    Covers disconnect, cancellation, malformed deliveries, acknowledgement
    deadlines, queue overflow and an unavailable observation window.
    An OSError, so existing handlers can name the failure. During
    an apply it becomes WriteFailed with exact sent and pending paths;
    a failed receiver must not erase a known link contradiction by falling
    back to blind writes.
    """


#: Compatibility name from the UDP receiver; the same exception class.
ReceivePortError = BackendControlError


class WriteFailed(OSError):
    """The wire gave out, and this is how far the write had come.

    ``written`` are the register paths that were handed to the kernel
    before the failure, in order; ``unwritten`` the ones that were not.
    A submitted request whose acknowledgement is lost may already have
    reached the device. It is the difference between "nothing happened" and
    "the desk is somewhere between two configs", which is the one thing a
    person at that desk needs to know.

    All operation logs include these lists. A profile result also exposes
    them as structured fields. Neither submission nor the backend's ACK
    establishes that the hardware applied the value.
    """

    def __init__(self, cause: OSError, written: Sequence[str],
                 unwritten: Sequence[str]) -> None:
        super().__init__(cause.errno, cause.strerror or str(cause))
        self.written = tuple(written)
        self.unwritten = tuple(unwritten)

    @property
    def refused(self) -> bool:
        """This program stopped the write, not the connection.

        A remembered value the plan cannot keep, a link it may not trust,
        a playback mode that changed: raised as an OSError without an
        errno, before the next burst. The connection is still usable.
        """
        return self.errno is None

    def __str__(self) -> str:
        # A refusal the plan made has no errno; OSError would print
        # "[Errno None]" in front of it.
        cause = self.strerror if self.errno is None else super().__str__()
        return ("%s; sent (hardware unconfirmed): %s; pending: %s"
                % (cause, ", ".join(self.written) or "none",
                   ", ".join(self.unwritten) or "none"))
