"""Hardware input routing: `input = 1/2` as a route source.

Zero-latency direct monitoring -- the reason TotalMix exists on a
tracking session, and not expressible here at all before 0.3.0.

The reason this is the feature to start with: `/mix/<out>/input/<in>`
**is reported by the device dump**, while `/mix/<out>/playback/<pb>` is
not. So a monitoring path can be *verified* rather than only
re-established from a known link state, which is the first time this
project can make that promise about anything it routes.
"""

import pytest
from conftest import repo_file  # noqa: F401  (parity with sibling modules)

from oscmix_desk import devices, reconcile, registers


def write(tmp_path, text):
    path = tmp_path / "routing.conf"
    path.write_text(text)
    return path


DEVICE = "[device]\nname = Fireface UCX II\n\n"


# --------------------------------------------------------------------------
# The config surface.
# --------------------------------------------------------------------------

def test_an_input_route_parses(session_mod, tmp_path):
    config = session_mod.load_config(write(tmp_path, DEVICE + (
        "[route:monitor]\ninput = 1/2\noutput = 5/6\nlevel = -6.0\n")))
    route, = config.routes
    assert route.source == ("input", (1, 2))
    assert route.playback == ()
    assert route.output == (5, 6)
    assert route.level == -6.0


def test_a_playback_route_is_unchanged(session_mod, tmp_path):
    config = session_mod.load_config(write(tmp_path, DEVICE + (
        "[route:main]\nplayback = 1/2\noutput = 1/2\n")))
    route, = config.routes
    assert route.source == ("playback", (1, 2))
    assert route.input == ()


def test_both_sources_at_once_is_refused(session_mod, tmp_path):
    # Two routes wearing one name. Guessing which was meant would apply
    # half of what the file says.
    with pytest.raises(session_mod.ConfigError, match="alternatives"):
        session_mod.load_config(write(tmp_path, DEVICE + (
            "[route:x]\ninput = 1/2\nplayback = 1/2\noutput = 5/6\n")))


def test_no_source_at_all_is_refused(session_mod, tmp_path):
    with pytest.raises(session_mod.ConfigError, match="missing a source"):
        session_mod.load_config(write(tmp_path, DEVICE + (
            "[route:x]\noutput = 5/6\n")))


def test_input_channels_are_checked_against_the_device(session_mod, tmp_path):
    # The register model earns its keep again: 'input' has its own
    # channel range, and it is not the output range.
    with pytest.raises(session_mod.ConfigError) as excinfo:
        session_mod.load_config(write(tmp_path, DEVICE + (
            "[route:x]\ninput = 40/41\noutput = 5/6\n")))
    assert "input 1..20" in str(excinfo.value)


def test_mono_and_pair_must_still_agree(session_mod, tmp_path):
    with pytest.raises(session_mod.ConfigError, match="both be mono"):
        session_mod.load_config(write(tmp_path, DEVICE + (
            "[route:x]\ninput = 1/2\noutput = 5\n")))


def test_output_stayed_required_when_the_source_became_a_choice(session_mod):
    """Only the source is optional-per-kind; the destination never is.

    Giving `output` a default made a route with no destination
    constructible, and `mix_messages` then unpacked an empty tuple and
    crashed. A route without an output is not a route, so the dataclass
    refuses it rather than the caller having to.
    """
    import dataclasses

    with pytest.raises(TypeError):
        session_mod.Route(name="no-destination")     # type: ignore[call-arg]

    fields = {f.name: f for f in dataclasses.fields(session_mod.Route)}
    assert fields["output"].default is dataclasses.MISSING
    assert fields["playback"].default == ()
    assert fields["input"].default == ()


# --------------------------------------------------------------------------
# What it writes.
# --------------------------------------------------------------------------

def test_an_input_pair_links_the_input_not_the_playback(session_mod):
    route = session_mod.Route(name="m", input=(1, 2), output=(5, 6))
    assert reconcile.link_messages(route) == [
        ("/input/1/stereo", "i", (1,)),
        ("/output/5/stereo", "i", (1,)),
    ]


