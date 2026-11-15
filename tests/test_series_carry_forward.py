"""Measurements reach the shipped series only through their recorded source proof (patch 0020)."""

import json

import pytest
import series_carry
from series_carry import (
    ALLOWED,
    RECORD,
    changed_functions,
    manifest_for,
    predecessor,
    sha,
    shipped_manifest,
)
from support import repo_file

SHIPPED = shipped_manifest()


def record():
    return json.loads(repo_file(*RECORD).read_text())


def test_the_shipped_series_is_reached_from_the_carried_one():
    old = predecessor(record(), SHIPPED)
    assert sha(old) == record()["from_series_sha256"]
    assert json.loads(old)["upstream"] == json.loads(SHIPPED)["upstream"]
    assert len(json.loads(SHIPPED)["patches"]) == len(json.loads(old)["patches"]) + 1


def test_every_carried_record_was_taken_with_the_carried_series():
    for name in record()["evidence"]:
        parts = tuple(name.split("/"))
        data = json.loads(repo_file(*parts).read_text())
        assert manifest_for(parts, data["running_backend"]["series_sha256"]) == predecessor()


def test_patch_0020_changes_only_the_durec_list_and_fatal():
    assert changed_functions(repo_file("patches", record()["added_patches"][0]).read_text()) \
        == ALLOWED


def test_a_record_of_the_shipped_series_needs_no_proof():
    assert manifest_for(("tests", "data", "absent.json"), sha(SHIPPED)) == SHIPPED


def manifest_with(change):
    manifest = json.loads(SHIPPED)
    change(manifest)
    return json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"


@pytest.mark.parametrize("change", [
    lambda m: m["patches"].append({"file": "0021-more.patch", "sha256": "0" * 64}),
    lambda m: m["patches"].pop(),
    lambda m: m["patches"][3].update(sha256="0" * 64),
    lambda m: m.update(upstream="0" * 40),
], ids=["another-patch", "without-0020", "earlier-patch", "pin"])
def test_any_other_series_change_breaks_the_chain(change):
    with pytest.raises(ValueError, match=r"carried patches|earlier patch|pin changed"):
        predecessor(record(), manifest_with(change))


@pytest.mark.parametrize(("damage", "message"), [
    (lambda r: r.update(from_manifest=r["from_manifest"] + " "), "another series"),
    (lambda r: r.update(added_patches=["0020-other.patch"]), "carried patches"),
    (lambda r: r.update(kind="something-else"), "not a carry-forward record"),
], ids=["manifest", "patches", "kind"])
def test_a_record_that_claims_more_than_it_shows_is_refused(damage, message):
    value = record()
    damage(value)
    with pytest.raises(ValueError, match=message):
        predecessor(value, SHIPPED)


def test_only_the_recorded_evidence_is_carried(tmp_path, monkeypatch):
    carried = record()["from_series_sha256"]
    with pytest.raises(ValueError, match="does not cover"):
        manifest_for(("tests", "data", "roomeq-delay-ucx2.json"), carried)
    with pytest.raises(ValueError, match="does not ship"):
        manifest_for(("tests", "data", "phase-probes-ucx2.json"), "0" * 64)
    changed = record()
    name = "tests/data/phase-probes-ucx2.json"
    changed["evidence"][name] = "0" * 64
    copy = tmp_path / "carry.json"
    copy.write_text(json.dumps(changed))
    monkeypatch.setattr(series_carry, "RECORD", (str(copy),))
    with pytest.raises(ValueError, match="differs from the recorded one"):
        manifest_for(tuple(name.split("/")), carried)


def patch(header, *lines):
    return "\n".join(["diff --git a/oscmix.c b/oscmix.c", "--- a/oscmix.c", "+++ b/oscmix.c",
                      header, *lines]) + "\n"


@pytest.mark.parametrize(("text", "found"), [
    (patch("@@ -1,3 +1,4 @@ newmix(struct context *ctx, int val)",
           " \tint x;", "+\tsetreg(0, 0);", " \treturn;"), {"newmix"}),
    (patch("@@ -1,2 +1,3 @@", " }", "+int stray;"), {""}),
    (patch("@@ -1,3 +1,4 @@", " }", " ", "-static void", "+static bool",
           " resizedurecfiles(size_t len)", " {"), {"resizedurecfiles"}),
    (patch("@@ -1,2 +1,3 @@", " #include <stdio.h>", "+#include <errno.h>", "+"), None),
], ids=["another-function", "top-level", "return-type", "include"])
def test_the_scanner_names_the_function_of_every_changed_line(text, found):
    functions = changed_functions(text)
    if found is None:
        assert functions == {}
    else:
        assert functions == {"oscmix.c": found}
        assert (found <= ALLOWED["oscmix.c"]) is (found == {"resizedurecfiles"})
