"""Hardware evidence taken at one backend series, carried to the next by a source proof.

ADR 0008 ties evidence to the exact backend source. The playback-mute evidence of 2026-10-08
(series ed304b2a, pin f2fdd5e) was not recorded again when the pin moved to 89ed835 and patches
0015/0016 joined: the user chose a carry-forward (2026-11-05) over a new recorder. This module
holds that choice to what it rests on, offline: the predecessor manifest is the one the evidence
names, the shipped manifest only adds 0015 and 0016 to it, the pin moved by the one measured
line, and every prepared source line that differs comes from one of those or the build identity.
Any further change to the series breaks the chain, and the evidence has to be taken again.
"""

import hashlib
import json

from support import repo_file

RECORD = ("tests", "data", "playback-mute-carry-forward-ucx2.json")
LOOPBACK = ("tests", "data", "loopback-output-index-ucx2.json")
#: The prepared sources the carried series may change, and nothing else.
CHANGED = ["control.c", "desk-build-id.h", "gtk/main.c", "oscmix.c"]


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def changed_lines(diff_text):
    """The removed and added lines of a unified diff, without its file headers."""
    lines = []
    for line in diff_text.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith(("-", "+")):
            lines.append(line)
    return lines


def carried_manifest(evidence, manifest_bytes, record=None, evidence_bytes=None):
    """The manifest ``evidence`` was taken with: the shipped one, or its carried predecessor."""
    backend = evidence["backend"]
    if sha(manifest_bytes) == backend["series_sha256"]:
        return manifest_bytes
    record = record if record is not None else json.loads(repo_file(*RECORD).read_text())
    if evidence_bytes is None:
        evidence_bytes = repo_file(*record["evidence"].split("/")).read_bytes()
    need(record["schema"] == 1 and record["kind"] == "playback-mute-evidence-carry-forward",
         "not a carry-forward record")
    need(sha(evidence_bytes) == record["evidence_sha256"], "carried evidence differs")
    old_bytes = record["from_manifest"].encode()
    need(sha(old_bytes) == backend["series_sha256"], "carried from another series")
    old, new = json.loads(old_bytes), json.loads(manifest_bytes)
    need(old["upstream"] == backend["upstream"], "carried from another upstream")
    target = record["to_source"]
    need(target["series_sha256"] == sha(manifest_bytes) and target["upstream"] == new["upstream"],
         "series differs from the one the evidence was carried to")
    count = len(old["patches"])
    need(new["patches"][:count] == old["patches"], "an earlier patch of the series changed")
    added = new["patches"][count:]
    need([entry["file"][:4] for entry in added] == ["0015", "0016"],
         "the series adds more than patches 0015 and 0016")
    sources = set(backend["source_sha256"]) | set(target["source_sha256"])
    changed = sorted(name for name in sources
                     if backend["source_sha256"].get(name) != target["source_sha256"].get(name))
    need(changed == record["changed_sources"] == CHANGED, "other prepared sources changed")
    loopback = json.loads(repo_file(*LOOPBACK).read_text())["backend"]
    need(loopback["previous_pin"] == old["upstream"] and loopback["upstream"] == new["upstream"]
         and loopback["change"]["files_changed"] == 1, "the pin moved by more than the fix")
    allowed = set(changed_lines(loopback["change"]["diff"]))
    for entry in added:
        text = repo_file("patches", entry["file"]).read_text()
        need(sha(text.encode()) == entry["sha256"], "patch differs from its manifest entry")
        allowed.update(changed_lines(text))
    allowed.update('%s#define OSCMIX_DESK_BUILD_ID "%s"' % (sign, series)
                   for sign, series in (("-", backend["series_sha256"]),
                                        ("+", target["series_sha256"])))
    lines = changed_lines(record["source_diff"])
    need(lines and all(line in allowed for line in lines),
         "a source line changed that no carried change explains")
    return old_bytes
