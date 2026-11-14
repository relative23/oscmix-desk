"""The shipped sources name no real device serial.

Until 2026-11-14 the serial of the UCX II that took every measurement stood in
docs, examples, docstrings, tests and recorded fixtures. They now use the
placeholder 12345678, and the multi-device cases a few more serials that are
plainly made up. The recording tools read the real serial from the device, so
a new fixture or document would bring it back: every number in a serial
context has to be one of the made-up ones.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "12345678"
#: Made up for the context samples below; this file is scanned as well.
SAMPLE = "13572468"
MADE_UP = {PLACEHOLDER, SAMPLE, "00000000", "11111111", "11112222", "11223344", "23456789",
           "55554444", "87654321", "99887766", "99999999"}
CONTEXTS = (
    re.compile(r"(?i)serial\W{0,12}(\d{8})(?!\d)"),
    re.compile(r"(?:UCX II|802) \((\d{8})\)"),
    re.compile(r"2a39-3f[0-9a-f]{2}-(\d{8})(?!\d)"),
    re.compile(r"\bII(\d{8})(?!\d)"),
)
SKIP = {".git", "build", "mutants", ".venv", "__pycache__", ".hypothesis", ".mypy_cache",
        ".ruff_cache"}


def shipped_files():
    """Tracked files in a checkout; otherwise every file below ROOT.

    An extracted source archive has no Git, and mutmut runs these tests from a
    copy in the ignored mutants/ directory, where `git ls-files` lists nothing.
    """
    try:
        names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                               check=True, timeout=30).stdout.decode().split("\0")
    except (OSError, subprocess.SubprocessError):
        names = []
    if any(names):
        return [ROOT / name for name in names if name]
    return [path for path in ROOT.rglob("*")
            if path.is_file() and not SKIP & set(path.relative_to(ROOT).parts)]


def serials():
    found = {}
    for path in shipped_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in CONTEXTS:
            for match in pattern.finditer(text):
                found.setdefault(match.group(1), set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_serial_in_the_sources_is_made_up():
    found = serials()
    assert PLACEHOLDER in found, "the scan found no serial context at all"
    strangers = {serial: sorted(paths)[:3] for serial, paths in found.items()
                 if serial not in MADE_UP}
    assert strangers == {}, "use the placeholder %s instead: %s" % (PLACEHOLDER, strangers)


def test_the_contexts_catch_the_forms_the_tools_write():
    forms = ("Fireface UCX II ({})", '"serial": "{}"', "serial = {}",
             "/run/oscmix-desk/2a39-3fd9-{}.lock", " 2 [II{} ]: USB-Audio")
    for form in forms:
        sample = form.format(SAMPLE)
        assert any(pattern.search(sample) for pattern in CONTEXTS), sample
