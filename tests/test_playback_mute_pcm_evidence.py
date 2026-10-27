"""A representative DSP effect requires retained, independently accepted hardware evidence."""

import json
from pathlib import Path

from playback_mute_pcm_evidence import need, validate


def test_warm_representative_playback_mute_DSP_meter_effect_is_independently_accepted():
    path = Path(__file__).resolve().parent / "data/playback-mute-pcm-effect-ucx2.json"
    value = json.loads(path.read_text())
    need(
        value.get("accepted") is True,
        "Representative PCM DSP-meter hardware acceptance is still missing",
    )
    validate(value)
