"""Upgrade and return to the actual preceding release, with isolated paths."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_install_sh import PROJECT_ROOT, make_fake_home, run

from oscmix_desk import __version__

BASES = [
    ("0.6.11", "ddca339e808361f7114d3c2099a557a3859140c1"),
    ("0.7.0", "25eb57dff7a305b0e6452010ce15992c90ad144b"),
]
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("MUTANT_UNDER_TEST")),
    reason="installed subprocesses do not load mutants",
)


@pytest.mark.parametrize("rootless", [True, False])
@pytest.mark.parametrize(("previous_version", "base"), BASES)
def test_upgrade_and_rollback_preserve_the_desk(tmp_path, rootless, previous_version, base):
    home, env, _log = make_fake_home(tmp_path)
    if not rootless:
        # Only the three paths redirected by make_fake_home are written;
        # udevadm and systemd-tmpfiles remain inert stubs on PATH.
        (tmp_path / "stub-bin" / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    previous = tmp_path / "previous"
    previous.mkdir()
    archived = subprocess.run(
        ["git", "archive", base], cwd=str(PROJECT_ROOT),
        capture_output=True, check=False)
    if archived.returncode:
        if os.environ.get("OSCMIX_REQUIRE_CONTRACTS"):
            pytest.fail("upgrade test needs " + base + " (fetch-depth: 0 in CI)")
        pytest.skip("upgrade test needs " + base + " (fetch-depth: 0 in CI)")
    subprocess.run(["tar", "-xf", "-", "-C", str(previous)],
                   input=archived.stdout, check=True)
    args = ["--no-build"] + (["--no-udev"] if rootless else [])

    def install(tree):
        result = run(str(tree / "install.sh"), args, env)
        assert result.returncode == 0, result.stderr + result.stdout
        package = home / ".local" / "lib" / "oscmix-desk" / "oscmix_desk"
        assert {p.name: p.read_bytes() for p in package.glob("*.py")} == {
            p.name: p.read_bytes() for p in (tree / "src" / "oscmix_desk").glob("*.py")}
        version = subprocess.run(
            [sys.executable, str(home / ".local" / "bin" / "oscmix-session"),
             "--version"], env=env, capture_output=True, text=True, check=True)
        assert version.stdout.strip() == (previous_version if tree == previous else __version__)
        if not rootless:
            for variable, source in (
                    ("OSCMIX_UDEV_RULE", "udev/90-rme-fireface.rules"),
                    ("OSCMIX_SLEEP_HOOK", "systemd/system-sleep/oscmix"),
                    ("OSCMIX_TMPFILES_CONF", "systemd/tmpfiles.d/oscmix-desk.conf")):
                assert Path(env[variable]).read_bytes() == (tree / source).read_bytes()
        return package

    install(previous)
    config = home / ".config" / "oscmix"
    files = {config / "routing.conf": "# custom desk\n",
             config / "profiles" / "tracking.conf": "# custom profile\n",
             config / "active-profile": "tracking\n"}
    for path, content in files.items():
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    upgraded = install(PROJECT_ROOT)
    (upgraded / "obsolete.py").write_text("raise RuntimeError('stale module')\n")
    install(previous)
    assert all(path.read_text() == content for path, content in files.items())
