"""Argument parsing and the process entry point."""

from __future__ import annotations

import argparse
import logging
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from .arguments import build_arg_parser
from .constants import (
    EXIT_CONFIG,
    EXIT_DIFFERS,
    EXIT_FAILURE,
    EXIT_NOT_PERSISTED,
    EXIT_OK,
    EXIT_RELOAD_FAILED,
)
from .errors import ConfigError
from .hostservice import maintenance_problem
from .log import log
from .model import Config, Overrides
from .notices import log_desk_notices
from .outcome import APPLIED_MISMATCHED, REFUSED, WRITTEN_IN_PART, Outcome
from .paths import discover_config_path, profile_path
from .pipewire import find_sink, generate_pipewire_conf, pw_dump_objects
from .preview import transition_lines
from .profiles import (
    _check_configurations,
    _restore_config,
    compare_desks,
    describe_profiles,
    effective_config,
    load_profile,
    restore_main,
    save_capture,
    switch_profile,
)
from .reads import _diff, _dump_config, _snapshot, capture_text
from .session import run_session
from .status import print_status


def main(argv: Sequence[str] | None = None) -> int:
    """The entry point: `_main`, with Ctrl-C turned into an exit code.

    A SIGINT handler exists once the backend runs; before that -- the
    device wait, a lock wait, a read of the device -- an interrupt was a
    traceback. 130 is the shell's convention for it.
    """
    try:
        return _main(argv)
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130


def _config_overrides(args: argparse.Namespace) -> Overrides | None:
    """``--device`` and ``--osc-port`` as the parser takes them, or None.

    Handed to the parser rather than put over its result: the desk is then
    validated for the interface it goes to, where ``--device`` used to
    arrive after the file had been checked for the one it names (0.7.0).
    Refused together with a switch or a restore, which take their
    interface from the config and never saw the override
    (``_refuse_conflicting_actions``).
    """
    if args.osc_port is not None and not 1 <= args.osc_port <= 65535:
        # Bounded like `[osc] port` in the file. A port outside the range
        # used to pass straight through: nothing bound it, and the first
        # symptom was the backend failing to start.
        log.error("configuration error: --osc-port %d out of range 1..65535",
                  args.osc_port)
        return None
    # Stripped like `[device] name` is: the client search compares the
    # name as given, and padding matched nothing.
    return Overrides(args.device.strip() if args.device else None,
                       args.osc_port)


def _desk_in_effect(config_path: Path | None,
                    overrides: Overrides, *, strict: bool = False) -> Config | None:
    """The desk this invocation is about, named in the log; None if refused.

    The active profile if one is remembered, else routing.conf; only
    the selected validation mode can refuse either file.
    """
    try:
        config, active = effective_config(config_path, overrides, strict=strict)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return None
    if config_path is None:
        log.info("no routing.conf found; using defaults without routing")
    else:
        source = (profile_path(active, config_path) if active
                  else config_path)
        log.info("configuration: %s (%d route(s), %d channel setting(s), "
                 "%d global setting(s))", source, len(config.routes),
                 len(config.channels), len(config.globals))
    return config


def _main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    # A usage error before any file is read: it ran after the config
    # load until 0.6.10, so a broken routing.conf answered a conflicting
    # pair with "configuration error" and the pair went unnamed.
    _refuse_conflicting_actions(parser, args)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    config_path = args.config or discover_config_path()
    overrides = _config_overrides(args)
    if args.check and overrides is not None:
        return _check_files(config_path)
    if args.diff_profile and overrides is not None:
        return _compare_desks(args.diff_profile, config_path, strict=args.strict)
    if args.status and overrides is not None:
        return print_status(config_path, overrides, args.json, strict=args.strict)
    if overrides is not None and args.dry_run and (args.profile is not None or args.no_profile):
        return _dry_run_desk(args, config_path)
    if overrides is None:
        return EXIT_CONFIG

    # An explicit target can recover an unusable active profile. Its loader
    # validates the main config and target; it does not use an old selection.
    if args.profile is not None or args.no_profile:
        problem = maintenance_problem()
        if problem:
            log.error("%s; complete installation recovery before activation", problem)
            return EXIT_CONFIG
        if args.profile is not None:
            return _switch_profile(args.profile, config_path, strict=args.strict)
        return _report_outcome(restore_main(config_path, strict=args.strict))

    config = _desk_in_effect(config_path, overrides, strict=args.strict)
    if config is None:
        return EXIT_CONFIG

    if args.list_profiles:
        for line in describe_profiles(config_path, strict=args.strict):
            sys.stdout.write(line + "\n")
        return EXIT_OK

    if not args.dry_run and not args.pipewire_sinks:
        problem = maintenance_problem()
        if problem:
            log.error("%s; complete installation recovery before activation", problem)
            return EXIT_CONFIG

    return _act(args, config, config_path)


