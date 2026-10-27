"""A lowered AutoSet-owned gain must not be raised by an unrelated repair."""

from dataclasses import replace

import pytest

from oscmix_desk.autogain import (
    dependency_paths,
    flag_value,
    retained_gains,
    validate_autogain,
)
from oscmix_desk.errors import ConfigError
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.registers import PIN, REMEMBER


def gain(channel, value=6.):
    return ChannelSetting("input", channel, "gain", value)


def autoset(channel, value=True):
    return ChannelSetting("input", channel, "autoset", value)


def state(left, own=0, partner=0, stereo=0):
    return {f"/input/{left}/autoset": ("i", (own,)),
            f"/input/{left + 1}/autoset": ("i", (partner,)),
            f"/input/{left}/stereo": ("i", (stereo,))}


@pytest.mark.parametrize("value", [0, 1])
def test_flag_value_requires_an_exact_typed_boolean_report(value):
    assert flag_value(("i", (value,))) == value


@pytest.mark.parametrize("report", [
    None, (), ("i",), ("i", None), ("f", (0.,)), ("ii", (0, 0)),
    ("i", ()), ("i", (0, 0)), ("i", (False,)), ("i", (True,)),
    ("i", (0.,)), ("i", (1.,)), ("i", ("0",)), ("i", (-1,)),
    ("i", (2,)), ("i", (float("nan"),)), ("i", (float("inf"),)),
])
def test_malformed_dependency_is_unknown_and_never_permission(report):
    assert flag_value(report) is None


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("setting", [gain, autoset])
def test_dependencies_include_the_omitted_partner_for_each_affected_direction(channel, setting):
    config = Config(channels=(setting(channel),))
    left = channel - (channel - 1) % 2
    assert dependency_paths(config) == (
        f"/input/{left}/autoset", f"/input/{left + 1}/autoset", f"/input/{left}/stereo")


def test_dependency_selection_is_deterministic_and_does_not_change_the_config():
    settings = (gain(4), autoset(2), gain(3), autoset(1))
    config = Config(channels=settings)
    assert dependency_paths(config) == dependency_paths(replace(config, channels=settings[::-1]))
    assert len(dependency_paths(config)) == 6
    assert config.channels == settings
    assert config.policies == {}


@pytest.mark.parametrize("source", [(1,), (2,), (3, 4), (3,), (4,)])
def test_route_only_input_link_protects_an_undeclared_autoset_gain(source):
    config = Config(routes=(Route(name="input", input=source, output=(5, 6)),))
    left = source[0] - (source[0] - 1) % 2
    assert dependency_paths(config) == (
        f"/input/{left}/autoset", f"/input/{left + 1}/autoset", f"/input/{left}/stereo")
    assert retained_gains(config) == {f"/input/{left}/gain", f"/input/{left + 1}/gain"}
    latest = state(left, own=1)
    assert retained_gains(config, latest) == {f"/input/{left}/gain"}
    assert retained_gains(config, latest, linked=(f"/input/{left}/stereo",)) == {
        f"/input/{left}/gain", f"/input/{left + 1}/gain"}


@pytest.mark.parametrize("route", [
    Route(name="playback", playback=(1, 2), output=(3, 4)),
    Route(name="line-input", input=(5, 6), output=(1, 2)),
])
def test_playback_and_other_input_sources_do_not_acquire_autoset_dependencies(route):
    config = Config(routes=(route,))
    assert dependency_paths(config) == ()
    assert retained_gains(config) == frozenset()


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("link", [0, 1, None])
def test_live_autoset_protects_own_gain_and_linked_or_unknown_partner(channel, link):
    left = channel - (channel - 1) % 2
    partner = left + (channel == left)
    config = Config(channels=(gain(partner),))  # AutoSet itself is omitted.
    latest = state(left)
    latest[f"/input/{channel}/autoset"] = ("i", (1,))
    if link is None:
        del latest[f"/input/{left}/stereo"]
    else:
        latest[f"/input/{left}/stereo"] = ("i", (link,))
    expected = {f"/input/{channel}/gain"}
    if link != 0:
        expected.add(f"/input/{partner}/gain")
    assert retained_gains(config, latest) == frozenset(expected)


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
def test_declaration_protects_gain_even_when_live_flag_is_off(channel):
    left = channel - (channel - 1) % 2
    config = Config(channels=(autoset(channel), gain(channel)))
    assert retained_gains(config, state(left)) == {f"/input/{channel}/gain"}
    assert retained_gains(config, state(left), use_declarations=False) == frozenset()


