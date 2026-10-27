"""Measured M/S flags through config, readback, ownership and export."""

import pytest
from backend_doubles import RecordingBackend
from profile_desk import desk
from support import routing_conf

from oscmix_desk import (
    APPLIED_MISMATCHED,
    APPLIED_UNVERIFIED,
    APPLIED_VERIFIED,
    dump,
    reconcile,
    routing,
    switch_profile,
    verify,
)
from oscmix_desk import desk as desk_mod
from oscmix_desk.backend import OSCMIX
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError, WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.registers import BOOL, PIN, REMEMBER, VERIFIABLE, cold_plug_complete, register_at


@pytest.mark.parametrize("channel", range(1, 21))
@pytest.mark.parametrize("value", [False, True])
def test_msproc_reaches_each_named_input_as_an_int32(tmp_path, channel, value):
    config = load_config(routing_conf(
        tmp_path, f"[input:{channel}]\nmsproc = {str(value).lower()}\n"))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    path = f"/input/{channel}/msproc"
    assert backend.sent == [(path, "i", (int(value),))]
    assert type(backend.sent[0][2][0]) is int
    assert verify.expected_registers(config) == {path: ("i", (int(value),))}
    row = register_at(UCX2, path)
    assert row.domain is BOOL
    assert row.verify is VERIFIABLE
    assert row.policy is REMEMBER
    assert not cold_plug_complete(UCX2, path)


@pytest.mark.parametrize("value", ["2", "-1", "0.5", "maybe", "nan"])
def test_nonboolean_msproc_file_is_refused(tmp_path, value):
    with pytest.raises(ConfigError, match="boolean"):
        load_config(routing_conf(tmp_path, f"[input:13]\nmsproc = {value}\n"))


@pytest.mark.parametrize("value", [-1, 2, .5, float("nan"), float("inf"), "true"])
@pytest.mark.parametrize("option", ["msproc", "mute"])
def test_invalid_programmatic_boolean_refuses_every_write(value, option):
    config = Config(channels=(ChannelSetting("input", 13, "fx", -30.),
                              ChannelSetting("input", 13, option, value)))
    backend = RecordingBackend()
    with pytest.raises((ValueError, TypeError), match=r"boolean|integral|finite|numeric"):
        routing.apply_routing(config, backend)
    assert backend.sent == []
    assert backend.dumps == 0


@pytest.mark.parametrize("channel", [-1, 0, 21])
def test_msproc_outside_the_device_refuses_file_and_programmatic_plan(tmp_path, channel):
    with pytest.raises(ConfigError, match="does not have"):
        load_config(routing_conf(tmp_path, f"[input:{channel}]\nmsproc = true\n"))
    config = Config(channels=(ChannelSetting("input", 13, "fx", -30.),
                              ChannelSetting("input", channel, "msproc", True)))
    backend = RecordingBackend()
    with pytest.raises(ValueError, match="does not have"):
        routing.apply_routing(config, backend)
    assert backend.sent == []
    assert backend.dumps == 0


def test_msproc_is_not_an_output_option(tmp_path):
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(routing_conf(tmp_path, "[output:1]\nmsproc = true\n"))


@pytest.mark.parametrize(("wanted", "reported", "confirmed"), [
    (False, 0, True), (True, 1, True), (True, 0, False), (False, 1, False),
    (True, 2, False), (False, -1, False), (True, .5, False), (False, float("nan"), False),
])
def test_msproc_readback_compares_boolean_values(wanted, reported, confirmed):
    path = "/input/13/msproc"
    config = Config(channels=(ChannelSetting("input", 13, "msproc", wanted),))
    plan = reconcile.plan(reconcile.desired(config), {path: (reported,)}, UCX2)
    assert plan.confirmed == ((path,) if confirmed else ())
    assert [write.path for write in plan.writes] == ([] if confirmed else [path])
    assert reconcile.matches("i", (int(wanted),), (reported,),
                             register=register_at(UCX2, path)) is confirmed


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("intent", list(reconcile.ApplyIntent))
def test_msproc_obeys_explicit_and_remembered_ownership(tmp_path, value, intent):
    config = load_config(routing_conf(
        tmp_path, f"[input:13]\nmsproc = {str(value).lower()}\n"))
    expected = () if intent.preserves_remember else (("/input/13/msproc", "i", (int(value),)),)
    assert reconcile.application_plan(config, intent).messages() == expected


