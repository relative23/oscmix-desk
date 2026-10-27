"""Measured FX send/return contracts through config, writes and readback."""

import pytest
from backend_doubles import RecordingBackend
from support import routing_conf

from oscmix_desk import dump, reconcile, routing, verify
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.model import ChannelSetting, Config
from oscmix_desk.registers import cold_plug_complete, register_at


@pytest.mark.parametrize("family", ["input", "output"])
@pytest.mark.parametrize("channel", [1, 20])
@pytest.mark.parametrize("level", [-65.0, -64.9, -30.0, 0.0])
def test_fx_config_reaches_the_named_channel(tmp_path, family, channel, level):
    config = load_config(routing_conf(
        tmp_path, "[%s:%d]\nfx = %s\n" % (family, channel, level)))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    path = "/%s/%d/fx" % (family, channel)
    assert backend.sent == [(path, "f", (level,))]
    assert verify.expected_registers(config) == {path: ("f", (level,))}
    assert not cold_plug_complete(UCX2, path)


@pytest.mark.parametrize("family", ["input", "output"])
@pytest.mark.parametrize("value", [-65.1, 0.1, float("nan"), float("inf"), -float("inf")])
def test_invalid_fx_refuses_file_and_programmatic_writes(tmp_path, family, value):
    with pytest.raises(ConfigError, match="fx"):
        load_config(routing_conf(tmp_path, "[%s:1]\nfx = %s\n" % (family, value)))
    backend = RecordingBackend()
    config = Config(channels=(ChannelSetting(family, 1, "fx", value),))
    with pytest.raises(ValueError, match=r"out of range|finite"):
        routing.apply_routing(config, backend)
    assert backend.sent == []
    assert backend.dumps == 0


@pytest.mark.parametrize("family", ["input", "output"])
@pytest.mark.parametrize("channel", [0, 21])
def test_fx_outside_the_device_channel_range_is_refused(tmp_path, family, channel):
    with pytest.raises(ConfigError):
        load_config(routing_conf(tmp_path, "[%s:%d]\nfx = -30\n" % (family, channel)))


@pytest.mark.parametrize("family", ["input", "output"])
@pytest.mark.parametrize(("wanted", "reported", "confirmed"), [
    (-65.0, -65.0, True),
    (-64.9, -64.9000015258789, True),
    (-30.0, -30.0, True),
    (0.0, 0.0, True),
    (-64.9, -64.80000305175781, False),
    (-65.0, -float("inf"), False),
])
def test_fx_readback_compares_the_measured_scalar_step(family, wanted, reported, confirmed):
    path = "/%s/1/fx" % family
    config = Config(channels=(ChannelSetting(family, 1, "fx", wanted),))
    plan = reconcile.plan(reconcile.desired(config), {path: (reported,)}, UCX2)
    assert plan.confirmed == ((path,) if confirmed else ())
    assert [write.path for write in plan.writes] == ([] if confirmed else [path])
    register = register_at(UCX2, path)
    assert reconcile.matches("f", (wanted,), (reported,), register=register) is confirmed


@pytest.mark.parametrize("family", ["input", "output"])
@pytest.mark.parametrize("intent", list(reconcile.ApplyIntent))
def test_fx_follows_initial_explicit_and_remembered_ownership(tmp_path, family, intent):
    config = load_config(routing_conf(tmp_path, "[%s:1]\nfx = -30\n" % family))
    plan = reconcile.application_plan(config, intent)
    expected = () if intent.preserves_remember else (("/%s/1/fx" % family, "f", (-30.0,)),)
    assert plan.messages() == expected


@pytest.mark.parametrize("family", ["input", "output"])
@pytest.mark.parametrize("intent", [reconcile.ApplyIntent.REPAIR, reconcile.ApplyIntent.RECONCILE])
def test_fx_can_be_pinned_for_later_repair(tmp_path, family, intent):
    config = load_config(routing_conf(
        tmp_path, "[%s:1]\nfx = -30\n[pin]\n%s.fx = pin\n" % (family, family)))
    assert reconcile.application_plan(config, intent).messages() == (
        ("/%s/1/fx" % family, "f", (-30.0,)),)


def test_fx_dump_comments_live_values_without_owning_them(tmp_path):
    observed = {"/input/13/fx": (-64.9000015258789,), "/output/1/fx": (-30.0,)}
    warnings = []
    settings = dump.channels_from_observed(observed, UCX2, warnings)
    assert warnings == []
    assert {(item.family, item.channel, item.option, item.value) for item in settings} == {
        ("input", 13, "fx", -64.9000015258789), ("output", 1, "fx", -30.0)}
    text = dump.render_config(Config(channels=settings), UCX2)
    recovered = load_config(routing_conf(tmp_path, text))
    expected = {
        "/input/13/fx": ("f", (-64.9,)), "/output/1/fx": ("f", (-30.0,))}
    assert verify.expected_registers(recovered) == {}
    assert "# fx = -64.9" in text
    assert "# fx = -30.0" in text
    # Choosing the exported starting values keeps REMEMBER ownership.
    selected = "\n".join(line[2:] if line.startswith("# fx = ") else line
                         for line in text.splitlines()) + "\n"
    recovered = load_config(routing_conf(tmp_path, selected))
    assert verify.expected_registers(recovered) == expected
    assert reconcile.application_plan(recovered, reconcile.ApplyIntent.RECONCILE).writes == ()


@pytest.mark.parametrize("family", ["input", "output"])
def test_omitting_fx_keeps_it_out_of_the_write_plan(tmp_path, family):
    config = load_config(routing_conf(tmp_path, "[%s:1]\nmute = false\n" % family))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    assert backend.sent == [("/%s/1/mute" % family, "i", (0,))]
