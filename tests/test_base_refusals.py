"""`[desk] base`: what each refusal says, which links a base may make, and what it compares.

test_base.py pins the cells and routes each base writes. These pin the complete refusals a
person reads -- the base, the pair, the conflicting settings, the mirror that stands in the way
--, that a mirror target's pair is checked when its source is linked, that a pair the desk links
itself is not the base's to check, and which silent registers a declared unlink compares.
"""

import pytest

from oscmix_desk.base import base_entries, base_routes, check_base
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.model import ChannelSetting, Route

MIC = Route("mic", input=(5, 6), output=(1, 2))
APART = (ChannelSetting("output", 5, "volume", -10.0),
         ChannelSetting("output", 6, "volume", -20.0))
HINT = "declare the pair with a route of its own (stereo = false keeps it two channels)"


def refusal(*args):
    with pytest.raises(ConfigError) as refused:
        base_routes(*args)
    return str(refused.value)


def test_an_unknown_base_names_every_choice():
    with pytest.raises(ConfigError) as refused:
        check_base("all")
    assert str(refused.value) == ("[desk] base: expected one of keep, clear-inputs, "
                                  "clear-submixes, straight-playback, daw, got 'all'")


def test_mono_playback_on_a_linked_output_pair_cannot_play_one_to_one():
    routes = [Route("m", playback=(1,), output=(1,)), MIC]
    assert refusal("daw", routes, [], [], UCX2) == (
        "[desk] base = daw: playback 1/2 is two mono channels and output 1/2 a linked pair, so "
        "playback cannot play 1:1 there; declare both alike")


def test_a_link_that_would_merge_settings_names_every_one_that_differs():
    settings = [ChannelSetting("output", 1, "volume", -10.0),
                ChannelSetting("output", 2, "volume", -20.0),
                ChannelSetting("output", 1, "mute", 1)]
    assert refusal("straight-playback", [], settings, [], UCX2) == (
        "[desk] base = straight-playback links output 1/2 to play 1:1, but output 1 and 2 set "
        "mute, volume differently; " + HINT)


def test_linking_a_mirror_source_checks_the_mirror_target_pair_too():
    # 5/6 gets no 1:1 of its own (it is a mirror target); it is linked as 1/2's copy.
    assert refusal("daw", [], list(APART), [((5, 6), (1, 2))], UCX2) == (
        "[desk] base = daw links output 5/6 to play 1:1, but output 5 and 6 set volume "
        "differently; " + HINT)


def test_a_pair_the_desk_links_itself_is_not_the_bases_to_check():
    settings = [ChannelSetting("output", 1, "volume", -10.0),
                ChannelSetting("output", 2, "volume", -20.0)]
    routes = base_routes("daw", [MIC], settings, [], UCX2)
    assert ("[desk] base 1/2", (1, 2), (1, 2)) in [(r.name, r.output, r.playback)
                                                   for r in routes]


@pytest.mark.parametrize("alone", [2, 1])
def test_a_one_to_one_pair_with_an_output_mirrored_alone_names_the_mirror(alone):
    assert refusal("daw", [], [], [((5,), (alone,))], UCX2) == (
        "[desk] base = daw plays playback 1/2 on output 1/2, but one of those outputs is "
        "mirrored alone; mirror pairs whole, or declare 1/2 with routes of your own")


def test_a_mirror_target_gets_no_one_to_one_of_its_own_and_the_rest_still_do():
    routes = base_routes("daw", [], [], [((3, 4), (1, 2))], UCX2)
    outputs = [route.output for route in routes]
    assert (3, 4) not in outputs
    assert outputs == [(left, left + 1) for left in range(1, 21, 2) if left != 3]


def test_a_declared_unlink_compares_each_odd_source_register_on_both_outputs():
    unlinked = Route("wide", input=(5, 6), output=(1, 2), stereo=False)
    entries = {path: compared for path, _tags, _args, compared
               in base_entries("clear-inputs", [unlinked], set(), UCX2)}
    assert entries["/mix/1/input/3"] is True
    assert entries["/mix/2/input/3"] is True, "output 2 is its own register when declared apart"
    assert entries["/mix/2/input/4"] is False, "input 4 may fold onto input 3"
