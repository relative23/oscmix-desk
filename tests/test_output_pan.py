"""Measured integer output balance through config, ownership and export."""

import pytest
from backend_doubles import RecordingBackend
from support import routing_conf

from oscmix_desk import dump, reconcile, routing, verify
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError, WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.registers import PIN, cold_plug_complete, register_at


@pytest.mark.parametrize("channel", [1, 20])
@pytest.mark.parametrize("value", [-100, -99, 0, 1, 100])
def test_output_pan_reaches_the_named_channel_as_integer(tmp_path, channel, value):
    config = load_config(routing_conf(tmp_path, f"[output:{channel}]\npan = {value}\n"))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    path = f"/output/{channel}/pan"
    assert backend.sent == [(path, "i", (value,))]
    assert type(backend.sent[0][2][0]) is int
    assert verify.expected_registers(config) == {path: ("i", (value,))}
    assert not cold_plug_complete(UCX2, path)


@pytest.mark.parametrize("value", [-101, 101, 1.5, float("nan"), float("inf"), -float("inf")])
def test_invalid_output_pan_refuses_file_and_programmatic_writes(tmp_path, value):
    with pytest.raises(ConfigError, match="pan"):
        load_config(routing_conf(tmp_path, f"[output:1]\npan = {value}\n"))
    backend = RecordingBackend()
    config = Config(channels=(ChannelSetting("output", 1, "pan", value),))
    with pytest.raises(ValueError, match=r"out of range|integral|finite"):
        routing.apply_routing(config, backend)
    assert backend.sent == []
    assert backend.dumps == 0


@pytest.mark.parametrize("channel", [0, 21])
def test_output_pan_refuses_channels_outside_the_device(tmp_path, channel):
    with pytest.raises(ConfigError):
        load_config(routing_conf(tmp_path, f"[output:{channel}]\npan = 0\n"))


def test_pan_is_not_an_input_option(tmp_path):
    with pytest.raises(ConfigError, match="pan"):
        load_config(routing_conf(tmp_path, "[input:1]\npan = 0\n"))


@pytest.mark.parametrize(("wanted", "reported", "confirmed"), [
    (-100, -100, True), (0, 0, True), (100, 100, True),
    (0, 1, False), (0, .5, False), (100, 101, False), (0, float("inf"), False),
])
def test_output_pan_readback_requires_the_declared_integer(wanted, reported, confirmed):
    path = "/output/1/pan"
    config = Config(channels=(ChannelSetting("output", 1, "pan", wanted),))
    plan = reconcile.plan(reconcile.desired(config), {path: (reported,)}, UCX2)
    assert plan.confirmed == ((path,) if confirmed else ())
    assert [write.path for write in plan.writes] == ([] if confirmed else [path])
    assert reconcile.matches(
        "i", (wanted,), (reported,), register=register_at(UCX2, path)) is confirmed


@pytest.mark.parametrize(("intent", "writes"), [
    (reconcile.ApplyIntent.INITIAL, True), (reconcile.ApplyIntent.EXPLICIT, True),
    (reconcile.ApplyIntent.REPAIR, False), (reconcile.ApplyIntent.RECONCILE, False),
])
def test_output_pan_respects_initial_explicit_and_remembered_ownership(tmp_path, intent, writes):
    config = load_config(routing_conf(tmp_path, "[output:1]\npan = -25\n"))
    assert reconcile.application_plan(config, intent).messages() == (
        (("/output/1/pan", "i", (-25,)),) if writes else ())


@pytest.mark.parametrize("intent", [reconcile.ApplyIntent.REPAIR, reconcile.ApplyIntent.RECONCILE])
def test_output_pan_can_be_pinned_for_later_repair(tmp_path, intent):
    config = load_config(routing_conf(
        tmp_path, "[output:1]\npan = -25\n[pin]\noutput.pan = pin\n"))
    assert reconcile.application_plan(config, intent).messages() == (
        ("/output/1/pan", "i", (-25,)),)


def test_output_pan_export_roundtrips_integers_without_claiming_ownership(tmp_path):
    observed = {"/output/1/pan": (-100,), "/output/20/pan": (100,)}
    warnings = []
    settings = dump.channels_from_observed(observed, UCX2, warnings)
    assert warnings == []
    assert {(item.channel, item.option, item.value) for item in settings} == {
        (1, "pan", -100), (20, "pan", 100)}
    text = dump.render_config(Config(channels=settings), UCX2)
    recovered = load_config(routing_conf(tmp_path, text))
    assert verify.expected_registers(recovered) == {}
    assert "# pan = -100" in text
    assert "# pan = 100" in text
    selected = "\n".join(line[2:] if line.startswith("# pan = ") else line
                         for line in text.splitlines()) + "\n"
    recovered = load_config(routing_conf(tmp_path, selected))
    assert verify.expected_registers(recovered) == {
        "/output/1/pan": ("i", (-100,)), "/output/20/pan": ("i", (100,))}
    assert reconcile.application_plan(recovered, reconcile.ApplyIntent.RECONCILE).writes == ()


def test_omitting_output_pan_keeps_it_out_of_the_write_plan(tmp_path):
    config = load_config(routing_conf(tmp_path, "[output:1]\nmute = false\n"))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    assert backend.sent == [("/output/1/mute", "i", (0,))]


@pytest.mark.parametrize("unlinked", [False, True])
def test_pan_repair_protects_a_retained_partner_until_unlink_is_confirmed(unlinked):
    route = (Route(name="mono", playback=(1,), output=(5,)) if unlinked else
             Route(name="stereo", playback=(1, 2), output=(5, 6)))
    config = Config(routes=[route], policies={("output", "pan"): PIN}, channels=[
        ChannelSetting("output", 5, "pan", -25), ChannelSetting("output", 6, "pan", -25)])
    backend = RecordingBackend()
    options = dict(intent=reconcile.ApplyIntent.RECONCILE, leave_alone=["/output/6/pan"],
                   confirmed=["/playback/1/stereo", "/output/5/stereo"])
    if unlinked:
        routing.apply_routing(config, backend, **options)
        assert ("/output/5/pan", "i", (-25,)) in backend.sent
    else:
        with pytest.raises(WriteFailed, match="retained stereo partner"):
            routing.apply_routing(config, backend, **options)
        assert backend.sent == []
