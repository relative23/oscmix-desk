"""The evidence rules of release.yml, run here as the release runs them.

The release workflow checks the records attached to a draft against the tagged sources before
anything is attested. Until 0.10.2 those rules ran only at a release: a mistake in them, or a
new rule such as the loopback route evidence, met its first test on a release day. This runs the
workflow's own step on a tree linked to this one, with a consistent set of records, and holds
each rule to refusing the record that breaks it.
"""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest
from support import repo_file

from oscmix_desk.constants import __version__

ROOT = repo_file("README.md").parent
TESTED = ("src", "tests", "bin", "scripts")
TESTED_FILES = ("pyproject.toml", "requirements-dev.txt", "requirements-mutation.txt", "Makefile")
SUPPORT = ("config", "systemd", "udev", "packaging", "patches", "service", "desktop")
SUPPORT_FILES = ("install.sh", "uninstall.sh", "quality/vulture-allowlist.py")
ROUTES = ["krk-monitors", "main", "phones"]


def step():
    """The Python block of the evidence step, exactly as the workflow runs it."""
    text = repo_file(".github", "workflows", "release.yml").read_text()
    block = text.split("- name: Require the release tag and include its measured evidence", 1)[1]
    body = block.split("python - <<'PY'\n", 1)[1].split("\n          PY\n", 1)[0]
    return "\n".join(line[10:] for line in body.splitlines()) + "\n"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(tree, directories, names):
    found = [path for directory in directories for path in (tree / directory).rglob("*")
             if path.is_file() and "__pycache__" not in path.parts
             and path.suffix not in (".pyc", ".pyo")]
    found += [tree / name for name in names]
    return {str(path.relative_to(tree)): sha(path) for path in found}


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """This tree's sources, linked file by file: the step hashes what the links point to."""
    tree = tmp_path_factory.mktemp("release")
    for directory in TESTED + SUPPORT + ("quality",):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                link = tree / path.relative_to(ROOT)
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(path)
    for name in TESTED_FILES + SUPPORT_FILES[:2]:
        (tree / name).symlink_to(ROOT / name)
    return tree


@pytest.fixture(scope="module")
def records(tree):
    """A consistent set of the records a draft carries."""
    upstream = json.loads((tree / "patches" / "backend-series.json").read_text())["upstream"]
    series = sha(tree / "patches" / "backend-series.json")
    runtime = {path.name: sha(path) for path in (tree / "src" / "oscmix_desk").glob("*.py")}
    required = {"check", "coverage", "flake", "soak-200", "unit", "mutation"}
    required.update("python-3.%d" % minor for minor in range(12, 16))
    required.update("fault-%02d" % index for index in range(1, 16))
    measured = {"oscmix_revision": upstream, "running_backend": {"series_sha256": series},
                "desk_source": {"runtime_sha256": runtime}}
    return {
        "software-qualification.json": {
            "candidate": __version__, "status": "qualified", "backend_series_sha256": series,
            "gates": [{"gate": gate, "exit": 0} for gate in sorted(required)],
            "tested_files_sha256": files(tree, TESTED, TESTED_FILES),
            "supporting_files_sha256": files(tree, SUPPORT, SUPPORT_FILES)},
        "lifecycle.json": {"ok": True, "physical_disconnect": {"no_interface_check": {"ok": True}},
                           "installed_runtime_sha256": runtime},
        "hardware-evidence.json": dict(measured, ok=True, complete=True, unmeasured=[],
                                       routes=[{"route": name} for name in ROUTES]),
        "write-sweep-ucx2.json": dict(
            measured, schema=3, error=None, complete=True, not_restored=[], probed=2,
            transactions=[{"error": None, "not_restored": [], "before_messages": 7,
                           "after_messages": 7}],
            findings=[{"verdict": "confirmed"}, {"verdict": "skipped"}]),
        "loopback-route-evidence.json": dict(
            measured, schema=2, kind="playback-routes-through-loopback", ok=True,
            complete=True, unmeasured=[], routes=[{"name": name, "ok": True} for name in ROUTES]),
        "manifest": {"version": __version__, "oscmix_revision": upstream},
    }


def run(tree, records, tag=None):
    for name in ("build/release", "build/evidence"):
        shutil.rmtree(tree / name, ignore_errors=True)
        (tree / name).mkdir(parents=True)
    (tree / "build/release/release-manifest.json").write_text(json.dumps(records["manifest"]))
    (tree / "build/evidence/release-notes.md").write_text("# notes\n")
    for name, record in records.items():
        if name != "manifest" and record is not None:
            (tree / "build/evidence" / name).write_text(json.dumps(record))
    env = dict(os.environ, RELEASE_TAG=tag or "v" + __version__)
    return subprocess.run([sys.executable, "-"], input=step(), cwd=tree, env=env, text=True,
                          capture_output=True, timeout=120, check=False)


def test_a_consistent_draft_passes_and_its_records_are_attached(tree, records):
    done = run(tree, records)
    assert done.returncode == 0, done.stderr
    sums = (tree / "build/release/SHA256SUMS").read_text()
    for name in ("loopback-route-evidence.json", "hardware-evidence.json", "release-notes.md"):
        assert "  %s\n" % name in sums


def broken(records, name, change):
    damaged = copy.deepcopy(records)
    if name is None:
        change(damaged)
    else:
        change(damaged[name])
    return damaged


@pytest.mark.parametrize(("name", "change", "message"), [
    (None, lambda r: r.update({"loopback-route-evidence.json": None}), "No such file"),
    ("loopback-route-evidence.json", lambda r: r.update(ok=False), "AssertionError"),
    ("loopback-route-evidence.json", lambda r: r.update(unmeasured=["main"]), "AssertionError"),
    ("loopback-route-evidence.json", lambda r: r["routes"].pop(), "shows other routes"),
    ("loopback-route-evidence.json", lambda r: r["routes"][0].update(ok=False),
     "AssertionError"),
    ("loopback-route-evidence.json", lambda r: r.update(schema=1), "AssertionError"),
    ("loopback-route-evidence.json",
     lambda r: r.update(desk_source={"runtime_sha256": {}}), "predates this runtime"),
    ("hardware-evidence.json",
     lambda r: r.update(running_backend={"series_sha256": "0" * 64}), "another backend series"),
    ("write-sweep-ucx2.json", lambda r: r["findings"][0].update(verdict="mismatched"),
     "AssertionError"),
    ("lifecycle.json", lambda r: r.update(installed_runtime_sha256={}), "AssertionError"),
    ("software-qualification.json", lambda r: r["tested_files_sha256"].popitem(),
     "predates these files"),
], ids=["loopback-missing", "loopback-failed", "loopback-unmeasured", "loopback-routes",
        "loopback-route-failed", "loopback-schema", "loopback-runtime", "hardware-series",
        "sweep-verdict", "lifecycle-runtime", "software-files"])
def test_a_draft_that_breaks_a_rule_is_refused(tree, records, name, change, message):
    done = run(tree, broken(records, name, change))
    assert done.returncode != 0
    assert message in done.stderr


def test_a_tag_of_another_version_is_refused(tree, records):
    done = run(tree, records, tag="v0.0.1")
    assert done.returncode != 0
    assert "AssertionError" in done.stderr
