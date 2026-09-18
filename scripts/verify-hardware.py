#!/usr/bin/env python3
"""Measure what the routing actually does to the audio, on real hardware.

Every other test in this repository checks the OSC messages the routing
produces. That is not the same as checking what comes out of the device,
and the difference is not academic: all three defects fixed in 0.1.3 were
invisible at message level. They were found by playing a tone and reading
the device's own meters back off the wire, by hand. This makes that
reproducible.

What it does, per configured stereo route:

  1. plays a left-only and then a right-only tone into the playback pair
  2. captures ``/output/<n>/level`` reported by oscmix while it plays
  3. asserts the tone appears on the matching output and not on the other

Requires a connected interface and a running oscmix backend. Without them
it exits 77 (skip), so it can be wired into CI without CI needing a
Fireface.

    python3 scripts/verify-hardware.py --evidence evidence.json
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oscmix_desk import (
    decode_osc,
    discover_config_path,
    encode_osc,
    iter_osc_messages,
    load_config,
)
from oscmix_desk.constants import LEVEL_MIN
from oscmix_desk.discovery import (
    built_backend_revision,
    device_firmware,
    resolve_device,
)
from oscmix_desk.errors import DeviceAmbiguous

EXIT_SKIP = 77
# How much louder an output must be when its own side carries the tone
# than when the other side does. Comparing a channel against *itself*
# across the two runs is the discriminator that works: anything else
# playing at the same time sits in both measurements, whereas comparing
# left against right within one run is masked by it.
MIN_RESPONSE_DB = 12.0
# The tone must also stand clear of whatever else is on the bus, or the
# comparison above measures the other audio's stereo image instead.
MIN_ABOVE_BACKGROUND_DB = 12.0
# The meter floor: oscmix reports -inf for digital silence, which is the
# single most important reading this tool can take -- it is what a dead
# output looks like. Mapping it to a number keeps it in the arithmetic
# instead of dropping it as "no data".
SILENCE_DB = -144.0
TONE_SECONDS = 5.0
TONE_HZ = 1000.0
TONE_AMPLITUDE = 0.3


def write_tone(path: Path, left: bool, right: bool, rate: int = 48000) -> None:
    """A stereo WAV with the tone on the requested side(s)."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for n in range(int(rate * TONE_SECONDS)):
            value = int(TONE_AMPLITUDE * 32767
                        * math.sin(2 * math.pi * TONE_HZ * n / rate))
            frames += struct.pack("<hh", value if left else 0,
                                  value if right else 0)
        handle.writeframes(bytes(frames))


