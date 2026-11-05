"""Measured output calibration through the existing generic runtime paths."""

import math
from types import SimpleNamespace

import pytest
from backend_doubles import RecordingBackend
from support import routing_conf

from oscmix_desk import dump, numeric, profiles, reconcile, routing, verify
from oscmix_desk.config import load_config
from oscmix_desk.devices import FF802, UCX2
from oscmix_desk.errors import ConfigError, WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.outcome import APPLIED_MISMATCHED, APPLIED_UNVERIFIED, APPLIED_VERIFIED
from oscmix_desk.registers import (
    NUMBER,
    PIN,
    REMEMBER,
    VERIFIABLE,
    cold_plug_complete,
    register_at,
    settable_options,
)


@pytest.mark.parametrize("channel", [1, 2, 20])
@pytest.mark.parametrize(("requested", "raw", "reported"), [
    (-24.0, -2400, -24.0), (-0.1, -10, -0.09999999403953552),
    (-0.02, -2, -0.019999999552965164), (-0.01, -1, -0.009999999776482582),
    (0.0, 0, 0.0), (3.0, 300, 3.0),
])
def test_calibration_config_reaches_named_output_in_float_wire_precision(
        tmp_path, channel, requested, raw, reported):
    config = load_config(routing_conf(
        tmp_path, f"[output:{channel}]\nvolumecal = {requested}\n"))
    path = f"/output/{channel}/volumecal"
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    assert backend.sent == [(path, "f", (requested,))]
    assert type(backend.sent[0][2][0]) is float
    assert verify.expected_registers(config) == {path: ("f", (requested,))}
    row = register_at(UCX2, path)
    assert numeric.raw_value(requested, row) == raw
    assert numeric.expected_report(requested, row) == reported
    assert reconcile.matches("f", (requested,), (reported,), register=row)


@pytest.mark.parametrize("value", [-24.001, 3.001, math.nan, math.inf, -math.inf, 1e100])
def test_bad_calibration_is_refused_from_file_and_programmatic_config_before_io(tmp_path, value):
    with pytest.raises(ConfigError, match="volumecal"):
        load_config(routing_conf(tmp_path, f"[output:1]\nvolumecal = {value}\n"))
    backend = RecordingBackend()
    config = Config(channels=(ChannelSetting("output", 1, "volumecal", value),))
    with pytest.raises(ValueError, match=r"finite|out of range|outside"):
        routing.apply_routing(config, backend)
    assert backend.sent == []
    assert backend.dumps == 0


@pytest.mark.parametrize("value", [True, False])
def test_calibration_bool_is_not_a_quantity_before_io(value):
    backend = RecordingBackend()
    config = Config(channels=(ChannelSetting("output", 1, "volumecal", value),))
    with pytest.raises(TypeError, match="boolean"):
        routing.apply_routing(config, backend)
    assert backend.sent == []
    assert backend.dumps == 0
    row = register_at(UCX2, "/output/1/volumecal")
    for operation in (numeric.number_value, numeric.raw_value, numeric.expected_report,
                      numeric.report_value, numeric.render_number, dump._render_value):
        with pytest.raises(TypeError, match="boolean"):
            operation(value, row)


@pytest.mark.parametrize("raw", ["true", "false", "yes", "no"])
def test_file_boolean_spelling_is_not_calibration(tmp_path, raw):
    with pytest.raises(ConfigError, match=r"volumecal.*not a number"):
        load_config(routing_conf(tmp_path, f"[output:1]\nvolumecal = {raw}\n"))


@pytest.mark.parametrize("channel", [0, 21])
def test_calibration_rejects_channels_outside_ucx2(tmp_path, channel):
    with pytest.raises(ConfigError):
        load_config(routing_conf(tmp_path, f"[output:{channel}]\nvolumecal = 0\n"))
    backend = RecordingBackend()
    config = Config(channels=(ChannelSetting("output", channel, "volumecal", 0.0),))
    with pytest.raises(ValueError, match="does not have volumecal"):
        routing.apply_routing(config, backend)
    assert backend.sent == []
    assert backend.dumps == 0


def test_calibration_is_output_only_and_unqualified_devices_have_no_row(tmp_path):
    with pytest.raises(ConfigError, match="volumecal"):
        load_config(routing_conf(tmp_path, "[input:1]\nvolumecal = 0\n"))
    with pytest.raises(ConfigError, match="no register model"):
        load_config(routing_conf(tmp_path,
                                "[device]\nname = Fireface 802\n"
                                "[output:1]\nvolumecal = 0\n"), strict=True)
    assert register_at(UCX2, "/input/1/volumecal") is None
    assert register_at(FF802, "/output/1/volumecal") is None
    # Existing generic policy for an unmodelled device emits no registers.
    config = Config(device_name=FF802.name,
                    channels=(ChannelSetting("output", 1, "volumecal", 0.0),))
    assert reconcile.desired(config) == ()


