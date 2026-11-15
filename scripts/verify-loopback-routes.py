#!/usr/bin/env python3
"""Show every declared playback route of the desk through output loopback (release evidence).

The UCX II cannot report its playback matrix, so a route is shown by sound: a left-only and a
right-only 1 kHz tone go into the PipeWire sink that feeds the route's playback pair, and with
`/output/N/loopback 1` on the route's output pair the capture channels of those outputs carry
what the outputs carry. A stereo route passes when the left tone stands on the odd output's
capture channel at least MARGIN_DB above the even one, and the right tone the other way round.
The device does not report the loopback flags either, so before a route each output's flag is
measured: on where the output's own tone stands MARGIN_DB above a silent window and above the
other side's tone on its capture channel. A live input on that channel, such as a microphone,
is loud in every window or in none, so it does not pass for loopback. Each flag is given back
to its own state and measured again. The only register writes are the loopback flags; a sink
quieter than 1.0 is raised for the run and given back.

Run after the user's hardware go: UCX on, monitors off, headphones unplugged, 48 V off. The
record names the box by device_pseudonym, with the placeholder where its serial would stand.
release.yml requires it beside the hardware evidence from 0.10.2 on.
"""
import argparse
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from loopback_signal import RATE, channel, middle
from measurement import build_evidence, source_evidence, without_serial

from oscmix_desk import open_desk
from oscmix_desk.config import load_config
from oscmix_desk.discovery import resolve_device
from oscmix_desk.paths import discover_config_path

HERE = Path(__file__).resolve().parent
KIND = "playback-routes-through-loopback"
SCHEMA = 2
TONE_HZ = 1000.0
TONE_SECONDS = 3.0
TONE_DBFS = -20.0
#: The tone stands this far above the other capture channel of the pair, and a looped
#: output's own tone this far above the silent window and the other side's tone.
MARGIN_DB = 30.0
SAMPLES = 16384
EXIT_SKIP = 77
TOOLS = ("pw-play", "pw-record", "pw-dump", "wpctl")


