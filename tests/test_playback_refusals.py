"""Playback mute declarations: every refusal, by its complete text, and the edges of each rule.

test_playback_mute.py pins that each wrong declaration is refused before any I/O. These pin what
the person who wrote it is told -- the message names the pair, the route or the setting -- and
the exact boundaries: the level range of a playback route, the shapes of its channels, which
retained paths protect a pair.
"""

import math

import pytest

from oscmix_desk.errors import ConfigError
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.playback import declared_mutes, retained_pairs, validate_playback_mute

MUTED = (ChannelSetting("playback", 1, "mute", 1), ChannelSetting("playback", 2, "mute", 1))


def refusal(config):
    with pytest.raises(ConfigError) as refused:
        validate_playback_mute(config)
    return str(refused.value)


def with_route(route):
    return Config(channels=MUTED, routes=(route,))


def test_an_incomplete_unmute_names_the_pair_and_how_many_cells_are_missing():
    unmuted = (ChannelSetting("playback", 3, "mute", 0), ChannelSetting("playback", 4, "mute", 0))
    config = Config(channels=unmuted,
                    routes=(Route("daw", playback=(3, 4), output=(1, 2), level=-65.0),))
    assert refusal(config) == (
        "playback 3/4 unmute requires a complete explicit matrix for both columns at all 20 "
        "outputs (36 cells missing; a [desk] base's routes do not count, the unmute owns its "
        "columns)")


def test_an_undeclared_partner_is_named_with_the_pair():
    config = Config(channels=(ChannelSetting("playback", 3, "mute", 1),))
    assert refusal(config) == "playback mute requires both explicit partners 3/4"


def test_partners_that_disagree_are_named_with_the_pair():
    config = Config(channels=(ChannelSetting("playback", 3, "mute", 1),
                              ChannelSetting("playback", 4, "mute", 0)))
    assert refusal(config) == ("playback 3/4 mute values must agree; live link state is "
                               "unknown")


def test_settings_of_other_families_before_the_mutes_do_not_hide_them():
    config = Config(channels=(ChannelSetting("input", 5, "gain", 10.0),
                              ChannelSetting("playback", 3, "mute", 1)))
    assert refusal(config) == "playback mute requires both explicit partners 3/4"


@pytest.mark.parametrize(("setting", "message"), [
    (ChannelSetting("playbak", 1, "mute", 1), "unknown channel family 'playbak'"),
    (ChannelSetting("playback", "1", "mute", 1), "playback channel must be an integer"),
    (ChannelSetting("playback", 21, "mute", 1), ("playback channel 21 has no settable option "
                                                 "'mute'")),
    (ChannelSetting("playback", 1, "mute", 2), "playback 1 mute: invalid boolean report"),
    (ChannelSetting("playback", 1, "stereo", 1), ("playback channel 1 has no settable option "
                                                  "'stereo'")),
])
def test_a_wrong_playback_setting_is_named(setting, message):
    assert refusal(Config(channels=(setting,))) == message


def test_conflicting_values_for_one_channel_are_named():
    config = Config(channels=(*MUTED, ChannelSetting("playback", 1, "mute", 0)))
    assert refusal(config) == "conflicting playback 1 mute values"


def test_another_model_has_no_playback_register_model():
    config = Config(device_name="Fireface 802", usb_id="2a39:3fcd", channels=MUTED)
    assert refusal(config) == "no playback register model for 'Fireface 802' / 2a39:3fcd"


def test_routes_that_disagree_on_a_link_are_named():
    config = Config(channels=MUTED, routes=(
        Route("a", playback=(1, 2), output=(1, 2)),
        Route("b", playback=(1, 2), output=(1, 2), stereo=False)))
    assert refusal(config) == "routes disagree on output 1 stereo"


@pytest.mark.parametrize(("playback", "output", "family"), [
    ((21,), (1,), "playback"),
    ((1, 2, 3), (1, 2, 3), "playback"),
    ((2, 3), (1, 2), "playback"),
    ((1, 3), (1, 2), "playback"),
    ((1,), (0,), "output"),
])
def test_a_playback_route_on_channels_that_are_no_cell_or_pair_is_named(playback, output, family):
    route = Route("bad", playback=playback, output=output)
    assert refusal(with_route(route)) == "invalid %s channels in route 'bad'" % family


def test_a_playback_route_with_unequal_sides_is_named():
    route = Route("half", playback=(1, 2), output=(3,))
    assert refusal(with_route(route)) == ("playback route 'half' must name one channel or one "
                                          "pair on both sides")


def test_a_playback_route_stereo_must_be_a_boolean():
    route = Route("odd", playback=(1, 2), output=(1, 2), stereo=2)
    assert refusal(with_route(route)) == "playback route 'odd' stereo must be boolean"


def test_a_mono_playback_route_with_stereo_off_is_valid():
    validate_playback_mute(with_route(Route("m", playback=(1,), output=(3,), stereo=False)))


def test_a_playback_route_level_that_is_no_number_is_named():
    route = Route("nan", playback=(1, 2), output=(1, 2), level=math.nan)
    assert refusal(with_route(route)) == "playback route 'nan': number must be finite"


@pytest.mark.parametrize(("output", "stereo", "level"), [
    ((1, 2), True, -65.0),
    ((1, 2), True, 6.0),
    ((1, 2), True, 3.0),
    ((1,), True, 3.0),
    ((1,), False, 3.0),
    ((1, 2), False, 0.0),
])
def test_playback_route_levels_within_the_range_are_valid(output, stereo, level):
    playback = (1, 2) if len(output) == 2 else (1,)
    route = Route("ok", playback=playback, output=output, stereo=stereo, level=level)
    validate_playback_mute(with_route(route))


@pytest.mark.parametrize(("output", "stereo", "level"), [
    ((1, 2), True, -65.5),
    ((1, 2), True, 6.5),
    ((1,), True, 7.0),
    ((1, 2), False, 0.5),
    ((1, 2), False, 3.0),
])
def test_playback_route_levels_outside_the_range_are_named(output, stereo, level):
    playback = (1, 2) if len(output) == 2 else (1,)
    route = Route("loud", playback=playback, output=output, stereo=stereo, level=level)
    assert refusal(with_route(route)) == "invalid playback route level in 'loud'"


def test_only_playback_mute_settings_are_declared_mutes():
    config = Config(channels=(*MUTED, ChannelSetting("output", 3, "mute", 1),
                              ChannelSetting("playback", 1, "volume", 0)))
    assert declared_mutes(config) == {1: 1, 2: 1}


def test_retained_flags_protect_their_pairs_across_the_whole_range():
    assert retained_pairs(["/playback/1/mute", "/playback/20/mute"]) == {1, 19}
    assert retained_pairs(["/playback/x/mute", "/playback/4/mute"]) == {3}, (
        "an unreadable path is skipped, not the end of the list")
    assert retained_pairs(["/playback/0/mute", "/playback/21/mute"]) == frozenset()
