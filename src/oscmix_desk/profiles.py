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

That is why this states an outcome rather than raising. Three, and only
three:

``APPLIED_VERIFIED``
    Written, and the device reported the values back.

``APPLIED_UNVERIFIED``
    Written, and the read-back could not confirm it -- normally because
    the mixer GUI holds UDP 8222, which is the common desktop case, not
    a fault. Carries the list of what went unconfirmed, because
    "unverified" without the list is not an outcome a person can act on.

``REFUSED``
    Nothing was written. The config did not parse, the profile does not
    exist, or the name was not a name.

There is deliberately no fourth. "Partly applied, and here is a
traceback" is the state this module exists to make unrepresentable.
"""

from __future__ import annotations

import configparser
import contextlib
import fcntl
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from .backend import Backend, loopback
from .config import Config, list_profiles, load_config, profile_path
from .constants import SWITCH_LOCK_WAIT, VERIFY_TIMEOUT
from .errors import ConfigError
from .log import log
from .registers import device_for_name
from .routing import apply_routing
from .verify import expected_registers, register_ever_reported, verify_routing


def _short(paths: List[str], limit: int = 6) -> str:
    """A register list a person can read at the end of a sentence."""
    shown = ", ".join(paths[:limit])
    return shown if len(paths) <= limit else "%s and %d more" % (
        shown, len(paths) - limit)

#: Written, and the device reported it back.
APPLIED_VERIFIED = "applied-verified"
#: Written; the read-back could not confirm it. ``unverified`` says what.
APPLIED_UNVERIFIED = "applied-unverified"
#: Nothing was written. ``reason`` says why.
REFUSED = "refused"

#: The ``reason`` on an outcome where the read-back was never attempted,
#: because the caller asked for none. Distinct wording from a read-back
#: that ran and came up short: the register list means "unknown" here
#: and "looked for and absent" there, and the state cannot say so.
NOT_CHECKED = "verification not requested"

#: The complete set. A fourth member is a design change, and
#: ``tests/test_profiles.py`` asserts this is exhaustive so it cannot
#: arrive by accretion.
STATES = (APPLIED_VERIFIED, APPLIED_UNVERIFIED, REFUSED)


@dataclass(frozen=True)
class Outcome:
    """What a switch did. Every field answerable without a traceback."""

    state: str
    name: str
    reason: str = ""
    #: Everything not confirmed at its expected value.
    unverified: List[str] = field(default_factory=list)
    #: The subset of ``unverified`` this backend never reports at all --
    #: the playback mix matrix, and anything write-only. Kept separate
    #: because "I could not check it" and "it cannot be checked" are
    #: different facts, and on a real routing the second is the normal
    #: case: every switch leaves /mix/<out>/playback/<pb> unconfirmed,
    #: measured, by design (backend.Traits.dumps_playback_matrix).
    unverifiable: List[str] = field(default_factory=list)
    #: Whether the marker now says what the device does. False when the
    #: switch landed but the marker could not be written, or the restore
    #: could not remove it: the desk holds only until the next reload or
    #: start, and the caller must not send that reload itself (ADR 0019).
    persisted: bool = True

    @property
    def applied(self) -> bool:
        """Whether anything reached the device.

        The field a script branches on, derived from the state rather
        than stored beside it: two sources for one fact is how "applied
        but the flag says otherwise" happens.
        """
        return self.state != REFUSED

    def describe(self) -> str:
        """One line, for a person."""
        line = self._describe_state()
        if self.persisted:
            return line
        return line + "; not remembered, so the next reload or start undoes it"

    def _describe_state(self) -> str:
        if self.state == REFUSED:
            return "refused %r, nothing written: %s" % (self.name, self.reason)
        if self.state == APPLIED_VERIFIED:
            return "applied %r and verified it at the device" % self.name
        if self.reason == NOT_CHECKED:
            return ("applied %r; not checked, so none of its %d register(s) "
                    "is confirmed" % (self.name, len(self.unverified)))
        missed = [p for p in self.unverified if p not in self.unverifiable]
        if not missed:
            return ("applied %r; %d register(s) this backend cannot report: %s"
                    % (self.name, len(self.unverifiable),
                       _short(self.unverifiable)))
        return ("applied %r; %d register(s) unconfirmed: %s%s"
                % (self.name, len(missed), _short(missed),
                   "" if not self.unverifiable
                   else " (plus %d this backend cannot report)"
                        % len(self.unverifiable)))


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

    A profile may still set them, and then it wins: the machine that
    needs a second backend on another port is exactly the machine whose
    profiles are per-backend.
    """
    path = profile_path(name, config_path)
    if not path.is_file():
        raise ConfigError("no profile %r (looked in %s)" % (name, path.parent))
    profile = load_config(path)
    if config_path is not None and Path(config_path).is_file():
        _inherit_transport(profile, load_config(config_path), path)
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
)