@pytest.mark.parametrize("link_report", [None, ("i", (2,)), ("f", (0.,)), ("i", (False,))])
def test_malformed_last_link_revokes_the_mono_partner_exception(link_report):
    config = Config(channels=(gain(1),))
    latest = state(1, own=1)
    latest["/input/1/stereo"] = link_report
    assert retained_gains(config, latest) == {"/input/1/gain", "/input/2/gain"}


@pytest.mark.parametrize("report", [None, ("i", (2,)), ("f", (0.,)), ("i", (False,)), ("i", ())])
def test_missing_or_malformed_last_autoset_revokes_manual_gain_permission(report):
    config = Config(channels=(gain(1),))
    latest = state(1)
    latest["/input/1/autoset"] = report
    assert retained_gains(config, latest) == {"/input/1/gain"}
    assert retained_gains(config, latest, use_declarations=False) == {"/input/1/gain"}


def test_old_valid_report_cannot_survive_a_malformed_new_last_report():
    config = Config(channels=(gain(1),))
    latest = state(1)
    assert retained_gains(config, latest) == frozenset()
    latest["/input/1/autoset"] = ("i", (2,))
    assert retained_gains(config, latest) == {"/input/1/gain"}
    latest["/input/1/autoset"] = ("i", (0,))
    assert retained_gains(config, latest) == frozenset()


def test_no_reports_retains_both_members_of_each_relevant_pair_only():
    config = Config(channels=(gain(1), gain(6)))
    assert retained_gains(config) == {"/input/1/gain", "/input/2/gain"}
    assert dependency_paths(config) == dependency_paths(Config(channels=(gain(1),)))


@pytest.mark.parametrize("left", [1, 3])
def test_planned_relinking_revokes_a_fresh_mono_exception(left):
    config = Config(channels=(autoset(left),))
    latest = state(left)
    expected = {f"/input/{left}/gain", f"/input/{left + 1}/gain"}
    assert retained_gains(config, latest, linked=(f"/input/{left}/stereo",)) == expected
    assert latest == state(left)


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("live", [0, 1, None])
def test_initial_live_precondition_ignores_declared_on_but_never_unknown(channel, live):
    config = Config(channels=(gain(channel), autoset(channel)))
    left = channel - (channel - 1) % 2
    latest = state(left)
    latest[f"/input/{channel}/autoset"] = None if live is None else ("i", (live,))
    expected = frozenset() if live == 0 else {f"/input/{channel}/gain"}
    assert retained_gains(config, latest, use_declarations=False) == expected


