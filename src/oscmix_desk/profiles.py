"""Switching the whole desk to a named config, with a stated outcome.

A profile is a complete ``routing.conf`` in ``profiles/`` beside the
main one. Not a new section type: a profile *is* a config, parsed by the
same code, subject to the same compatibility rule (ADR 0006), and
``--dump-config > profiles/tracking.conf`` composes for free.

The design constraint is the desk, not the file format. Switching
happens while someone is listening, and there is no rollback for a
mixer: once ``/output/1/volume`` is on the wire, the monitors are loud.
So the order is fixed and the whole module is arranged around it --
**parse and validate everything, then write, then check.** A config that
cannot be understood costs an error message and not one datagram.

That is why this states an outcome rather than raising (``outcome``):
applied and verified, applied and unverified, refused, or written in
part. A partial write names the submitted and unsent registers and
leaves the active marker unchanged (ADR 0027). It cannot promise a
hardware rollback.

The order of a switch is this module's, and only this module's: which
interface and backend it may write to, the lock taken (``locking``), the
write, the profile remembered (``marker``), the read-back.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .backend import Control, connect_backend
from .config import load_config
from .constants import VERIFY_TIMEOUT
from .devices import device_for_name
from .discovery import (
    Device,
    resolve_device,
    usb_device_present,
)
from .errors import (
    ConfigError,
    DeviceAmbiguous,
    WriteFailed,
)
from .locking import _switch_lock, held_elsewhere
from .log import log
from .marker import (
    active_profile,
    forget_active_profile,
    remember_active_profile,
)
from .model import CommandLine, Config, Machine
from .notices import log_desk_notices
from .outcome import (
    APPLIED_UNVERIFIED,
    APPLIED_VERIFIED,
    NOT_CHECKED,
    REFUSED,
    WRITTEN_IN_PART,
    Outcome,
)
from .paths import list_profiles, profile_path
from .reconcile import ApplyIntent
from .routing import apply_routing
from .verify import expected_registers, register_ever_reported, verify_routing


def load_profile(name: str, config_path: Optional[Path] = None,
                 said: Optional[CommandLine] = None) -> Config:
    """Parse a profile, or raise ``ConfigError``.

    Separate from :func:`switch_profile` so the refusal path can be
    tested, and used, without a device anywhere near it -- that is what
    makes ``--dry-run`` on a profile honest.

    **Transport settings come from the main config, not the profile.**
    A profile describes the desk: what is routed where, and at what
    level. The OSC ports and the device name describe the *machine*, and
    a profile that had to restate them would be wrong the moment it was
    copied between two machines -- or, worse, silently right. That is
    not hypothetical: a profile with no ``[osc]`` section fell back to
    the compiled-in default 7222 during development and wrote to a live
    Fireface from a unit test, because the default happened to match.

    **A profile that names another machine is refused** (ADR 0026, since
    0.7.0; 0.6.11 warned). Until then it won, and one persisted profile
    meant three targets: its own backend for the switch, the running
    session's for the reload the switch sent, its own again after a
    restart -- with two device locks over one marker. Restating
    ``routing.conf``'s own values is not that: ``--dump-config >
    profiles/x.conf``, the documented way to make a profile, writes
    ``[device]`` and ``[osc]`` into every one.
    """
    path = profile_path(name, config_path)
    if not path.is_file():
        raise ConfigError("no profile %r (looked in %s)" % (name, path.parent))
    # Read *onto* the machine settings rather than patched with them
    # afterwards. Patched, the profile was validated while it still named
    # the default device: on a desk for another interface its channels
    # were checked against the UCX II's twenty, and a channel section the
    # main config has ignored with a warning since 0.6.2 was accepted
    # through the UCX II's table and written (0.6.11). The parser takes
    # every machine setting with the value it finds as the fallback, so a
    # profile that states one still wins.
    if config_path is None or not Path(config_path).is_file():
        return load_config(path, said=said)
    main = load_config(config_path, said=said)
    # Onto what routing.conf itself says, not onto what the command line
    # made of it: the comparison below is between the two files, and the
    # command line is put over both alike.
    onto = replace(Config(), **(main.loaded or Machine(
        main.device_name, main.usb_id, main.serial, main.osc_port,
        main.osc_recv_port))._asdict())
    profile = load_config(path, onto, said)
    theirs, ours = profile.loaded, main.loaded
    if theirs is not None and ours is not None and theirs != ours:
        raise ConfigError(
            "profile %r names another backend or interface than %s -- %s. A "
            "profile is the desk, not the machine (ADR 0026): take [osc] "
            "and [device] out of %s"
            % (name, config_path, theirs.differs_from(ours), path))
    return profile


#: Everything in a config that describes the *machine* rather than the
#: desk, as (section, option, Config attribute). A profile inherits each
#: of these unless it states it itself.
#:
#: A table rather than a list of ifs, because the failure mode is
#: forgetting one: `usb-id` was left out of the first version and would
#: have silently reverted to the compiled-in default on any machine that
#: sets it. `tests/test_profile_machine.py` holds this table against the
#: set of fields on Config that no `[route]` or channel section can
#: write, so a new machine-level setting cannot be added without landing
#: here too.
MACHINE_SETTINGS = (
    ("osc", "port", "osc_port"),
    ("osc", "recv-port", "osc_recv_port"),
    ("device", "name", "device_name"),
    ("device", "usb-id", "usb_id"),
    ("device", "serial", "serial"),
)


def keep_machine_settings(desk: Config, running: Config) -> Config:
    """``desk``, with the machine-level settings of the process that runs.

    The backend is bound and talking to one interface; a desk re-read
    under it -- by the start once it holds the lock, by a SIGHUP -- changes
    the routing and nothing in ``MACHINE_SETTINGS``. The session spelled
    the five assignments out twice until 0.6.11, which is the way one
    gets forgotten: the table is what a test holds against ``Config``.

    ``load_profile`` is the third caller, with the main config as
    ``running`` and an empty ``Config`` as the desk: what a profile is
    read onto.
    """
    kept = {attr: getattr(running, attr)
            for _section, _option, attr in MACHINE_SETTINGS}
    # Where two of those came from goes with them: the kept desk is
    # resolved for this command line as much as the running one is.
    return replace(desk, overrides=running.overrides, **kept)


class _Refused(Exception):
    """Why a writer will not write, carried to the outcome."""


def _proc_root() -> Path:
    return Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))


def _target(config: Config, reach: bool) -> Device:
    """Resolve the lock identity before waiting; the connection checks it again."""
    try:
        device = resolve_device(config.usb_id, config.device_name,
                                config.serial, _proc_root())
    except DeviceAmbiguous as exc:
        raise _Refused(str(exc)) from exc
    if reach:
        sysfs = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))
        if not usb_device_present(config.usb_id, sysfs):
            raise _Refused("%s is not connected" % config.usb_id)
        if device.client is None or not device.serial:
            raise _Refused("the selected interface and its serial are not visible to ALSA")
    return device


def _refused_for_the_device(name: str, reason: str) -> "Outcome":
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


def _refused_for_the_lock(name: str) -> Outcome:
    reason = held_elsewhere()
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


def effective_config(config_path: Optional[Path],
                     said: Optional[CommandLine] = None
                     ) -> Tuple[Config, Optional[str]]:
    """The desk a start applies, and the profile it came from.

    The active profile when one is remembered and loads, `routing.conf`
    otherwise. Every path that asks "what desk did you declare" -- the
    start, SIGHUP, --dry-run, --diff -- asks here, so they cannot
    disagree about it.

    Raises ConfigError only for `routing.conf` itself: a main config
    that does not parse is exit 2 as it always was. A remembered profile
    that does not load is a warning and a fallback, because a refused
    start over a file nobody edited is the failure ADR 0006 exists to
    prevent; the marker stays, so the warning stays until somebody
    decides (ADR 0018).
    """
    main = load_config(config_path, said=said)
    name = active_profile(config_path)
    if name is None:
        return main, None
    try:
        profile = load_profile(name, config_path, said)
    except ConfigError as exc:
        log.warning("active profile %r is not usable (%s); applying %s "
                    "instead -- fix the file, or `--no-profile`",
                    name, exc, config_path)
        return main, None
    log.info("active profile %r (%s) is in effect; the routes and channel "
             "state in %s are not", name, profile_path(name, config_path),
             config_path)
    return profile, name


def switch_profile(name: str, config_path: Optional[Path] = None,
                   backend: Optional[Control] = None,
                   verify: bool = True) -> Outcome:
    """Parse a profile fully, then apply it as one coordinated operation."""
    try:
        config = load_profile(name, config_path)
    except ConfigError as exc:
        return _refused_for_the_device(name, str(exc))
    return _activate(config, config_path, name, backend, verify)


def restore_main(config_path: Optional[Path] = None,
                 backend: Optional[Control] = None,
                 verify: bool = True) -> Outcome:
    """Apply the main desk; clear the marker only after the operation finishes."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return _refused_for_the_device("routing.conf", str(exc))
    return _activate(config, config_path, None, backend, verify)


