#!/usr/bin/env python3
"""Build the source distribution from a Git commit, never from a dirty tree.

The archive includes the installer and system integration, as well as
the tests and documentation. Only the standard library and Git are needed.
This makes no claim about reproducibility of the separately built backend.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Dict


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, check=True).stdout


def build(root: Path, ref: str, output: Path) -> Dict[str, str]:
    commit = git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    constants = git(root, "show", commit + ":src/oscmix_desk/constants.py").decode()
    version = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', constants, re.MULTILINE)
    installer = git(root, "show", commit + ":install.sh").decode()
    upstream = re.search(r'^OSCMIX_REF="\$\{OSCMIX_REF:-([a-f0-9]{40})\}"$',
                         installer, re.MULTILINE)
    if version is None or upstream is None:
        raise ValueError("the commit must name a release version and a full upstream SHA")
    name = "oscmix-desk-%s" % version[1]
    archive = git(root, "archive", "--format=tar", "--prefix=" + name + "/", commit)
    output.mkdir(parents=True, exist_ok=True)
    filename = name + ".tar.gz"
    with (output / filename).open("wb") as destination, \
            gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as zipped:
        zipped.write(archive)
    manifest = {"version": version[1], "desk_commit": commit,
                "oscmix_revision": upstream[1], "archive": filename,
                "archive_sha256": hashlib.sha256((output / filename).read_bytes()).hexdigest()}
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksums = "".join(
        "%s  %s\n" % (hashlib.sha256((output / path).read_bytes()).hexdigest(), path)
        for path in (filename, "release-manifest.json"))
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="commit or release tag")
    parser.add_argument("--output", type=Path, default=Path("build/release"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        manifest = build(root, args.ref, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, "cannot build release: %s\n" % exc)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
