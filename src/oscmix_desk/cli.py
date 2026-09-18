"""Argument parsing and the process entry point."""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import math
import os
import sys
import time
from argparse import ArgumentParser
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .backend import loopback
from .config import (
    Config,
    discover_config_path,
    load_config,
    log_device_replaced,
    profile_path,
)
from .constants import (
    DEFAULT_DEVICE_TIMEOUT,
    DUMP_LISTEN_SETTLE,
    EXIT_CONFIG,
    EXIT_DIFFERS,
    EXIT_FAILURE,
    EXIT_NOT_PERSISTED,
    EXIT_OK,
    EXIT_RELOAD_FAILED,
    SERVICE_UNIT,
    __version__,
)
from .discovery import device_firmware, resolve_device
from .errors import ConfigError, DeviceAmbiguous, ReceivePortError
from .log import log
from .pipewire import find_sink, generate_pipewire_conf, pw_dump_objects
from .process import (
    RELOAD_DONE,
    RELOAD_NOT_RUNNING,
    port_holder,
    reload_service,
    unit_process,
)
from .profiles import (
    REFUSED,
    Outcome,
    describe_profiles,
    effective_config,
    load_profile,
    restore_main,
    switch_profile,
)
from .reconcile import (
    PHASE_CHANNEL,
    PHASE_LINK,
    PHASE_MIX,
    REWRITE,
    Write,
    channels_from_observed,
    desired,
    globals_from_observed,
    observed,
    plan,
    render_config,
    routes_from_observed,
)
from .registers import device_for_name
from .session import run_session

#: How long --dump-config listens for the device's reply. The dump is
#: over in ~2 s on a UCX II (tests/data/cold-plug-timeline.json); this is
#: several times that so a slower device is not truncated, and it costs
#: nothing on a fast one because the read stops when the window ends.
DUMP_READ_SECONDS = 8.0

#: Stop early once no *new* register has arrived for this long.
DUMP_QUIET_SECONDS = 1.0


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


