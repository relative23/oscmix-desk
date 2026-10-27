"""Hold AutoSet's narrow register claim against retained DEVICE evidence."""

import json
import math
import re
from pathlib import PurePosixPath

import pytest
from support import repo_file

from oscmix_desk.devices import FF802, UCX2
from oscmix_desk.registers import BOOL, REMEMBER, VERIFIABLE, cold_plug_complete, register_at

EVIDENCE = json.loads(repo_file("tests", "data", "input-autoset-probes-ucx2.json").read_text())
STAGES = ("baseline_first", "baseline", "probe", "restoration", "final")
PROTECTED = {f"/input/{ch}/{field}" for ch in range(1, 5)
             for field in ("autoset", "stereo", "gain")} | {"/input/1/48v", "/input/2/48v"}


def args(state):
    return {path: report["args"] for path, report in state.items()}


def assert_quiet_meter(report):
    assert report["tags"] == "ffffi"
    values = report["args"]
    assert len(values) == 5
    for value in values[:4]:
        # RAW JSON deliberately encodes negative infinity as a string.
        assert value == "-inf" or (type(value) is float and math.isfinite(value))
        assert value == "-inf" or value <= -30.0
    assert type(values[4]) is int
    assert values[4] == 0


def test_autoset_evidence_covers_only_four_warm_linked_flags():
    assert EVIDENCE["schema"] == 1
    assert EVIDENCE["device"] == UCX2.name
    assert EVIDENCE["firmware"] == {"usb_revision": "3.01", "dsp_version": 36}
    assert EVIDENCE["backend"]["upstream"] == "f2fdd5ec78338848754aad32cc07f3440de63395"
    assert EVIDENCE["backend"]["protocol"] == "ODK1"
    assert EVIDENCE["accepted_summary_sha256"] == (
        "e48cde87cb804b4ab507b4a690d178e14f3b145dee6d1bac3f39b016d192645e")
    for field in ("binary_sha256", "series_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", EVIDENCE["backend"][field])
    assert UCX2.channels["input-autoset"] == (1, 2, 3, 4)
    assert len(EVIDENCE["cases"]) == 4
    assert {case["target"] for case in EVIDENCE["cases"]} == {
        f"/input/{ch}/autoset" for ch in UCX2.channels["input-autoset"]}
    assert EVIDENCE["host_write_count"] == 12
    assert EVIDENCE["full_fresh_windows"] == 20
    assert EVIDENCE["not_qualified"] == [
        "induced overload/gain lowering", "acoustic protection",
        "cold completeness", "mono AutoSet independence"]


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: case["id"])
def test_autoset_fresh_device_states_and_own_restoration(case):
    ch = case["channel"]
    first = ch if ch % 2 else ch - 1
    pair = [f"/input/{n}/autoset" for n in (first, first + 1)]
    assert case["kind"] == "linked-flag"
    assert case["target"] == f"/input/{ch}/autoset"
    assert case["pair"] == pair
    assert case["requested_autoset"] == 1
    assert case["partner_finding"] == "followed"
    assert case["same_control_connection"] is True
    assert case["baseline_refreshes"] == 2
    assert case["full_restoration_matches_original"] is True
    assert tuple(case["states"]) == STAGES
    previous = {}
    for name in STAGES:
        state = case["states"][name]
        floor = case["receipt_boundaries"][name]
        assert type(floor) is int
        assert set(state) == PROTECTED
        assert case["complete_device_paths"][name] == 2232
        assert re.fullmatch(r"[0-9a-f]{64}", case["full_state_sha256"][name])
        for path, report in state.items():
            assert report["origin"] == "DEVICE"
            assert type(report["sequence"]) is int
            assert report["sequence"] > floor
            assert report["sequence"] > previous.get(path, 0)
            previous[path] = report["sequence"]
            if path.endswith("/gain"):
                assert report["tags"] == "f"
                assert len(report["args"]) == 1
                assert type(report["args"][0]) is float
                assert report["args"] == [0.0]
            else:
                assert report["tags"] == "i"
                assert len(report["args"]) == 1
                assert type(report["args"][0]) is int
                expected = int(path.endswith("/stereo") or (name == "probe" and path in pair))
                assert report["args"] == [expected]
    for name in ("baseline_first", "restoration", "final"):
        assert args(case["states"][name]) == args(case["states"]["baseline"])
        assert case["full_state_sha256"][name] == case["full_state_sha256"]["baseline"]
    assert case["full_state_sha256"]["probe"] != case["full_state_sha256"]["baseline"]
    writes = case["writes"]
    assert [write["path"] for write in writes] == [case["target"], *pair]
    assert [write["args"] for write in writes] == [[1], [0], [0]]
    assert [write["phase"] for write in writes] == ["probe", "restore-autoset", "restore-autoset"]
    for write in writes:
        assert write["submitted"] is True
        assert write["acknowledged"] is True
        assert write["tags"] == "i"
        assert type(write["args"][0]) is int


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: case["id"])
def test_autoset_sole_end_precedes_nonexclusive_watch(case):
    assert case["begin_count"] == 1
    assert case["end_count"] == 1
    assert case["end_acknowledged"] is True
    assert case["writer_elapsed_seconds"] <= case["writer_limit_seconds"] == 87
    assert case["backend_lease_limit_seconds"] == 90
    assert case["watch_seconds"] == 24
    assert case["watch_device_events"] == 0
    assert case["watch_after_end"] is True
    assert case["watch_nonexclusive"] is True
    timing = case["capture_timing"]
    for name in STAGES:
        capture = timing[name]
        assert capture["elapsed_started"] <= capture["refresh_ack_elapsed"]
        assert capture["refresh_ack_elapsed"] <= capture["elapsed_finished"]
        assert capture["after_end"] is (name == "final")
    end = case["end_elapsed_finished"]
    assert timing["restoration"]["elapsed_finished"] <= case["end_elapsed_started"] <= end
    assert end <= case["writer_elapsed_seconds"]
    watch = case["watch_timing"]
    assert end <= watch["elapsed_started"] <= watch["elapsed_finished"]
    assert watch["elapsed_finished"] - watch["elapsed_started"] >= 23.999
    assert watch["elapsed_finished"] <= timing["final"]["elapsed_started"]
    observation = case["observation"]
    assert observation["nonexclusive"] is True
    assert observation["begin_attempt_count"] == 0
    assert observation["limit"] == 40
    assert observation["elapsed_after_persist"] - observation["elapsed_started"] <= 40
    counts = case["counts"]
    assert counts["passed"] is True
    assert counts["whole_own_baseline_restored"] is True
    assert counts["full_fresh_windows"] == 5
    assert counts["host_write_count"] == 3


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: case["id"])
def test_autoset_meter_transients_are_quiet_and_fresh_at_submission(case):
    meter = case["meters"]
    assert meter["origin"] == "METERS"
    assert meter["new_connection"] is True
    assert meter["reader_closed"] is True
    assert meter["passed"] is True
    assert meter["receipt_time_is_device_time"] is False
    assert meter["total_seconds"] <= 4
    paths = {f"/input/{ch}/level" for ch in range(1, 5)}
    assert set(meter["levels"]) == paths
    submitted = case["writes"][0]["submission_check_elapsed"]
    reserve = case["writes"][0]["socket_send_reserve"]
    assert reserve == 0.5
    assert meter["finished_elapsed"] <= submitted
    assert len(meter["events"]) == case["counts"]["raw_meter_events"]
    for path, level in meter["levels"].items():
        assert_quiet_meter(level)
        events = [event for event in meter["events"] if event["path"] == path]
        sequences = level["sequences"]
        assert sequences == sorted(set(sequences))
        assert level["sequence_count"] == len(sequences) >= 2
        assert [event["sequence"] for event in events] == sequences
        assert events[-1]["args"] == level["args"]
        assert events[-1]["receipt_elapsed"] == level["last_receipt_elapsed"]
        assert 0 <= submitted + reserve - level["last_receipt_elapsed"] <= 1
        for event in events:
            assert event["origin"] == "METERS"
            assert event["receipt_elapsed"] <= meter["finished_elapsed"]
            assert_quiet_meter(event)


