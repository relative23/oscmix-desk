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
