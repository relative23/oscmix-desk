"""The playback-mute record taken with the shipped series, and what it may not claim."""

import copy
import json

import pytest
from playback_mute_wire_evidence import FIXTURE, PAIRS, covered_paths, registers, validate
from support import repo_file

RECORD = json.loads(repo_file(*FIXTURE).read_text())
MANIFEST = repo_file("patches", "backend-series.json").read_bytes()


def test_the_record_passes_with_the_series_this_tree_ships():
    paths = validate(RECORD, MANIFEST)
    assert paths == {"/playback/%d/mute" % n for n in range(1, 21)}


def test_the_registers_are_the_pair_s_columns_on_every_output():
    assert registers(1)[:2] == [0x4020, 0x4021]
    assert registers(19)[-1] == 0x4000 | 19 << 6 | 51
    assert len({reg for pair in PAIRS for reg in registers(pair)}) == 400


def test_mute_on_writes_zero_and_off_writes_what_the_wire_last_carried():
    for case in RECORD["cases"]:
        assert [step["name"] for step in case["steps"]] == ["on", "off-target", "off-partner"]
        assert all(step["complete"] for step in case["steps"])
        assert len(case["restore_words"]) == 40


def mutated(change):
    value = copy.deepcopy(RECORD)
    change(value)
    return value


@pytest.mark.parametrize(("change", "message"), [
    (lambda v: v["cases"].pop(), "every pair"),
    (lambda v: v["cases"][3]["steps"][0].update(coefficient_words=39), "40 words"),
    (lambda v: v["cases"][0]["steps"][1].update(messages=2), "40 words"),
    (lambda v: v["cases"][5]["restore_words"].pop(), "restore words"),
    (lambda v: v["claims"].update(device_mute_readback=True), "claims more"),
    (lambda v: v["capture"].update(first_message=v["running_backend"]["started"] + 1),
     "from its start"),
    (lambda v: v["acoustic"]["stages"][1].update(verdict="carried"), "silent muted"),
    (lambda v: v["acoustic"]["stages"][0].update(device_rate=[96000, 48000]), "another rate"),
    (lambda v: v["final_dump"].update(changed=["/output/15/volume"]), "given back"),
    (lambda v: v["running_backend"].update(series_sha256="0" * 64), "series"),
])
def test_a_record_that_shows_less_is_refused(change, message):
    with pytest.raises(ValueError, match=message):
        validate(mutated(change), MANIFEST)


def test_an_incomplete_record_covers_nothing():
    value = mutated(lambda v: v["cases"][2].update(passed=False))
    with pytest.raises(ValueError, match="incomplete"):
        covered_paths(value)