def test_autoset_fixture_retains_hashes_and_failed_attempt_without_private_paths():
    for case in EVIDENCE["cases"]:
        for field in ("probe_source_sha256", "review_source_sha256", "raw_evidence_sha256",
                      "wrapper_sha256", "hardware_review_sha256", "acceptance_review_sha256"):
            assert re.fullmatch(r"[0-9a-f]{64}", case[field])
        assert case["hardware_review_passed"] is (case["channel"] in (1, 4))
        assert not PurePosixPath(case["evidence_id"]).is_absolute()
    failed, = EVIDENCE["excluded_attempts"]
    assert failed["accepted"] is False
    assert failed["host_write_count"] == 0
    assert failed["missing_device_reports"] == ["/mix/11/input/17"]
    assert failed["raw_evidence_sha256"] == (
        "f90d65b0aceae3fb97d8ef070cf44971ab14fcb212d2104866fd41605e395a59")
    assert failed["raw_evidence_sha256"] not in {
        case["raw_evidence_sha256"] for case in EVIDENCE["cases"]}
    assert not PurePosixPath(failed["evidence_id"]).is_absolute()
    encoded = json.dumps(EVIDENCE)
    assert "/home/" not in encoded
    assert "started_monotonic" not in encoded
    assert "submission_receipt_check_at" not in encoded


def test_autoset_register_is_remembered_and_only_declared_for_ucx2_inputs_one_to_four():
    for ch in range(1, 5):
        path = f"/input/{ch}/autoset"
        row = register_at(UCX2, path)
        assert row.tags == "i"
        assert row.domain is BOOL
        assert row.verify is VERIFIABLE
        assert row.policy is REMEMBER
        assert cold_plug_complete(UCX2, path) is False
    for ch in range(5, 21):
        assert register_at(UCX2, f"/input/{ch}/autoset") is None
    assert "input-autoset" not in FF802.channels
    for ch in range(1, 5):
        assert register_at(FF802, f"/input/{ch}/autoset") is None