def _inherit_transport(profile: Config, main: Config, path: Path) -> None:
    """Fill the machine-level settings a profile did not state itself."""
    text = path.read_text()
    for section, option, attr in MACHINE_SETTINGS:
        if not _states(text, section, option):
            setattr(profile, attr, getattr(main, attr))


def _states(text: str, section: str, option: str) -> bool:
    """Whether the profile file actually names this option.

    Reads the file rather than comparing against the default, because a
    profile that deliberately sets the default is stating it, and
    "equals the default" cannot tell those apart.
    """
    # Same parser settings as load_config. A second parser with
    # different rules reading the same file is the shape of three
    # separate defects in 0.3.0; matching them costs one line.
    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(text)
    except configparser.Error:
        return False
    return parser.has_option(section, option)


#: Where the active profile's name is kept: one line, beside
#: routing.conf. Written by the CLI after an applied switch, removed by
#: `--no-profile`, and only ever *read* by the session -- which is what
#: keeps the unit's ProtectHome=read-only true. Beside the config rather
#: than in a state directory, so that `--config` selects the profiles
#: and the marker together (ADR 0018).
ACTIVE_MARKER = "active-profile"


def active_profile_path(config_path: Optional[Path]) -> Optional[Path]:
    """The marker file for this config, or None without a config."""
    if config_path is None:
        return None
    return Path(config_path).parent / ACTIVE_MARKER


def active_profile(config_path: Optional[Path] = None) -> Optional[str]:
    """The remembered profile name, or None.

    A marker whose content is not a profile name is ignored with a
    warning rather than trusted: the name is used to build a path.
    """
    path = active_profile_path(config_path)
    if path is None or not path.is_file():
        return None
    try:
        name = path.read_text().strip()
    except OSError as exc:
        log.warning("ignoring %s: %s", path, exc)
        return None
    if not name:
        return None
    try:
        profile_path(name, config_path)
    except ConfigError as exc:
        log.warning("ignoring %s: %s", path, exc)
        return None
    return name


