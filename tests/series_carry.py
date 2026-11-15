"""Measurements taken with one backend series, carried to the next by a source proof (ADR 0008).

0.10.2 adds patch 0020 to the series of 0.10.1 (48a73594). The user chose on 2026-11-15 to carry
that series' measurements forward instead of recording them again: 0020 changes only the DURec
file list handlers and fatal(), and on every run in which the backend of 0.10.1 did not exit it
does what that backend did (tests/backend_mixer/durec_files.c, its surviving reports also built
against the series before), so no completed measurement ran a changed line. This module holds
the choice to what it rests on, offline: the carried manifest is the one the evidence names,
the shipped manifest adds only the recorded patches to it, each of them changes nothing but the
allowed functions, and each carried record is the one recorded. Any further change to the
series breaks the chain, and the evidence has to be taken again.
"""

import hashlib
import json
import re

from support import repo_file

RECORD = ("tests", "data", "series-carry-forward-0020.json")
#: The functions the carried patches may change, by prepared source file.
ALLOWED = {"oscmix.c": {"resizedurecfiles", "newdurecfileslen", "newdurecindex"},
           "util.c": {"fatal"}}
FUNCTION = re.compile(r"([A-Za-z_]\w*)\(")
RETURN_TYPE = re.compile(r"(?:static\s+)?[A-Za-z_][\w\s*]*")
INCLUDE = re.compile(r"#include <[\w/.]+>")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def shipped_manifest():
    return repo_file("patches", "backend-series.json").read_bytes()


def changed_functions(patch_text):
    """{file: names of the functions whose lines the patch changes}.

    The prepared sources name each function at the start of a line, after a line with its
    return type, and close it with a brace there. A changed line outside every function is
    allowed only as an include or a blank line; anything else names the file's "" entry.
    """
    found, path, lines = {}, None, []

    def scan(hunk_lines, current):
        for index, (marker, text) in enumerate(hunk_lines):
            name = FUNCTION.match(text)
            if name and not text.startswith(("if", "for", "while", "switch", "return")):
                current = name.group(1)
            if marker != " ":
                owner = current
                if owner is None and RETURN_TYPE.fullmatch(text):
                    following = [t for _m, t in hunk_lines[index + 1:]
                                 if not RETURN_TYPE.fullmatch(t)]
                    nxt = FUNCTION.match(following[0]) if following else None
                    owner = nxt.group(1) if nxt else None
                if owner is None and not (INCLUDE.fullmatch(text) or not text.strip()):
                    owner = ""
                if owner is not None:
                    found.setdefault(path, set()).add(owner)
            if text == "}":
                current = None
        return current

    header = None
    for raw in patch_text.splitlines():
        if raw.startswith(("diff --git ", "--- ", "+++ ")):
            if lines:
                scan(lines, header)
                lines = []
            if raw.startswith("+++ "):
                path = raw[len("+++ b/"):]
            continue
        if raw.startswith("@@"):
            if lines:
                scan(lines, header)
                lines = []
            context = FUNCTION.match(raw.split("@@", 2)[2].strip())
            header = context.group(1) if context else None
            continue
        if raw[:1] in (" ", "+", "-"):
            lines.append((raw[0], raw[1:]))
    if lines:
        scan(lines, header)
    return found


def predecessor(record=None, shipped=None):
    """The manifest of the carried evidence, once the chain to the shipped one holds."""
    record = record if record is not None else json.loads(repo_file(*RECORD).read_text())
    shipped = shipped if shipped is not None else shipped_manifest()
    need(record.get("schema") == 1 and record.get("kind") == "backend-series-carry-forward",
         "not a carry-forward record")
    old_bytes = record["from_manifest"].encode()
    need(sha(old_bytes) == record["from_series_sha256"], "carried from another series")
    old, new = json.loads(old_bytes), json.loads(shipped)
    need(old["upstream"] == new["upstream"], "the upstream pin changed")
    count = len(old["patches"])
    need(new["patches"][:count] == old["patches"], "an earlier patch of the series changed")
    added = new["patches"][count:]
    need([entry["file"] for entry in added] == record["added_patches"],
         "the series adds more than the carried patches")
    for entry in added:
        text = repo_file("patches", entry["file"]).read_bytes()
        need(sha(text) == entry["sha256"], "patch differs from its manifest entry")
        functions = changed_functions(text.decode())
        need(functions and all(name in ALLOWED and changed <= ALLOWED[name]
                               for name, changed in functions.items()),
             "a carried patch changes more than the DURec file list and fatal()")
    return old_bytes


def manifest_for(evidence, series_sha256):
    """The manifest a record was taken with: the shipped one, or the carried predecessor."""
    shipped = shipped_manifest()
    if series_sha256 == sha(shipped):
        return shipped
    record = json.loads(repo_file(*RECORD).read_text())
    old = predecessor(record, shipped)
    need(series_sha256 == sha(old), "taken with a backend series this tree does not ship")
    name = "/".join(evidence)
    need(name in record["evidence"], "a record the carry-forward does not cover")
    need(sha(repo_file(*evidence).read_bytes()) == record["evidence"][name],
         "carried evidence differs from the recorded one")
    return old
