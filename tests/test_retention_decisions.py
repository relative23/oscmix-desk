"""The pure N0/N1 decisions, each branch checked where it is made.

Production-path tests (test_remember_ownership.py, test_verify.py) show the
effect of these decisions on actual sends. These pin the decisions
themselves: which pair a channel belongs to, which report is invalid rather
than a user adjustment, and which reported value matches an expectation.
"""

import math

import pytest

from oscmix_desk import reconcile
from oscmix_desk.devices import UCX2
from oscmix_desk.model import Config, Route
from oscmix_desk.observation import Observation
from oscmix_desk.reconcile import MISMATCHED, MISSING, Phase, Write, retention_problem
from oscmix_desk.registers import register_at

FRONT = Route(name="front", playback=(1, 2), output=(5, 6))
REAR = Route(name="rear", playback=(3, 4), output=(7, 8))


def write(path):
    return Write(path, "i", (1,), Phase.CHANNEL, MISMATCHED)


def test_a_later_route_is_checked_after_an_unaffected_earlier_one():
    config = Config(routes=[FRONT, REAR])
    matrix = [w for w in reconcile.mix_messages(REAR) if w[0].startswith("/mix/")]
    selected = [Write(path, tags, args, Phase.MIX, MISMATCHED) for path, tags, args in matrix]
    problem = retention_problem(config, selected, ["/output/7/stereo", "/playback/3/stereo"], [])
    assert problem == ("retained link state is not confirmed for "
                       "/output/7/stereo, /playback/3/stereo")


@pytest.mark.parametrize(("written", "retained", "partner"), [
    ("/output/6/volume", "/output/5/volume", "/output/5/volume"),        # even channel
    ("/output/5/volume", "/output/6/volume", "/output/6/volume"),        # odd channel
    ("/input/1/gain", "/input/2/gain", "/input/2/gain"),                 # input family
    ("/output/5/roomeq/delay", "/output/6/roomeq/delay", "/output/6/roomeq/delay"),
])
def test_a_scalar_write_beside_a_retained_partner_needs_a_known_unlinked_pair(
        written, retained, partner):
    config = Config(routes=[FRONT])
    problem = retention_problem(config, [write(written)], [retained], [])
    assert problem == "write %s could change retained stereo partner %s" % (written, partner)


@pytest.mark.parametrize("retained", ["/output/5/volume", "/output/6/volume"])
def test_a_link_write_protects_both_members_of_its_pair(retained):
    problem = retention_problem(Config(), [write("/output/5/stereo")], [retained], [])
    assert problem == ("link write /output/5/stereo could change retained "
                       "partner settings: %s" % retained)


@pytest.mark.parametrize(("written", "retained"), [
    ("/playback/1/stereo", "/playback/2/stereo"),
    ("/mix/5/playback/1", "/mix/6/playback/1"),
])
def test_only_input_and_output_channels_have_partner_settings(written, retained):
    assert retention_problem(Config(), [write(written)], [retained], []) is None


# ---------------------------------------------------------------------------
# Matching a reported value

LEVEL = register_at(UCX2, "/mix/5/input/1")
REFLEVEL = register_at(UCX2, "/output/5/reflevel")


@pytest.mark.parametrize(("tags", "want", "got"), [
    ("f", (1.0,), ()),               # a report shorter than the expectation
    ("ff", (1.0,), (1.0,)),          # tags that do not describe the expectation
    ("", (), ()),                    # nothing expected is not a confirmation
    ("s", ("x",), ("x",)),           # a type this register model never compares
])
def test_a_malformed_expectation_or_short_report_never_matches(tags, want, got):
    assert not reconcile.matches(tags, want, got)


@pytest.mark.parametrize(("pan", "equal"), [(100, True), (-100, True),
                                            (101, False), (-101, False)])
def test_a_mix_pan_is_compared_only_within_its_range(pan, equal):
    assert reconcile.matches("fi", (-6.0, pan), (-6.0, pan), register=LEVEL) is equal


@pytest.mark.parametrize(("wanted", "equal"), [(30, True), (100, True),
                                               (101, False), (-101, False)])
def test_a_silent_mix_pan_is_irrelevant_only_when_the_request_is_valid(wanted, equal):
    assert reconcile.matches("fi", (-65.0, wanted), (-math.inf, 0),
                             register=LEVEL) is equal