def remember_active_profile(name: str, config_path: Optional[Path]) -> bool:
    """Record a switch that was applied. False, and a warning, if it cannot.

    Not an outcome state: the device already has the profile, and a
    fourth state for "applied but forgotten" would be the "applied, but
    the flag says otherwise" case ADR 0011 forbids.
    """
    path = active_profile_path(config_path)
    if path is None:
        return False
    # Written beside and renamed over, never in place: a crash or a
    # power loss between open and close would otherwise leave an empty
    # marker, and an empty marker reads as "no profile" -- the choice
    # silently gone on the next start. The old marker stays whole until
    # the new one is complete on disk, and the rename is atomic.
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, (name + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        log.warning("profile %r applied but not remembered: cannot write "
                    "%s (%s); the desk holds until the next reload or "
                    "start, which applies routing.conf", name, path, exc)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    """Make a rename durable; best effort, some filesystems refuse it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


#: The lock every writer of this desk takes, beside the marker: a
#: switch, `--no-profile`, and the unit's own apply, verifier and
#: reconcile (ADR 0019). Two writers at once would interleave their link
#: phases and mix writes on the wire -- the ordering ADR 0001 exists to
#: guarantee. One file per config directory, so two desks selected by
#: `--config` do not contend.
SWITCH_LOCK = "active-profile.lock"


class DeviceLock:
    """A held device lock, or a stand-in for "there was nothing to lock".

    The stand-in exists so no caller has to branch: a session without a
    config directory, and an install whose lock file predates ADR 0019,
    both write without one and say so once, rather than refusing to
    drive the device at all.
    """

    def __init__(self, fd: Optional[int] = None) -> None:
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _open_lock(path: Path) -> Optional[int]:
    """Open the lock file read-write, or read-only, or not at all.

    The unit runs with `ProtectHome=read-only`, so it can neither create
    the file nor open it for writing. `flock` needs neither: the lock
    lives on the open file description, and a read-only one holds it
    exactly as well. None means the file is not there -- the installer
    creates it, and an older install has to be told rather than blocked.
    """
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        pass
    try:
        return os.open(path, os.O_RDONLY)
    except OSError:
        return None


def take_device_lock(config_path: Optional[Path],
                     wait: Optional[float] = None) -> Optional[DeviceLock]:
    """Take the lock every writer of this desk holds, or None after the wait.

    Every writer: a switch, `--no-profile`, and the unit's own apply,
    verifier and reconcile (ADR 0019). What the caller does with None is
    its own contract -- a switch refuses, a start writes anyway, a
    reconcile stands down -- because the cost of writing over another
    writer is not the same in the three places.
    """
    marker = active_profile_path(config_path)
    if marker is None:
        return DeviceLock()
    path = marker.with_name(SWITCH_LOCK)
    fd = _open_lock(path)
    if fd is None:
        log.warning("no device lock at %s; writing without one -- run "
                    "install.sh to create it", path)
        return DeviceLock()
    deadline = time.monotonic() + (SWITCH_LOCK_WAIT if wait is None else wait)
    announced = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return DeviceLock(fd)
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            if not announced:
                log.info("another writer holds the device lock; waiting "
                         "for it")
                announced = True
            time.sleep(0.1)


def _switch_lock_held(config_path: Optional[Path]) -> Iterator[bool]:
    """Hold the device lock for this config, or yield False after the wait.

    Taken *after* the profile parsed: a refusal for a bad config needs
    no lock and costs nothing, as ADR 0011 promises. Without a config
    there is no directory to lock in and nothing to contend with.
    """
    lock = take_device_lock(config_path)
    if lock is None:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


#: Wrapped here rather than with the decorator, on purpose: mutmut
#: leaves decorated functions unmutated, and this loop is what the
#: no-interleave guarantee rests on (ADR 0018). A decorator kept it out
#: of the 0.6.4 mutation run entirely; as a plain generator it is in.
_switch_lock = contextlib.contextmanager(_switch_lock_held)


def _refused_for_the_lock(name: str) -> Outcome:
    reason = ("another writer still holds the device lock after %.0fs"
              % SWITCH_LOCK_WAIT)
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


def forget_active_profile(config_path: Optional[Path]) -> bool:
    """Remove the marker; nothing to remove is not an error.

    False when it is still there afterwards, which the caller carries in
    the outcome: a reload sent then would re-apply the profile the
    marker still names and undo the restore (ADR 0019).
    """
    path = active_profile_path(config_path)
    if path is None:
        return True
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("cannot remove %s (%s); the desk holds until the next "
                    "reload or start, which applies the profile it names",
                    path, exc)
        return False
    return True


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

    with _switch_lock(config_path) as held:
        if not held:
            return _refused_for_the_lock(name)
        device = backend if backend is not None else loopback(
            config.osc_port, config.osc_recv_port)
        _write(config, device)
        # Applied, so remembered: from here on every start and reload is
        # this profile's, until --no-profile (ADR 0018). A marker that
        # could not be written travels in the outcome rather than only
        # in the log: the caller must then not reload the unit, whose
        # reconcile would undo what just landed (ADR 0019).
        remembered = remember_active_profile(name, config_path)

        if not verify:
            # Not confirmed, because nobody looked -- which is a different
            # fact from "looked and did not see it", and the state is the
            # same either way. Everything expected goes in the list.
            return Outcome(state=APPLIED_UNVERIFIED, name=name,
                           reason=NOT_CHECKED, persisted=remembered,
                           unverified=sorted(expected_registers(config)))
        return replace(_check(name, config, device), persisted=remembered)


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
    with _switch_lock(config_path) as held:
        if not held:
            return _refused_for_the_lock("routing.conf")
        device = backend if backend is not None else loopback(
            config.osc_port, config.osc_recv_port)
        _write(config, device)
        forgotten = forget_active_profile(config_path)
        if not verify:
            return Outcome(state=APPLIED_UNVERIFIED, name="routing.conf",
                           reason=NOT_CHECKED, persisted=forgotten,
                           unverified=sorted(expected_registers(config)))
        return replace(_check("routing.conf", config, device),
                       persisted=forgotten)


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
    result = verify_routing(registers, config.osc_port, config.osc_recv_port,
                            VERIFY_TIMEOUT, device_model=model,
                            backend=device)
    if result is None:
        # The mixer GUI holds the port. Applied, blind, and said so.
        log.info("profile %r applied; read-back port in use, cannot verify",
                 name)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason="receive port in use (mixer GUI running?)",
                       unverified=sorted(registers))

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
