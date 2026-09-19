"""The three actions that read the device: snapshot, diff, dump.

``--snapshot`` prints what the device reports, ``--diff`` what a start
would write and why, ``--dump-config`` the device's state as a
``routing.conf``. Each binds the receive port, asks for a dump and
listens until it has gone quiet; none of them writes a register. A port
the mixer GUI holds is exit 1 with that reason, and so is one that
cannot be bound at all (ADR 0025).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from .backend import loopback
from .constants import (
    DUMP_LISTEN_SETTLE,
    EXIT_DIFFERS,
    EXIT_FAILURE,
    EXIT_OK,
)
from .devices import device_for_name
from .discovery import device_firmware, resolve_device
from .dump import (
    channels_from_observed,
    globals_from_observed,
    render_config,
    routes_from_observed,
)
from .errors import DeviceAmbiguous, ReceivePortError
from .log import log
from .model import Config
from .osc import (
    Args,
    Value,
)
from .process import port_holder
from .reconcile import (
    PHASE_CHANNEL,
    PHASE_LINK,
    PHASE_MIX,
    REWRITE,
    Write,
    desired,
    observed,
    plan,
)

#: How long --dump-config listens for the device's reply. The dump is
#: over in ~2 s on a UCX II (tests/data/cold-plug-timeline.json); this is
#: several times that so a slower device is not truncated, and it costs
#: nothing on a fast one because the read stops when the window ends.
DUMP_READ_SECONDS = 8.0

#: Stop early once no *new* register has arrived for this long.
DUMP_QUIET_SECONDS = 1.0


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


def _diff_line(write: Write, seen: Dict[str, Args]) -> str:
    """One write as `path  config-value  device-value  reason`."""
    return "%-34s %-14s device %-14s %s" % (
        write.path, _values(write.args), _values(seen.get(write.path)),
        write.reason.value)


def _values(args: Optional[Args]) -> str:
    """OSC arguments as a config would read them, or a dash for absent.

    A missing register and a register holding an empty value are
    different facts, and a diff that printed both as blank would be
    saying the device is silent when it answered.
    """
    if args is None:
        return "-"
    return ", ".join(_one_value(value) for value in args)


def _one_value(value: Value) -> str:
    if isinstance(value, float):
        return "%.1f" % value
    return str(value)


def _read_device(config: Config) -> Optional[Dict[str, Args]]:
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

    seen: Dict[str, Args] = {}
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
    except ReceivePortError as exc:
        # The port was had and then could not be read. Not silence from
        # the backend, which is what an empty read is taken for below.
        log.error("%s", exc.strerror)
        return None
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
                    routes=tuple(routes_from_observed(observed(seen))),
                    channels=tuple(channels_from_observed(seen, model)),
                    globals=tuple(globals_from_observed(seen, model)))
    log.info("read %d registers; %d input route(s), %d channel setting(s) "
             "and %d global setting(s) reconstructed",
             len(seen), len(dumped.routes), len(dumped.channels),
             len(dumped.globals))
    sys.stdout.write(render_config(dumped, model))
    return EXIT_OK
