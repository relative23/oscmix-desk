"""Validate the playback-mute record of scripts/probe-playback-mute.py, without live I/O.

The device never reports playback mute, so the record holds what the backend sent and what a
loopback recording heard. Per pair, ON on the odd channel, then OFF on it, then an idempotent OFF
on the even one: each step one message with exactly the pair's 40 coefficient words, zero for ON
and for OFF the value each register last carried on the wire, in a usbmon capture that covers the
backend from its start. One pair also played a tone through loopback: heard unmuted and restored,
silent muted. It succeeds the 2026-10-08 projection, whose series this tree no longer ships.
"""

import hashlib
import json

from support import repo_file

FIXTURE = ("tests", "data", "playback-mute-wire-ucx2.json")
PAIRS = tuple(range(1, 21, 2))
STEPS = ("on", "off-target", "off-partner")
CLAIMS = ("device_mute_readback", "coefficient_device_readback", "acoustic_output_claim",
          "cold_start_qualified")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def registers(pair):
    """The pair's coefficient registers: playback starts at wire column 32 on the UCX II."""
    return sorted(0x4000 | out << 6 | (32 + pair - 1 + side)
                  for out in range(20) for side in (0, 1))


def validate_case(case, pair):
    need(case["pair"] == [pair, pair + 1] and case["registers"] == registers(pair),
         "pair %d: other registers than its 40 cells" % pair)
    need([step["name"] for step in case["steps"]] == list(STEPS), "pair %d: steps" % pair)
    words = case["restore_words"]
    need(len(words) == 40 and all(type(w) is int and 0 <= w <= 0xFFFF for w in words),
         "pair %d: restore words" % pair)
    previous = None
    for step in case["steps"]:
        need(step["start"] < step["end"] and (previous is None or previous <= step["start"]),
             "pair %d: step times" % pair)
        previous = step["end"]
        need(step["complete"] is True and step["messages"] == 1
             and step["coefficient_words"] == 40,
             "pair %d, %s: not exactly its 40 words in one message" % (pair, step["name"]))
    need(case["passed"] is True, "pair %d did not pass" % pair)


def validate_acoustic(record):
    stages = record["stages"]
    need([s["name"] for s in stages] == ["unmuted", "muted", "restored"], "tone stages")
    for stage in [record["before"], *stages]:
        need(stage["device_rate"] == [48000, 48000], "tone recorded at another rate")
    verdicts = [s["verdict"] for s in stages]
    need(verdicts == ["carried", "silent", "carried"] and record["passed"] is True,
         "the tone was not heard unmuted, silent muted and heard again: %s" % verdicts)
    # The level follows PipeWire's playback volume, so what counts is that the tone makes up
    # the recording (a sine's mean power is 3.01 dB below its peak) and comes back unchanged.
    for stage in stages[0], stages[2]:
        need(stage["tone_dbfs"] > -90 and stage["total_dbfs"] - (stage["tone_dbfs"] - 3.01) <= 1,
             "the tone does not make up the recording")
    need(abs(stages[0]["tone_dbfs"] - stages[2]["tone_dbfs"]) <= 0.5, "tone level differs")
    need(record["before"]["verdict"] in ("carried", "silent")
         and record["loopback_given_back"] == int(record["before"]["verdict"] == "carried"),
         "loopback not given back as found")


def validate(fixture, manifest_bytes):
    """Every pair muted and unmuted on the wire, one heard; the paths it addresses."""
    need(fixture["schema"] == 2 and fixture["kind"] == "playback-mute-probes-ucx2",
         "wrong record identity")
    need(fixture["passed"] is True and fixture["executed"] is True
         and fixture["hardware_used"] is True and "error" not in fixture
         and "restore_error" not in fixture, "the record did not pass")
    need(all(fixture["claims"][name] is False for name in CLAIMS)
         and set(fixture["claims"]) == set(CLAIMS), "the record claims more than it shows")
    backend = fixture["running_backend"]
    manifest = json.loads(manifest_bytes)
    need(backend["series_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
         and backend["upstream"] == manifest["upstream"] and backend["matches_local_build"],
         "taken with a backend series this tree does not ship")
    capture = fixture["capture"]
    need(capture["first_message"] < backend["started"] and capture["messages"] > 0,
         "the capture does not cover the backend from its start")
    need(fixture["restored"] is True
         and not any(fixture["final_dump"][key] for key in ("missing", "changed", "new")),
         "the device was not given back")
    need([case["pair"][0] for case in fixture["cases"]] == list(PAIRS),
         "not every pair, once and in order")
    for case, pair in zip(fixture["cases"], PAIRS, strict=True):
        validate_case(case, pair)
    validate_acoustic(fixture["acoustic"])
    return covered_paths(fixture)


def covered_paths(fixture):
    """The mute paths the record addresses directly: ON and OFF odd, OFF even."""
    need(len(fixture["cases"]) == len(PAIRS) and all(c["passed"] for c in fixture["cases"]),
         "an incomplete record covers nothing")
    return {"/playback/%d/mute" % n for pair in PAIRS for n in (pair, pair + 1)}


def recorded():
    fixture = json.loads(repo_file(*FIXTURE).read_text())
    return fixture, validate(fixture, repo_file("patches", "backend-series.json").read_bytes())
