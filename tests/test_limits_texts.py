"""DSP limit refusals by their complete text, and which links make Room EQ count twice.

test_dsp_limits.py pins the limits and the counting. These pin what the refusal tells the person
(the feature, the count, every channel or pair, the limit), that only output links pair Room EQ,
that a pair whose even channel alone runs Room EQ counts both, and the load warning's threshold.
"""

import pytest

from oscmix_desk.errors import ConfigError
from oscmix_desk.limits import DSPLOAD, dsp_problem, overload, validate_dsp_limits
from oscmix_desk.model import ChannelSetting, Config, Route


def roomeq(channels, routes=()):
    return Config(channels=tuple(ChannelSetting("output", ch, "roomeq", 1) for ch in channels),
                  routes=tuple(routes))


def refusal(config):
    with pytest.raises(ConfigError) as refused:
        validate_dsp_limits(config)
    return str(refused.value)


def test_too_many_room_eq_channels_are_listed_with_the_limit():
    assert refusal(roomeq(range(1, 18))) == (
        "the config declares Room EQ on 17 output channels (%s); the device runs it on at "
        "most 16" % ", ".join(map(str, range(1, 18))))


def test_too_many_crossfeed_pairs_are_listed_with_the_limit():
    config = Config(channels=tuple(ChannelSetting("output", ch, "crossfeed", 2)
                                   for ch in (1, 3, 5, 7, 9)))
    assert refusal(config) == ("the config declares Crossfeed on 5 stereo outputs (1/2, 3/4, "
                               "5/6, 7/8, 9/10); the device runs it on at most 4")


#: Sixteen channels, input 17's output partner 18 not among them.
SIXTEEN = (*range(1, 14), 15, 17, 19)


def test_an_input_link_does_not_pair_room_eq_on_the_outputs():
    route = Route("mic", input=(17, 18), output=(1, 2), level=-12.0)
    validate_dsp_limits(roomeq(SIXTEEN, [route]))


def test_an_output_link_counts_room_eq_on_both_channels():
    route = Route("mic", input=(1, 2), output=(17, 18), level=-12.0)
    assert refusal(roomeq(SIXTEEN, [route])) == (
        "the config declares Room EQ on 17 output channels (%s); the device runs it on at "
        "most 16" % ", ".join(map(str, sorted({*SIXTEEN, 18}))))


def test_a_linked_pair_whose_even_channel_runs_room_eq_counts_twice():
    # Output 3 stays off, so only output 2 can make pair 1/2 count.
    channels = (2, *range(4, 19))
    route = Route("daw", playback=(1, 2), output=(1, 2))
    assert len(channels) == 16
    assert refusal(roomeq(channels, [route])) == (
        "the config declares Room EQ on 17 output channels (%s); the device runs it on at "
        "most 16" % ", ".join(map(str, sorted({1, *channels}))))


def test_a_link_reported_on_an_even_output_does_not_join_it_to_the_next():
    config = roomeq([18])
    observed = {"/output/%d/roomeq" % ch: ("i", (1,)) for ch in range(3, 18)}
    observed["/output/2/stereo"] = ("i", (1,))
    writes = [("/output/18/roomeq", (1,))]
    assert dsp_problem(config, writes, observed) is None, "outputs 3 to 18: sixteen"
    observed["/output/2/roomeq"] = ("i", (1,))
    assert dsp_problem(config, writes, observed) == (
        "this apply would leave Room EQ on 17 output channels (%s); the device runs it on at "
        "most 16" % ", ".join(map(str, range(2, 19)))), "one more is over the limit"


@pytest.mark.parametrize(("load", "warned"), [(99, False), (100, True), (101, True)])
def test_the_load_warning_starts_at_one_hundred_percent(load, warned):
    note = overload({DSPLOAD: ("i", (load,))})
    if not warned:
        assert note is None
        return
    assert note == ("the device reports a DSP load of %d%%: some of the Room EQ, Crossfeed and "
                    "other effects it reports on do not run; fewer effects, or a lower sample "
                    "rate, fit" % load)