def sibling(name, filename):
    """A tool of this directory whose file name is not a module name."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now():
    return datetime.now(UTC).isoformat()


def write_tone(path, side):
    """A stereo 16-bit file with the tone on ``side`` only."""
    amplitude = 32767 * 10 ** (TONE_DBFS / 20)
    frames = bytearray()
    for i in range(int(RATE * TONE_SECONDS)):
        sample = int(amplitude * math.sin(2 * math.pi * TONE_HZ * i / RATE))
        left, right = (sample, 0) if side == "left" else (0, sample)
        frames += left.to_bytes(2, "little", signed=True)
        frames += right.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(frames))


def tone_dbfs(samples, frequency=TONE_HZ):
    """The level of one frequency in ``samples`` (Goertzel), None where there is none."""
    k = 2 * math.cos(2 * math.pi * frequency / RATE)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + k * s1 - s2, s1
    power = s1 * s1 + s2 * s2 - k * s1 * s2
    amplitude = 2 * math.sqrt(max(power, 0.0)) / len(samples)
    return round(20 * math.log10(amplitude), 2) if amplitude > 0 else None


def tone_levels(raw, outputs):
    """The tone's level on each output's capture channel, keyed by output number."""
    return {str(n): tone_dbfs(middle(channel(raw, n - 1), SAMPLES)) for n in outputs}


def carried(levels, wanted, other):
    level, rest = levels[str(wanted)], levels[str(other)]
    return level is not None and (rest is None or level - rest >= MARGIN_DB)


def level(levels, output):
    """The tone's level on an output's capture channel; minus infinity where there is none."""
    value = levels[str(output)]
    return -math.inf if value is None else value


def looped(windows, output, side, other):
    """Loopback on at ``output``: the tone of its own ``side`` comes back on its capture
    channel MARGIN_DB above the silent window and above the ``other`` side's tone. With
    loopback off that channel carries the input, and an input that carries the tone, a
    microphone in a room with the monitors on, carries both sides of it."""
    own = level(windows[side], output)
    return (own - level(windows["silence"], output) >= MARGIN_DB
            and own - level(windows[other], output) >= MARGIN_DB)


def states(windows, outputs):
    """The loopback flag of each output of the pair as measured: 1 on, 0 off."""
    odd, even = outputs
    return {str(odd): int(looped(windows, odd, "left", "right")),
            str(even): int(looped(windows, even, "right", "left"))}


def verdict(row):
    """Left on the odd output, right on the even one, and each output's loopback flag
    measured the same after the route as before it."""
    odd, even = row["output"]
    return (carried(row["left"], odd, even) and carried(row["right"], even, odd)
            and row["loopback_after"] == row["loopback_before"])


def sink_volume(name):
    """The PipeWire node of a sink and its volume as wpctl prints it."""
    dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, check=True,
                                     timeout=15).stdout)
    node = next(item["id"] for item in dump
                if ((item.get("info") or {}).get("props") or {}).get("node.name") == name)
    text = subprocess.run(["wpctl", "get-volume", str(node)], capture_output=True, text=True,
                          check=True, timeout=10).stdout
    return node, text.split()[1]


def set_volume(node, volume):
    subprocess.run(["wpctl", "set-volume", str(node), volume], check=True, timeout=10)


def played(probe, connection, node, sink, wav):
    """Every capture channel while ``wav`` plays into ``sink``, keeping the lease; with
    ``wav`` None, while nothing plays."""
    if wav is None:
        probe.drain(connection, 0.6)
        return probe.record(connection, node)
    player = subprocess.Popen(["pw-play", "--target", sink, str(wav)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        probe.drain(connection, 0.6)
        return probe.record(connection, node)
    finally:
        player.wait(timeout=TONE_SECONDS + 10)


def windows(probe, connection, node, sink, wavs, outputs):
    """The tone's level on the pair's capture channels with nothing played, then with the
    left and with the right tone."""
    return {name: tone_levels(played(probe, connection, node, sink, wavs.get(name)), outputs)
            for name in ("silence", "left", "right")}


def measure(probe, config, path, route, sink, wavs, node):
    """One route in a lease of its own; the loopback flags it sets are given back."""
    outputs = list(route.output)
    row = {"name": route.name, "playback": list(route.playback), "output": outputs,
           "sink": sink, "ok": False}
    if sink is None or len(outputs) != 2:
        row["reason"] = "no stereo sink or output pair"
        return row
    with open_desk(config, path) as desk:
        connection = desk._begin()
        row["before"] = windows(probe, connection, node, sink, wavs, outputs)
        given = row["loopback_before"] = states(row["before"], outputs)
        connection.send([("/output/%d/loopback" % n, "i", (1,)) for n in outputs])
        try:
            probe.drain(connection, 0.4)
            for side in ("left", "right"):
                row[side] = tone_levels(played(probe, connection, node, sink, wavs[side]),
                                        outputs)
        finally:
            connection.send([("/output/%d/loopback" % n, "i", (given[str(n)],))
                             for n in outputs])
            probe.drain(connection, 0.4)
        row["after"] = windows(probe, connection, node, sink, wavs, outputs)
        row["loopback_after"] = states(row["after"], outputs)
    row["ok"] = verdict(row)
    return row


def measure_all(args, record, volumes):
    """Raise quiet sinks, identify the backend and the box, then show each route."""
    probe = sibling("probe_loopback", "probe-loopback.py")
    hardware = sibling("verify_hardware", "verify-hardware.py")
    path = args.config or discover_config_path()
    config = load_config(path, strict=True)
    sinks = hardware.playback_sinks()
    routes = [route for route in config.routes if route.playback]
    record["declared_routes"] = len(routes)
    for name in sorted({sinks.get(tuple(r.playback)) for r in routes} - {None}):
        node, found = sink_volume(name)
        if float(found) < 1.0:
            volumes[name] = {"node": node, "found": found}
            set_volume(node, "1.0")
    with tempfile.TemporaryDirectory() as scratch:
        wavs = {side: Path(scratch) / (side + ".wav") for side in ("left", "right")}
        for side, wav in wavs.items():
            write_tone(wav, side)
        with open_desk(config, path) as desk:
            record["running_backend"] = build_evidence(desk._control, args.backend_build,
                                                       Path("/proc"))
        record["oscmix_revision"] = record["running_backend"]["upstream"]
        serial = resolve_device(config.usb_id, config.device_name, config.serial,
                                Path("/proc")).serial
        record["serial"] = serial
        node = probe.pro_source(serial)
        for route in routes:
            record["routes"].append(measure(probe, config, path, route,
                                            sinks.get(tuple(route.playback)), wavs, node))
    return serial


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, help="the desk whose routes are shown")
    parser.add_argument("--evidence", type=Path, required=True, help="write the record here")
    parser.add_argument("--backend-build", type=Path, default=HERE.parent / "build/coordinated",
                        help="the prepared backend build that is running")
    args = parser.parse_args()
    if args.evidence.exists():
        print("refused: %s exists already" % args.evidence, file=sys.stderr)
        return 1
    if not all(shutil.which(tool) for tool in TOOLS):
        print("skip: needs %s" % ", ".join(TOOLS), file=sys.stderr)
        return EXIT_SKIP
    record = {"schema": SCHEMA, "kind": KIND, "started": now(),
              "desk_source": source_evidence(Path(__file__)),
              "tone": {"hz": TONE_HZ, "seconds": TONE_SECONDS, "dbfs": TONE_DBFS},
              "margin_db": MARGIN_DB, "routes": []}
    volumes = {}
    try:
        serial = measure_all(args, record, volumes)
    finally:
        for name, row in volumes.items():
            set_volume(row["node"], row["found"])
            row["given_back"] = sink_volume(name)[1] == row["found"]
    record["sink_volumes"] = volumes
    record["finished"] = now()
    record["unmeasured"] = [row["name"] for row in record["routes"] if "reason" in row]
    record["complete"] = bool(record["routes"]) and not record["unmeasured"]
    record["ok"] = (record["complete"] and all(row["ok"] for row in record["routes"])
                    and all(row["given_back"] for row in volumes.values()))
    args.evidence.write_text(json.dumps(without_serial(record, serial), indent=1,
                                        allow_nan=False) + "\n")
    for row in record["routes"]:
        if "reason" in row:
            print("n/a  %-16s %s" % (row["name"], row["reason"]))
            continue
        odd, even = row["output"]
        print("%-4s %-16s playback %s -> outputs %s  L %s / R %s dBFS" % (
            "ok" if row["ok"] else "FAIL", row["name"], "/".join(map(str, row["playback"])),
            "/".join(map(str, row["output"])), row["left"][str(odd)], row["right"][str(even)]))
    print("verify-loopback-routes: %s -> %s" % ("passed" if record["ok"] else "FAILED",
                                               args.evidence))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
