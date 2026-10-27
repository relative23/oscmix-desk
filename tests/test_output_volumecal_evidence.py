"""Keep the calibration row bounded by actual, independently accepted evidence."""

import json
import math
import re
from pathlib import Path

import pytest

from oscmix_desk import numeric, reconcile
from oscmix_desk.devices import FF802, UCX2
from oscmix_desk.registers import NUMBER, PIN, VERIFIABLE, cold_plug_complete, register_at

# On integration this lives in tests/data; the fallback is for this private draft.
HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "output-volumecal-probes-ucx2.json"
if not DATA.exists():
    DATA = HERE / "output-volumecal-probes-ucx2.json"
EVIDENCE = json.loads(DATA.read_text())
STAGES = ("baseline_first", "baseline", "probe", "restoration", "final")


def values(state):
    return {path: report["args"] for path, report in state.items()}


def test_calibration_evidence_states_its_exact_warm_scope():
    assert EVIDENCE["schema"] == 1
    assert EVIDENCE["device"] == UCX2.name
    assert EVIDENCE["serial"] == "24216011"
    assert EVIDENCE["firmware"] == {"usb_revision": "3.01", "dsp_version": 36}
    assert EVIDENCE["backend"]["protocol"] == "ODK1"
    assert EVIDENCE["backend"]["upstream"] == "f2fdd5ec78338848754aad32cc07f3440de63395"
    for field in ("binary_sha256", "series_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", EVIDENCE["backend"][field])
    assert len(EVIDENCE["cases"]) == 24
    direct = [case for case in EVIDENCE["cases"] if case["kind"] == "direct"]
    precision = [case for case in EVIDENCE["cases"] if case["kind"] == "precision"]
    assert {case["channel"] for case in direct} == set(range(1, 21))
    assert {case["requested"] for case in direct} == {-0.10}
    assert {(case["channel"], case["requested"]) for case in precision} == {
        (1, -0.01), (1, -0.02), (1, -24.0), (1, 3.0)}
    assert "No mono, cold completeness, acoustic gain or exhaustive-domain qualification" in (
        EVIDENCE["scope"])
    encoded = json.dumps(EVIDENCE)
    assert "/home/" not in encoded


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: case["id"])
def test_calibration_measured_target_and_partner_hold_the_register_contract(case):
    channel = case["channel"]
    left = channel if channel % 2 else channel - 1
    pair = [f"/output/{ch}/volumecal" for ch in (left, left + 1)]
    target = f"/output/{channel}/volumecal"
    partner, = set(pair) - {target}
    protected = {"/input/1/48v", "/input/2/48v",
                 f"/output/{left}/stereo", f"/output/{left + 1}/stereo", *pair}
    assert case["target"] == target
    assert case["pair"] == pair
    assert case["partner_finding"] == "unchanged"
    assert tuple(case["states"]) == STAGES
    previous = {}
    for stage in STAGES:
        state = case["states"][stage]
        assert set(state) == protected
        assert case["complete_device_paths"][stage] == 2232
        for path, report in state.items():
            assert report["origin"] == "DEVICE"
            assert type(report["sequence"]) is int
            assert report["sequence"] > previous.get(path, 0)
            previous[path] = report["sequence"]
            assert len(report["args"]) == 1
            value, = report["args"]
            if path.endswith("/volumecal"):
                assert report["tags"] == "f"
                assert type(value) is float
                assert math.isfinite(value)
                expected = case["requested"] if stage == "probe" and path == target else 0.0
                row = register_at(UCX2, path)
                assert value == numeric.expected_report(expected, row)
                assert reconcile.matches("f", (expected,), (value,), register=row)
            else:
                assert report["tags"] == "i"
                assert type(value) is int
                assert value == int(path.endswith("/stereo"))
    for stage in ("baseline_first", "restoration", "final"):
        assert values(case["states"][stage]) == values(case["states"]["baseline"])
    assert case["states"]["probe"][partner]["args"] == case["states"]["baseline"][partner]["args"]
    assert case["full_restoration_matches_original"] is True
    assert case["baseline_refreshes"] == 2
    row = register_at(UCX2, target)
    assert row.tags == "f"
    assert row.domain is NUMBER
    assert row.verify is VERIFIABLE
    assert row.policy is PIN
    assert (row.lo, row.hi, row.scale, row.unit) == (-24.0, 3.0, 0.01, "dB")
    assert row.lo <= case["requested"] <= row.hi
    assert cold_plug_complete(UCX2, target) is False
    assert register_at(FF802, target) is None


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: case["id"])
def test_calibration_three_owned_writes_end_and_watch_are_retained(case):
    target = case["target"]
    partner, = set(case["pair"]) - {target}
    writes = case["writes"]
    assert len(writes) == 3
    assert [write["path"] for write in writes] == [target, target, partner]
    assert [write["tags"] for write in writes] == ["f", "f", "f"]
    assert [write["args"] for write in writes] == [[case["requested"]], [0.0], [0.0]]
    assert [write["phase"] for write in writes] == ["probe", "restore-target", "restore-partner"]
    for write in writes:
        assert write["submitted"] is True
        assert write["acknowledged"] is True
        assert type(write["args"][0]) is float
    assert case["individual_fresh_meter_windows"] == ["probe", "restore-target", "restore-partner"]
    assert case["end_count"] == 1
    assert case["end_acknowledged"] is True
    assert case["watch_seconds"] == 24
    assert case["watch_device_events"] == 0
    assert case["writer_elapsed_seconds"] < case["writer_limit_seconds"] == 87
    assert case["observer_limit_seconds"] == 40
    for field in ("raw_evidence_sha256", "probe_source_sha256", "review_source_sha256",
                  "acceptance_audit_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", case[field])
