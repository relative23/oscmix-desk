"""A partial mutation run must not pass merely because its early score does."""

import importlib.util
import json
import subprocess

import pytest
from support import repo_file


@pytest.fixture
def policy(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "mutation_policy", repo_file("scripts", "mutation-policy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATS", tmp_path / "stats.json")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"min_score": 0.79, "tolerance": 0.01}))
    monkeypatch.setattr(module, "BASELINE", baseline)
    return module


def export(policy, monkeypatch, **changes):
    counts = dict(total=100, killed=79, survived=19, no_tests=1, timeout=1,
                  skipped=0, suspicious=0, check_was_interrupted_by_user=0,
                  segfault=0)
    counts.update(changes)

    def run(command, **kwargs):
        policy.STATS.write_text(json.dumps(counts))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(policy.subprocess, "run", run)


def test_an_interrupted_run_with_a_passing_partial_score_is_refused(
        policy, monkeypatch, capsys):
    # Actual interrupted 0.7.2 qualification: 2,044 mutants not yet run.
    export(policy, monkeypatch, total=8737, killed=5286, survived=1399,
           no_tests=0, timeout=8)
    assert policy.main() == 2
    assert "6693 of 8737" in capsys.readouterr().err


@pytest.mark.parametrize("field", [
    "skipped", "suspicious", "check_was_interrupted_by_user", "segfault",
])
def test_unresolved_outcomes_are_not_a_success(policy, monkeypatch, field):
    export(policy, monkeypatch, survived=18, **{field: 1})
    assert policy.main() == 2


@pytest.mark.parametrize(("killed", "survived", "expected"), [
    (79, 19, 0), (70, 28, 1),
])
def test_a_complete_run_still_has_to_pass_the_score(
        policy, monkeypatch, killed, survived, expected):
    export(policy, monkeypatch, killed=killed, survived=survived)
    assert policy.main() == expected


@pytest.mark.parametrize("returncode", [0, 1])
def test_an_export_without_new_data_cannot_reuse_old_results(
        policy, monkeypatch, returncode):
    export(policy, monkeypatch)
    assert policy.main() == 0
    monkeypatch.setattr(policy.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, returncode))
    assert policy.main() == 2


@pytest.mark.parametrize("value", [None, True, -1, 100.0, "100"])
def test_invalid_counts_are_refused(policy, monkeypatch, value):
    export(policy, monkeypatch, total=value)
    assert policy.main() == 2


def test_no_judged_mutants_cannot_pass(policy, monkeypatch):
    export(policy, monkeypatch, killed=0, survived=0, no_tests=99)
    assert policy.main() == 2