@pytest.mark.parametrize("intent", list(reconcile.ApplyIntent))
def test_fixed_calibration_is_pinned_for_all_apply_intents(tmp_path, intent):
    config = load_config(routing_conf(tmp_path, "[output:1]\nvolumecal = -0.01\n"))
    assert reconcile.application_plan(config, intent).messages() == (
        ("/output/1/volumecal", "f", (-0.01,)),)
    assert reconcile.policy_for("/output/1/volumecal", UCX2, config.policies) is PIN


@pytest.mark.parametrize("intent", list(reconcile.ApplyIntent))
def test_explicit_remember_override_keeps_calibration_on_later_operations(tmp_path, intent):
    config = load_config(routing_conf(
        tmp_path, "[output:1]\nvolumecal = -0.01\n[pin]\noutput.volumecal = remember\n"))
    expected = (() if intent.preserves_remember else
                (("/output/1/volumecal", "f", (-0.01,)),))
    assert reconcile.application_plan(config, intent).messages() == expected
    assert reconcile.policy_for("/output/1/volumecal", UCX2, config.policies) is REMEMBER
    # A retained value is not a confirmation of the requested starting value.
    assert verify.expected_registers(config) == {"/output/1/volumecal": ("f", (-0.01,))}


@pytest.mark.parametrize(("reported", "state", "mismatched", "unobserved", "invalid"), [
    (-0.009999999776482582, APPLIED_VERIFIED, (), (), ()),
    (0.0, APPLIED_MISMATCHED, ("/output/1/volumecal",), (), ()),
    (None, APPLIED_UNVERIFIED, (), ("/output/1/volumecal",), ()),
    (math.nan, APPLIED_MISMATCHED, ("/output/1/volumecal",), (), ("/output/1/volumecal",)),
    (3.01, APPLIED_MISMATCHED, ("/output/1/volumecal",), (), ("/output/1/volumecal",)),
    (False, APPLIED_MISMATCHED, ("/output/1/volumecal",), (), ("/output/1/volumecal",)),
])
def test_calibration_observation_and_profile_outcome_preserve_missing_differing_invalid(
        tmp_path, reported, state, mismatched, unobserved, invalid):
    config = load_config(routing_conf(tmp_path, "[output:1]\nvolumecal = -0.01\n"))
    path = "/output/1/volumecal"
    reports = [] if reported is None else [(path, "f", (reported,))]
    backend = RecordingBackend(reports=lambda _sent: reports)
    result = verify.verify_routing(verify.expected_registers(config), backend,
                                  timeout=.001, device_model=UCX2)
    assert tuple(result.mismatched) == mismatched
    assert tuple(result.unobserved) == unobserved
    assert tuple(result.invalid) == invalid
    assert result.confirmed == ([path] if state is APPLIED_VERIFIED else [])
    outcome = profiles._check("calibration", SimpleNamespace(config=config, verify=lambda: result))
    assert outcome.state is state
    assert outcome.mismatched == mismatched
    assert outcome.unobserved == unobserved
    assert outcome.invalid == invalid
    assert outcome.unverifiable == ()  # missing warm/cold feedback is not WRITE_ONLY
    assert outcome.read_back is True
    assert outcome.persisted is False
    assert backend.dumps == 1
    assert backend.sent == []


def test_missing_calibration_remains_observable_without_claiming_cold_completeness():
    path = "/output/1/volumecal"
    assert verify.register_ever_reported(path, UCX2) is True
    assert verify.register_promptly_reported(path, UCX2) is False
    assert cold_plug_complete(UCX2, path) is False


@pytest.mark.parametrize(("requested", "reported", "confirmed"), [
    (-0.01, -0.009999999776482582, True),
    (-0.01, -0.019999999552965164, False),
    (-0.01, 0.0, False), (0.0, False, False), (1.0, True, False),
    (True, 1.0, False), (False, 0.0, False),
])
def test_calibration_readback_uses_quantum_and_retains_bool_provenance(
        requested, reported, confirmed):
    row = register_at(UCX2, "/output/1/volumecal")
    assert reconcile.matches("f", (requested,), (reported,), register=row) is confirmed


