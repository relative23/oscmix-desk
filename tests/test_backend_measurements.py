"""What patches 0017-0019 change on the device, held against the records of 2026-11-07.

Each claim comes in a pair: measured with the series before the patches (``BEFORE``) and with
the series this tree ships, both through output loopback or a usbmon capture of the same
session, every register given back. The pair is the evidence; either half alone is not.
"""

import hashlib
import json

import pytest
from support import repo_file

#: The series before 0017-0019 (0.9.0's, pin 89ed835).
BEFORE = "75f8a9673b57891291c9511c2999b8f6a761b41bfd50632fa61efc3998daba3b"
SHIPPED = hashlib.sha256(repo_file("patches", "backend-series.json").read_bytes()).hexdigest()


def record(name, series):
    data = json.loads(repo_file("tests", "data", name).read_text())
    assert data["running_backend"]["series_sha256"] == series
    assert data["running_backend"]["matches_local_build"] is True
    assert data["hardware_used"] is True
    assert "error" not in data
    assert data["restored"] is True
    assert not any(data["final_dump"][key] for key in ("missing", "changed", "new"))
    return data


def recordings(case):
    return [value for value in case.values()
            if isinstance(value, dict) and "device_rate" in value]


@pytest.mark.parametrize(("name", "series", "verdict"), [
    ("phase-register-only-ucx2.json", BEFORE, "unchanged"),
    ("phase-probes-ucx2.json", SHIPPED, "inverted"),
])
def test_phase_turns_the_signal_around_only_through_the_coefficients(name, series, verdict):
    """The register alone changes nothing; 0018 negates the cells as TotalMix FX does."""
    data = record(name, series)
    assert data["passed"] is True
    cases = data["cases"]
    assert [c["output"] for c in cases if c["kind"] == "output"] == list(range(1, 21))
    assert sorted(c["source"] for c in cases if c["kind"] == "input") == [1, 2, 3, 4]
    for case in cases:
        assert case["verdict"] == verdict, case["switch"]
        assert case["reversible"] is True
        assert all(r["device_rate"] == [48000, 48000] for r in recordings(case))


@pytest.mark.parametrize(("name", "series", "signs"), [
    ("coefficient-half-before-ucx2.json", BEFORE, ["+", "-", "+"]),
    ("coefficient-half-ucx2.json", SHIPPED, ["+", "+", "+"]),
])
def test_a_cell_at_exactly_half_keeps_its_polarity(name, series, signs):
    """0x4000 reads as -0.5: the old encoding inverted a cell at -6.0206 dB; 0017 does not."""
    data = record(name, series)
    case, = data["cases"]
    assert abs(case["unity_gain_db"]) <= 0.05
    assert [p["level_db"] for p in case["points"]] == [-6.1, -6.0206, -6.0]
    assert case["signs"] == signs
    assert all(p["verdict"] == "carried" for p in case["points"])


def test_a_refresh_wrote_every_input_cell_back_and_rounded_it():
    data = record("refresh-echo-before-ucx2.json", BEFORE)
    written = dict(data["off_grid"]["written_words"])
    assert data["off_grid"]["level_db"] == -3.05
    for window in data["refreshes"]:
        assert window["input_coefficient_words"] == 800
        echoed = dict(window["off_grid_words"])
        # The cell came back at the report's 0.1 dB: another word than the one written.
        assert any(echoed[reg] != written[reg] for reg in written if written[reg])


def test_a_refresh_now_leaves_confirmed_cells_alone():
    """What is left are silent cells reported with a pan: a zero coefficient holds no pan."""
    data = record("refresh-echo-ucx2.json", SHIPPED)
    assert len(data["refreshes"]) == 5
    for window in data["refreshes"]:
        assert window["input_coefficient_words"] <= 8
        assert window["off_grid_words"] == []