def test_an_input_pair_writes_the_input_matrix(session_mod):
    route = session_mod.Route(name="m", input=(1, 2), output=(5, 6),
                              level=-6.0)
    assert reconcile.mix_messages(route) == [
        ("/mix/5/input/1", "fi", (-6.0, 0)),
    ]


def test_a_mono_input_route_needs_no_link(session_mod):
    route = session_mod.Route(name="m", input=(3,), output=(9,))
    assert reconcile.link_messages(route) == []
    assert [p for p, _t, _a in reconcile.mix_messages(route)] == \
        ["/mix/9/input/3"]


def test_an_unlinked_input_pair_uses_the_same_pair_balance(session_mod):
    # Same shape as the playback path, including the 6 dB compensation.
    # See the comment in mix_messages: that offset was measured on a
    # playback source, and oscmix runs both through one setlevel(), so
    # the same halving is *expected* for inputs -- not measured. This
    # test pins the inference so it is visible rather than buried.
    route = session_mod.Route(name="m", input=(1, 2), output=(5, 6),
                              stereo=False)
    messages = reconcile.mix_messages(route)
    assert [p for p, _t, _a in messages] == ["/mix/5/input/1", "/mix/6/input/1"]
    for (_p, _t, args), pan in zip(messages, (-100, 100)):
        assert abs(args[0] - 6.0206) < 0.001
        assert args[1] == pan


def test_playback_routes_are_byte_for_byte_unchanged(session_mod):
    # The feature must not move the path that carries every existing
    # config. Same registers, same values, same order as before.
    route = session_mod.Route(name="m", playback=(1, 2), output=(5, 6),
                              volume=0.0)
    assert reconcile.link_messages(route) + reconcile.mix_messages(route) == [
        ("/playback/1/stereo", "i", (1,)),
        ("/output/5/stereo", "i", (1,)),
        ("/mix/5/playback/1", "fi", (0.0, 0)),
        ("/output/5/volume", "f", (0.0,)),
        ("/output/6/volume", "f", (0.0,)),
    ]


# --------------------------------------------------------------------------
# Point 2 of the bar: written paths are a subset of declared paths.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route_kwargs", [
    dict(input=(1, 2), output=(5, 6)),
    dict(input=(1, 2), output=(5, 6), stereo=False),
    dict(input=(3,), output=(9,)),
    dict(input=(1, 2), output=(5, 6), volume=-10.0),
])
def test_an_input_route_writes_only_what_the_model_declares(session_mod,
                                                            route_kwargs):
    """The rule the `volume` bug produced, applied to the new family.

    Every path an input route writes must be a register the model knows,
    on a channel the device has. A typo in a format string would
    otherwise write to a register nobody declared -- silently, because
    an unknown path draws no reply.
    """
    route = session_mod.Route(name="m", **route_kwargs)
    written = [p for p, _t, _a in
               reconcile.link_messages(route) + reconcile.mix_messages(route)]
    for path in written:
        assert registers.verify_class(devices.UCX2, path) is not None, (
            "%s is not a register the model declares" % path)


def test_the_input_matrix_is_verifiable_unlike_the_playback_matrix(session_mod):
    """The reason this feature came first.

    `/mix/<out>/playback/<pb>` is re-established from a known link state
    and never confirmed (ADR 0002) because the dump omits it. The input
    matrix is in the dump, so a monitoring path is the first thing this
    project routes that it can actually verify.
    """
    assert registers.verify_class(
        devices.UCX2, "/mix/5/input/1") == registers.VERIFIABLE
    assert registers.verify_class(
        devices.UCX2, "/mix/5/playback/1") == registers.REESTABLISHED


