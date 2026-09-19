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
applied and verified, applied and unverified, or refused. There is
deliberately no fourth. "Partly applied, and here is a traceback" is the
state this module exists to make unrepresentable.

The order of a switch is this module's, and only this module's: which
interface and backend it may write to, the lock taken (``locking``), the
write, the profile remembered (``marker``), the read-back.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .backend import Backend, loopback
from .config import load_config
from .constants import VERIFY_TIMEOUT
from .devices import device_for_name
from .discovery import (
    Device,
    resolve_device,
    udp_port_listening,
    usb_device_present,
)
from .errors import ConfigError, DeviceAmbiguous, ReceivePortError
from .locking import _switch_lock, held_elsewhere
from .log import log
from .marker import (
    active_profile,
    forget_active_profile,
    remember_active_profile,
)
from .model import Config
from .notices import log_desk_notices
from .outcome import (
    APPLIED_UNVERIFIED,
    APPLIED_VERIFIED,
    NOT_CHECKED,
    REFUSED,
    Outcome,
)
from .paths import list_profiles, profile_path
from .process import port_holder
from .routing import apply_routing
from .verify import expected_registers, register_ever_reported, verify_routing


def load_profile(name: str, config_path: Optional[Path] = None) -> Config:
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

    A profile that states them still wins in 0.6.x. The reason once given
    here -- a machine with a second backend, whose profiles would be
    per-backend -- is withdrawn by ADR 0026: one marker per directory
    cannot say which backend "the active profile" is for. Such a profile
    is told so where it is written (``notices.other_machine_warning``),
    and a running session does not apply it to its own interface.
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
        return load_config(path)
    main = load_config(config_path)
    profile = load_config(path, keep_machine_settings(Config(), main))
    profile.main = main.loaded
    return profile


#: Everything in a config that describes the *machine* rather than the
#: desk, as (section, option, Config attribute). A profile inherits each
#: of these unless it states it itself.
#:
#: A table rather than a list of ifs, because the failure mode is
#: forgetting one: `usb-id` was left out of the first version and would
#: have silently reverted to the compiled-in default on any machine that
#: sets it. `tests/test_profiles.py` holds this table against the set of
#: fields on Config that no `[route]` or channel section can write, so a
#: new machine-level setting cannot be added without landing here too.
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
    for _section, _option, attr in MACHINE_SETTINGS:
        setattr(desk, attr, getattr(running, attr))
    # Where two of those came from goes with them: the kept desk is
    # resolved for this command line as much as the running one is.
    desk.overrides = running.overrides
    return desk


class _Refused(Exception):
    """Why a writer will not write, carried to the outcome."""


def _proc_root() -> Path:
    return Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))


def _target(config: Config, reach: bool) -> Device:
    """The interface this write is for, or _Refused with the reason.

    One resolution for the lock key and for the reachability check, so
    the two cannot describe different boxes (ADR 0024). ``reach`` is
    False when the caller hands in its own backend and so owns the other
    end of the socket -- the test seam, and the only caller that uses it.
    Ambiguity refuses either way: it is about which lock to take.
    """
    try:
        device = resolve_device(config.usb_id, config.device_name,
                                config.serial, _proc_root())
    except DeviceAmbiguous as exc:
        raise _Refused(str(exc)) from exc
    if reach:
        reason = _unreachable(config, device)
        if reason is not None:
            raise _Refused(reason)
    return device


def _still_the_target(config: Config, target: Device) -> Optional[str]:
    """Why a write must not go ahead after the lock wait, or None.

    The wait can take 30 s, and the backend on the port can change in
    that time: checked only before it, a switch wrote to whatever held the
    port afterwards and recorded the marker (found by review, 0.6.9). A
    window of milliseconds remains between this and the first datagram;
    it is the one a UDP write cannot close.
    """
    try:
        now = _target(config, reach=True)
    except _Refused as refusal:
        return str(refusal)
    if now.key != target.key:
        return ("the interface changed while waiting for the device lock "
                "(%s, now %s)" % (target.key, now.key))
    return None