def _act(args: argparse.Namespace, config: Config, config_path: Path | None) -> int:
    """The actions on the desk in effect, once it loaded and no maintenance blocks them."""
    if args.snapshot:
        return _snapshot(config, config_path)

    if args.diff or args.pipewire_sinks:
        # The two that show this desk. A dump shows the device's, and a
        # snapshot and a listing show none.
        log_desk_notices(config)
    if args.diff:
        return _diff(config, config_path)

    if args.capture is not None:
        return _capture(args.capture, config, config_path)

    if args.dump_config and args.include_written:
        return _dump_config(config, config_path, include_written=True)
    if args.dump_config:
        return _dump_config(config, config_path)

    if args.pipewire_sinks:
        return _pipewire_sinks(args, config)

    return run_session(args, config)


def _check_files(config_path: Path | None) -> int:
    """Report strict offline validation, without entering a device operation."""
    try:
        checked = _check_configurations(config_path)
    except ConfigError as exc:
        log.error("configuration check refused: %s", exc)
        return EXIT_CONFIG
    for path in checked:
        print("valid: %s" % path)
    print("configuration check passed (%d file(s)); no device access" % len(checked))
    return EXIT_OK


def _capture(name: str, config: Config, config_path: Path | None) -> int:
    """Read the device and keep its desk as a new profile; nothing on the device changes."""
    text = capture_text(config, config_path)
    if text is None:
        return EXIT_FAILURE
    try:
        path = save_capture(name, text, config_path)
    except ConfigError as exc:
        log.error("capture refused: %s", exc)
        return EXIT_CONFIG
    print("captured profile %r: %s" % (name, path))
    return EXIT_OK


def _compare_desks(names: Sequence[str], config_path: Path | None, *,
                   strict: bool) -> int:
    """Print what two desks set differently; EXIT_DIFFERS if anything does."""
    first, second = names
    try:
        lines = compare_desks(first, second, config_path, strict=strict)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    for line in lines:
        print(line)
    if not lines:
        print("%s and %s set the same registers to the same values" % (first, second))
        return EXIT_OK
    print("%d register(s) differ; no device access" % len(lines))
    return EXIT_DIFFERS


def _pipewire_sinks(args: argparse.Namespace, config: Config) -> int:
    """Print one named PipeWire sink per stereo route of the desk in effect."""
    target, positions = args.pipewire_target, None
    objects = pw_dump_objects()
    info = None if objects is None else find_sink(objects, config.device_name,
                                                  target)
    if info:
        target, positions = info
        log.info("target sink %s (%s channel layout)", target,
                 "%d-channel" % len(positions) if positions else "unknown")
    elif target is None:
        log.warning("could not auto-detect the Fireface sink via pw-dump; "
                    "replace the FIXME target in the output "
                    "('wpctl status' shows the sink name)")
    else:
        # Named and not found: the channel layout below is then the
        # surround table, which is wrong for a Direct/pro-audio sink --
        # the failure TROUBLESHOOTING section 9 describes. Say so rather
        # than print a config that looks right.
        log.warning("%s; the channel positions below assume the 7.1 "
                    "surround layout, check them against 'pw-dump' before "
                    "loading this",
                    "pw-dump could not be read, so sink %r was not checked"
                    % target if objects is None
                    else "no sink named %r in pw-dump" % target)
    try:
        sys.stdout.write(generate_pipewire_conf(config, target, positions))
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    return EXIT_OK


#: The things one invocation can be asked to do. Two of them at once is
#: a config error, refused before anything is written.
_ACTIONS = (("--profile", "profile"), ("--no-profile", "no_profile"),
            ("--diff", "diff"), ("--dump-config", "dump_config"),
            ("--snapshot", "snapshot"), ("--pipewire-sinks", "pipewire_sinks"),
            ("--list-profiles", "list_profiles"), ("--status", "status"), ("--check", "check"),
            ("--diff-profile", "diff_profile"), ("--capture", "capture"))