@pytest.mark.parametrize(("got", "equal"), [(1, True), (9, False)])
def test_a_report_outside_its_register_domain_never_matches(got, equal):
    assert reconcile.matches("i", (got,), (got,), register=REFLEVEL) is equal


def test_plan_writes_carry_the_declared_message_and_its_reason():
    entries = (reconcile.Entry("/output/5/volume", "f", (-6.0,), Phase.CHANNEL),
               reconcile.Entry("/output/5/mute", "i", (1,), Phase.CHANNEL))
    plan = reconcile.plan(entries, {"/output/5/volume": (-20.0,)}, UCX2)
    assert plan.writes == (
        Write("/output/5/volume", "f", (-6.0,), Phase.CHANNEL, MISMATCHED),
        Write("/output/5/mute", "i", (1,), Phase.CHANNEL, MISSING))
    assert [write.message() for write in plan.writes] == [
        ("/output/5/volume", "f", (-6.0,)), ("/output/5/mute", "i", (1,))]


def test_plan_honours_an_explicit_tolerance_for_an_unmodelled_value():
    # A modelled register keeps its own encoding; see test_numeric.py.
    entries = (reconcile.Entry("/output/5/volume", "f", (-6.0,), Phase.CHANNEL),)
    seen = {"/output/5/volume": (-6.4,)}
    assert reconcile.plan(entries, seen).confirmed == ()
    assert reconcile.plan(entries, seen, tolerance=.5).confirmed == ("/output/5/volume",)


# ---------------------------------------------------------------------------
# Invalid feedback is its own result, not an observed user adjustment

@pytest.mark.parametrize(("path", "expected", "reported"), [
    ("/output/5/volume", ("f", (-6.0,)), ()),                      # short report
    ("/mix/5/input/1", ("fi", (-6.0, 0)), (-6.0, 101)),             # pan above range
    ("/mix/5/input/1", ("fi", (-6.0, 0)), (-6.0, -101)),            # pan below range
    ("/mix/5/input/1", ("fi", (-6.0, 0)), (-math.inf, 150)),        # silent level, bad pan
    ("/mix/5/input/1", ("fi", (-6.0, 0)), ("loud", 0)),             # level not a number
    ("/output/5/stereo", ("i", (1,)), (2,)),                        # link neither 0 nor 1
    ("/output/5/reflevel", ("i", (1,)), (9,)),                      # outside the domain
    ("/output/5/volume", ("s", ("x",)), ("x",)),                    # uncomparable type
])
def test_invalid_feedback_is_classified_apart_and_a_valid_report_restores(
        path, expected, reported):
    observed = Observation({path: expected}, UCX2)
    observed.absorb((path, expected[0], reported))
    assert observed.invalid == {path}
    assert observed.mismatched == {path}
    assert observed.confirmed == set()
    if expected[0] != "s":
        observed.absorb((path, expected[0], expected[1]))
        assert (observed.confirmed, observed.mismatched, observed.invalid) == (
            {path}, set(), set())


@pytest.mark.parametrize("pan", [100, -100])
def test_mix_pan_limits_are_valid_feedback(pan):
    observed = Observation({"/mix/5/input/1": ("fi", (-6.0, 0))}, UCX2)
    observed.absorb(("/mix/5/input/1", "fi", (-6.0, pan)))
    assert observed.invalid == set()
    assert observed.mismatched == {"/mix/5/input/1"}


def test_a_silent_mix_level_is_valid_feedback():
    observed = Observation({"/mix/5/input/1": ("fi", (-65.0, 0))}, UCX2)
    observed.absorb(("/mix/5/input/1", "fi", (-math.inf, 0)))
    assert observed.confirmed == {"/mix/5/input/1"}
    assert observed.invalid == set()


# ---------------------------------------------------------------------------
# A directly constructed desk: an option the device does not have is skipped,
# never the settings that follow it.

def test_an_unknown_channel_option_does_not_drop_later_settings():
    from oscmix_desk.model import ChannelSetting

    config = Config(channels=(ChannelSetting("output", 5, "no-such-option", 1),
                              ChannelSetting("output", 5, "mute", True)))
    assert [entry.path for entry in reconcile.channel_entries(config)] == ["/output/5/mute"]


def test_an_unknown_global_option_does_not_drop_later_settings():
    from oscmix_desk.model import GlobalSetting

    config = Config(globals=(GlobalSetting("echo", "no-such-option", 1),
                             GlobalSetting("echo", "type", "Stereo Echo")))
    assert [entry.path for entry in reconcile.global_entries(config)] == ["/echo/type"]
