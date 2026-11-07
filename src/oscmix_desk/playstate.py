"""What status can say about one interface's playback matrix, which the device never reports.

The UCX II cannot report its playback mix (ADR 0037: captures, firmware and TotalMix evidence,
2026-09-28 to 2026-11-04), so the desk tracks it from what it wrote. Status names one of
three states and what it rests on:

- ``known (CC Mix preset <name>)``, evidence *static*: since the interface appeared the device
  reported CC Mode on and a CC Mix preset ("6ch + phones", "8ch", "20ch"), and no desk wrote
  playback since. In those modes the firmware routes playback by a fixed table of its own --
  a static reading of the firmware (2026-11-04), not a read-back and not measured.
- ``re-established by the desk``, evidence *write record*: since the interface appeared the
  desk wrote every playback register it declares, the latest recorded write of each is the
  declared value, and the device reported CC Mix "TotalMix App". It says what was sent and
  acknowledged, never that the hardware holds it.
- ``unknown``: anything else, with the reason -- no running backend, the interface re-appeared
  since the writes or nothing was applied yet, another desk writer's write differs, the mode
  was not reported, or a preset met playback writes whose effect in that mode is not
  established.

"Since the interface appeared" is since the running backend process started: the backend exits
with its interface, so a write older than the backend was made to an interface that has gone
away since (or to a backend that restarted, which is treated alike). Neither state sees what
clients without a journal write -- the GTK mixer, raw OSC --, nor the front panel or another
host: the backend reports no playback write. Pure apart from ``interface_sections``, which reads
the journals, the records beside them (``seen``) and /proc.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path

from .constants import DEFAULT_USB_ID
from .devices import device_for_name
from .diagnostics import BackendStatus
from .discovery import lock_key
from .journal import Journal, read_journals, stamp, stamp_seconds
from .model import Config
from .osc import Args
from .reconcile import desired
from .registers import Device, register_at
from .seen import CC_MIX, CC_MODE, Record, Seen, read_all_seen

Info = dict[str, object]

KNOWN_NOTE = ("the firmware's fixed playback routing for this CC Mix mode, from a static reading "
              "of the firmware (2026-11-04); not read back and not measured. A playback write "
              "by a client without a journal (the GTK mixer, raw OSC) or by another host is "
              "not excluded")
REESTABLISHED_NOTE = ("a record of writes the backend acknowledged, never a hardware "
                      "confirmation: the device cannot report its playback matrix. Writes by "
                      "clients without a journal (the GTK mixer, raw OSC), the front panel and "
                      "other hosts are not visible; playback cells the desk does not declare "
                      "are not covered")
UNKNOWN_NOTE = ("the device cannot report its playback matrix; the desk re-establishes the "
                "playback routes it declares on its next start, reload or switch")
CHECK_NOTE = ("the device's input mix for the declared input routes when a session started, "
              "before the desk overwrote it; compared within TotalMix FX's tolerances "
              "(0.07 dB, pan 0.007 of full scale) and never a reason to stop")


def is_playback(path: str) -> bool:
    """A register of the playback matrix, or a playback link or mute flag."""
    return path.startswith("/playback/") or (path.startswith("/mix/") and "/playback/" in path)


def backend_started(pid: int, proc_root: Path, now: float | None = None) -> float | None:
    """When a process started, in seconds since the epoch; None when /proc does not say."""
    try:
        line = (proc_root / str(pid) / "stat").read_text()
        uptime = float((proc_root / "uptime").read_text().split()[0])
        # Field 22, counted after the parenthesised name, which may contain spaces.
        ticks = int(line[line.rindex(")") + 2:].split()[19])
        since_boot = ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    if not 0 <= since_boot <= uptime:
        return None
    return (time.time() if now is None else now) - uptime + since_boot


def _since(at: str, started: float) -> bool:
    moment = stamp_seconds(at)
    return moment is not None and moment > started


def _same(declared: tuple[str, Args], written: tuple[str, Args, str]) -> bool:
    """Whether a recorded write carries the declared value."""
    tags, args = declared
    if written[0] != tags or len(written[1]) != len(args):
        return False
    for tag, want, got in zip(tags, args, written[1], strict=False):
        try:
            if (float(want) != float(got)) if tag == "f" else want != got:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _mix_mode(device: Device, seen: Seen, started: float) -> tuple[int, str] | None:
    """CC Mix as the device last reported it since the interface appeared, by index and name."""
    report = seen.reports.get(CC_MIX)
    register = register_at(device, CC_MIX)
    if report is None or register is None or not _since(report[2], started):
        return None
    tags, args, _at = report
    index = args[0] if tags.startswith("i") and args else None
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    return (index, register.choices[index]) if 0 <= index < len(register.choices) else None


def _cc_mode_on(seen: Seen, started: float) -> bool:
    report = seen.reports.get(CC_MODE)
    return (report is not None and _since(report[2], started)
            and report[0] == "i" and report[1] == (1,))


def _state(state: str, summary: str, evidence: str | None, detail: str, note: str,
           **facts: object) -> Info:
    return {"state": state, "summary": summary, "evidence": evidence, "detail": detail,
            **facts, "note": note}


def unknown(detail: str, **facts: object) -> Info:
    """The state without a basis, and why."""
    return _state("unknown", "unknown", None, detail, UNKNOWN_NOTE, **facts)


def _preset(name: str, mode_on: bool, written: Mapping[str, object], **facts: object) -> Info:
    if not mode_on:
        return unknown("CC Mix reports the preset %r, but CC Mode was not reported on since the "
                       "interface appeared; whether the preset applies is not established"
                       % name, ccmix=name, **facts)
    if written:
        return unknown("CC Mix reports the preset %r, and %d playback register(s) were written "
                       "since the interface appeared; whether such writes act in this mode is "
                       "not established" % (name, len(written)), ccmix=name, **facts)
    return _state("known", "known (CC Mix preset %s)" % name, "static",
                  "CC Mode on and CC Mix %r reported since the interface appeared, and no "
                  "playback write recorded since" % name, KNOWN_NOTE, ccmix=name, **facts)


def playback_state(config: Config, journal: Journal, seen: Seen,
                   started: float | None) -> Info:
    """The playback matrix state of the desk's interface; see the module docstring."""
    device = device_for_name(config.device_name)
    if device is None or config.usb_id != DEFAULT_USB_ID:
        return _state("not-applicable", "not applicable", None,
                      "the playback evidence is for the UCX II", UNKNOWN_NOTE)
    if started is None:
        return unknown("no running backend for the interface, or its start time is unreadable")
    facts: dict[str, object] = {"since": stamp(started)}
    declared = {entry.path: (entry.tags, entry.args) for entry in desired(config)
                if is_playback(entry.path)}
    written = {path: entry for path, entry in journal.items()
               if is_playback(path) and _since(entry[2], started)}
    mix = _mix_mode(device, seen, started)
    if mix is not None and mix[0] != 0:
        return _preset(mix[1], _cc_mode_on(seen, started), written, **facts)
    if not declared:
        return unknown("the desk declares no playback routes, so nothing re-establishes them",
                       **facts)
    missing = [path for path in declared if path not in written]
    if missing:
        return unknown("%d of %d declared playback register(s) have no desk write since the "
                       "interface appeared: it re-appeared, or nothing was applied yet"
                       % (len(missing), len(declared)), **facts)
    differs = sorted(path for path in declared if not _same(declared[path], journal[path]))
    if differs:
        return unknown("the latest recorded write of %d declared playback register(s) is not "
                       "the desk's value: another desk writer changed %s"
                       % (len(differs), ", ".join(differs[:3])), **facts)
    if mix is None:
        return unknown("CC Mix was not reported since the interface appeared, so the playback "
                       "mode is not known", **facts)
    last = max(entry[2] for path, entry in written.items() if path in declared)
    return _state("re-established", "re-established by the desk", "write record",
                  "%d declared playback register(s) written by the desk since the interface "
                  "appeared, the last at %s; CC Mix %r" % (len(declared), last, mix[1]),
                  REESTABLISHED_NOTE, declared=len(declared), ccmix=mix[1], **facts)


