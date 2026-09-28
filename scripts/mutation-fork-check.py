#!/usr/bin/env python3
"""Refuse a mutation run whose tests fail only in a forked child.

mutmut imports the tests and runs the clean suite in its own process, then
forks one child per mutant and counts any failing test as a kill. A test
that captures process state at import time -- `os.getpid()` in a
parameter list or a class attribute -- passes the clean run and fails in
every child, so each mutant it covers is killed whatever the mutation.

0.8.0 development runs measured 0.897-0.912 that way: two device-identity
tests stored the importing PID as the fake backend's, the session then
correctly refused a control endpoint owned by another process, and none of
the 4737 mutants those tests covered survived. This runs the suite the way
mutmut does -- once here, then once in a forked child -- and fails unless
both pass.

Usage: python3 scripts/mutation-fork-check.py [pytest arguments]
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    import pytest

    args = ["-q", "-p", "no:cacheprovider", "-p", "no:randomly", *(argv or ["tests/"])]
    parent = int(pytest.main(args))
    sys.stdout.flush()
    sys.stderr.flush()
    if parent != 0:
        print("mutation-fork-check: the suite fails before forking (exit %d)" % parent,
              file=sys.stderr)
        return 1
    pid = os.fork()
    if pid == 0:
        code = int(pytest.main(args))
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    child = os.waitstatus_to_exitcode(status)
    if child != 0:
        print("mutation-fork-check: the suite passes in this process but fails in a "
              "forked child (exit %d); mutants covered by the failing tests would be "
              "counted as killed" % child, file=sys.stderr)
        return 1
    print("mutation-fork-check: the suite passes before and after fork")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
