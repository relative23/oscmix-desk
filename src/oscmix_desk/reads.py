"""The three actions that read the device: snapshot, diff, dump.

``--snapshot`` prints what the device reports, ``--diff`` what a start
would write and why, ``--dump-config`` the device's state as a
``routing.conf``. Each opens a checked reader connection, requests a fresh
window and listens until it has gone quiet; none writes a register. Identity,
transport and decoding failures exit 1 without publishing a partial read.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .autogain import retained_gains
from .constants import (
    EXIT_DIFFERS,
    EXIT_FAILURE,
    EXIT_OK,
)
from .desk import _operation
from .devices import device_for_name
from .discovery import device_firmware, lock_key, serial_in
from .dump import (
    WrittenState,
    channels_from_observed,
    globals_from_observed,
    render_config,
    routes_from_observed,
    written_state,
)
from .journal import Journal, journal_path, journal_paths, read_journals
from .log import log
from .model import Config
from .osc import (
    Args,
    Value,
)
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
from .registers import Device

#: How long --dump-config listens for the device's reply. The dump is
#: over in ~2 s on a UCX II (tests/data/cold-plug-timeline.json); this is
#: several times that so a slower device is not truncated, and it costs
#: nothing on a fast one because the read stops when the window ends.
DUMP_READ_SECONDS = 8.0

#: Stop early once no *new* register has arrived for this long.
DUMP_QUIET_SECONDS = 1.0


@dataclass(frozen=True)
class DeviceRead:
    """Latest values received from one checked backend instance, never a snapshot cache."""

    registers: dict[str, Args]
    serial: str
    epoch: str


#: Phase numbers as the diff prints them. The apply writes in this
#: order and the barrier between the first two is what the two-phase
#: apply is about, so a diff that listed writes in path order would hide the one
#: thing about them that is not obvious.
_PHASE_NAMES = ((PHASE_LINK, "links"),
                (PHASE_MIX, "mix matrix"),
                (PHASE_CHANNEL, "channel and global state"))


#: Registers that stream on their own. A snapshot exists to be diffed,
#: and a level meter changes between any two reads.
_STREAMING_SUFFIXES = ("/level", "/meter")


def _snapshot(config: Config, config_path: Path | None = None) -> int:
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
    reading = _read_device(config, config_path)
    if reading is None:
        return EXIT_FAILURE
    seen = reading.registers

    rows = [(path, args) for path, args in seen.items()
            if not path.endswith(_STREAMING_SUFFIXES)]
    log.info("read %d registers; %d in the snapshot, %d streaming and left out",
             len(seen), len(rows), len(seen) - len(rows))
    firmware = device_firmware(
        config.usb_id,
        Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices")), seen,
        serial=reading.serial)
    # Provenance on the first line: two snapshots are only comparable
    # when they come from the same device on the same firmware, and a
    # file that does not say cannot be checked later.
    sys.stdout.write(
        "# oscmix-session --snapshot: %d registers; %s serial %s, usb %s, "
        "dsp %s; backend epoch %s; device-origin reports\n"
        % (len(rows), config.device_name, reading.serial, firmware["usb_revision"] or "?",
           "?" if firmware["dsp_version"] is None else firmware["dsp_version"], reading.epoch))
    for path, args in sorted(rows):
        sys.stdout.write("%s %s\n" % (path, " ".join(_one_value(a)
                                                     for a in args)))
    return EXIT_OK


def _diff(config: Config, config_path: Path | None = None) -> int:
    """Print what an apply would write, and what it would leave alone.

    The pure comparison uses the same declarations as application planning.
    It prints differences without applying them; operation intent separately
    decides whether an initial apply or later repair may write REMEMBER.

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
    never reported and is written on every apply whatever the
    device holds, so counting it would make the exit code permanently 3
    and worth nothing.
    """
    reading = _read_device(config, config_path)
    if reading is None:
        return EXIT_FAILURE
    seen = reading.registers

    model = device_for_name(config.device_name)
    result = plan(desired(config), seen, model)

    # A rewrite is not a difference. `/mix/<out>/playback/<pb>` is never
    # reported, so it is written on every apply whatever the
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
            "right.\n" % len(rewritten))
    return EXIT_DIFFERS if differing else EXIT_OK


def _diff_line(write: Write, seen: dict[str, Args]) -> str:
    """One write as `path  config-value  device-value  reason`."""
    return "%-34s %-14s device %-14s %s" % (
        write.path, _values(write.args), _values(seen.get(write.path)),
        write.reason.value)


def _values(args: Args | None) -> str:
    """OSC arguments as a config would read them, or a dash for absent.

    A missing register and a register holding an empty value are
    different facts, and a diff that printed both as blank would be
    saying the device is silent when it answered.
    """
    if args is None:
        return "-"
    return ", ".join(_one_value(value) for value in args)


def _one_value(value: Value) -> str:
    """Keep every reported digit: snapshots must expose sub-tenth drift."""
    return str(value)


def _read_device(config: Config, config_path: Path | None = None
                 ) -> DeviceRead | None:
    """Request a fresh window as a nonwriting subscriber of the checked backend.

    Readings belong to this epoch and use the latest decoded value per path.
    Missing, derived or disconnected data is never a complete device snapshot.
    Physical changes and GUI writes may occur during this observation window.
    """
    try:
        with _operation(config, config_path, reader=True) as desk:
            device = desk._control
            seen: dict[str, Args] = {}
            device.request_dump()
            deadline = time.monotonic() + DUMP_READ_SECONDS
            quiet_after = deadline
            while time.monotonic() < deadline:
                fresh = False
                for path, _tags, args in device.messages(0.25):
                    if path not in seen or tuple(args) != seen[path]:
                        fresh = True
                    seen[path] = tuple(args)
                if fresh:
                    quiet_after = time.monotonic() + DUMP_QUIET_SECONDS
                elif seen and time.monotonic() > quiet_after:
                    break
            if not seen:
                log.error("the backend supplied no device-origin reports in the refresh window")
                return None
            return DeviceRead(seen, serial_in(device.device_name) or "?", device.epoch.hex())
    except OSError as exc:
        log.error("cannot read the selected backend: %s", exc)
        return None


def _dump_config(config: Config, config_path: Path | None = None, *,
                 include_written: bool = False) -> int:
    """Print a routing.conf built from what the device reports.

    ``include_written`` adds what desk itself last wrote where the device reports nothing --
    the playback matrix and mute flags -- from the write journal, marked unconfirmed.
    """
    text = _rendered(config, config_path, include_written=include_written)
    if text is None:
        return EXIT_FAILURE
    sys.stdout.write(text)
    return EXIT_OK


def capture_text(config: Config, config_path: Path | None = None) -> str | None:
    """The device's state as a profile for ``--capture``, journal included; None if unread."""
    return _rendered(config, config_path, include_written=True, capture=True)


