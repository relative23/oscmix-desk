"""Argument parsing and the process entry point."""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import math
import os
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .config import load_config
from .constants import (
    DEFAULT_DEVICE_TIMEOUT,
    EXIT_CONFIG,
    EXIT_NOT_PERSISTED,
    EXIT_OK,
    EXIT_RELOAD_FAILED,
    SERVICE_UNIT,
    __version__,
)
from .errors import ConfigError
from .log import log
from .model import Config
from .notices import log_desk_notices
from .outcome import REFUSED, Outcome
from .paths import discover_config_path, profile_path
from .pipewire import find_sink, generate_pipewire_conf, pw_dump_objects
from .process import (
    RELOAD_DONE,
    RELOAD_NOT_RUNNING,
    reload_service,
    unit_process,
)
from .profiles import (
    describe_profiles,
    effective_config,
    load_profile,
    restore_main,
    switch_profile,
)
from .reads import _diff, _dump_config, _snapshot
from .session import run_session


def build_arg_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="oscmix-session",
        description="Supervise the oscmix backend for an RME Fireface interface.",
    )
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="routing config (default: ~/.config/oscmix/routing.conf)")
    parser.add_argument("--device", metavar="NAME",
                        help="ALSA client name to wait for (overrides config)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_DEVICE_TIMEOUT,
                        metavar="SECONDS", help="how long to wait for the device")
    parser.add_argument("--osc-port", type=int, metavar="PORT",
                        help="UDP port oscmix listens on (overrides config)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be started and sent, then exit")
    parser.add_argument("--snapshot", action="store_true",
                        help="print every register the device reports, for "
                             "comparing two moments; unlike --dump-config "
                             "this is not a config and holds nothing back")
    parser.add_argument("--diff", action="store_true",
                        help="compare the running device against the config "
                             "and print what an apply would write, without "
                             "writing it")
    parser.add_argument("--dump-config", action="store_true",
                        help="ask the running device for its state and print "
                             "a routing.conf that reproduces what it reports")
    parser.add_argument("--pipewire-sinks", action="store_true",
                        help="print a PipeWire config with one named sink "
                             "per stereo route, then exit")
    parser.add_argument("--pipewire-target", metavar="NODE",
                        help="Fireface sink node.name for --pipewire-sinks "
                             "(default: auto-detect via pw-dump)")
    parser.add_argument("--profile", metavar="NAME",
                        help="switch the desk to profiles/NAME.conf and "
                             "report the outcome, then exit")
    parser.add_argument("--list-profiles", action="store_true",
                        help="list the profiles found beside the config; "
                             "the active one is marked")
    parser.add_argument("--no-profile", action="store_true",
                        help="apply routing.conf again and forget the "
                             "active profile, then exit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=__version__)
    return parser



def main(argv: Optional[Sequence[str]] = None) -> int:
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


def _override_device(config: Config, name: str) -> None:
    """``--device``: the ALSA client to wait for, and the model from here on.

    It arrives after the file was validated, so the channels and sections
    were checked for the device the *file* names; see
    ``notices.replaced_device_warning``. Refused together with a switch or
    a restore, which take their interface from the config and never saw
    the override (``_refuse_conflicting_actions``). Remembered as an
    override, like ``--osc-port``: a desk this process reads again is
    resolved the way a restart would resolve it.
    """
    config.device_name = name
    config.overrides = config.overrides._replace(device_name=name)


def _desk_in_effect(config_path: Optional[Path]) -> Optional[Config]:
    """The desk this invocation is about, named in the log; None if refused.

    The active profile if one is remembered, else routing.conf (ADR
    0018); only routing.conf itself can refuse.
    """
    try:
        config, active = effective_config(config_path)
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


def _main(argv: Optional[Sequence[str]] = None) -> int:
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
    config = _desk_in_effect(config_path)
    if config is None:
        return EXIT_CONFIG

    if args.device:
        # Stripped like `[device] name` is: the client search compares
        # the name as given, and padding matched nothing.
        _override_device(config, args.device.strip())
    if args.osc_port is not None:
        # Bounded like `[osc] port` in the file. A port outside the range
        # used to pass straight through: nothing bound it, and the first
        # symptom was the backend failing to start.
        if not 1 <= args.osc_port <= 65535:
            log.error("configuration error: --osc-port %d out of range 1..65535",
                      args.osc_port)
            return EXIT_CONFIG
        config.osc_port = args.osc_port
        config.overrides = config.overrides._replace(osc_port=args.osc_port)

    if args.dry_run and (args.profile is not None or args.no_profile):
        return _dry_run_desk(args, config_path)

    if args.list_profiles:
        for line in describe_profiles(config_path):
            sys.stdout.write(line + "\n")
        return EXIT_OK

    if args.profile is not None:
        return _switch_profile(args.profile, config_path)

    if args.no_profile:
        return _report_outcome(restore_main(config_path), config_path)

    if args.snapshot:
        return _snapshot(config)

    if args.diff or args.pipewire_sinks:
        # The two that show this desk. A dump shows the device's, and a
        # snapshot and a listing show none.
        log_desk_notices(config)
    if args.diff:
        return _diff(config)

    if args.dump_config:
        return _dump_config(config)

    if args.pipewire_sinks:
        return _pipewire_sinks(args, config)

    return run_session(args, config)


def _pipewire_sinks(args: "argparse.Namespace", config: Config) -> int:
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
#: a config error, refused before anything is written (ADR 0011).
_ACTIONS = (("--profile", "profile"), ("--no-profile", "no_profile"),
            ("--diff", "diff"), ("--dump-config", "dump_config"),
            ("--snapshot", "snapshot"), ("--pipewire-sinks", "pipewire_sinks"),
            ("--list-profiles", "list_profiles"))


def _refuse_conflicting_actions(parser: ArgumentParser,
                                args: "argparse.Namespace") -> None:
    """Two actions asked for in one command is a config error.

    Refusing before anything is written is the promise a bad profile
    already gets: it costs a message, never a fader (ADR 0011). Until
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
    if args.device is not None and not args.device.strip():
        # '' was skipped without a word, and '  ' was searched for.
        parser.error("--device needs a name")
    overrides = [flag for flag, given in (("--device", args.device),
                                          ("--osc-port", args.osc_port))
                 if given is not None]
    if overrides and asked and asked[0] in ("--profile", "--no-profile"):
        # A switch and a restore resolve their interface and ports from
        # the config (profiles._target). The overrides never reached
        # them: they were dropped without a word, while the dry run of
        # the same switch looked for the interface --device named, and
        # so showed something the switch would not do.
        parser.error("%s cannot be combined with %s: a switch or a restore "
                     "takes its interface and ports from the config"
                     % (" and ".join(overrides), asked[0]))
    if not (math.isfinite(args.timeout) and args.timeout >= 0):
        parser.error("--timeout must be a finite number of seconds, not %r"
                     % args.timeout)


def _dry_run_desk(args: "argparse.Namespace",
                  config_path: Optional[Path]) -> int:
    """Show the desk a switch would apply, without switching to it.

    Until 0.6.6 `--profile X --dry-run` took the lock, wrote the device
    and recorded the marker, which is the one thing the flag exists to
    promise it will not do. `--no-profile --dry-run` shows routing.conf
    for the same reason: it is the desk that command would restore.
    """
    try:
        desk = (load_profile(args.profile, config_path) if args.profile is not None
                else load_config(config_path))
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    # The desk exactly as the switch would load it, machine settings
    # included. Until 0.6.11 the ports and the device name of the desk
    # *in effect* were written over it, and the name is what a dry run
    # acts on: it looked for, and warned about, the active desk's
    # interface while showing a profile that names its own.
    return run_session(args, desk)


def _switch_profile(name: str, config_path: Optional[Path]) -> int:
    """Apply a profile and turn its outcome into an exit code.

    Four states, four codes, and the distinction the caller needs is
    between "nothing happened" and "something happened that I could not
    check" -- a script that treats those the same will re-run a switch
    that already took effect.

    EXIT_CONFIG for a refusal is the same code a bad routing.conf gives
    at startup, because it is the same failure: the config did not parse
    and nothing was written.
    """
    return _report_outcome(switch_profile(name, config_path=config_path),
                           config_path)


def _report_outcome(outcome: "Outcome",
                    config_path: Optional[Path] = None) -> int:
    """One line on stdout and the exit code the outcome maps to.

    Shared by the switch and by `--no-profile`, which is the same
    transaction with routing.conf as the desk (ADR 0018). Four states,
    four codes: a script that branches on `$?` has to be able to tell
    "the desk is yours and will stay" from "it is yours until the next
    start", and neither from "the unit did not hear about it".
    """
    sys.stdout.write(outcome.describe() + "\n")
    if outcome.state == REFUSED:
        return EXIT_CONFIG
    if not outcome.persisted:
        # Measured on the desk: with the marker unwritten, the reload's
        # reconcile re-read routing.conf and undid the switch two
        # seconds after it was reported as applied (ADR 0019).
        log.warning("%s not reloaded: the marker was not written, and its "
                    "reconcile would undo what was just applied",
                    SERVICE_UNIT)
        return EXIT_NOT_PERSISTED
    # The unit's own state has to follow, or its start-up verifier may
    # still be re-applying the desk it started with (process.reload_service).
    # Only when it is the unit's desk that changed: a reload after a
    # switch of some other file -- named by --config, or by OSCMIX_CONFIG
    # in this shell -- made the unit re-apply its own routing.conf over
    # that switch (0.6.9). The unit's desk is what the unit resolved --
    # its own --config, else its own environment -- not what this
    # process would.
    told, unit_desk = _unit_desk()
    if config_path is not None and unit_desk is not None \
            and not _same_file(config_path, unit_desk):
        log.info("%s not reloaded: it runs %s, and this switch was for %s",
                 SERVICE_UNIT, unit_desk, config_path)
        return EXIT_OK
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
        return EXIT_OK
    if reloaded == RELOAD_NOT_RUNNING:
        log.info("%s is not running; nothing to reload", SERVICE_UNIT)
        return EXIT_OK
    log.error("%s is running but refused the reload, so it may still be "
              "acting on the previous desk; send it again with "
              "systemctl --user reload %s", SERVICE_UNIT, SERVICE_UNIT)
    return EXIT_RELOAD_FAILED


def _unit_desk() -> Tuple[bool, Optional[Path]]:
    """(whether it could be told, the config the running unit resolves).

    ``(True, None)`` is an answer -- the unit resolves no config, so it has
    no desk to re-apply -- and ``(False, None)`` is not: read once, because
    a second look after the reload may see another unit state.

    Worked out the way the unit did (session._config_path): --config on
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
