"""Hold the M/S register model against retained DEVICE measurements."""

import json
import re

import pytest
from support import repo_file

from oscmix_desk.devices import UCX2
from oscmix_desk.registers import BOOL, REMEMBER, VERIFIABLE, register_at

EVIDENCE = json.loads(repo_file("tests", "data", "input-msproc-probes-ucx2.json").read_text())


def args(state):
    return {path: report["args"] for path, report in state.items()}


def test_msproc_evidence_covers_the_declared_inputs_and_both_mono_directions():
    assert EVIDENCE["schema"] == 1
    assert EVIDENCE["device"] == UCX2.name
    assert EVIDENCE["firmware"] == {"usb_revision": "3.01", "dsp_version": 36}
    backend = EVIDENCE["backend"]
    assert backend["upstream"] == "f2fdd5ec78338848754aad32cc07f3440de63395"
    assert backend["protocol"] == "ODK1"
    for field in ("binary_sha256", "series_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", backend[field])
    forward = [case for case in EVIDENCE["cases"] if case["kind"] == "linked-forward"]
    assert len(forward) == 20
    assert {case["target"] for case in forward} == {
        f"/input/{channel}/msproc" for channel in range(1, 21)}
    for kind in ("linked-reverse", "mono"):
        cases = [case for case in EVIDENCE["cases"] if case["kind"] == kind]
        assert len(cases) == 2
        assert {case["target"] for case in cases} == {"/input/13/msproc", "/input/14/msproc"}
    dependency = EVIDENCE["link_dependency"]
    assert dependency["accepted"] is True
    assert dependency["msproc_writes"] == 0
    assert dependency["original_device_paths"] == 2232
    assert dependency["mono_device_paths"] == 2242
    assert dependency["full_restoration_matches_original"] is True
    assert dependency["end_acknowledged"] is True


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: case["id"])
def test_each_msproc_case_retains_fresh_flags_and_original_restoration(case):
    pair = case["pair"]
    target = case["target"]
    partner = next(path for path in pair if path != target)
    stereo = {path.removesuffix("msproc") + "stereo" for path in pair}
    required = set(pair) | stereo | {"/input/1/48v", "/input/2/48v"}
    assert len(pair) == 2
    assert target in pair
    # One old forward case needed an extra pre-write dump after a gap.
    # Its two complete stable baseline windows are retained below.
    assert case["baseline_refreshes"] in ((2, 3) if case["kind"] == "linked-forward" else (2,))
    assert case["same_control_connection"] is True
    assert case["full_restoration_matches_original"] is True
    assert case["end_acknowledged"] is True
    assert case["end_count"] == 1
    assert case["watch_seconds"] == 24
    assert case["watch_device_events"] == 0
    for field in ("probe_source_sha256", "raw_evidence_sha256",
                  "review_sha256", "acceptance_review_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", case[field])
    previous = {}
    states = case["states"]
    for name, state in states.items():
        assert set(state) == required
        assert case["complete_device_paths"][name] in (2232, 2242)
        for path, report in state.items():
            assert report["origin"] == "DEVICE"
            assert report["tags"] == "i"
            assert len(report["args"]) == 1
            assert type(report["args"][0]) is int
            assert report["args"][0] in (0, 1)
            assert type(report["sequence"]) is int
            assert report["sequence"] > previous.get(path, 0)
            previous[path] = report["sequence"]
            if path.endswith("/48v"):
                assert report["args"] == [0]
        for path in pair:
            row = register_at(UCX2, path)
            assert row.domain is BOOL
            assert row.verify is VERIFIABLE
            assert row.policy is REMEMBER
    probe = states["probe"]
    assert probe[target]["args"] == [case["requested_msproc"]]
    writes = case["writes"]
    assert all(write["submitted"] is True for write in writes)
    assert all(write["acknowledged"] is True for write in writes)
    assert all(write["tags"] == "i" for write in writes)
    assert all(type(write["args"][0]) is int for write in writes)
    if case["kind"] == "linked-forward":
        assert case["requested_msproc"] == 1
        assert case["partner_finding"] == "followed"
        assert probe[partner]["args"] == [1]
        assert args(states["baseline_first"]) == args(states["baseline"])
        assert args(states["restoration"]) == args(states["baseline"])
        assert args(states["final"]) == args(states["baseline"])
        assert [write["path"] for write in writes] == [target, *pair]
        assert [write["args"] for write in writes] == [[1], [0], [0]]
        assert case["watch_after_end"] is False
        assert case["watch_nonexclusive"] is False
        assert all(timing is None for timing in case["capture_timing"].values())
    elif case["kind"] == "linked-reverse":
        assert case["requested_msproc"] == 0
        assert case["partner_finding"] == "followed"
        assert probe[partner]["args"] == [0]
        assert case["restoration_alias"] == "probe"
        assert args(states["probe"]) == args(states["original"])
        assert args(states["final"]) == args(states["original"])
        assert [write["path"] for write in writes] == [target, target]
        assert [write["args"] for write in writes] == [[1], [0]]
        assert case["counts"]["full_windows"] == 6
        assert case["watch_after_end"] is False
        assert case["watch_nonexclusive"] is False
    else:
        assert case["kind"] == "mono"
        assert case["requested_msproc"] == 1
        assert case["partner_finding"] == "independent"
        assert probe[partner]["args"] == [0]
        assert all(probe[path]["args"] == [0] for path in stereo)
        assert args(states["mono_first"]) == args(states["mono"])
        assert args(states["mono_restoration"]) == args(states["mono"])
        assert args(states["restoration"]) == args(states["original"])
        assert args(states["final"]) == args(states["original"])
        assert [write["path"] for write in writes] == [
            "/input/13/stereo", target, *pair, "/input/13/stereo"]
        assert [write["args"] for write in writes] == [[0], [1], [0], [0], [1]]
        assert case["writer_elapsed_seconds"] <= case["writer_limit_seconds"] == 87
        assert case["backend_lease_limit_seconds"] == 90
        assert case["watch_after_end"] is True
        assert case["watch_nonexclusive"] is True
        end = case["end_elapsed_finished"]
        timing = case["capture_timing"]
        assert timing["restoration"]["elapsed_finished"] <= case["end_elapsed_started"] <= end
        watch = case["watch_timing"]
        assert end <= watch["elapsed_started"] <= watch["elapsed_finished"]
        assert watch["elapsed_finished"] - watch["elapsed_started"] >= 23.999
        assert watch["elapsed_finished"] <= timing["final"]["elapsed_started"]
        assert timing["final"]["after_end"] is True
        observation = case["observation"]
        assert observation["nonexclusive"] is True
        assert observation["limit"] == 40
        assert observation["elapsed_after_persist"] - observation["elapsed_started"] <= 40
