#!/usr/bin/env python3
"""Refuse a mutation run with tools other than the pinned ones.

The mutation tools are pinned together in ``requirements-mutation.txt``.
mutation-shards.py joins mutmut 3.7's result files and refuses any other
layout, and mutmut 3.7.0 reads pytest's private ``Item._nodeid``, which
pytest 9.2.0 removed. The nightly CI pinned mutmut alone: from November 8
to 12, 2026 pip gave it pytest 9.2.0, and every shard stopped after its
first test with "failed to collect stats", nine minutes into a run planned
for an hour. This compares the installed versions with the pins before
anything runs, so a wrong pair costs a second, not a night.

Usage: python3 scripts/mutation-tools-check.py [requirements file]
"""

from __future__ import annotations

import importlib.metadata
import re
import sys
from collections.abc import Callable
from pathlib import Path

PINS = Path(__file__).resolve().parent.parent / "requirements-mutation.txt"
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#]+)\s*(?:#.*)?")


def pins(path: Path) -> dict[str, str]:
    """Package -> version, for every ``name==version`` line of ``path``."""
    found = {}
    for line in path.read_text().splitlines():
        match = PIN.fullmatch(line.strip())
        if match:
            found[match.group(1).lower()] = match.group(2)
    if not found:
        raise ValueError("no pinned tools in %s" % path)
    return found


def mismatches(pinned: dict[str, str],
               installed: Callable[[str], str] = importlib.metadata.version) -> list[str]:
    """One sentence per pinned package that is missing or another version."""
    problems = []
    for name, version in sorted(pinned.items()):
        try:
            have = installed(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append("%s is not installed (pinned %s)" % (name, version))
            continue
        if have != version:
            problems.append("%s %s is installed, %s is pinned" % (name, have, version))
    return problems


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else PINS
    try:
        pinned = pins(path)
    except (OSError, ValueError) as exc:
        print("mutation-tools-check: %s" % exc, file=sys.stderr)
        return 2
    problems = mismatches(pinned)
    if problems:
        print("mutation-tools-check: %s; install the pinned tools with `pip install -r %s`"
              % ("; ".join(problems), path.name), file=sys.stderr)
        return 1
    print("mutation-tools-check: %s"
          % ", ".join("%s %s" % item for item in sorted(pinned.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