def _unreachable(config: Config, device: Device) -> Optional[str]:
    """Why a write would not reach this interface, or None when it would.

    A write to an unreachable device is not a write: the datagrams land
    in a port nobody bound, or in the wrong one, and a switch that
    reports `applied` for that records a desired state that was never at
    the device.

    Presence in sysfs is not reachability -- a logical disconnect leaves
    the sysfs entry in place (0.6.8) -- and a bound port is not either:
    in 0.6.8 a stranger's socket on the OSC port took a whole routing
    and the marker was set. The port has to be held by an oscmix of this
    user, and when its alsaseqio parent says which client it bridges,
    that has to be the interface resolved above (ADR 0024).
    """
    sysfs = Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices"))
    if not usb_device_present(config.usb_id, sysfs):
        return "%s is not connected" % config.usb_id
    if device.client is None:
        # The backend bridges a sequencer client; no client, no backend for
        # this box -- whatever holds the port. Checked here and not only by
        # the serial comparison below, which has nothing to compare once
        # the card list and the clients are gone: in that state a switch
        # keyed on `<usb id>-unknown` and wrote beside the unit's lock
        # (found by review, 0.6.9).
        return ("%s%s is not visible to ALSA, so no backend can be driving it"
                % (config.usb_id,
                   " with serial %s" % device.serial if device.serial else ""))
    port = config.osc_port
    if not udp_port_listening(port, _proc_root()):
        return ("nothing is listening on UDP %d, so the backend is not "
                "running" % port)
    holder = port_holder(port, _proc_root())
    if holder is None or not holder.oscmix:
        return ("UDP %d is held by %s, not by an oscmix backend of this user"
                % (port, "pid %d" % holder.pid if holder is not None
                   else "a process that cannot be identified"))
    if device.serial and holder.serial and holder.serial != device.serial:
        return ("the backend on UDP %d drives the interface %s, not %s"
                % (port, holder.serial, device.serial))
    if (device.client is not None and holder.client is not None
            and holder.client != device.client):
        return ("the backend on UDP %d bridges sequencer client %d, not %d"
                % (port, holder.client, device.client))
    return None


def _refused_for_the_device(name: str, reason: str) -> "Outcome":
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


def _refused_for_the_lock(name: str) -> Outcome:
    reason = held_elsewhere()
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


def effective_config(config_path: Optional[Path]) -> Tuple[Config, Optional[str]]:
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
    main = load_config(config_path)
    name = active_profile(config_path)
    if name is None:
        return main, None
    try:
        profile = load_profile(name, config_path)
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
                   backend: Optional[Backend] = None,
                   verify: bool = True) -> Outcome:
    """Switch the desk to a profile and say what happened.

    Never raises for a bad config: an unparseable profile is an
    ``Outcome``, because the caller has to distinguish "your typo cost
    you nothing" from "it is applied but I could not check" and an
    exception collapses those into the same thing.
    """
    try:
        config = load_profile(name, config_path)
    except ConfigError as exc:
        # Before any socket exists. The ordering is the promise.
        # The message names the profile: on a desk with five of them,
        # "channel 99 out of range" without a name is a search.
        log.error("profile %r refused, nothing written: %s", name, exc)
        return Outcome(state=REFUSED, name=name, reason=str(exc))
    log_desk_notices(config)

    try:
        target = _target(config, reach=backend is None)
    except _Refused as refusal:
        return _refused_for_the_device(name, str(refusal))

    with _switch_lock(config_path, target.key) as held:
        if not held:
            return _refused_for_the_lock(name)
        changed = _still_the_target(config, target) if backend is None else None
        if changed is not None:
            return _refused_for_the_device(name, changed)
        device = backend if backend is not None else loopback(
            config.osc_port, config.osc_recv_port)
        _write(config, device)
        # Applied, so remembered: from here on every start and reload is
        # this profile's, until --no-profile (ADR 0018). A marker that
        # could not be written travels in the outcome rather than only
        # in the log: the caller must then not reload the unit, whose
        # reconcile would undo what just landed (ADR 0019).
        marked = remember_active_profile(name, config_path)

        if not verify:
            # Not confirmed, because nobody looked -- which is a different
            # fact from "looked and did not see it", and the state is the
            # same either way. Everything expected goes in the list.
            outcome = Outcome(state=APPLIED_UNVERIFIED, name=name,
                              reason=NOT_CHECKED,
                              unverified=sorted(expected_registers(config)),
                              read_back=False)
        else:
            outcome = _check(name, config, device)
        return replace(outcome, persisted=marked.in_effect,
                       durable=marked.durable)


