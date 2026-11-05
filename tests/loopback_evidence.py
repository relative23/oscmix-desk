"""Validate the output-loopback probe (scripts/probe-loopback.py) without live I/O.

The device never reports `/output/N/loopback`, so the only evidence that a write reached output
N is acoustic: with the flag on, capture channel N carried the probe's source noise, and with it
off it did not. The fixture is tied to the backend series it was taken with, because only the pin
89ed835 sends the output's own index (the one before sent 0xffffffff for every output).
"""

import hashlib
import json

from support import repo_file

FIXTURE = ("tests", "data", "output-loopback-probes-ucx2.json")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def validate(fixture, manifest_bytes):
    """Every output carried with the flag on and not with it off; everything given back."""
    need(fixture["schema"] == 1 and fixture["kind"] == "output-loopback-probes-ucx2",
         "wrong fixture identity")
    need(fixture["hardware_used"] is True and fixture["passed"] is True
         and "error" not in fixture and "restore_error" not in fixture, "probe did not pass")
    backend = fixture["running_backend"]
    manifest = json.loads(manifest_bytes)
    need(backend["series_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
         and backend["upstream"] == manifest["upstream"] and backend["matches_local_build"],
         "taken with a backend series this tree does not ship")
    need(fixture["restored"] is True and fixture["final_dump"]["paths"]
         == fixture["first_dump"]["paths"], "device not given back")
    need(all(not fixture["final_dump"][key] for key in ("missing", "changed", "new")),
         "the final dump differs from the first")
    outputs = [case["output"] for case in fixture["cases"]]
    need(outputs == list(range(1, 21)), "not every output measured once, in order")
    for case in fixture["cases"]:
        need(case["passed"] and case["on"]["verdict"] == "carried"
             and case["off"]["verdict"] == "not-carried"
             and case["before"]["verdict"] in ("carried", "not-carried")
             and case["given_back"] == int(case["before"]["verdict"] == "carried"),
             "output %d: loopback not shown both ways" % case["output"])
        need(case["source"] != case["output"], "a source cannot be its own reference")
    delay = fixture["delay"]
    need(delay["carried"] is True and len(delay["points"]) >= 4, "delay not measured")
    return {"/output/%d/loopback" % n for n in outputs}


def loopback_paths():
    """The loopback paths the committed fixture measured, after validating it."""
    fixture = json.loads(repo_file(*FIXTURE).read_text())
    return validate(fixture, repo_file("patches", "backend-series.json").read_bytes())
