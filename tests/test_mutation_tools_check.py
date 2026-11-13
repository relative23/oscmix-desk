"""The mutation tools run as the pinned pair or not at all.

From November 8 to 12, 2026 the nightly shards ran mutmut 3.7.0 under
pytest 9.2.0, which removed the Item._nodeid mutmut reads: every shard
stopped after its first test, and the nights stayed green.
"""

import importlib.metadata
import importlib.util
import re
import subprocess
import sys

import pytest
from support import repo_file


@pytest.fixture
def check():
    spec = importlib.util.spec_from_file_location(
        "mutation_tools_check", repo_file("scripts", "mutation-tools-check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(path):
    return subprocess.run(
        [sys.executable, str(repo_file("scripts", "mutation-tools-check.py")), str(path)],
        capture_output=True, text=True, timeout=60, check=False)


def test_pins_are_the_exact_lines_only(check, tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("# comment\n-r requirements-dev.txt\nhypothesis>=6.100\n"
                    "MutMut==3.7.0  # the join reads its layout\npytest==9.1.1\n")
    assert check.pins(path) == {"mutmut": "3.7.0", "pytest": "9.1.1"}


def test_a_file_without_pins_is_refused(check, tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("pytest>=7.0\n")
    with pytest.raises(ValueError, match="no pinned tools"):
        check.pins(path)


def test_each_wrong_or_missing_tool_is_named(check):
    installed = {"mutmut": "3.7.0", "pytest": "9.2.0"}

    def version(name):
        if name not in installed:
            raise importlib.metadata.PackageNotFoundError(name)
        return installed[name]

    pinned = {"mutmut": "3.7.0", "pytest": "9.1.1", "libcst": "1.8.5"}
    assert check.mismatches(pinned, version) == [
        "libcst is not installed (pinned 1.8.5)",
        "pytest 9.2.0 is installed, 9.1.1 is pinned"]
    assert check.mismatches({"mutmut": "3.7.0"}, version) == []


def test_the_installed_pair_passes_and_another_version_is_refused(tmp_path):
    here = importlib.metadata.version("pytest")
    good = tmp_path / "good.txt"
    good.write_text("pytest==%s\n" % here)
    result = run(good)
    assert result.returncode == 0, result.stderr
    assert "pytest %s" % here in result.stdout

    bad = tmp_path / "bad.txt"
    bad.write_text("pytest==0.0.1\n")
    result = run(bad)
    assert result.returncode == 1
    assert "pytest %s is installed, 0.0.1 is pinned" % here in result.stderr
    assert "pip install -r bad.txt" in result.stderr

    empty = tmp_path / "empty.txt"
    empty.write_text("pytest>=7.0\n")
    assert run(empty).returncode == 2


def test_the_repository_pins_mutmut_and_pytest_together(check):
    pinned = check.pins(repo_file("requirements-mutation.txt"))
    assert {"mutmut", "pytest"} <= set(pinned)
    # mutation-shards.py checks every result file against mutmut 3.7's layout;
    # another mutmut needs that check moved and a measured run first.
    assert pinned["mutmut"].startswith("3.7."), pinned


def test_every_mutation_target_checks_the_tools_first():
    makefile = repo_file("Makefile").read_text()
    for target in ("mutation", "mutation-shard", "mutation-join"):
        recipe = re.search(r"^%s:.*\n\t(.+)" % re.escape(target), makefile, re.MULTILINE)
        assert recipe, target
        assert recipe.group(1) == "$(PYTHON) scripts/mutation-tools-check.py", target
    assert re.search(r"^mutation-merge: mutation-join$", makefile, re.MULTILINE)
