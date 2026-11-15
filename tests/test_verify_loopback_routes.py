"""The loopback route evidence (scripts/verify-loopback-routes.py), tested without hardware.

The device cannot report its playback matrix, so the release shows each declared route by
sound. What a recording means is arithmetic and belongs under test: the tone on the right
capture channel with margin, loopback given back, and a record that names no serial.
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
LEFT, RIGHT, ON = {"1": -20.0, "2": None}, {"1": None, "2": -20.0}, {"1": -20.0, "2": -20.0}


@pytest.mark.parametrize(("left", "right", "before", "after", "given", "ok"), [
    (LEFT, RIGHT, QUIET, QUIET, 0, True),
    (RIGHT, LEFT, QUIET, QUIET, 0, False),                       # the channels are crossed
    ({"1": -20.0, "2": -45.0}, RIGHT, QUIET, QUIET, 0, False),   # 25 dB apart, not 30
    (LEFT, RIGHT, QUIET, ON, 0, False),                          # loopback left on
    (LEFT, RIGHT, ON, ON, 1, True),                              # it was on before
    ({"1": None, "2": None}, RIGHT, QUIET, QUIET, 0, False),     # the left tone is missing
], ids=["passes", "crossed", "too-close", "not-given-back", "was-on", "silent"])
def test_a_route_passes_only_with_both_sides_apart_and_loopback_given_back(
        tool, left, right, before, after, given, ok):
    row = {"output": [1, 2], "left": left, "right": right, "before": before, "after": after,
           "loopback_before": given}
    assert tool.verdict(row) is ok


class Box:
    """The interface as the tool meets it: loopback flags set through a lease, and capture
    channels that carry the outputs' tone only while loopback is on."""

    def __init__(self, crossed=()):
        self.loopback, self.sent, self.crossed = {}, [], set(crossed)

    def send(self, messages):
        self.sent.append(list(messages))
        for path, _tags, (value,) in messages:
            self.loopback[int(path.split("/")[2])] = value

    def played(self, _probe, _connection, _node, sink, wav):
        odd, even = OUTPUTS[sink]
        left = wav.name.startswith("left")
        if sink in self.crossed:
            left = not left
        lit = odd if left else even
        return recording(((lit - 1, -20.0),) if self.loopback.get(lit) else ())


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
    assert all(row["ok"] and row["loopback_before"] == 0 for row in record["routes"])
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


def test_an_earlier_record_is_never_replaced(tool, monkeypatch, tmp_path):
    evidence = tmp_path / "loopback-route-evidence.json"
    evidence.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["verify-loopback-routes.py", "--evidence", str(evidence)])
    assert tool.main() == 1
    assert evidence.read_text() == "{}"
