"""Combined calibration, route balance and unobservable mute contracts."""

from dataclasses import replace

import pytest
from support import routing_conf

from oscmix_desk.config import load_config
from oscmix_desk.model import ChannelSetting
from oscmix_desk.reconcile import ApplyIntent, Phase, application_plan, desired


def config(tmp_path):
    routes = "\n".join(
        "[route:out%d]\nplayback = 1/2\noutput = %d/%d\nlevel = %s\npan = %d\n" % (
            out, out, out + 1, "-30" if out == 1 else "-65", 1 if out == 1 else 0)
        for out in range(1, 21, 2))
    return load_config(routing_conf(tmp_path, routes +
                                   "\n[playback:1]\nmute = false\n"
                                   "\n[playback:2]\nmute = false\n"
                                   "\n[output:1]\nvolumecal = -0.01\n"))


def test_combination_preserves_visible_mute_prerequisites_and_calibration(tmp_path):
    wanted = config(tmp_path)
    selected = application_plan(wanted, ApplyIntent.INITIAL)
    phases = [write.phase for write in selected.writes]
    assert phases == sorted(phases)
    assert phases[:2] == [Phase.PLAYBACK_MUTE_ON] * 2
    assert phases[-2:] == [Phase.PLAYBACK_MUTE_OFF] * 2
    messages = selected.messages()
    assert ("/mix/1/playback/1", "fi", (-30.0, 1)) in messages
    assert ("/output/1/volumecal", "f", (-0.01,)) in messages
    assert all(write.path.startswith("/playback/") for write in selected.writes[:2])
    assert len([entry for entry in desired(wanted) if entry.path.startswith("/mix/")]) == 10


def test_retained_unobservable_mute_blocks_balanced_matrix_repair_but_not_calibration(tmp_path):
    wanted = config(tmp_path)
    selected = application_plan(wanted, ApplyIntent.REPAIR)
    messages = selected.messages()
    assert not any("/playback/" in path for path, tags, args in messages)
    assert ("/output/1/volumecal", "f", (-0.01,)) in messages
    assert "/mix/1/playback/1" in selected.retained


def test_direct_api_boolean_calibration_is_rejected_in_combined_configuration(tmp_path):
    wanted = config(tmp_path)
    wanted = replace(wanted, channels=wanted.channels + (
        ChannelSetting("output", 3, "volumecal", True),))
    with pytest.raises((TypeError, ValueError), match=r"boolean|quantity|number"):
        application_plan(wanted, ApplyIntent.INITIAL)
