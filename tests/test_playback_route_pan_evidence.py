"""The future retained fixture must exist; no skip or synthetic substitute."""

import json
from pathlib import Path

from playback_route_pan_evidence import validate


def test_retained_playback_pan_evidence_has_only_its_measured_scope():
    path = Path(__file__).parent / "data/playback-route-pan-probes-ucx2.json"
    validate(json.loads(path.read_bytes()))
