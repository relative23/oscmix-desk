"""The playback-mute evidence reaches the shipped series only through its recorded source proof."""

import copy
import hashlib
import json

import pytest
from evidence_carry_forward import RECORD, carried_manifest, changed_lines
from support import repo_file

EVIDENCE_BYTES = repo_file("tests", "data", "playback-mute-probes-ucx2.json").read_bytes()
EVIDENCE = json.loads(EVIDENCE_BYTES)
SHIPPED = repo_file("patches", "backend-series.json").read_bytes()


def record():
    return json.loads(repo_file(*RECORD).read_text())


def test_the_shipped_series_is_reached_from_the_evidence_series():
    old = carried_manifest(EVIDENCE, SHIPPED, record(), EVIDENCE_BYTES)
    assert json.loads(old)["upstream"] == EVIDENCE["backend"]["upstream"]
    assert json.loads(SHIPPED)["upstream"] == "89ed835ecf8118a21d1d451be7d71944fa5ec384"


def test_evidence_of_the_shipped_series_needs_no_record():
    evidence = copy.deepcopy(EVIDENCE)
    evidence["backend"]["series_sha256"] = hashlib.sha256(SHIPPED).hexdigest()
    assert carried_manifest(evidence, SHIPPED, {"kind": "unused"}) is SHIPPED


def test_the_proof_names_only_lines_the_carried_changes_explain():
    lines = changed_lines(record()["source_diff"])
    assert len(lines) == 10
    assert sum(line.startswith("+") for line in lines) == 7


def manifest_with(change):
    manifest = json.loads(SHIPPED)
    change(manifest)
    return json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"


@pytest.mark.parametrize("change", [
    lambda m: m["patches"].append(dict(file="0017-more.patch", sha256="0" * 64)),
    lambda m: m["patches"][3].update(sha256="0" * 64),
    lambda m: m.update(upstream="0" * 40),
])
def test_any_further_series_change_breaks_the_chain(change):
    with pytest.raises(ValueError, match=r"series differs|changed|adds more"):
        carried_manifest(EVIDENCE, manifest_with(change), record(), EVIDENCE_BYTES)


@pytest.mark.parametrize(("key", "damage", "message"), [
    ("source_diff", lambda r: r["source_diff"] + "+\tsetreg(0, 0);\n", "no carried change"),
    ("changed_sources", lambda r: r["changed_sources"] + ["main.c"], "other prepared sources"),
    ("to_source", lambda r: dict(r["to_source"], source_sha256=dict(
        r["to_source"]["source_sha256"], **{"main.c": "0" * 64})), "other prepared sources"),
    ("from_manifest", lambda r: r["from_manifest"] + " ", "another series"),
    ("evidence_sha256", lambda r: "0" * 64, "evidence differs"),
])
def test_a_record_that_claims_more_than_it_shows_is_refused(key, damage, message):
    value = record()
    value[key] = damage(value)
    with pytest.raises(ValueError, match=message):
        carried_manifest(EVIDENCE, SHIPPED, value, EVIDENCE_BYTES)