def connect_section(record: Record | None, started: float | None) -> Info:
    """The last connect check, and whether it belongs to the running backend."""
    if record is None:
        return {"state": "not-checked", "detail": "no connect check recorded: the desk "
                "declares no input routes, or no session started since the boot",
                "note": CHECK_NOTE}
    current = started is not None and _since(str(record["at"]), started)
    return {**record, "current": current, "note": CHECK_NOTE}


def interface_sections(config: Config, config_path: Path | None, backend: BackendStatus,
                       proc_root: Path) -> dict[str, Info]:
    """``playback_matrix`` and ``connect_check`` for the status report; only reads files."""
    serial = backend.device.serial if backend.device is not None else config.serial
    if config.usb_id != DEFAULT_USB_ID or not serial:
        state = playback_state(config, {}, Seen(), None)
        if state["state"] == "unknown":
            state = unknown("no interface identified")
        return {"playback_matrix": state, "connect_check": connect_section(None, None)}
    key = lock_key(config.usb_id, serial)
    seen = read_all_seen(config_path, key)
    started = (backend_started(backend.pid, proc_root)
               if backend.state == "ready" and backend.pid is not None else None)
    try:
        state = playback_state(config, read_journals(config_path, key), seen, started)
    except ValueError as exc:
        state = unknown("the desk in effect has no plan: %s" % exc)
    return {"playback_matrix": state, "connect_check": connect_section(seen.connect, started)}
