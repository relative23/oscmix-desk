"""A mutation run must not count failures that only a forked child sees."""

import os
import subprocess
import sys

from support import repo_file

SUITES = {
    "import-time": ("import os\nPID = os.getpid()\n\n"
                    "def test_owner():\n    assert os.getpid() == PID\n"),
    "run-time": ("import os\n\n"
                 "def test_owner():\n    assert os.getpid() > 1\n"),
    "failing": "def test_owner():\n    assert False\n",
}


def check(tmp_path, suite):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_suite.py").write_text(SUITES[suite])
    # The gates run this suite with PYTEST_ADDOPTS=--basetemp=...; the inner
    # suite must not inherit the outer session's temporary directory.
    env = {name: value for name, value in os.environ.items() if name != "PYTEST_ADDOPTS"}
    return subprocess.run(
        [sys.executable, str(repo_file("scripts", "mutation-fork-check.py")),
         str(tmp_path / "test_suite.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)


def test_a_test_bound_to_the_importing_process_is_refused(tmp_path):
    result = check(tmp_path, "import-time")
    assert result.returncode == 1
    assert "fails in a forked child" in result.stderr


def test_a_suite_that_resolves_process_state_when_it_runs_passes(tmp_path):
    result = check(tmp_path, "run-time")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passes before and after fork" in result.stdout


def test_a_suite_failing_before_the_fork_is_not_reported_as_fork_specific(tmp_path):
    result = check(tmp_path, "failing")
    assert result.returncode == 1
    assert "fails before forking" in result.stderr
    assert "forked child" not in result.stderr