class LevelReader:
    """Collects ``/output/<n>/level`` reports from oscmix.

    Binds the receive port directly. The mixer GUI holds it while it is
    open, which is why this refuses to run rather than competing for
    datagrams: a split stream would produce quietly wrong numbers.
    """

    def __init__(self, recv_port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", recv_port))
        self.sock.settimeout(0.2)
        #: `/hardware/dspvers`, when a dump has reported it; the evidence
        #: records it next to the USB revision (see discovery.device_firmware).
        self.dspvers: Optional[object] = None
        self.reports = 0            # any level message at all = backend alive

    def close(self) -> None:
        self.sock.close()

    def peaks(self, seconds: float) -> Dict[int, float]:
        """Highest peak level per output channel over ``seconds``."""
        result: Dict[int, float] = {}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                datagram, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            for message in iter_osc_messages(datagram):
                try:
                    path, _tags, args = decode_osc(message)
                except (ValueError, struct.error):
                    continue
                if not (path.startswith("/output/") and path.endswith("/level")):
                    continue
                if not args or not isinstance(args[0], float):
                    continue
                channel = int(path.split("/")[2])
                peak = args[0]
                if peak != peak:                               # NaN
                    continue
                self.reports += 1
                if peak == float("-inf"):
                    peak = SILENCE_DB
                result[channel] = max(result.get(channel, SILENCE_DB), peak)
        return result


    def output_state(self, send_port: int, outputs: Sequence[int],
                     seconds: float = 6.0) -> Dict[int, Dict[str, object]]:
        """The fader and mute of each output, straight from the device.

        A verdict that says "no audio here" without saying why is half a
        measurement. The first real run of this tool reported outputs 1/2
        as failing and blamed other audio on the bus; the actual cause
        was ``/output/1/volume`` sitting at -65 dB, which is the fader
        pulled shut. The tool could see that and did not say it.
        """
        wanted = {"/output/%d/%s" % (channel, field): (channel, field)
                  for channel in outputs for field in ("volume", "mute")}
        state: Dict[int, Dict[str, object]] = {c: {} for c in outputs}
        self.sock.sendto(encode_osc("/refresh"), ("127.0.0.1", send_port))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(len(fields) == 2 for fields in state.values()):
                break
            try:
                datagram, _ = self.sock.recvfrom(65536)
            except (socket.timeout, OSError):
                continue
            for message in iter_osc_messages(datagram):
                try:
                    path, _tags, args = decode_osc(message)
                except (ValueError, struct.error):
                    continue
                if path == "/hardware/dspvers" and args:
                    self.dspvers = args[0]
                if path in wanted and args:
                    channel, field = wanted[path]
                    state[channel][field] = args[0]
        return state


def backend_revision() -> Optional[str]:
    """The upstream oscmix commit this measurement was taken against.

    Moved into the library when the write sweep needed the same answer;
    see ``discovery.built_backend_revision`` for why it is recorded.
    """
    return built_backend_revision(Path(__file__).resolve().parent.parent)


def sink_layout(sink: Optional[str]) -> Optional[Tuple[str, List[str]]]:
    """The name and channel layout of the sink the tone will go to.

    Returns None when PipeWire cannot be asked, which is not an error --
    the measurement simply proceeds without the check.

    This exists because the tool once produced three identical, entirely
    convincing FAILs that had nothing to do with the routing: a USB
    replug had left the *default* sink as the interface's raw 20-channel
    Direct sink (`AUX0..AUX19`). A stereo WAV played into that has no
    FL/FR to land on, so the tone arrived weak and on the wrong
    channels, and every route "failed". The same command with an
    explicit stereo sink passed.

    A release gate that reports a broken routing when it is really an
    unusable measurement is as bad as one that misses a real fault, so
    that case is now a *skip* with the reason, not a failure.
    """
    try:
        dump = subprocess.run(["pw-dump"], capture_output=True, text=True,
                              check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        objects = json.loads(dump.stdout)
    except ValueError:
        return None

    sinks = {}
    default = None
    for obj in objects:
        props = ((obj.get("info") or {}).get("props")) or {}
        if props.get("media.class") == "Audio/Sink":
            name = props.get("node.name")
            position = props.get("audio.position") or []
            if isinstance(position, str):
                position = position.strip("[] ").replace(",", " ").split()
            if name:
                sinks[name] = [str(x) for x in position]
        # The metadata object carries the default sink under
        # 'default.audio.sink', as {"name": "..."}.
        for entry in obj.get("metadata") or []:
            if entry.get("key") == "default.audio.sink":
                value = entry.get("value")
                if isinstance(value, dict):
                    default = value.get("name")

    name = sink or default
    if name is None or name not in sinks:
        return None
    return name, sinks[name]


def playback_sinks() -> Dict[Tuple[int, ...], str]:
    """Which PipeWire sink feeds which pair of playback channels.

    The named sinks this project generates carry the mapping on their
    output node: ``oscmix.krk-monitors.out`` sits on ``AUX4 AUX5``,
    which is playback 5/6. Reading it means every route can be measured
    from the source that actually feeds it, instead of measuring the
    three fed from playback 1/2 and quietly omitting the rest.

    That omission was real: a five-route config produced a three-route
    artifact, and nothing in it said the other two had not been looked
    at.
    """
    try:
        dump = subprocess.run(["pw-dump"], capture_output=True, text=True,
                              check=False, timeout=15)
        objects = json.loads(dump.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}

    found: Dict[Tuple[int, ...], str] = {}
    for obj in objects:
        props = ((obj.get("info") or {}).get("props")) or {}
        name = props.get("node.name") or ""
        if not name.endswith(".out"):
            continue
        position = props.get("audio.position") or []
        if isinstance(position, str):
            position = position.strip("[] ").replace(",", " ").split()
        channels = []
        for entry in position:
            match = re.fullmatch(r"AUX(\d+)", str(entry).strip())
            if match:
                channels.append(int(match.group(1)) + 1)   # AUX0 = playback 1
        if len(channels) == 2:
            found.setdefault(tuple(channels), name[:-len(".out")])
    return found


def device_serial(config) -> Optional[str]:
    """The serial of the interface this measures, from the one resolution.

    Raises DeviceAmbiguous on a machine with two identical interfaces and
    no `[device] serial`: evidence that cannot say which box it measured
    is not evidence, and naming the first one is how 0.6.8 did it.
    """
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    device = resolve_device(config.usb_id, config.device_name, config.serial,
                            proc_root)
    return device.serial or None


def is_stereo(positions: Sequence[str]) -> bool:
    """Whether a tone written as stereo will land where it is meant to."""
    upper = [p.upper() for p in positions]
    return len(upper) == 2 and upper[0].startswith("FL") and upper[1].startswith("FR")


def play(wav: Path, sink: Optional[str]) -> None:
    command = ["pw-play"]
    if sink:
        command += ["--target", sink]
    command.append(str(wav))
    subprocess.run(command, capture_output=True, check=False)


def measure(reader: LevelReader, wav: Path, sink: Optional[str]) -> Dict[int, float]:
    """Play the tone and return the peak level seen per output."""
    import threading

    peaks: Dict[int, float] = {}

    def collect() -> None:
        peaks.update(reader.peaks(TONE_SECONDS + 1.0))

    thread = threading.Thread(target=collect)
    thread.start()
    time.sleep(0.3)
    play(wav, sink)
    thread.join()
    return peaks


def explain_silence(channel: int, state: Dict[int, Dict[str, object]]) -> str:
    """Why an output carries nothing, when the device can say.

    Only two causes are visible from here, and both are ordinary user
    state rather than faults -- which is exactly why they have to be
    named: an operator reading "output 1 is not carrying that channel"
    should not go looking for a routing bug when the fader is shut.
    """
    fields = state.get(channel) or {}
    volume = fields.get("volume")
    if isinstance(volume, float) and volume <= LEVEL_MIN + 0.5:
        return (" -- /output/%d/volume is %.1f dB, the fader is shut; this "
                "route declares no 'volume', so that value is yours"
                % (channel, volume))
    if fields.get("mute"):
        return " -- /output/%d/mute is set" % channel
    if isinstance(volume, float) and volume < -20.0:
        return " -- /output/%d/volume is %.1f dB" % (channel, volume)
    return ""


def check_route(name: str, outputs: Tuple[int, ...], left: Dict[int, float],
                right: Dict[int, float], silence: Dict[int, float],
                state: Optional[Dict[int, Dict[str, object]]] = None) -> dict:
    """Turn the measurements into a verdict for one stereo route.

    Each output is compared against itself across the two runs: it must
    be materially louder when its own side of the pair carries the tone.
    That is what caught every routing defect so far -- a dead output
    shows no response at all, and a mono-summed pair responds equally to
    both tones.
    """
    low, high = outputs
    state = state or {}
    finding = {
        "route": name,
        "outputs": list(outputs),
        "output_state": {"output_%d" % c: dict(state.get(c) or {})
                         for c in outputs},
        "peaks_db": {
            "output_%d" % low: {"left_tone": left.get(low),
                                "right_tone": right.get(low),
                                "silence": silence.get(low)},
            "output_%d" % high: {"left_tone": right.get(high) and left.get(high),
                                 "right_tone": right.get(high),
                                 "silence": silence.get(high)},
        },
    }
    problems = []
    for side, channel, driven_run, other_run in (("left", low, left, right),
                                                 ("right", high, right, left)):
        driven = driven_run.get(channel)
        if driven is None:
            problems.append("output %d never reported a level%s"
                            % (channel, explain_silence(channel, state)))
            continue
        floor = silence.get(channel)
        if floor is not None and driven - floor < MIN_ABOVE_BACKGROUND_DB:
            reason = explain_silence(channel, state)
            problems.append(
                "output %d: tone only %.1f dB above what was already "
                "playing%s" % (channel, driven - floor,
                               reason or " -- stop other audio and retry"))
            continue
        quiet = other_run.get(channel)
        response = 999.0 if quiet is None else driven - quiet
        finding["response_%s_db" % side] = round(response, 1)
        if response < MIN_RESPONSE_DB:
            problems.append(
                "output %d responds to the %s tone by only %.1f dB -- it is "
                "not carrying that channel alone%s"
                % (channel, side, response, explain_silence(channel, state)))
    finding["problems"] = problems
    finding["ok"] = not problems
    return finding


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="routing config to verify")
    parser.add_argument("--evidence", type=Path,
                        help="write the measurements here as JSON")
    parser.add_argument("--sink", help="PipeWire sink to play into")
    args = parser.parse_args()

    if not shutil.which("pw-play"):
        print("skip: pw-play not found", file=sys.stderr)
        return EXIT_SKIP

    config = load_config(args.config or discover_config_path())
    try:
        serial = device_serial(config)
    except DeviceAmbiguous as exc:
        print("refused: %s -- set [device] serial in the config it measures"
              % exc, file=sys.stderr)
        return 1
    pairs = [route for route in config.routes if len(route.output) == 2]
    if not pairs:
        print("skip: no stereo routes configured", file=sys.stderr)
        return EXIT_SKIP

    # Only when a sink was named explicitly. Without --sink the per-source
    # discovery below picks the right stereo sink for each playback pair,
    # and the default sink -- whatever PipeWire last decided, often the
    # raw 20-channel Direct sink -- is not consulted at all.
    layout = sink_layout(args.sink) if args.sink else None
    if layout is not None and not is_stereo(layout[1]):
        print("skip: %s is a %d-channel sink (%s), not stereo -- a stereo "
              "tone has nothing to land on there and every route would "
              "'fail'. Name a stereo sink with --sink."
              % (layout[0], len(layout[1]),
                 " ".join(layout[1][:4]) + (" ..." if len(layout[1]) > 4 else "")),
              file=sys.stderr)
        return EXIT_SKIP

    try:
        reader = LevelReader(config.osc_recv_port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            # Not the GUI, and not a reason to skip: a measurement that
            # cannot bind its port for another reason has failed (0.6.11).
            print("error: cannot bind the receive port UDP %d: %s"
                  % (config.osc_recv_port, exc.strerror or exc),
                  file=sys.stderr)
            return 1
        print("skip: UDP %d is in use -- close the mixer GUI, its meters "
              "and ours would split the stream" % config.osc_recv_port,
              file=sys.stderr)
        return EXIT_SKIP

    sinks = playback_sinks()
    if args.sink:
        # An explicit --sink overrides the discovery for playback 1/2,
        # which is what it has always meant.
        sinks[(1, 2)] = args.sink

    by_source: Dict[Tuple[int, ...], List] = {}
    for route in pairs:
        by_source.setdefault(tuple(route.playback), []).append(route)

    findings: List[dict] = []
    unmeasured: List[dict] = []
    reports = 0

    with tempfile.TemporaryDirectory() as tmp:
        left_wav, right_wav = Path(tmp) / "l.wav", Path(tmp) / "r.wav"
        write_tone(left_wav, left=True, right=False)
        write_tone(right_wav, left=False, right=True)
        try:
            for source in sorted(by_source):
                sink = sinks.get(source)
                if sink is None:
                    # Named, not dropped. A route nobody measured must
                    # not read as a route that passed.
                    for route in by_source[source]:
                        unmeasured.append({
                            "route": route.name,
                            "outputs": list(route.output),
                            "reason": "no PipeWire sink feeds playback %s"
                                      % "/".join(map(str, source)),
                        })
                    continue
                layout = sink_layout(sink)
                if layout is not None and not is_stereo(layout[1]):
                    for route in by_source[source]:
                        unmeasured.append({
                            "route": route.name,
                            "outputs": list(route.output),
                            "reason": "%s is not a stereo sink" % sink,
                        })
                    continue

                silence = reader.peaks(2.0)
                left = measure(reader, left_wav, sink)
                right = measure(reader, right_wav, sink)
                reports = max(reports, reader.reports)
                state = reader.output_state(
                    config.osc_port,
                    sorted({c for route in by_source[source]
                            for c in route.output}))
                for route in by_source[source]:
                    finding = check_route(route.name, route.output, left,
                                          right, silence, state)
                    finding["playback"] = list(source)
                    finding["sink"] = sink
                    # Two of 0.1.3's three defects were specific to
                    # `stereo = false` routes: one silenced half the
                    # pair, the other landed 6 dB low. An artifact that
                    # does not say which routes were unlinked cannot
                    # show that either is fixed -- a linked and an
                    # unlinked run came out byte-identical apart from
                    # the timestamp, which is how this was noticed.
                    finding["stereo"] = route.stereo
                    findings.append(finding)
        finally:
            reader.close()

    if reports == 0:
        print("skip: no level reports -- is the oscmix backend running?",
              file=sys.stderr)
        return EXIT_SKIP

    evidence = {
        "measured": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "device": config.device_name,
        # Without this the measurement is not reproducible: the same
        # command on the same machine passes or fails depending on which
        # sink the tone went to, and that used to be recorded nowhere.
        # Per route now (see "routes"), because one run plays into
        # several sinks -- one per playback pair the config uses.
        "sinks": {"/".join(map(str, k)): v for k, v in sorted(sinks.items())},
        # The channel layout of each sink the tone went through, which is
        # the guard the release checklist turns on: a stereo tone into
        # the interface's raw 20-channel Direct sink has nothing to land
        # on, and the 0.2.0 release run produced three convincing FAILs
        # that way with nothing wrong at all.
        #
        # The tool now *refuses* a non-stereo sink rather than measuring
        # through one, so this cannot be wrong -- but it was dropped from
        # the artifact by a refactor during 0.3.0 while the checklist
        # still required it, and a check nobody can perform is not a
        # check. Recorded again, per sink, because a run now uses one per
        # playback pair.
        "sink_channels": {name: (sink_layout(name) or (name, []))[1]
                          for name in sorted(set(sinks.values()))},
        "serial": serial,
        "oscmix_revision": backend_revision(),
        # Which firmware the numbers below were taken against. Two
        # artifacts that differ here are measurements of two devices,
        # whatever the serial says; a claim about "the device" carried
        # across a firmware update is a claim nobody re-measured.
        "firmware": device_firmware(
            config.usb_id,
            Path(os.environ.get("OSCMIX_SYSFS_USB", "/sys/bus/usb/devices")),
            {"/hardware/dspvers": reader.dspvers}),
        "min_response_db": MIN_RESPONSE_DB,
        "min_above_background_db": MIN_ABOVE_BACKGROUND_DB,
        "tone": {"hz": TONE_HZ, "seconds": TONE_SECONDS,
                 "amplitude": TONE_AMPLITUDE},
        "routes": findings,
        "unmeasured": unmeasured,
        "ok": all(finding["ok"] for finding in findings),
        # Separate from ok on purpose: a route nobody could measure did
        # not pass and did not fail. The release checklist requires this
        # to be true, so a shrinking artifact cannot pass unnoticed.
        "complete": not unmeasured and bool(findings),
    }
    if args.evidence:
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")

    for finding in findings:
        status = "ok" if finding["ok"] else "FAIL"
        print("%-4s %-16s outputs %s  response L/R: %s/%s dB"
              % (status, finding["route"],
                 "/".join(map(str, finding["outputs"])),
                 finding.get("response_left_db", "-"),
                 finding.get("response_right_db", "-")))
        for problem in finding["problems"]:
            print("       " + problem)

    if not findings:
        print("skip: no routes fed from playback 1/2", file=sys.stderr)
        return EXIT_SKIP
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
