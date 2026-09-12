#!/usr/bin/env python3
"""Compare a mutation run against the recorded baseline.

Fails when the *ratio* killed / (killed + survived) falls below
min_score - tolerance. Absolute counts are deliberately not gated:
every line added brings its own mutants, so a healthy change with good
tests would trip a count-based rule. quality/mutation-baseline.json
carries that reasoning and the measured history.

The wording here said "fails when survivors grow or kills shrink" until
0.6.6, which is a stricter promise than the code keeps.

Usage: mutmut run && python3 scripts/mutation-policy.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASELINE = Path(__file__).resolve().parent.parent / "quality" / "mutation-baseline.json"


STATS = Path("mutants") / "mutmut-cicd-stats.json"


def current_counts() -> dict:
    """Read the outcome of the last `mutmut run`.

    `mutmut results` only lists the mutants that were *not* killed, so it
    cannot answer whether kills went down. The stats export carries every
    bucket.
    """
    # Through this interpreter rather than a bare `mutmut`: the run
    # itself is `$(PYTHON) -m mutmut`, so `make mutation
    # PYTHON=.venv/bin/python` mutates 4885 mutants over an hour and
    # then dies here with FileNotFoundError, because the venv's bin is
    # not on PATH. CI hid it by activating the venv.
    subprocess.run([sys.executable, "-m", "mutmut", "export-cicd-stats"],
                   capture_output=True, text=True, check=False)
    if not STATS.is_file():
        return {}
    raw = json.loads(STATS.read_text())
    return {
        "killed": raw.get("killed", 0),
        "survived": raw.get("survived", 0),
        "not_covered": raw.get("no_tests", 0),
        "timeout": raw.get("timeout", 0),
    }


def main() -> int:
    baseline = json.loads(BASELINE.read_text())
    counts = current_counts()
    if not any(counts.values()):
        print("mutation-policy: no results found -- run `mutmut run` first",
              file=sys.stderr)
        return 2

    judged = counts["killed"] + counts["survived"]
    if judged == 0:
        print("mutation-policy: no mutant was judged", file=sys.stderr)
        return 2
    score = counts["killed"] / judged
    floor = baseline["min_score"] - baseline["tolerance"]

    for name in ("killed", "survived", "not_covered"):
        print("%-12s %5d" % (name, counts[name]))
    print("%-12s %5.3f  (floor %.3f)" % ("score", score, floor))

    if score < floor:
        print("\nmutation policy failed: score %.3f is below the floor %.3f "
              "-- assertions are catching less than they were"
              % (score, floor), file=sys.stderr)
        return 1
    if score > baseline["min_score"] + baseline["tolerance"]:
        print("\nscore rose to %.3f; raise min_score in "
              "quality/mutation-baseline.json to lock it in" % score)
    print("\nmutation policy satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