def _snapshot_serial(config: Config) -> str:
    """The box a snapshot names in its header: the one it read.

    The read goes to whatever backend holds the OSC port, so the header
    names the interface that backend bridges when that can be followed,
    and the resolved interface otherwise. In 0.6.9's first form it named
    the resolved box even when the port belonged to another one's backend
    (found by review); until 0.6.9, the first serial in the card list.
    """
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    holder = port_holder(config.osc_port, proc_root)
    if holder is not None and holder.serial:
        return holder.serial
    try:
        device = resolve_device(config.usb_id, config.device_name,
                                config.serial, proc_root)
    except DeviceAmbiguous:
        return "ambiguous"
    return device.serial or "?"


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
    ``config.log_device_replaced``. Refused together with a switch or a
    restore, which take their interface from the config and never saw
    the override (``_refuse_conflicting_actions``).
    """
    config.device_name = name
    log_device_replaced(config,
                        "--device replaces [device] name after validation")


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
        _override_device(config, args.device)
    if args.osc_port is not None:
        # Bounded like `[osc] port` in the file. A port outside the range
        # used to pass straight through: nothing bound it, and the first
        # symptom was the backend failing to start.
        if not 1 <= args.osc_port <= 65535:
            log.error("configuration error: --osc-port %d out of range 1..65535",
                      args.osc_port)
            return EXIT_CONFIG
        config.osc_port = args.osc_port

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
        # Falsy, so the override below skipped it without a word.
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
    unit_desk = _unit_desk()
    if config_path is not None and unit_desk is not None \
            and not _same_file(config_path, unit_desk):
        log.info("%s not reloaded: it runs %s, and this switch was for %s",
                 SERVICE_UNIT, unit_desk, config_path)
        return EXIT_OK
    reloaded = reload_service()
    if reloaded == RELOAD_DONE:
        log.info("%s reloaded, so its own reconcile follows the new desk",
                 SERVICE_UNIT)
        return EXIT_OK
    if reloaded == RELOAD_NOT_RUNNING:
        log.info("%s is not running; nothing to reload", SERVICE_UNIT)
        return EXIT_OK
    log.error("%s is running but refused the reload, so it may still be "
              "acting on the previous desk; send it again with "
              "systemctl --user reload %s", SERVICE_UNIT, SERVICE_UNIT)
    return EXIT_RELOAD_FAILED


def _unit_desk() -> Optional[Path]:
    """The config the running unit resolves, or None when that cannot be told.

    Worked out the way the unit did (session._config_path): --config on
    its command line first, then its environment. Its own parser reads
    the command line, so an abbreviated or ``--config=`` form counts as
    it did there. A relative path is the unit's, taken against the
    unit's working directory, not this shell's.
    """
    unit = unit_process(Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc")))
    if unit is None:
        return None
    # A line this parser cannot read is "cannot be told", and its usage
    # text belongs to the unit, not on this switch's stderr.
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            args, _ = build_arg_parser().parse_known_args(list(unit.argv[1:]))
    except SystemExit:
        return None
    named = args.config or discover_config_path(unit.environ)
    return None if named is None else unit.cwd / named


def _same_file(one: Path, other: Optional[Path]) -> bool:
    if other is None:
        return False
    try:
        return one.resolve() == other.resolve()
    except OSError:
        return False


#: Phase numbers as the diff prints them. The apply writes in this
#: order and the barrier between the first two is what ADR 0001 is
#: about, so a diff that listed writes in path order would hide the one
#: thing about them that is not obvious.
_PHASE_NAMES = ((PHASE_LINK, "links"),
                (PHASE_MIX, "mix matrix"),
                (PHASE_CHANNEL, "channel and global state"))


#: Registers that stream on their own. A snapshot exists to be diffed,
#: and a level meter changes between any two reads.
_STREAMING_SUFFIXES = ("/level", "/meter")


def _snapshot(config: Config) -> int:
    """Print every register the device reports, verbatim and sorted.

    `--dump-config` renders a *config*, so it can only show registers a
    config can express: everything with a value domain. That leaves the
    link flags, phantom power, Room EQ and the rest invisible, and a
    diff of two dumps therefore cannot prove they are unchanged.

    This was found the hard way. A measurement left `/output/9/stereo`
    unlinked on a working desk and two dumps compared equal, because
    `stereo` has no domain and no dump ever carried it. The link flags
    are the register class that produced every defect in 0.1.3.

    Meters are excluded because they change between any two reads, which
    would make every comparison noisy and none of them wrong.
    """
    seen = _read_device(config)
    if seen is None:
        return EXIT_FAILURE

    rows = [(path, args) for path, args in seen.items()
            if not path.endswith(_STREAMING_SUFFIXES)]
    log.info("read %d registers; %d in the snapshot, %d streaming and left out",
             len(seen), len(rows), len(seen) - len(rows))
    firmware = device_firmware(
        config.usb_id,
        Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices")), seen)
    # Provenance on the first line: two snapshots are only comparable
    # when they come from the same device on the same firmware, and a
    # file that does not say cannot be checked later.
    sys.stdout.write(
        "# oscmix-session --snapshot: %d registers; %s serial %s, usb %s, "
        "dsp %s\n" % (len(rows), config.device_name, _snapshot_serial(config),
                       firmware["usb_revision"] or "?",
                       "?" if firmware["dsp_version"] is None
                       else firmware["dsp_version"]))
    for path, args in sorted(rows):
        sys.stdout.write("%s %s\n" % (path, " ".join(_one_value(a)
                                                     for a in args)))
    return EXIT_OK


def _diff(config: Config) -> int:
    """Print what an apply would write, and what it would leave alone.

    The reconciler already answers this -- `plan()` is what the session
    runs on every start -- so this prints its result instead of sending
    it. Nothing is written and no register is touched.

    Exit codes, and the middle one is why this is worth stating:

        0  the device matches the config
        3  it does not (`EXIT_DIFFERS`)
        1  the read failed, so nothing is known either way

    `diff(1)` uses 1 for "differing", and that is not available here: 1
    already means EXIT_FAILURE, and a caller has to be able to tell "the
    desk drifted" from "the backend never answered". Those are opposite
    situations, and conflating them makes a monitoring check report
    healthy silence when the backend is down.

    **A rewrite is not a difference.** `/mix/<out>/playback/<pb>` is
    never reported (ADR 0002) and is written on every apply whatever the
    device holds, so counting it would make the exit code permanently 3
    and worth nothing.
    """
    seen = _read_device(config)
    if seen is None:
        return EXIT_FAILURE

    model = device_for_name(config.device_name)
    result = plan(desired(config), seen, model)

    # A rewrite is not a difference. `/mix/<out>/playback/<pb>` is never
    # reported (ADR 0002), so it is written on every apply whatever the
    # device holds -- listing it next to a real mismatch would answer
    # "has the desk drifted?" with a number that is always non-zero.
    differing = [w for w in result.writes if w.reason != REWRITE]
    rewritten = [w for w in result.writes if w.reason == REWRITE]

    log.info("read %d registers; %d differ, %d always rewritten, "
             "%d already match", len(seen), len(differing), len(rewritten),
             len(result.confirmed))

    if not differing:
        sys.stdout.write("the device matches the config\n")
    else:
        sys.stdout.write("%d register(s) differ from the config:\n\n"
                         % len(differing))
        for phase, name in _PHASE_NAMES:
            writes = [w for w in differing if w.phase == phase]
            if not writes:
                continue
            sys.stdout.write("phase %d -- %s\n" % (phase, name))
            for write in sorted(writes, key=lambda w: w.path):
                sys.stdout.write("  %s\n" % _diff_line(write, seen))
            sys.stdout.write("\n")

    if rewritten:
        sys.stdout.write(
            "%d more would be rewritten regardless: a dump never reports "
            "them, so\nan apply cannot tell whether they are already "
            "right (ADR 0002).\n" % len(rewritten))
    return EXIT_DIFFERS if differing else EXIT_OK


def _diff_line(write: Write, seen: Dict[str, Tuple[object, ...]]) -> str:
    """One write as `path  config-value  device-value  reason`."""
    return "%-34s %-14s device %-14s %s" % (
        write.path, _values(write.args), _values(seen.get(write.path)),
        write.reason)


def _values(args: Optional[Tuple[object, ...]]) -> str:
    """OSC arguments as a config would read them, or a dash for absent.

    A missing register and a register holding an empty value are
    different facts, and a diff that printed both as blank would be
    saying the device is silent when it answered.
    """
    if args is None:
        return "-"
    return ", ".join(_one_value(value) for value in args)


def _one_value(value: object) -> str:
    if isinstance(value, float):
        return "%.1f" % value
    return str(value)


def _read_device(config: Config) -> Optional[Dict[str, Tuple[object, ...]]]:
    """Every register the running backend reports, or None with a reason.

    Shared by `--dump-config` and `--diff`, which ask the device the same
    question and differ only in what they do with the answer. An empty
    read is a failure rather than an empty result: "you have no routing"
    and "nobody answered" call for opposite responses.
    """
    device = loopback(config.osc_port, config.osc_recv_port)
    try:
        listener = device.listen()
    except ReceivePortError as exc:
        # Not the GUI, so closing it would not help; say what it is.
        log.error("%s", exc.strerror)
        return None
    if listener is None:
        log.error("UDP %d is in use -- close the mixer GUI; its meters and "
                  "this read would split the device's replies",
                  config.osc_recv_port)
        return None

    seen: Dict[str, Tuple[object, ...]] = {}
    try:
        # The same settle the verifier takes, and for the same reason.
        # `setrefresh` answers with `/playback/N/stereo` synchronously,
        # out of oscmix's own memory, before the device's dump reaches
        # the wire; while nothing is bound on the receive port every
        # meter datagram draws an ICMP port-unreachable that Linux
        # queues, and the next write is dropped with it. Measured here:
        # without this, 4 of 8 reads came back with 1982 registers and
        # no playback stereo at all; with it, 11 of 11 read 2002.
        #
        # `--dump-config` has had this hole since it existed, while the
        # constant's own docstring claimed this path paid the wait.
        time.sleep(DUMP_LISTEN_SETTLE)
        device.request_dump()
        deadline = time.monotonic() + DUMP_READ_SECONDS
        quiet_after = deadline
        while time.monotonic() < deadline:
            fresh = False
            for path, _tags, args in listener.messages(0.25):
                if path not in seen:
                    fresh = True
                seen.setdefault(path, tuple(args))
            if fresh:
                # Stop once the dump goes quiet rather than always
                # waiting out the window: it is over in ~2 s on a UCX II,
                # and a command that takes 8 s regardless invites being
                # interrupted halfway. The level meters keep streaming,
                # so "quiet" means no register we had not already seen.
                quiet_after = time.monotonic() + DUMP_QUIET_SECONDS
            elif seen and time.monotonic() > quiet_after:
                break
    finally:
        listener.close()

    if not seen:
        log.error("no reply from the backend on UDP %d -- is oscmix running?",
                  config.osc_recv_port)
        return None
    return seen


def _dump_config(config: Config) -> int:
    """Print a routing.conf built from what the device reports."""
    seen = _read_device(config)
    if seen is None:
        return EXIT_FAILURE

    model = device_for_name(config.device_name)
    dumped = Config(device_name=config.device_name, usb_id=config.usb_id,
                    osc_port=config.osc_port,
                    osc_recv_port=config.osc_recv_port,
                    routes=list(routes_from_observed(observed(seen))),
                    channels=list(channels_from_observed(seen, model)),
                    globals=list(globals_from_observed(seen, model)))
    log.info("read %d registers; %d input route(s), %d channel setting(s) "
             "and %d global setting(s) reconstructed",
             len(seen), len(dumped.routes), len(dumped.channels),
             len(dumped.globals))
    sys.stdout.write(render_config(dumped, model))
    return EXIT_OK