@pytest.mark.parametrize("policy", [PIN, REMEMBER])
def test_remembered_autoset_off_does_not_override_observed_on(policy):
    config = Config(channels=(gain(1), autoset(1, False)), policies={("input", "autoset"): policy})
    assert retained_gains(config, state(1, own=1)) == {"/input/1/gain"}


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
@pytest.mark.parametrize("on_partner", [False, True])
def test_explicit_gain_pin_conflicts_on_an_unknown_or_linked_pair(channel, on_partner):
    left = channel - (channel - 1) % 2
    auto_channel = left + (channel == left) if on_partner else channel
    config = Config(channels=(gain(channel), autoset(auto_channel)),
                    policies={("input", "gain"): PIN})
    with pytest.raises(ConfigError, match="gain PIN conflicts with declared AutoSet"):
        validate_autogain(config)


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
def test_static_mono_excludes_partner_but_does_not_excuse_own_autoset_gain_pin(channel):
    left = channel - (channel - 1) % 2
    partner = left + (channel == left)
    route = Route(name="mono", input=(channel,), output=(1,))
    config = Config(routes=(route,), channels=(gain(channel), autoset(partner)),
                    policies={("input", "gain"): PIN})
    validate_autogain(config)
    with pytest.raises(ConfigError, match="gain PIN conflicts"):
        validate_autogain(replace(config, channels=(gain(channel), autoset(channel))))
    # Static routing is not a fresh physical mono observation.
    assert retained_gains(config) == {f"/input/{left}/gain", f"/input/{left + 1}/gain"}


def test_conflicting_route_declarations_never_establish_a_static_mono_exception():
    config = Config(routes=(Route(name="mono", input=(2,), output=(1,)),
                            Route(name="linked", input=(1, 2), output=(3, 4))),
                    channels=(gain(2), autoset(1)), policies={("input", "gain"): PIN})
    with pytest.raises(ConfigError, match="gain PIN conflicts"):
        validate_autogain(config)


@pytest.mark.parametrize("policy", [None, REMEMBER])
def test_default_gain_pin_is_not_an_explicit_contradiction(policy):
    config = Config(channels=(gain(1), autoset(1)),
                    policies={} if policy is None else {("input", "gain"): policy})
    validate_autogain(config)
    assert retained_gains(config, state(1)) == {"/input/1/gain"}


def test_explicit_gain_pin_without_a_declared_affected_gain_has_no_conflict():
    config = Config(channels=(autoset(1), gain(6)), policies={("input", "gain"): PIN})
    validate_autogain(config)
    assert retained_gains(config, state(1)) == {"/input/1/gain"}


@pytest.mark.parametrize("channel", [5, 6, 7, 8])
def test_line_input_gain_pin_has_no_autoset_dependencies_or_new_retention(channel):
    config = Config(channels=(gain(channel),), policies={("input", "gain"): PIN})
    validate_autogain(config)
    assert dependency_paths(config) == ()
    assert retained_gains(config) == frozenset()


@pytest.mark.parametrize("name", ["Fireface 802", "Unmodelled interface"])
def test_the_ucx2_rule_is_not_assumed_for_unsupported_devices(name):
    config = Config(device_name=name, channels=(gain(1), autoset(1, "invalid")),
                    policies={("input", "gain"): PIN})
    validate_autogain(config)
    assert dependency_paths(config) == ()
    assert retained_gains(config) == frozenset()


@pytest.mark.parametrize("value", [True, False, 0, 1, 0., 1.])
def test_programmatic_autoset_keeps_the_existing_boolean_write_domain(value):
    validate_autogain(Config(channels=(autoset(1, value),)))


@pytest.mark.parametrize("value", [-1, 2, .5, "true", float("nan"), float("inf")])
def test_invalid_programmatic_autoset_is_refused_before_the_owner_opens_io(value):
    with pytest.raises(ConfigError, match="expected a boolean"):
        validate_autogain(Config(channels=(autoset(1, value),)))


@pytest.mark.parametrize("channel", [0, 5, 20, True, .5])
def test_autoset_is_only_a_ucx2_input_1_to_4_setting(channel):
    with pytest.raises(ConfigError, match="does not have AutoSet"):
        validate_autogain(Config(channels=(autoset(channel),)))


@pytest.mark.parametrize("value", ["6", -1., 76., float("nan"), float("inf"), 10 ** 400])
def test_invalid_affected_gain_cannot_bypass_dependency_validation(value):
    with pytest.raises((TypeError, ValueError), match=r"numeric|finite|range"):
        validate_autogain(Config(channels=(gain(1, value),)))
