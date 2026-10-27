"""Validate a complete desk, apply it through Desk, then commit its selection.

The marker changes only after all writes and an acknowledged lease end, while
still holding the device lock. Partial writes retain the previous marker and
report sent and pending paths; a hardware rollback cannot be promised.
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple, Union

from .arguments import build_arg_parser
from .backend import Control
from .config import load_config
from .constants import SERVICE_UNIT
from .desk import Desk, _operation
from .devices import device_for_name
from .errors import (
    ConfigError,
    DeviceLockUnavailable,
    WriteFailed,
)
from .hostservice import maintenance_problem
from .log import log
from .marker import (
    active_profile,
    forget_active_profile,
    remember_active_profile,
)
from .model import Config, Machine, Overrides, _resolve_overrides
from .notices import log_desk_notices
from .outcome import (
    APPLIED_MISMATCHED,
    APPLIED_UNVERIFIED,
    APPLIED_VERIFIED,
    NOT_CHECKED,
    REFUSED,
    WRITTEN_IN_PART,
    Outcome,
)
from .paths import discover_config_path, list_profiles, profile_path, profiles_dir, regular_file
from .process import RELOAD_DONE, RELOAD_NOT_RUNNING, reload_service, unit_process
from .reconcile import ApplyIntent, desired, policy_for
from .verify import expected_registers, register_ever_reported


def load_profile(name: str, config_path: Optional[Path] = None,
                 overrides: Optional[Overrides] = None, *,
                 said: Optional[Overrides] = None, strict: bool = False) -> Config:
    """Parse and validate a profile for the main config's device.

    Missing machine settings are inherited from the main config. A
    different device name, USB ID or serial is refused before any write.
    Legacy [osc] ports remain validated data and select no backend.
    This path needs no connection, so previews use the same validation.
    ``strict=True`` rejects ignored sections in both the main config and
    the selected profile before an operation can write anything.
    ``overrides`` applies to both files; ``said`` is a compatible keyword
    alias, and two non-None inputs are refused before file discovery.
    """
    # None is "the config discovery finds", for the profile's file and for
    # the machine settings alike: until 0.8.1 the file was discovered and
    # the machine settings were the defaults.
    overrides = _resolve_overrides(overrides, said)
    config_path = config_path or discover_config_path()
    path = profile_path(name, config_path)
    if not regular_file(path):
        raise ConfigError("no profile %r (looked in %s)" % (name, path.parent))
    return _profile_at(path, name, config_path, overrides, strict=strict)


def _profile_at(path: Path, name: str, config_path: Optional[Path],
                overrides: Optional[Overrides], *, strict: bool) -> Config:
    """The profile in ``path``, validated as profile ``name`` of ``config_path``."""
    # Read *onto* the machine settings rather than patched with them
    # afterwards. Patched, the profile was validated while it still named
    # the default device: on a desk for another interface its channels
    # were checked against the UCX II's twenty, and a channel section the
    # main config has ignored with a warning since 0.6.2 was accepted
    # through the UCX II's table and written (0.6.11). The parser takes
    # every machine setting with the value it finds as the fallback, so a
    # profile that states one still wins.
    if config_path is None or not regular_file(Path(config_path)):
        return load_config(path, overrides=overrides, strict=strict, profile=True)
    main = load_config(config_path, overrides=overrides, strict=strict)
    # Onto what routing.conf itself says, not onto what the command line
    # made of it: the comparison below is between the two files, and the
    # command line is put over both alike.
    onto = replace(Config(), **(main.loaded or Machine(
        main.device_name, main.usb_id, main.serial, main.osc_port,
        main.osc_recv_port))._asdict())
    profile = load_config(path, onto, overrides, strict=strict, profile=True)
    theirs, ours = profile.loaded, main.loaded
    moved = "" if theirs is None or ours is None else theirs.differs_from(ours)
    if moved:
        raise ConfigError(
            "profile %r names another backend or interface than %s -- %s. A "
            "profile is the desk, not the machine: take [device] out of %s"
            % (name, config_path, moved, path))
    # How the session keeps the desk is routing.conf's, whichever desk it keeps.
    return replace(profile, enforce=main.enforce)


#: Device identity and retained legacy ports, as (section, option, Config
#: attribute). A profile inherits each unless it states it itself; only
#: the three device fields determine compatibility.
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
    """Keep this process's device identity, legacy values and overrides.

    A desk re-read under the lock cannot retarget the running backend.
    The field table is checked against Config so an inherited field
    cannot silently be dropped.
    """
    kept = {attr: getattr(running, attr)
            for _section, _option, attr in MACHINE_SETTINGS}
    # Where two of those came from goes with them: the kept desk is
    # resolved for this command line as much as the running one is.
    return replace(desk, overrides=running.overrides, **kept)


def _refused_for_the_device(name: str, reason: str) -> "Outcome":
    log.error("profile %r refused, nothing written: %s", name, reason)
    return Outcome(state=REFUSED, name=name, reason=reason)


def effective_config(config_path: Optional[Path],
                     overrides: Optional[Overrides] = None, *,
                     said: Optional[Overrides] = None, strict: bool = False,
                     quiet: bool = False) -> Tuple[Config, Optional[str]]:
    """The desk a start applies, and the profile it came from.

    The active profile when one is remembered and loads, `routing.conf`
    otherwise. Every path that asks "what desk did you declare" -- the
    start, SIGHUP, --dry-run, --diff -- asks here, so they cannot
    disagree about it.

    In the default mode, raises ConfigError only for `routing.conf`: a main config
    that does not parse is exit 2 as it always was. A remembered profile
    that does not load is a warning and a fallback, because a refused
    start over a file nobody edited is the failure config compatibility
    exists to prevent; the marker stays, so the warning stays until somebody
    decides.

    Strict resolution rejects ignored sections and an unusable remembered
    profile instead of choosing another desk. Explicit selection/restore
    validates its target independently, so it can repair a broken selection.
    ``overrides`` selects the validation device for both files; ``said``
    is its compatible keyword alias. Supplying both is a TypeError.
    ``quiet`` says nothing about which desk it is: for a reader that asks
    often, such as the watcher, while the reconcile it may lead to says it.
    """
    overrides = _resolve_overrides(overrides, said)
    config_path = config_path or discover_config_path()
    main = load_config(config_path, overrides=overrides, strict=strict)
    name = active_profile(config_path)
    if name is None:
        return main, None
    try:
        profile = load_profile(name, config_path, overrides, strict=strict)
    except ConfigError as exc:
        if strict:
            raise ConfigError("active profile %r is not usable (%s)" % (name, exc)) from None
        if not quiet:
            log.warning("active profile %r is not usable (%s); applying %s "
                        "instead -- fix the file, or `--no-profile`",
                        name, exc, config_path)
        return main, None
    if not quiet:
        log.info("active profile %r (%s) is in effect; the routes and channel "
                 "state in %s are not", name, profile_path(name, config_path),
                 config_path)
    return profile, name


def switch_profile(name: str, config_path: Optional[Path] = None,
                   backend: Optional[Control] = None,
                   verify: bool = True, *, strict: bool = False) -> Outcome:
    """Parse a profile fully, then apply it as one coordinated operation.

    ``config_path`` None is the config discovery finds, as for the CLI.
    Installation maintenance fences refuse before opening the device. A
    committed or partial write is handed to the matching running service;
    Outcome.reload_failed reports a refused reload without hiding the writes.
    ``strict=True`` refuses ignored sections before locking or connecting.
    """
    config_path = config_path or discover_config_path()
    try:
        config = load_profile(name, config_path, strict=strict)
    except ConfigError as exc:
        return _refused_for_the_device(name, str(exc))
    return _hand_over_to_the_unit(
        _activate(config, config_path, name, backend, verify), config_path)


def restore_main(config_path: Optional[Path] = None,
                 backend: Optional[Control] = None,
                 verify: bool = True, *, strict: bool = False) -> Outcome:
    """Apply the main desk; clear the marker only after the operation finishes.

    ``config_path`` None is the config discovery finds. Without any, there
    is no main desk to return to: until 0.8.1 the defaults were applied,
    0 of 0 registers verified and the restore reported as a success.
    Maintenance fences and service hand-over follow switch_profile's rules.
    ``strict=True`` refuses ignored sections in the main config.
    """
    config_path = config_path or discover_config_path()
    try:
        config = _restore_config(config_path, strict=strict)
    except ConfigError as exc:
        return _refused_for_the_device(Path(config_path or "routing.conf").name, str(exc))
    return _hand_over_to_the_unit(
        _activate(config, config_path, None, backend, verify), config_path)


def _restore_config(config_path: Optional[Path], *, strict: bool = False) -> Config:
    """Load the resolved main config for both restore and read-only preview."""
    if config_path is None:
        raise ConfigError("no config file found, so there is no main desk to restore")
    return load_config(config_path, strict=strict)


def _activate(config: Config, config_path: Optional[Path], name: Optional[str],
              backend: Optional[Control], verify: bool) -> Outcome:
    label = name if name is not None else Path(config_path or "routing.conf").name
    problem = maintenance_problem()
    if problem:
        return _refused_for_the_device(
            label, problem + "; complete installation recovery before activation")
    log_desk_notices(config)
    applied = False
    gave_out = None
    try:
        with _operation(config, config_path, backend=backend) as desk:
            gave_out = _written(label, desk)
            if gave_out is not None:
                return gave_out
            applied = True
            if verify:
                outcome = _check(label, desk)
            else:
                outcome = Outcome(state=APPLIED_UNVERIFIED, name=label,
                                  reason=NOT_CHECKED,
                                  unverified=tuple(sorted(expected_registers(config))),
                                  read_back=False)
            # A dead/lost lease invalidates observations and cannot publish a
            # successful marker, even when all individual writes were sent.
            desk._finish()
            desk._disconnect()
            marked = (remember_active_profile(name, config_path) if name is not None
                      else forget_active_profile(config_path))
            return replace(outcome, persisted=marked.in_effect, durable=marked.durable)
    except (ConfigError, DeviceLockUnavailable, OSError) as exc:
        if gave_out is not None:
            log.warning("profile %r cleanup failed: %s", label, exc)
            return gave_out
        if not applied:
            reason = str(exc)
            if isinstance(exc, OSError) and exc.errno == errno.ENODEV and exc.strerror:
                reason = exc.strerror
            return _refused_for_the_device(label, reason)
        return Outcome(state=APPLIED_UNVERIFIED, name=label,
                       reason="backend operation did not finish: %s" % exc,
                       unverified=tuple(sorted(expected_registers(config))),
                       persisted=False, read_back=False)


def _written(name: str, desk: Desk) -> Optional[Outcome]:
    """Classify sent and pending paths after an explicit application.

    Partial writes retain the previous marker. Reload repairs PIN only;
    restoring REMEMBER starting values requires explicit selection or a new
    session. No classification promises a hardware rollback.
    """
    try:
        desk.apply(ApplyIntent.EXPLICIT)
    except WriteFailed as exc:
        cause = "cannot complete backend writes (%s)" % (exc.strerror or exc)
        if not exc.written:
            log.error("%r refused, nothing written: %s", name, cause)
            return Outcome(state=REFUSED, name=name, reason=cause)
        log.error("%r written in part -- %d of %d register(s): %s", name,
                  len(exc.written), len(exc.written) + len(exc.unwritten),
                  cause)
        return Outcome(state=WRITTEN_IN_PART, name=name, reason=cause,
                       unverified=exc.written + exc.unwritten,
                       written=exc.written,
                       unwritten=exc.unwritten, persisted=False,
                       read_back=False)
    return None


def _check(name: str, desk: Desk) -> Outcome:
    """Classify strict read-back, keeping blind paths explicitly unverified.

    A failed read leaves every expectation unconfirmed. The operation must
    still acknowledge END before any selection can be committed.
    """
    model = device_for_name(desk.config.device_name)
    registers = expected_registers(desk.config)
    try:
        result = desk.verify()
    except OSError as exc:
        # All apply phases completed, but this read-back failed. The outer
        # operation still requires a successful lease end before changing
        # the profile marker; connection loss cannot commit success.
        log.error("profile %r applied; it cannot be verified: %s", name, exc)
        return Outcome(state=APPLIED_UNVERIFIED, name=name,
                       reason=exc.strerror or str(exc),
                       unverified=tuple(sorted(registers)), read_back=False)
    unverified = sorted(result.mismatched + result.unobserved)
    if not unverified:
        return Outcome(state=APPLIED_VERIFIED, name=name, read_back=True)
    # Registers this backend never reports are not evidence of a problem,
    # but they are still not confirmation -- so they stay in the list
    # rather than being quietly dropped from it, and are named as the
    # separate thing they are.
    blind = sorted(p for p in result.unobserved
                   if not register_ever_reported(p, model))
    state = APPLIED_MISMATCHED if result.mismatched else APPLIED_UNVERIFIED
    reason = ("%d mismatched, %d not observed, %d never reported by this backend"
              % (len(result.mismatched), len(result.unobserved), len(blind))
              if result.mismatched else
              "%d unconfirmed, %d of them never reported by this backend"
              % (len(unverified), len(blind)))
    return Outcome(state=state, name=name, reason=reason,
                   unverified=tuple(unverified), unverifiable=tuple(blind), read_back=True,
                   mismatched=tuple(result.mismatched), unobserved=tuple(result.unobserved),
                   invalid=tuple(result.invalid))


def save_capture(name: str, text: str, config_path: Optional[Path] = None) -> Path:
    """Store a captured desk as a new profile, kept only if it loads like any other.

    Never replaces a file: the name must be free, and the profile appears in one link of a
    completed temporary file that has already loaded as that profile -- so at no moment is
    there an unchecked profile a switch could pick up. Validated in the default mode: a
    section routing.conf has that this version ignores is no reason to refuse a capture.
    """
    config_path = config_path or discover_config_path()
    target = profile_path(name, config_path)
    if os.path.lexists(target):
        raise ConfigError("profile %r already exists (%s); choose another name" % (name, target))
    try:
        target.parent.mkdir(mode=0o755, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".capture-", suffix=".tmp", dir=target.parent)
    except OSError as exc:
        raise ConfigError("cannot store profile %r in %s: %s"
                          % (name, target.parent, exc.strerror or exc)) from None
    try:
        _store_checked(fd, Path(temporary), target, name, text, config_path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
    return target


def _store_checked(fd: int, temporary: Path, target: Path, name: str, text: str,
                   config_path: Optional[Path]) -> None:
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ConfigError("cannot store profile %r: %s" % (name, exc.strerror or exc)) from None
    try:
        _profile_at(temporary, name, config_path, None, strict=False)
    except ConfigError as exc:
        raise ConfigError("the captured desk would not load as profile %r (%s); "
                          "nothing kept" % (name, exc)) from None
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise ConfigError("profile %r appeared while capturing; nothing replaced"
                          % name) from None
    except OSError as exc:
        raise ConfigError("cannot store profile %r: %s" % (name, exc.strerror or exc)) from None


#: The main desk on a `--diff-profile` command line. No profile name can start with ':'.
MAIN_DESK = ":main"


def _desk_named(name: str, config_path: Optional[Path], strict: bool) -> Config:
    if name == MAIN_DESK:
        return _restore_config(config_path or discover_config_path(), strict=strict)
    return load_profile(name, config_path, strict=strict)


def _set_by(config: Config) -> Dict[str, Tuple[str, str]]:
    """Each register a desk sets: its value as written, and the policy it holds it with."""
    device = device_for_name(config.device_name)
    return {entry.path: (" ".join(map(str, entry.args)),
                         "remember" if entry.base
                         else policy_for(entry.path, device, config.policies).value)
            for entry in desired(config)}


def _natural(path: str) -> Tuple[Union[int, str], ...]:
    return tuple(int(part) if part.isdigit() else part for part in path.split("/"))


def compare_desks(first: str, second: str, config_path: Optional[Path] = None, *,
                  strict: bool = False) -> Sequence[str]:
    """What choosing ``second`` over ``first`` would set differently, without a device.

    Either name is a profile, or ``:main`` for routing.conf, loaded as a switch would load
    it. One line per register the two desks set differently: its value in each (``-`` where
    a desk leaves it alone) and, where it differs, the policy it is held with.
    """
    sides = [_set_by(_desk_named(name, config_path, strict)) for name in (first, second)]
    lines = []
    for path in sorted(set(sides[0]) | set(sides[1]), key=_natural):
        before, after = (side.get(path) for side in sides)
        if before == after:
            continue
        line = "%s: %s -> %s" % (path, before[0] if before else "-", after[0] if after else "-")
        if before and after and before[1] != after[1]:
            line += " (policy %s -> %s)" % (before[1], after[1])
        lines.append(line)
    return lines


def describe_profiles(config_path: Optional[Path] = None, *,
                      strict: bool = False) -> Sequence[str]:
    """Profile names with a one-line summary each, for ``--list-profiles``.

    The active one is marked, because "which desk am I on" is the
    question this list is most often asked.
    """
    config_path = config_path or discover_config_path()
    active = active_profile(config_path)
    lines = []
    for name in list_profiles(config_path):
        mark = "  (active)" if name == active else ""
        try:
            config = load_profile(name, config_path, strict=strict)
        except ConfigError as exc:
            lines.append("%-16s  BROKEN: %s%s" % (name, exc, mark))
            continue
        lines.append("%-16s  %d route(s), %d channel setting(s)%s"
                     % (name, len(config.routes), len(config.channels), mark))
    return lines


def _check_configurations(config_path: Optional[Path]) -> Sequence[Path]:
    """Strictly validate the main desk, every profile and the stored selection.

    No device access or marker change. Directory enumeration errors and broken
    .conf entries must refuse the check rather than shrink its apparent scope.
    """
    if config_path is None:
        raise ConfigError("no config file found, so there is no main desk to check")
    try:
        _restore_config(config_path, strict=True)
    except ConfigError as exc:
        raise ConfigError("%s: %s" % (config_path, exc)) from None
    directory = profiles_dir(config_path)
    if directory is None:
        raise ConfigError("no config directory, so no profiles to check")
    try:
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".conf")
    except FileNotFoundError:
        paths = []
    except OSError as exc:
        raise ConfigError("cannot list profiles in %s: %s" % (directory, exc)) from None
    for path in paths:
        try:
            load_profile(path.stem, config_path, strict=True)
        except ConfigError as exc:
            raise ConfigError("%s: %s" % (path, exc)) from None
    # A marker naming a removed profile is not in the directory listing.
    effective_config(config_path, strict=True)
    return (config_path, *paths)


def _hand_over_to_the_unit(outcome: Outcome, config_path: Optional[Path]) -> Outcome:
    """Hand a persisted or partial selection to the matching running service.

    Its verifier must follow the new selection. Reloading another config
    could overwrite this one; the unit's own arguments and environment
    determine its config. A failed reload preserves the operation's facts.
    """
    if outcome.state == REFUSED:
        return outcome
    if outcome.state != WRITTEN_IN_PART and not outcome.persisted:
        log.warning("%s not reloaded: the marker was not written, and its "
                    "reconcile would undo what was just applied", SERVICE_UNIT)
        return outcome
    told, unit_desk = _unit_desk()
    if config_path is not None and unit_desk is not None \
            and not _same_file(config_path, unit_desk):
        log.info("%s not reloaded: it runs %s, and this switch was for %s",
                 SERVICE_UNIT, unit_desk, config_path)
        return outcome
    reloaded = reload_service()
    if reloaded == RELOAD_DONE:
        # The unit decides what it does with the desk: it has the facts.
        # One that names another backend is not applied there, and its
        # journal says so (reload._kept_for_this_process).
        log.info("%s reloaded; its own reconcile follows the new desk, or "
                 "says in its journal why it does not", SERVICE_UNIT)
        if config_path is not None and not told:
            # Reloaded all the same: nearly every switch is for the unit's
            # own desk, and leaving the unit untold lets its verifier
            # re-apply the old one (0.6.3). But it is a guess, so say so.
            log.warning("could not tell which desk %s runs (its /proc entry "
                        "or its command line could not be read): if this "
                        "switch was for another desk, the unit has "
                        "re-applied its own over it", SERVICE_UNIT)
        return outcome
    if reloaded == RELOAD_NOT_RUNNING:
        log.info("%s is not running; nothing to reload", SERVICE_UNIT)
        return outcome
    log.error("%s is running but refused the reload, so it may still be "
              "acting on the previous desk; send it again with "
              "systemctl --user reload %s", SERVICE_UNIT, SERVICE_UNIT)
    return replace(outcome, reload_failed=True)


def _unit_desk() -> Tuple[bool, Optional[Path]]:
    """(whether it could be told, the config the running unit resolves).

    ``(True, None)`` is an answer -- the unit resolves no config, so it has
    no desk to re-apply -- and ``(False, None)`` is not: read once, because
    a second look after the reload may see another unit state.

    Worked out the way the unit did (reload._config_path): --config on
    its command line first, then its environment. Its own parser reads
    the command line, so an abbreviated or ``--config=`` form counts as
    it did there. A relative path is the unit's, taken against the
    unit's working directory, not this shell's.
    """
    unit = unit_process(Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc")))
    if unit is None:
        return False, None
    # A line this parser cannot read is "cannot be told", and its usage
    # text belongs to the unit, not on this switch's stderr.
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            args, _ = build_arg_parser().parse_known_args(list(unit.argv[1:]))
    except SystemExit:
        return False, None
    named = args.config or discover_config_path(unit.environ)
    return True, None if named is None else unit.cwd / named


def _same_file(one: Path, other: Optional[Path]) -> bool:
    if other is None:
        return False
    try:
        return one.resolve() == other.resolve()
    except OSError:
        return False