def restore_main(config_path: Optional[Path] = None,
                 backend: Optional[Backend] = None,
                 verify: bool = True) -> Outcome:
    """Apply `routing.conf` again and forget the active profile.

    The same transaction as a switch, with the main config as the desk
    and "routing.conf" as the name the outcome carries: parsed and
    validated in full before the first datagram, then written, then
    checked. The marker goes only once the write is on the wire; a
    refused restore leaves the profile in effect and says so.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("routing.conf refused, nothing written: %s", exc)
        return Outcome(state=REFUSED, name="routing.conf", reason=str(exc))
    log_desk_notices(config)
    try:
        target = _target(config, reach=backend is None)
    except _Refused as refusal:
        return _refused_for_the_device("routing.conf", str(refusal))

    with _switch_lock(config_path, target.key) as held:
        if not held:
            return _refused_for_the_lock("routing.conf")
        changed = _still_the_target(config, target) if backend is None else None
        if changed is not None:
            return _refused_for_the_device("routing.conf", changed)
        device = backend if backend is not None else loopback(
            config.osc_port, config.osc_recv_port)
        _write(config, device)
        marked = forget_active_profile(config_path)
        if not verify:
            outcome = Outcome(state=APPLIED_UNVERIFIED, name="routing.conf",
                              reason=NOT_CHECKED,
                              unverified=sorted(expected_registers(config)),
                              read_back=False)
        else:
            outcome = _check("routing.conf", config, device)
        return replace(outcome, persisted=marked.in_effect,
                       durable=marked.durable)


def _write(config: Config, device: Backend) -> None:
    """Apply the profile, barrier and all.

    Delegates to ``routing.apply_routing`` rather than sending the three
    phases itself. The first version did send them itself -- links, mix,
    channel state, one after the other with nothing in between -- and
    dropped the link barrier in the process. On a live UCX II the whole
    switch, apply and read-back, took 48 ms; the barrier alone is
    measured in seconds. That is the stereo-link race the 0.2.0 release
    was about, reintroduced on a new write path.

    Three defects in 0.3.0 had the same shape: a second
    implementation of something that already existed, correct in
    everything it did and missing something the original had. This is
    the third and last of them.
    """
    apply_routing(config, config.osc_port, config.osc_recv_port,
                  backend=device)


def _check(name: str, config: Config, device: Backend) -> Outcome:
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
        result = verify_routing(registers, config.osc_port,
                                config.osc_recv_port, VERIFY_TIMEOUT,
                                device_model=model, backend=device)
    except ReceivePortError as exc:
        # Applied -- the barrier waited blind -- and unverifiable for a
        # reason that is not the mixer GUI. The outcome carries it, where
        # it used to read "receive port in use" (0.6.11).
        log.error("profile %r applied; it cannot be verified: %s", name, exc)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason=exc.strerror or str(exc),
                       unverified=sorted(registers), read_back=False)
    if result is None:
        # The mixer GUI holds the port. Applied, blind, and said so.
        log.info("profile %r applied; read-back port in use, cannot verify",
                 name)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason="receive port in use (mixer GUI running?)",
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
