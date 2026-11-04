"""Source files stay short enough to read: at most 500 lines, and the longer ones never grow.

A hard limit for every source file written from 2026-11-04 on. The files that were already
longer then are listed with their length at that date: each may shrink, never grow, and leaves
the list once it fits. Splitting them is follow-up work, not a side effect of a change.
"""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIMIT = 500
#: The same files ruff checks.
PATTERNS = ("src/oscmix_desk/*.py", "tests/*.py", "scripts/*.py", "packaging/repository/*.py")
SINGLES = ("bin/oscmix-session", "bin/oscmix-launch", "service/run-session",
           "packaging/oscmix-setup", "packaging/package-guard")
#: Longer than LIMIT on 2026-11-04, with the length each may not exceed.
LONGER = {
    "scripts/probe-fx.py": 591,
    "scripts/sweep-writes.py": 898,
    "scripts/verify-hardware.py": 568,
    "src/oscmix_desk/config.py": 543,
    "src/oscmix_desk/devices.py": 626,
    "src/oscmix_desk/dump.py": 626,
    "src/oscmix_desk/profiles.py": 579,
    "src/oscmix_desk/reconcile.py": 720,
    "src/oscmix_desk/registers.py": 539,
    "src/oscmix_desk/session.py": 585,
    "tests/backend_control.py": 726,
    "tests/conftest.py": 699,
    "tests/playback_mute_evidence.py": 824,
    "tests/playback_mute_pcm_evidence.py": 685,
    "tests/playback_route_pan_evidence.py": 786,
    "tests/test_autoset_ownership.py": 536,
    "tests/test_control_client.py": 850,
    "tests/test_device_identity.py": 570,
    "tests/test_fx_probe.py": 812,
    "tests/test_hostservice.py": 592,
    "tests/test_input_route_pan_evidence.py": 970,
    "tests/test_install_sh.py": 704,
    "tests/test_launcher.py": 554,
    "tests/test_playback_mute.py": 532,
    "tests/test_process.py": 608,
    "tests/test_profiles.py": 735,
    "tests/test_registers.py": 515,
    "tests/test_session_integration.py": 578,
    "tests/test_write_sweep.py": 1139,
}

# A mutation run rewrites the sources; their length there measures mutmut.
pytestmark = pytest.mark.skipif(bool(os.environ.get("MUTANT_UNDER_TEST")),
                                reason="file lengths describe the source tree")


def sources():
    found = {path for pattern in PATTERNS for path in ROOT.glob(pattern)}
    found.update(ROOT / name for name in SINGLES)
    return sorted(found)


def length(path):
    with path.open(encoding="utf-8") as stream:
        return sum(1 for _line in stream)


def test_every_source_file_fits_or_does_not_grow():
    over = []
    for path in sources():
        name = path.relative_to(ROOT).as_posix()
        allowed = LONGER.get(name, LIMIT)
        if length(path) > allowed:
            over.append("%s (%d lines, at most %d)" % (name, length(path), allowed))
    assert over == [], "split these instead of growing them: %s" % "; ".join(over)


def test_the_list_names_only_files_that_still_need_it():
    """A file that shrank to the limit, or went away, leaves the list."""
    stale = sorted(name for name in LONGER
                   if not (ROOT / name).is_file() or length(ROOT / name) <= LIMIT)
    assert stale == [], "no longer over %d lines, take them out of LONGER: %s" % (LIMIT, stale)