def _activate(config: Config, config_path: Optional[Path], name: Optional[str],
              backend: Optional[Control], verify: bool) -> Outcome:
    label = name if name is not None else "routing.conf"
    log_desk_notices(config)
    try:
        target = _target(config, reach=backend is None)
    except _Refused as refusal:
        return _refused_for_the_device(label, str(refusal))
    config = replace(config, serial=target.serial)
    with _switch_lock(config_path, target.key) as held:
        if not held:
            return _refused_for_the_lock(label)
        device = backend
        applied = False
        try:
            if device is None:
                device = connect_backend(config, config_path)
            device.begin()
            gave_out = _written(label, config, device)
            if gave_out is not None:
                return gave_out
            applied = True
            if verify:
                outcome = _check(label, config, device)
            else:
                outcome = Outcome(state=APPLIED_UNVERIFIED, name=label,
                                  reason=NOT_CHECKED,
                                  unverified=sorted(expected_registers(config)),
                                  read_back=False)
            # A dead/lost lease invalidates observations and cannot publish a
            # successful marker, even when all individual writes were sent.
            device.finish()
        except OSError as exc:
            if not applied:
                return _refused_for_the_device(label, str(exc))
            return Outcome(state=APPLIED_UNVERIFIED, name=label,
                           reason="backend operation did not finish: %s" % exc,
                           unverified=sorted(expected_registers(config)),
                           persisted=False, read_back=False)
        finally:
            if device is not None:
                device.close()
        marked = (remember_active_profile(name, config_path) if name is not None
                  else forget_active_profile(config_path))
        return replace(outcome, persisted=marked.in_effect, durable=marked.durable)


