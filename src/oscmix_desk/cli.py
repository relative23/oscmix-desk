"""Argument parsing and the process entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from argparse import ArgumentParser
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .backend import loopback
from .config import Config, discover_config_path, profile_path
from .constants import (
    DEFAULT_DEVICE_TIMEOUT,
    DUMP_LISTEN_SETTLE,
    EXIT_CONFIG,
    EXIT_DIFFERS,
    EXIT_FAILURE,
    EXIT_OK,
    SERVICE_UNIT,
    __version__,
)
from .discovery import device_firmware, device_serial
from .errors import ConfigError
from .log import log
from .pipewire import generate_pipewire_conf, pw_sink_info
from .process import reload_service
from .profiles import (
    REFUSED,
    Outcome,
    describe_profiles,
    effective_config,
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    config_path = args.config or discover_config_path()
    try:
        # The active profile if one is remembered, else routing.conf
        # (ADR 0018); only routing.conf itself can refuse the start.
        config, active = effective_config(config_path)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    if config_path is None:
        log.info("no routing.conf found; using defaults without routing")
    else:
        source = (profile_path(active, config_path) if active
                  else config_path)
        log.info("configuration: %s (%d route(s), %d channel setting(s), "
                 "%d global setting(s))", source, len(config.routes),
                 len(config.channels), len(config.globals))

    if args.device:
        config.device_name = args.device
    if args.osc_port is not None:
        # Bounded like `[osc] port` in the file. A port outside the range
        # used to pass straight through: nothing bound it, and the first
        # symptom was the backend failing to start.
        if not 1 <= args.osc_port <= 65535:
            log.error("configuration error: --osc-port %d out of range 1..65535",
                      args.osc_port)
            return EXIT_CONFIG
        config.osc_port = args.osc_port

    if args.list_profiles:
        for line in describe_profiles(config_path):
            sys.stdout.write(line + "\n")
        return EXIT_OK

    if args.profile:
        return _switch_profile(args.profile, config_path)

    if args.no_profile:
        return _report_outcome(restore_main(config_path))

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
    info = pw_sink_info(config.device_name, target=target)
    if info:
        target, positions = info
        log.info("target sink %s (%s channel layout)", target,
                 "%d-channel" % len(positions) if positions else "unknown")
    elif target is None:
        log.warning("could not auto-detect the Fireface sink via pw-dump; "
                    "replace the FIXME target in the output "
                    "('wpctl status' shows the sink name)")
    try:
        sys.stdout.write(generate_pipewire_conf(config, target, positions))
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    return EXIT_OK


def _switch_profile(name: str, config_path: Optional[Path]) -> int:
    """Apply a profile and turn its outcome into an exit code.

    Three states, three codes, and the distinction the caller needs is
    between "nothing happened" and "something happened that I could not
    check" -- a script that treats those the same will re-run a switch
    that already took effect.

    EXIT_CONFIG for a refusal is the same code a bad routing.conf gives
    at startup, because it is the same failure: the config did not parse
    and nothing was written.
    """
    return _report_outcome(switch_profile(name, config_path=config_path))


def _report_outcome(outcome: "Outcome") -> int:
    """One line on stdout and the exit code the outcome maps to.

    Shared by the switch and by `--no-profile`, which is the same
    transaction with routing.conf as the desk (ADR 0018).
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
        return EXIT_OK
    # The unit's own state has to follow, or its start-up verifier may
    # still be re-applying the desk it started with (process.reload_service).
    if reload_service():
        log.info("%s reloaded, so its own reconcile follows the new desk",
                 SERVICE_UNIT)
    else:
        log.info("%s is not running; nothing to reload", SERVICE_UNIT)
    return EXIT_OK


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
        "dsp %s\n" % (len(rows), config.device_name, device_serial() or "?",
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
    listener = device.listen()
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
