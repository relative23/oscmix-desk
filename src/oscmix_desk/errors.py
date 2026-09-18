"""Exception types shared across the package."""

from __future__ import annotations


class ConfigError(Exception):
    """A problem in routing.conf that the user has to fix."""


class DeviceAmbiguous(ConfigError):
    """More than one interface fits the config, and nothing says which.

    A config error because the remedy is a line in `routing.conf`:
    `[device] serial` names the box. Guessing is not an option -- the
    first match is whichever box the kernel enumerated first, so the desk
    would land on an arbitrary interface and its lock would name the
    other one (ADR 0024).
    """


class DeviceLockUnavailable(Exception):
    """The device lock could not be held, so nothing may be written.

    Raised rather than returned as None, because None already means
    "nothing to apply" or "a stop arrived", and the start treated all
    three alike: it sent READY=1 for a desk it never wrote (ADR 0024).
    """


class ReceivePortError(OSError):
    """The receive port cannot be bound, and not because somebody holds it.

    ``Backend.listen`` answers None for the one failure that is a normal
    state: EADDRINUSE, the mixer GUI has the port. Until 0.6.11 it
    answered None for every other OSError as well, and every caller then
    said "in use -- close the mixer GUI" about a port nothing held.
    Measured: ``[osc] recv-port = 80`` fails with EACCES for an ordinary
    user, and the desk ran unverified for good under a message that named
    the wrong cause.

    An OSError, so the handlers the verifier and the reconcile already
    have for a socket they cannot use apply to it. It is never allowed to
    end an apply half-way: the link barrier catches it and waits blind,
    because the links are on the wire by then (ADR 0025).
    """