def _written(name: str, config: Config, device: Control
             ) -> Optional[Outcome]:
    """Write the desk. None when all of it went out; else how far it came.

    A switch promises an outcome and never an exception (ADR 0011), and
    until 0.7.0 a socket error part of the way broke that promise as a
    traceback, with some of the profile on the device and nothing said
    about which part (third outside review). Nothing gone out is a
    refusal like any other: the desk is untouched. Some of it gone out is
    its own state, with both lists, and the marker is left alone -- the
    desk in effect remains the previous one. Reload repairs only PIN;
    restoring its REMEMBER starting values requires explicit selection
    or a new session (ADR 0012, ADR 0027).
    """
    try:
        apply_routing(config, device, intent=ApplyIntent.EXPLICIT)
    except WriteFailed as exc:
        cause = "cannot complete backend writes (%s)" % (exc.strerror or exc)
        if not exc.written:
            log.error("%r refused, nothing written: %s", name, cause)
            return Outcome(state=REFUSED, name=name, reason=cause)
        log.error("%r written in part -- %d of %d register(s): %s", name,
                  len(exc.written), len(exc.written) + len(exc.unwritten),
                  cause)
        return Outcome(state=WRITTEN_IN_PART, name=name, reason=cause,
                       written=list(exc.written),
                       unwritten=list(exc.unwritten), persisted=False,
                       read_back=False)
    return None


def _check(name: str, config: Config, device: Control) -> Outcome:
    """Read the state back and classify the switch.

    Delegates the whole read-back to ``verify.verify_routing`` against
    the caller's backend. It deliberately does not reimplement the loop:
    the first version of this function read a single datagram and gave
    up, so a register that was demonstrably correct at the device came
    back unconfirmed -- measured on a UCX II, where ``/output/1/volume``
    went 0.0 -> -6.0 and the switch reported it unverified anyway.

    Safe in the direction it failed, and useless: APPLIED_VERIFIED was
    unreachable on real hardware.
    """
    model = device_for_name(config.device_name)
    registers = expected_registers(config)
    try:
        result = verify_routing(registers, device, VERIFY_TIMEOUT, device_model=model)
    except OSError as exc:
        # All apply phases completed, but this read-back failed. The outer
        # operation still requires a successful lease end before changing
        # the profile marker; connection loss cannot commit success.
        log.error("profile %r applied; it cannot be verified: %s", name, exc)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason=exc.strerror or str(exc),
                       unverified=sorted(registers), read_back=False)
    unverified = sorted(result.mismatched + result.unobserved)
    if not unverified:
        return Outcome(state=APPLIED_VERIFIED, name=name)
    # Registers this backend never reports are not evidence of a problem,
    # but they are still not confirmation -- so they stay in the list
    # rather than being quietly dropped from it, and are named as the
    # separate thing they are.
    blind = sorted(p for p in unverified
                   if not register_ever_reported(p, model))
    reason = ("%d unconfirmed, %d of them never reported by this backend"
              % (len(unverified), len(blind)))
    return Outcome(state=APPLIED_UNVERIFIED, name=name, reason=reason,
                   unverified=unverified, unverifiable=blind)


def describe_profiles(config_path: Optional[Path] = None) -> Sequence[str]:
    """Profile names with a one-line summary each, for ``--list-profiles``.

    The active one is marked, because "which desk am I on" is the
    question this list is most often asked.
    """
    active = active_profile(config_path)
    lines = []
    for name in list_profiles(config_path):
        mark = "  (active)" if name == active else ""
        try:
            config = load_profile(name, config_path)
        except ConfigError as exc:
            lines.append("%-16s  BROKEN: %s%s" % (name, exc, mark))
            continue
        lines.append("%-16s  %d route(s), %d channel section(s)%s"
                     % (name, len(config.routes), len(config.channels), mark))
    return lines