@pytest.mark.parametrize("intent", [reconcile.ApplyIntent.REPAIR, reconcile.ApplyIntent.RECONCILE])
def test_msproc_can_be_pinned_for_later_repair(tmp_path, intent):
    config = load_config(routing_conf(
        tmp_path, "[input:13]\nmsproc = true\n[pin]\ninput.msproc = pin\n"))
    assert reconcile.application_plan(config, intent).messages() == (
        ("/input/13/msproc", "i", (1,)),)


@pytest.mark.parametrize("direct", [13, 14])
@pytest.mark.parametrize("unlinked", [False, True])
def test_msproc_repair_protects_retained_partner_until_unlink_is_confirmed(direct, unlinked):
    partner = 27 - direct
    route = Route(name="monitor", input=(direct,) if unlinked else (13, 14), output=(1, 2))
    config = Config(routes=(route,), policies={("input", "msproc"): PIN}, channels=(
        ChannelSetting("input", direct, "msproc", True),
        ChannelSetting("input", partner, "msproc", True)))
    backend = RecordingBackend()
    options = dict(intent=reconcile.ApplyIntent.RECONCILE,
                   leave_alone=[f"/input/{partner}/msproc"],
                   confirmed=["/input/13/stereo", "/output/1/stereo"])
    if unlinked:
        routing.apply_routing(config, backend, **options)
        assert (f"/input/{direct}/msproc", "i", (1,)) in backend.sent
    else:
        with pytest.raises(WriteFailed, match="retained stereo partner"):
            routing.apply_routing(config, backend, **options)
        assert backend.sent == []


def test_msproc_dump_preserves_both_flags_without_owning_them(tmp_path):
    warnings = []
    settings = dump.channels_from_observed(
        {"/input/13/msproc": (1,), "/input/14/msproc": (0,)}, UCX2, warnings)
    assert warnings == []
    text = dump.render_config(Config(channels=settings), UCX2)
    assert "# msproc = true" in text
    assert "# msproc = false" in text
    recovered = load_config(routing_conf(tmp_path, text))
    assert verify.expected_registers(recovered) == {}
    selected = "\n".join(line[2:] if line.startswith("# msproc = ") else line
                         for line in text.splitlines()) + "\n"
    recovered = load_config(routing_conf(tmp_path, selected))
    assert verify.expected_registers(recovered) == {
        "/input/13/msproc": ("i", (1,)), "/input/14/msproc": ("i", (0,))}
    assert reconcile.application_plan(recovered, reconcile.ApplyIntent.RECONCILE).writes == ()


def test_omitting_msproc_keeps_it_out_of_the_plan(tmp_path):
    config = load_config(routing_conf(tmp_path, "[input:13]\nmute = false\n"))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    assert backend.sent == [("/input/13/mute", "i", (0,))]


@pytest.mark.parametrize("feedback", ["correct", "different", "missing", "invalid"])
def test_msproc_profile_preserves_known_mismatch_and_missing_feedback(
        tmp_path, monkeypatch, feedback):
    text = "[input:13]\nmsproc = true\n"
    path = desk(tmp_path, main=text, tracking=text)
    reports = [] if feedback == "missing" else [
        ("/input/13/msproc", "i", ({"correct": 1, "different": 0, "invalid": 2}[feedback],))]
    backend = RecordingBackend(lambda _sent: reports)
    backend.traits = OSCMIX
    monkeypatch.setattr(desk_mod, "VERIFY_TIMEOUT", .003)
    outcome = switch_profile("tracking", path, backend=backend)
    expected = (APPLIED_VERIFIED if feedback == "correct" else
                APPLIED_UNVERIFIED if feedback == "missing" else APPLIED_MISMATCHED)
    assert outcome.state is expected
    differing = feedback in ("different", "invalid")
    assert outcome.mismatched == (("/input/13/msproc",) if differing else ())
    assert outcome.unobserved == (("/input/13/msproc",) if feedback == "missing" else ())
    assert outcome.invalid == (("/input/13/msproc",) if feedback == "invalid" else ())
    assert outcome.applied
    assert outcome.read_back
    assert outcome.persisted
    assert backend.operations == ["begin", "finish", "close"]
