"""The measurement that goes with the backend pin bump of 2026-11-04 (ADR 0008): loopback."""

import json
import re

from support import repo_file

EVIDENCE = json.loads(repo_file("tests", "data", "loopback-output-index-ucx2.json").read_text())
PIN = "89ed835ecf8118a21d1d451be7d71944fa5ec384"


def test_the_artifact_names_the_revision_it_bumped_to():
    backend = EVIDENCE["backend"]
    assert backend["upstream"] == PIN
    assert re.fullmatch(r"[0-9a-f]{40}", backend["previous_pin"])
    assert backend["previous_pin"] != PIN
    assert backend["measured_tree"] == backend["pinned_tree"]
    change = backend["change"]
    assert (change["files_changed"], change["insertions"], change["deletions"]) == (1, 1, 1)


def test_the_pin_files_carry_the_revision_while_it_is_the_pin():
    install = repo_file("install.sh").read_text()
    match = re.search(r'^OSCMIX_REF="\$\{OSCMIX_REF:-([0-9a-f]{40})\}"$', install, re.MULTILINE)
    assert match
    series = json.loads(repo_file("patches", "backend-series.json").read_text())
    assert series["upstream"] == match.group(1)
    if match.group(1) == PIN:
        assert EVIDENCE["backend"]["series_applies"] is True


def test_the_words_follow_the_zero_based_output_index_and_replace_the_all_ones_word():
    cases = EVIDENCE["offline"]["cases"]
    assert {c["output"] for c in cases} >= {1, 2, 5, 6, 20}
    for case in cases:
        assert int(case["before"], 16) == 0xFFFFFFFF
        assert int(case["after"], 16) == case["on"] << 7 | case["output"] - 1


def test_the_device_observations_name_the_output_that_loops_back():
    hardware = EVIDENCE["hardware"]
    looped = {o["case"]: o["loopback_channels_dbfs"] for o in hardware["observations"]}
    assert list(looped["/output/1/loopback 1"]) == ["1"]
    assert list(looped["/output/2/loopback 1"]) == ["2"]
    assert looped["/output/1/loopback 0"] == {}
    assert looped["/output/2/loopback 0"] == {}
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hardware["recordings"].values())
