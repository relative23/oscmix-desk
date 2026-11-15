"""The loopback route evidence (scripts/verify-loopback-routes.py), tested without hardware.

The device cannot report its playback matrix, so the release shows each declared route by
sound. What a recording means is arithmetic and belongs under test: the tone on the right
capture channel with margin, each loopback flag measured and given back, a live microphone
not taken for loopback, and a record that names no serial.
"""

import contextlib
import functools
import importlib.util
import json
import math
import struct
import sys
from types import SimpleNamespace

import pytest
from support import repo_file, signed
from two_boxes import B

sys.path.insert(0, str(repo_file("scripts")))

import loopback_signal as signal
import measurement

DESK = ("[device]\nserial = %s\n\n[route:main]\nplayback = 1/2\noutput = 1/2\n\n"
        "[route:phones]\nplayback = 7/8\noutput = 7/8\n" % B[1])
SINKS = {(1, 2): "oscmix.main", (7, 8): "oscmix.phones"}
OUTPUTS = {"oscmix.main": (1, 2), "oscmix.phones": (7, 8)}


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location(
        "verify_loopback_routes", repo_file("scripts", "verify-loopback-routes.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def recording(levels, frames=20000):
    """An interleaved s32 recording with the 1 kHz tone at its dBFS on each 0-based channel."""
    tones = {index: [10 ** (dbfs / 20) * math.sin(2 * math.pi * 1000 * i / signal.RATE)
                     for i in range(frames)] for index, dbfs in levels}
    data = bytearray()
    for i in range(frames):
        row = [0] * signal.CHANNELS
        for index, samples in tones.items():
            row[index] = int(samples[i] * (2 ** 31 - 1))
        data += struct.pack("<%di" % signal.CHANNELS, *row)
    return bytes(data)


def test_the_tone_is_found_on_its_capture_channel_only(tool):
    levels = tool.tone_levels(recording(((0, -20.0),)), [1, 2])
    assert abs(levels["1"] + 20.0) < 1.0
    assert levels["2"] is None


QUIET = {"1": None, "2": None}
LEFT, RIGHT = {"1": -20.0, "2": None}, {"1": None, "2": -20.0}
OFF, ON, ODD = {"1": 0, "2": 0}, {"1": 1, "2": 1}, {"1": 1, "2": 0}


@pytest.mark.parametrize(("left", "right", "before", "after", "ok"), [
    (LEFT, RIGHT, OFF, OFF, True),
    (RIGHT, LEFT, OFF, OFF, False),                       # the channels are crossed
    ({"1": -20.0, "2": -45.0}, RIGHT, OFF, OFF, False),   # 25 dB apart, not 30
    (LEFT, RIGHT, OFF, ON, False),                        # loopback left on
    (LEFT, RIGHT, ON, ON, True),                          # it was on before
    (LEFT, RIGHT, ODD, ODD, True),                        # one flag was on, and is again
    (LEFT, RIGHT, ODD, OFF, False),                       # that one was not given back
    (QUIET, RIGHT, OFF, OFF, False),                      # the left tone is missing
], ids=["passes", "crossed", "too-close", "not-given-back", "was-on", "odd-on", "odd-lost",
        "silent"])
def test_a_route_passes_only_with_both_sides_apart_and_loopback_given_back(
        tool, left, right, before, after, ok):
    row = {"output": [1, 2], "left": left, "right": right, "loopback_before": before,
           "loopback_after": after}
    assert tool.verdict(row) is ok


#: A microphone at the capture channel of output 1: its 1 kHz share in each window.
SPEECH = {"1": -50.0, "2": None}


@pytest.mark.parametrize(("silence", "left", "right", "flags"), [
    (QUIET, QUIET, QUIET, OFF),
    (QUIET, LEFT, RIGHT, ON),
    (QUIET, LEFT, QUIET, ODD),
    (SPEECH, {"1": -48.0, "2": None}, {"1": -52.0, "2": None}, OFF),
    (QUIET, {"1": -50.0, "2": None}, {"1": -55.0, "2": None}, OFF),
    (QUIET, {"1": -40.0, "2": -42.0}, {"1": -41.0, "2": -40.0}, OFF),
    ({"1": -60.0, "2": None}, {"1": -20.0, "2": None}, RIGHT, ON),
], ids=["off", "on", "odd-only", "microphone", "speech-in-one-window", "monitors-on",
        "on-beside-noise"])
def test_a_flag_counts_as_on_only_where_the_own_tone_comes_back(tool, silence, left, right,
                                                                flags):
    windows = {"silence": silence, "left": left, "right": right}
    assert tool.states(windows, [1, 2]) == flags


class Box:
    """The interface as the tool meets it: loopback flags set through a lease, and capture
    channels that carry their output while its loopback is on and their input otherwise,
    here a microphone at a steady 1 kHz level where one is plugged in."""

    def __init__(self, crossed=(), loopback=None, microphone=None):
        self.loopback, self.sent, self.crossed = dict(loopback or {}), [], set(crossed)
        self.microphone = dict(microphone or {})

    def send(self, messages):
        self.sent.append(list(messages))
        for path, _tags, (value,) in messages:
            self.loopback[int(path.split("/")[2])] = value

    def played(self, _probe, _connection, _node, sink, wav):
        odd, even = OUTPUTS[sink]
        lit = None
        if wav is not None:
            left = wav.name.startswith("left")
            if sink in self.crossed:
                left = not left
            lit = odd if left else even
        levels = []
        for output in (odd, even):
            if self.loopback.get(output):
                if output == lit:
                    levels.append((output - 1, -20.0))
            elif output in self.microphone:
                levels.append((output - 1, self.microphone[output]))
        return recording(tuple(levels))


def run(tool, monkeypatch, tmp_path, box, volumes):
    config = tmp_path / "routing.conf"
    config.write_text(DESK)
    machine = tmp_path / "machine-id"
    machine.write_text("0123456789abcdef0123456789abcdef\n")
    monkeypatch.setattr(measurement, "MACHINE_ID_FILES", (machine,))
    desk = SimpleNamespace(_control=SimpleNamespace(pid=1), _begin=lambda: box)
    monkeypatch.setattr(tool, "open_desk",
                        signed(tool.open_desk, body=lambda: contextlib.nullcontext(desk)))
    monkeypatch.setattr(tool, "build_evidence", signed(tool.build_evidence, {
        "upstream": "a" * 40, "series_sha256": "b" * 64}))
    monkeypatch.setattr(tool, "resolve_device",
                        signed(tool.resolve_device, body=lambda: SimpleNamespace(serial=B[1])))
    probe = SimpleNamespace(pro_source=lambda serial: "pro-input", drain=lambda *a: None)
    hardware = SimpleNamespace(playback_sinks=lambda: dict(SINKS))
    monkeypatch.setattr(tool, "sibling",
                        lambda name, _file: probe if "probe" in name else hardware)
    monkeypatch.setattr(tool, "played", box.played)
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(tool, "sink_volume", lambda name: (name, volumes[name]))
    monkeypatch.setattr(tool, "set_volume", volumes.__setitem__)
    evidence = tmp_path / "loopback-route-evidence.json"
    monkeypatch.setattr(sys, "argv", ["verify-loopback-routes.py", "--config", str(config),
                                      "--evidence", str(evidence)])
    code = tool.main()
    return code, evidence


def test_every_route_is_shown_and_everything_is_given_back(tool, monkeypatch, tmp_path):
    box, volumes = Box(), {"oscmix.main": "0.40", "oscmix.phones": "1.00"}
    code, evidence = run(tool, monkeypatch, tmp_path, box, volumes)
    assert code == 0
    text = evidence.read_text()
    record = json.loads(text)
    assert record["ok"] is True
    assert record["complete"] is True
    assert record["unmeasured"] == []
    assert [row["name"] for row in record["routes"]] == ["main", "phones"]
    for row in record["routes"]:
        assert row["ok"]
        assert row["loopback_before"] == row["loopback_after"] == {str(n): 0
                                                                   for n in row["output"]}
    assert box.sent == [[("/output/%d/loopback" % n, "i", (value,)) for n in pair]
                        for pair in ((1, 2), (7, 8)) for value in (1, 0)]
    assert volumes == {"oscmix.main": "0.40", "oscmix.phones": "1.00"}
    assert record["sink_volumes"] == {"oscmix.main": {"node": "oscmix.main", "found": "0.40",
                                                      "given_back": True}}
    assert B[1] not in text
    assert record["serial"] == measurement.PLACEHOLDER_SERIAL
    assert record["device_pseudonym"].startswith("box-")
    assert record["oscmix_revision"] == "a" * 40
    assert record["desk_source"]["runtime_sha256"]


def test_one_crossed_route_fails_the_record(tool, monkeypatch, tmp_path):
    box = Box(crossed=["oscmix.phones"])
    code, evidence = run(tool, monkeypatch, tmp_path, box, {"oscmix.main": "1.00",
                                                             "oscmix.phones": "1.00"})
    record = json.loads(evidence.read_text())
    assert code == 1
    assert record["ok"] is False
    assert record["complete"] is True
    assert [row["ok"] for row in record["routes"]] == [True, False]
    assert box.loopback == {1: 0, 2: 0, 7: 0, 8: 0}, "the flags are given back all the same"


def test_a_live_microphone_is_not_taken_for_loopback(tool, monkeypatch, tmp_path):
    # Speech on the microphone of input 1 carries 1 kHz, as the tone does, in every window.
    box = Box(microphone={1: -50.0})
    code, evidence = run(tool, monkeypatch, tmp_path, box, {"oscmix.main": "1.00",
                                                             "oscmix.phones": "1.00"})
    record = json.loads(evidence.read_text())
    assert code == 0
    assert record["routes"][0]["loopback_before"] == {"1": 0, "2": 0}
    assert box.loopback == {1: 0, 2: 0, 7: 0, 8: 0}, "loopback must not stay on over the input"


def test_each_flag_is_given_back_to_its_own_state(tool, monkeypatch, tmp_path):
    box = Box(loopback={7: 1}, microphone={8: -50.0})
    code, evidence = run(tool, monkeypatch, tmp_path, box, {"oscmix.main": "1.00",
                                                             "oscmix.phones": "1.00"})
    record = json.loads(evidence.read_text())
    assert code == 0
    assert record["routes"][1]["loopback_before"] == {"7": 1, "8": 0}
    assert record["routes"][1]["loopback_after"] == {"7": 1, "8": 0}
    assert box.sent[-1] == [("/output/7/loopback", "i", (1,)), ("/output/8/loopback", "i", (0,))]
    assert box.loopback == {1: 0, 2: 0, 7: 1, 8: 0}


def test_an_earlier_record_is_never_replaced(tool, monkeypatch, tmp_path):
    evidence = tmp_path / "loopback-route-evidence.json"
    evidence.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["verify-loopback-routes.py", "--evidence", str(evidence)])
    assert tool.main() == 1
    assert evidence.read_text() == "{}"
