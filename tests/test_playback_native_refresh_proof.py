"""Synthetic compact-proof regressions; no hardware observation is fabricated."""

import hashlib
import json

import pytest
from playback_mute_evidence import validate_native_refresh_proof


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def assessment(repeats=1):
    cells = []
    for output in range(0, 20, 2):
        for source in range(20):
            own = repeats if (output, source) == (16, 11) else 0
            word = ((0x2000 | output << 6 | source) << 16) | 0x8064
            word |= 0 if (word).bit_count() % 2 else 0x80000000
            cells.append(dict(
                output=output, source=source, db_events=1, pan_events=1 + own,
                pan_repeats=own, pan_word=word,
            ))
    proof = dict(
        schema="oscmix-desk.native-zero-refresh-summary.v1",
        coordinate_convention="zero-based-output-even-0..18/input-source-0..19",
        source_scope="native-input-only", cable=1, window_ns=[0, 10_000_000],
        cell_count=200, db_events=200, pan_events=200 + repeats, pan_repeats=repeats,
        cause_events=400 + repeats, matched_out_frames=400 + repeats, cells=cells,
        cells_sha256=digest(cells), full_out_frames_sha256="0" * 64,
        full_1to1_refs_sha256="0" * 64,
    )
    return dict(
        message_counts={"unchanged_native_zero_corrections": 400 + repeats},
        assessed_groups_sha256={"unchanged_native_zero_corrections": "0" * 64},
        native_refresh_proof=proof,
    )


@pytest.mark.parametrize("repeats", [0, 1, 4])
def test_exact200_cells_bind_every_identical_pan_repeat(repeats):
    value = assessment(repeats)
    validate_native_refresh_proof(value)
    proof = value["native_refresh_proof"]
    assert proof["matched_out_frames"] == 400 + repeats
    assert proof["cells"][171]["output"] == 16
    assert proof["cells"][171]["source"] == 11


@pytest.mark.parametrize("count", [0, 400, 401])
def test_legacy400_is_unchanged_but_extra_frames_require_complete_proof(count):
    value = dict(message_counts={"unchanged_native_zero_corrections": count})
    if count == 401:
        with pytest.raises(ValueError, match="extra native"):
            validate_native_refresh_proof(value)
    else:
        validate_native_refresh_proof(value)


@pytest.mark.parametrize("fault", [
    "schema", "coordinate", "scope", "cable-bool", "window-bool", "reverse-window",
    "missing-cell", "duplicate-cell", "offset-output", "odd-output", "source20",
    "db-two", "db-bool", "pan-zero", "pan-bool", "repeat-count", "repeat-bool",
    "pan-parity", "pan-register", "pan-invalid", "native-total", "cell-total",
    "db-total", "pan-total", "cause-total", "matched-total", "count-bool",
    "reference-sha", "out-sha", "cell-sha", "extra-field",
])
def test_portable_summary_never_loosens_geometry_counts_or_reference_commitments(fault):
    value = assessment()
    proof, cell = value["native_refresh_proof"], value["native_refresh_proof"]["cells"][0]
    if fault == "schema":
        proof["schema"] = "other"
    elif fault == "coordinate":
        proof["coordinate_convention"] = "physical-output-2..20"
    elif fault == "scope":
        proof["source_scope"] = "playback"
    elif fault == "cable-bool":
        proof["cable"] = True
    elif fault == "window-bool":
        proof["window_ns"][0] = False
    elif fault == "reverse-window":
        proof["window_ns"] = [2, 1]
    elif fault == "missing-cell":
        proof["cells"].pop()
    elif fault == "duplicate-cell":
        proof["cells"][1] = dict(cell)
    elif fault in ("offset-output", "odd-output"):
        cell["output"] = 2 if fault == "offset-output" else 1
    elif fault == "source20":
        cell["source"] = 20
    elif fault in ("db-two", "db-bool"):
        cell["db_events"] = 2 if fault == "db-two" else True
    elif fault in ("pan-zero", "pan-bool"):
        cell["pan_events"] = 0 if fault == "pan-zero" else True
    elif fault in ("repeat-count", "repeat-bool"):
        cell["pan_repeats"] = 1 if fault == "repeat-count" else False
    elif fault == "pan-parity":
        cell["pan_word"] ^= 0x80000000
    elif fault == "pan-register":
        cell["pan_word"] ^= 0x00010000 | 0x80000000
    elif fault == "pan-invalid":
        cell["pan_word"] ^= 0x8000 | 0x80000000
    elif fault == "native-total":
        value["message_counts"]["unchanged_native_zero_corrections"] += 1
    elif fault in ("cell-total", "db-total", "pan-total", "cause-total", "matched-total"):
        key = {
            "cell-total": "cell_count", "db-total": "db_events", "pan-total": "pan_events",
            "cause-total": "cause_events", "matched-total": "matched_out_frames",
        }[fault]
        proof[key] += 1
    elif fault == "count-bool":
        proof["cause_events"] = True
    elif fault == "reference-sha":
        proof["full_1to1_refs_sha256"] = "missing"
    elif fault == "out-sha":
        proof["full_out_frames_sha256"] = "1" * 64
    elif fault == "extra-field":
        proof["deduplicated"] = True
    proof["cells_sha256"] = digest(proof["cells"])
    if fault == "cell-sha":
        proof["cells_sha256"] = "0" * 64
    with pytest.raises(ValueError, match=r"native|SHA"):
        validate_native_refresh_proof(value)
