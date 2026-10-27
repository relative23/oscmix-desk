"""Retained Playback-Mute scope; no live process, private path or hardware access."""

import copy
import json

import pytest
from playback_mute_evidence import check_manifest, check_model, covered_paths, validate
from support import repo_file

from oscmix_desk.devices import FF802, UCX2
from oscmix_desk.registers import BOOL, REESTABLISHED, REMEMBER, cold_plug_complete, register_at

EVIDENCE = json.loads(repo_file("tests", "data", "playback-mute-probes-ucx2.json").read_text())


def test_retained_projection_preserves_its_exact_scope_and_backend_sources():
    validate(EVIDENCE)
    check_manifest(EVIDENCE, repo_file("patches", "backend-series.json").read_bytes())
    assert EVIDENCE["representative_pcm_effect"] is None
    assert EVIDENCE["independent_on_measurements"] == len(EVIDENCE["cases"])
    assert len(EVIDENCE["directly_addressed_paths"]) == 2 * len(EVIDENCE["cases"])
    assert "/home/" not in json.dumps(EVIDENCE)


@pytest.mark.parametrize("channel", range(1, 21))
def test_mute_evidence_never_expands_device_or_cold_readback(channel):
    check_model(UCX2, register_at)
    path = "/playback/%d/mute" % channel
    row = register_at(UCX2, path)
    assert row.tags == "i"
    assert row.domain is BOOL
    assert row.verify is REESTABLISHED
    assert row.policy is REMEMBER
    assert cold_plug_complete(UCX2, path) is False
    assert register_at(FF802, path) is None


def test_incomplete_evidence_cannot_contribute_to_full_hardware_coverage():
    value = copy.deepcopy(EVIDENCE)
    value["cases"] = []
    value["directly_addressed_paths"] = []
    value["independent_on_measurements"] = 0
    value["missing_pairs"] = [[n, n + 1] for n in range(1, 21, 2)]
    value["matrix_scope_complete"] = False
    validate(value)
    with pytest.raises(ValueError, match="incomplete"):
        covered_paths(value)


@pytest.mark.parametrize(
    "field",
    [
        "device_mute_readback",
        "coefficient_device_readback",
        "acoustic_safety_claim",
        "atomic_cache_snapshot_claim",
        "cold_start_qualified",
    ],
)
def test_projection_cannot_promote_cache_or_host_write_to_physical_claim(field):
    value = copy.deepcopy(EVIDENCE)
    value["claims"][field] = True
    with pytest.raises(ValueError, match="claim"):
        validate(value)


def test_source_series_changes_require_new_measurement_evidence():
    with pytest.raises(ValueError, match="series differs"):
        check_manifest(EVIDENCE, repo_file("patches", "backend-series.json").read_bytes() + b" ")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("static_sources_qualified_outside_METER_callbacks", False),
        ("peer_association_scope", "global-process-discovery"),
        ("global_socket_owner_exclusivity_claim", True),
        ("owned_unit_no_subgroups", False),
        ("fresh_scoped_peer_checks", 0),
        ("fresh_scoped_peer_checks", True),
        ("scoped_peer_facts_sha256", "malformed"),
        ("static_immediately_before_each_WRITE_and_REFRESH", False),
        ("meter_receipt_age_rechecked_after_static", False),
        ("inner_meter_seconds", 4.0),
        ("outer_meter_seconds", 4.1),
        ("static_seconds", 0.3),
        ("write_submission_seconds", 5.0),
        ("upfront_writer_cost", 84.78),
        ("narrowed_capture_cost", 41.78),
        ("cleanup_reserve", 24.82),
        ("actual_static_qualifications", 8),
        ("actual_static_qualifications", True),
        ("actual_max_static_seconds", 0.201),
        ("actual_max_static_seconds", float("nan")),
        ("raw_stages_sha256", "malformed"),
        ("raw_meter_attempts_sha256", "malformed"),
    ],
)
def test_guard_qualification_budgets_remain_strict(key, value):
    from playback_mute_evidence import validate_guards

    # Explicit synthetic schema-only fields, never hardware evidence.
    guards = dict(
        peer_association_scope="owned-unit-and-current-kernel-backend-family",
        global_socket_owner_exclusivity_claim=False,
        owned_unit_no_subgroups=True,
        fresh_scoped_peer_checks=9,
        scoped_peer_facts_sha256="0" * 64,
        static_sources_qualified_outside_METER_callbacks=True,
        static_immediately_before_each_WRITE_and_REFRESH=True,
        meter_receipt_age_rechecked_after_static=True,
        inner_meter_seconds=3.8,
        outer_meter_seconds=4.0,
        static_seconds=0.2,
        write_submission_seconds=5.2,
        upfront_writer_cost=85.38,
        narrowed_capture_cost=42.38,
        cleanup_reserve=25.22,
        actual_static_qualifications=9,
        actual_max_static_seconds=0.01,
        raw_stages_sha256="0" * 64,
        raw_meter_attempts_sha256="0" * 64,
    )
    validate_guards(guards)
    guards[key] = value
    with pytest.raises(ValueError, match=r"guard|source|qualification|SHA"):
        validate_guards(guards)