@pytest.mark.parametrize(("value", "text"), [
    (-0.009999999776482582, "-0.01"), (-0.019999999552965164, "-0.02"),
    (-0.09999999403953552, "-0.10"), (-24.0, "-24.00"), (3.0, "3.00"),
])
def test_calibration_export_roundtrips_quantum_as_pinned_config(tmp_path, value, text):
    path = "/output/1/volumecal"
    warnings = []
    settings = dump.channels_from_observed({path: (value,)}, UCX2, warnings)
    assert warnings == []
    assert len(settings) == 1
    rendered = dump.render_config(Config(channels=settings), UCX2)
    assert f"\nvolumecal = {text}\n" in rendered
    assert f"# volumecal = {text}" not in rendered
    recovered = load_config(routing_conf(tmp_path, rendered))
    entry, = reconcile.desired(recovered)
    row = register_at(UCX2, path)
    assert entry.path == path
    assert numeric.raw_value(entry.args[0], row) == numeric.raw_value(value, row)
    assert reconcile.application_plan(recovered, reconcile.ApplyIntent.RECONCILE).messages() == (
        (path, "f", (float(text),)),)


@pytest.mark.parametrize("reported", [True, False, math.nan, math.inf, -math.inf, 3.01, -24.01])
def test_invalid_calibration_export_omits_the_value_and_explains_why(reported):
    warnings = []
    assert dump.channels_from_observed(
        {"/output/1/volumecal": (reported,)}, UCX2, warnings) == ()
    assert len(warnings) == 1
    assert "/output/1/volumecal" in warnings[0]


def test_omitted_calibration_does_not_change_existing_writes(tmp_path):
    config = load_config(routing_conf(tmp_path, "[output:1]\nmute = false\n"))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    assert backend.sent == [("/output/1/mute", "i", (0,))]
    assert "/output/1/volumecal" not in verify.expected_registers(config)


def test_calibration_does_not_relax_existing_unknown_partner_retention():
    config = Config(routes=(Route(name="linked", playback=(1, 2), output=(5, 6)),),
                    channels=(ChannelSetting("output", 5, "volumecal", -0.01),
                              ChannelSetting("output", 6, "volumecal", -0.01)))
    backend = RecordingBackend()
    with pytest.raises(WriteFailed, match="retained stereo partner"):
        routing.apply_routing(config, backend, intent=reconcile.ApplyIntent.RECONCILE,
                              leave_alone=["/output/6/volumecal"],
                              confirmed=["/playback/1/stereo", "/output/5/stereo"])
    assert backend.sent == []
    assert backend.dumps == 0


@pytest.mark.parametrize(("path", "value", "tags"), [
    ("/output/1/pan", 1, "i"), ("/echo/width", 1.0, "f"),
    ("/output/1/volumecal", 1.0, "f"),
])
def test_generic_number_comparison_refuses_bool_wanted_or_reported(path, value, tags):
    row = register_at(UCX2, path)
    assert row.domain is NUMBER
    assert not reconcile.matches(tags, (True,), (value,), register=row)
    assert not reconcile.matches(tags, (value,), (True,), register=row)


@pytest.mark.parametrize("value", [True, False])
def test_generic_number_rejection_preserves_actual_boolean_settings(value):
    config = Config(channels=(ChannelSetting("output", 1, "mute", value),))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    assert backend.sent == [("/output/1/mute", "i", (int(value),))]
    row = register_at(UCX2, "/output/1/mute")
    assert numeric.report_value(value, row) == int(value)
    assert dump._render_value(value, row) == ("true" if value else "false")


def test_calibration_register_declares_only_measured_numeric_contract():
    for channel in range(1, 21):
        path = f"/output/{channel}/volumecal"
        row = register_at(UCX2, path)
        assert row.tags == "f"
        assert row.verify is VERIFIABLE
        assert row.domain is NUMBER
        assert row.policy is PIN
        assert row.channels == "output"
        assert (row.lo, row.hi, row.scale, row.unit) == (-24.0, 3.0, 0.01, "dB")
        assert cold_plug_complete(UCX2, path) is False


def test_calibration_extends_the_existing_settable_surface():
    assert set(settable_options(UCX2, "output")) == {
        "crossfeed", "fx", "loopback", "mute", "pan", "phase", "reflevel", "volumecal", "volume"}
    assert set(settable_options(UCX2, "input")) == {
        "autoset", "fx", "gain", "hi-z", "msproc", "mute", "phase", "reflevel"}