def test_an_input_route_is_planned_as_verifiable(session_mod):
    config = session_mod.Config(
        device_name="Fireface UCX II",
        routes=[session_mod.Route(name="m", input=(1, 2), output=(5, 6))])
    entries = reconcile.desired(config)
    seen = {e.path: e.args for e in entries}
    result = reconcile.plan(entries, seen, devices.UCX2)
    # Everything confirmed, nothing rewritten -- the playback matrix
    # cannot reach this state at all.
    assert result.writes == ()
    assert "/mix/5/input/1" in result.confirmed
    assert result.unverifiable == ()


# --------------------------------------------------------------------------
# The mute floor, found by measuring before shipping.
#
# The device's entire input matrix reads back as -inf, not -65. Upstream
# stores a gain at or below -65 dB as zero and reports zero as negative
# infinity:
#
#     level.vol = vol <= -65.f ? 0 : powf(10.f, vol / 20.f);   # setmix
#     ...vol > 0 ? 20.f * log10f(level.vol) : -INFINITY        # newmix
#
# routing.conf documents `level = -65` as mute, so a muted monitoring
# route would have been reported mismatched on every start and re-sent
# every time. Invisible until now only because the playback matrix --
# the only mix family before this feature -- is never reported at all.
# --------------------------------------------------------------------------

def test_a_muted_gain_reads_back_as_negative_infinity(session_mod):
    from oscmix_desk import reconcile as r

    assert r.matches("fi", (-65.0, 0), (float("-inf"), 0))
    # Anything below the floor is stored as zero too, so it matches as
    # well -- the config range stops at -65 but the rule is `<=`.
    assert r.matches("f", (-70.0,), (float("-inf"),))


def test_the_mute_floor_is_the_one_upstream_uses(session_mod):
    # Our LEVEL_MIN and upstream's `-65.f` are the same number. If they
    # ever diverge, a config value just under the floor would be written
    # as audible and read back as silent, or the reverse.
    from oscmix_desk import constants

    assert constants.LEVEL_MIN == -65.0


def test_an_audible_gain_still_compares_normally(session_mod):
    from oscmix_desk import reconcile as r

    assert r.matches("fi", (-6.0, 0), (-6.2, 0))       # device quantisation
    assert not r.matches("fi", (-6.0, 0), (-12.0, 0))
    # And a real silence where audio was asked for is still a mismatch.
    assert not r.matches("fi", (-6.0, 0), (float("-inf"), 0))


def test_a_muted_input_route_verifies_instead_of_re_sending(session_mod):
    """The failure this would have shipped, as a plan.

    Before the fix: written -65, reported -inf, difference infinite ->
    mismatched -> the verifier re-sends the whole routing, every start,
    and logs "unconfirmed after retry" forever.
    """
    from oscmix_desk import devices
    from oscmix_desk import reconcile as r

    config = session_mod.Config(
        device_name="Fireface UCX II",
        routes=[session_mod.Route(name="muted", input=(1, 2), output=(5, 6),
                                  level=-65.0)])
    entries = r.desired(config)
    # What the device actually reports for an unrouted input pair --
    # read off a UCX II, where all 100 input-matrix registers are -inf.
    seen = {e.path: ((float("-inf"), 0) if "/mix/" in e.path else e.args)
            for e in entries}
    result = r.plan(entries, seen, devices.UCX2)
    assert result.writes == (), (
        "a muted input route wants re-sending: %s"
        % [w.path for w in result.writes])
    assert "/mix/5/input/1" in result.confirmed


def test_the_verifier_and_the_plan_share_one_definition_of_equal(session_mod):
    # They disagreed for exactly as long as it took to write this: the
    # read-back had its own comparison, which did not know about the
    # mute floor.
    from oscmix_desk import reconcile as r
    from oscmix_desk import verify as v

    for want, got in ((( -65.0, 0), (float("-inf"), 0)),
                      ((-6.0, 0), (-6.2, 0)),
                      ((-6.0, 0), (-30.0, 0))):
        assert v._register_matches("fi", want, got) == r.matches("fi", want, got)