def _rendered(config: Config, config_path: Path | None, *, include_written: bool,
              capture: bool = False) -> str | None:
    reading = _read_device(config, config_path)
    if reading is None:
        return None
    seen = reading.registers

    model = device_for_name(config.device_name)
    warnings: list[str] = []
    dumped = Config(device_name=config.device_name, usb_id=config.usb_id,
                    osc_port=config.osc_port,
                    osc_recv_port=config.osc_recv_port,
                    routes=tuple(routes_from_observed(observed(seen), model, warnings)),
                    channels=tuple(channels_from_observed(seen, model, warnings)),
                    globals=tuple(globals_from_observed(seen, model, warnings)))
    log.info("read %d registers; %d input route(s), %d channel setting(s) "
             "and %d global setting(s) reconstructed",
             len(seen), len(dumped.routes), len(dumped.channels),
             len(dumped.globals))
    written = _written(config, config_path, reading, model, warnings) if include_written \
        else None
    for warning in warnings:
        log.warning("incomplete export: %s", warning)
    if capture and written is not None and not written.routes:
        log.warning("the capture knows no playback routing: desk wrote none since the boot, "
                    "and the device does not report it; switching to the capture leaves "
                    "every playback cell as it is then")
    return render_config(dumped, model, warnings, written, capture=capture,
                         autoset=_autoset_gains(dumped, seen) if capture else frozenset())


def _autoset_gains(dumped: Config, seen: Mapping[str, Args]) -> frozenset[str]:
    """The gains AutoSet owns on the device, partners of linked inputs included."""
    flags = {path: ("i", tuple(args)) for path, args in seen.items()
             if path.startswith("/input/") and path.endswith(("/autoset", "/stereo"))}
    return retained_gains(dumped, flags)


def _written(config: Config, config_path: Path | None, reading: DeviceRead,
             model: Device | None, warnings: list[str]) -> WrittenState:
    """What desk last wrote where the device is silent, from the journal beside the lock."""
    journal: Journal = {}
    source = "no write journal"
    if reading.serial == "?":
        log.warning("the interface named no serial, so its write journal is unknown")
    else:
        key = lock_key(config.usb_id, reading.serial)
        paths = journal_paths(config_path, key)
        if journal_path(config_path, key) is None:
            log.warning("no runtime lock directory, so there is no write journal")
        elif paths:
            journal = read_journals(config_path, key)
            source = ", ".join(map(str, paths))
    written = written_state(journal, reading.registers, model, source, warnings)
    log.info("write journal: %d playback route(s) and %d mute flag(s) desk wrote",
             len(written.routes), len(written.mutes))
    return written