def _refuse_conflicting_actions(parser: argparse.ArgumentParser,
                                args: argparse.Namespace) -> None:
    """Two actions asked for in one command is a config error.

    Refusing before anything is written is the promise a bad profile
    already gets: it costs a message, never a fader. Until
    0.6.6 the first branch in the dispatch simply won, so `--profile X
    --no-profile` switched and `--profile X --diff` wrote the device and
    then compared it against something else. Until 0.6.10 only those
    three pairs were refused: `--no-profile --diff` restored the desk and
    never diffed, `--profile X --snapshot` switched and printed nothing.
    Every pair is refused now, and `--dry-run` goes only with a switch, a
    restore, or a plain start.
    """
    asked = [flag for flag, attr in _ACTIONS
             if getattr(args, attr) not in (None, False)]
    if len(asked) > 1:
        parser.error("%s cannot be combined" % " and ".join(asked))
    if args.dry_run and asked and asked[0] not in ("--profile", "--no-profile"):
        parser.error("--dry-run cannot be combined with %s" % asked[0])
    if args.json and not args.status:
        parser.error("--json requires --status")
    if args.include_written and not args.dump_config:
        parser.error("--include-written requires --dump-config")
    if args.device is not None and not args.device.strip():
        # '' was skipped without a word, and '  ' was searched for.
        parser.error("--device needs a name")
    override_flags = [flag for flag, given in (("--device", args.device),
                                               ("--osc-port", args.osc_port))
                      if given is not None]
    if override_flags and asked and asked[0] in ("--profile", "--no-profile", "--check",
                                                 "--diff-profile"):
        # A switch and a restore resolve their interface and ports from
        # the config (profiles._target). The overrides never reached
        # them: they were dropped without a word, while the dry run of
        # the same switch looked for the interface --device named, and
        # so showed something the switch would not do.
        operation = {"--check": "a check", "--diff-profile": "a comparison"}.get(
            asked[0], "a switch or a restore")
        parser.error("%s cannot be combined with %s: %s "
                     "takes its interface and ports from the config"
                     % (" and ".join(override_flags), asked[0], operation))
    if not (math.isfinite(args.timeout) and args.timeout >= 0):
        parser.error("--timeout must be a finite number of seconds, not %r"
                     % args.timeout)


def _dry_run_desk(args: argparse.Namespace,
                  config_path: Path | None) -> int:
    """Show the desk a switch would apply, without switching to it.

    Until 0.6.6 `--profile X --dry-run` took the lock, wrote the device
    and recorded the marker, which is the one thing the flag exists to
    promise it will not do. `--no-profile --dry-run` shows routing.conf
    for the same reason: it is the desk that command would restore.
    """
    try:
        desk = (load_profile(args.profile, config_path, strict=args.strict)
                if args.profile is not None
                else _restore_config(config_path, strict=args.strict))
        # Only the target is applied. The previous declared desk is a preview
        # reference; its compatibility fallback also permits recovery from a
        # broken active selection, as the real explicit operation does.
        previous, active = effective_config(config_path)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    source = profile_path(active, config_path) if active else config_path
    target = (profile_path(args.profile, config_path) if args.profile is not None
              else config_path)
    print("Profile preview: %s -> %s" % (source or "defaults", target or "defaults"))
    for line in transition_lines(previous, desk):
        print(line)
    # The desk exactly as the switch would load it, machine settings
    # included. Until 0.6.11 the ports and the device name of the desk
    # *in effect* were written over it, and the name is what a dry run
    # acts on: it looked for, and warned about, the active desk's
    # interface while showing a profile that names its own.
    return run_session(args, desk)


def _switch_profile(name: str, config_path: Path | None, *, strict: bool = False) -> int:
    """Apply a profile and turn its outcome into an exit code.

    The codes are in docs/ARCHITECTURE.md, and the distinction the caller
    needs most is between "nothing happened" (2) and "something happened"
    (0, 1, 3, 4, 5) -- a script that treats those the same will re-run a
    switch that already took effect.

    EXIT_CONFIG for a refusal is the same code a bad routing.conf gives
    at startup, because it is the same failure: the config did not parse
    and nothing was written.
    """
    return _report_outcome(switch_profile(name, config_path=config_path, strict=strict))


def _report_outcome(outcome: Outcome) -> int:
    """Print the result and map device, persistence and reload facts to a code."""
    sys.stdout.write(outcome.describe() + "\n")
    if outcome.state == REFUSED:
        return EXIT_CONFIG
    if outcome.state == WRITTEN_IN_PART:
        return EXIT_FAILURE
    if not outcome.persisted:
        return EXIT_NOT_PERSISTED
    if outcome.reload_failed:
        return EXIT_RELOAD_FAILED
    if outcome.state == APPLIED_MISMATCHED:
        return EXIT_DIFFERS
    return EXIT_OK
