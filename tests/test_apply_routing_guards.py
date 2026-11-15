"""What apply_routing refuses or leaves alone before it sends anything.

The mutation run of 0.10.1 found these decisions untested: the retention and gain prechecks
could be dropped, the AutoSet-off exemption widened to any flag turned off, an input link that
already matched under live AutoSet sent again, the peak-safe path handed another backend,
intent, reports or reader, and the log's count of settings left to the device lost, all without
a failing test. Each double here has the signature of what it replaces.
"""

import logging

import pytest
from backend_doubles import RecordingBackend, echo_link_flags_only

from oscmix_desk import ApplyIntent, prechecks, routing
from oscmix_desk.constants import LEVEL_MIN
from oscmix_desk.errors import WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route

MAIN = Route("main", playback=(1, 2), output=(5, 6))
QUIET = [("/output/5/volume", "f", (LEVEL_MIN,)), ("/output/6/volume", "f", (LEVEL_MIN,))]


def paths(backend):
    return [path for path, _tags, _args in backend.sent]


def test_a_retention_problem_refuses_before_anything_is_sent(recording_backend, monkeypatch):
    def retention_problem(config, selected, retained, confirmed, *, observed=None):
        return "retained link state is not confirmed for /output/5/stereo"

    monkeypatch.setattr(routing, "retention_problem", retention_problem)
    with pytest.raises(WriteFailed) as refused:
        routing.apply_routing(Config(routes=[MAIN]), recording_backend, observed={})
    assert "retained link state is not confirmed" in str(refused.value)
    assert recording_backend.sent == []


@pytest.mark.parametrize(("setting", "first"), [
    (ChannelSetting("input", 1, "mute", False), []),
    (ChannelSetting("input", 1, "autoset", False), ["/input/1/autoset"]),
], ids=["another-flag-off", "autoset-off"])
def test_a_gain_problem_refuses_up_front_unless_autoset_goes_off(recording_backend, monkeypatch,
                                                                  setting, first):
    # Turning AutoSet off is sent first and the gain question asked again after it, on what
    # the device reports then; any other write waits for the answer up front.
    def gain_problem(config, selected, latest, intent, retained, confirmed):
        return "AutoSet is active or not confirmed off for gain writes: /input/1/gain"

    monkeypatch.setattr(prechecks, "gain_problem", gain_problem)
    backend = RecordingBackend(reports=lambda sent: [
        ("/input/1/autoset", "i", (0,)), ("/input/2/autoset", "i", (0,)),
        ("/input/1/stereo", "i", (0,)), *echo_link_flags_only(sent)])
    with pytest.raises(WriteFailed) as refused:
        routing.apply_routing(Config(routes=[MAIN], channels=(setting,)), backend, observed={})
    assert "AutoSet is active" in str(refused.value)
    assert paths(backend) == first


def live_autoset_on_a_linked_pair(sent):
    """The device: input 1/2 linked under live AutoSet, and every link flag it was sent."""
    return [("/input/1/autoset", "i", (1,)), ("/input/2/autoset", "i", (0,)),
            ("/input/1/stereo", "i", (1,)), *echo_link_flags_only(sent)]


def test_live_autoset_keeps_an_input_link_that_already_matches(recording_backend):
    backend = RecordingBackend(reports=live_autoset_on_a_linked_pair)
    mic = Route("mic", input=(1, 2), output=(5, 6))
    observed = {"/input/1/autoset": ("i", (1,)), "/input/1/stereo": ("i", (1,))}
    routing.apply_routing(Config(routes=[mic]), backend, observed=observed)
    sent = paths(backend)
    assert "/input/1/stereo" not in sent, "re-linking could reset the gains AutoSet owns"
    assert "/output/5/stereo" in sent
    assert any(path.startswith("/mix/5/input/") for path in sent)


def test_a_peak_safe_apply_quiets_the_outputs_on_its_own_backend(recording_backend):
    observed = {"/output/5/volume": ("f", (-6.0,)), "/output/6/volume": ("f", (-6.0,))}
    routing.apply_routing(Config(routes=[MAIN], peak_safe=True), recording_backend,
                          intent=ApplyIntent.EXPLICIT, observed=observed)
    sent = recording_backend.sent
    assert sent[:2] == QUIET
    assert sent[-2:] == [("/output/5/volume", "f", (-6.0,)), ("/output/6/volume", "f", (-6.0,))]


def test_a_peak_safe_apply_reads_missing_volumes_through_its_backend():
    # The volumes it gives back come from a fresh read on the operation's own connection.
    backend = RecordingBackend(reports=lambda sent: [
        ("/output/5/volume", "f", (-6.0,)), ("/output/6/volume", "f", (-6.0,)),
        *echo_link_flags_only(sent)])
    asked = []

    def stop():
        asked.append(True)
        return False

    routing.apply_routing(Config(routes=[MAIN], peak_safe=True), backend,
                          intent=ApplyIntent.EXPLICIT, observed={}, should_stop=stop)
    assert backend.dumps >= 1
    assert asked
    assert backend.sent[:2] == QUIET
    assert backend.sent[-2:] == [("/output/5/volume", "f", (-6.0,)),
                                 ("/output/6/volume", "f", (-6.0,))]


def test_the_log_counts_what_is_left_to_the_device(recording_backend, caplog):
    # Outputs 7 and 10 belong to two other pairs: leaving 10 alone touches nothing 7 writes.
    config = Config(routes=[MAIN], channels=(ChannelSetting("output", 7, "volume", -12.0),
                                             ChannelSetting("output", 10, "volume", -12.0)))
    with caplog.at_level(logging.INFO):
        routing.apply_routing(config, recording_backend, observed={},
                              leave_alone=("/output/10/volume",))
    assert "(1 left to the device)" in caplog.text
    assert "/output/7/volume" in paths(recording_backend)
    assert "/output/10/volume" not in paths(recording_backend)