# V12 synthetic portable arithmetic regressions; never hardware evidence.
def exact_cost_guard_model():
    return dict(
        peer_association_scope="owned-unit-and-current-kernel-backend-family",
        global_socket_owner_exclusivity_claim=False,
        owned_unit_no_subgroups=True,
        fresh_scoped_peer_checks=9,
        scoped_peer_facts_sha256="0" * 64,
        static_sources_qualified_outside_METER_callbacks=True,
        static_immediately_before_each_WRITE_and_REFRESH=True,
        meter_receipt_age_rechecked_after_static=True,
        inner_meter_seconds=3.8,
        outer_meter_seconds=4.0,
        static_seconds=0.2,
        write_submission_seconds=5.2,
        upfront_writer_cost=85.38,
        narrowed_capture_cost=42.38,
        cleanup_reserve=25.22,
        actual_static_qualifications=9,
        actual_max_static_seconds=0.01,
        raw_stages_sha256="0" * 64,
        raw_meter_attempts_sha256="0" * 64,
    )


@pytest.mark.parametrize("cost", [85.38, 0.2 + 2 * (20.1 + 0.2) + 42.38 + 2.0 + 0.2])
def test_portable_guard_accepts_only_exact_legacy_or_qualified_runtime_arithmetic(cost):
    from playback_mute_evidence import validate_guards

    guards = exact_cost_guard_model()
    guards["upfront_writer_cost"] = cost
    validate_guards(guards)
    assert guards["upfront_writer_cost"] == cost


@pytest.mark.parametrize("cost", [
    True, False, None, "85.38", float("nan"), float("inf"), -float("inf"),
    84.78, 85.38001, 85.37999,
    float.fromhex("0x1.55851eb851eb7p+6"), float.fromhex("0x1.55851eb851ebap+6"),
])
def test_portable_guard_refuses_nonfinite_types_and_neighboring_writer_costs(cost):
    from playback_mute_evidence import validate_guards

    guards = exact_cost_guard_model()
    guards["upfront_writer_cost"] = cost
    with pytest.raises(ValueError, match="source qualification cost"):
        validate_guards(guards)


@pytest.mark.parametrize(("key", "value"), [
    ("inner_meter_seconds", 3.8), ("outer_meter_seconds", 4.0),
    ("static_seconds", 0.2), ("write_submission_seconds", 5.2),
    ("narrowed_capture_cost", 42.38), ("cleanup_reserve", 25.22),
])
@pytest.mark.parametrize("direction", [-float("inf"), float("inf")])
def test_portable_guard_keeps_all_other_budgets_exact(key, value, direction):
    import math

    from playback_mute_evidence import validate_guards

    guards = exact_cost_guard_model()
    guards[key] = math.nextafter(value, direction)
    with pytest.raises(ValueError, match="source qualification cost"):
        validate_guards(guards)
