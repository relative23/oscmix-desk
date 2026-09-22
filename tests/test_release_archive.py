"""The source artifact must be reproducible and describe the code it ships."""

import hashlib
import importlib.util
import json
import subprocess
import tarfile

import pytest
from support import repo_file


def test_archive_is_reproducible_ignores_dirty_files_and_detects_tampering(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "build_release", repo_file("scripts", "build-release.py"))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    package = root / "src" / "oscmix_desk"
    package.mkdir(parents=True)
    (package / "constants.py").write_text('__version__ = "0.7.0"\n')
    pin = "f2fdd5ec78338848754aad32cc07f3440de63395"
    (root / "install.sh").write_text('OSCMIX_REF="${OSCMIX_REF:-%s}"\n' % pin)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Archive test",
                    "-c", "user.email=archive@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "fixture"], check=True)
    first, second = tmp_path / "first", tmp_path / "second"
    manifest = builder.build(root, "HEAD", first)
    (package / "constants.py").write_text('__version__ = "99.0.0"\n')
    builder.build(root, "HEAD", second)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {
        p.name: p.read_bytes() for p in second.iterdir()}
    assert manifest["oscmix_revision"] == pin
    assert json.loads((first / "release-manifest.json").read_text()) == manifest
    archive = first / manifest["archive"]
    with tarfile.open(archive) as contents:
        assert contents.extractfile("oscmix-desk-0.7.0/src/oscmix_desk/constants.py").read() \
            == b'__version__ = "0.7.0"\n'
    verified = subprocess.run(["sha256sum", "--check", "SHA256SUMS"],
                              cwd=str(first), capture_output=True)
    assert verified.returncode == 0, verified.stderr
    archive.write_bytes(archive.read_bytes() + b"changed")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["archive_sha256"]
    assert subprocess.run(["sha256sum", "--check", "SHA256SUMS"],
                          cwd=str(first), capture_output=True).returncode != 0
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(root, "no-such-ref", tmp_path / "bad")
