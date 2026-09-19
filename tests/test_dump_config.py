"""`--dump-config`: observed() rendered as a routing.conf.

The roadmap's test for it is a round trip -- dump, apply, dump again is
a fixed point -- and that is what most of this file is. A dumper that
does not round-trip produces a config which quietly differs from the
device it was read off, which is worse than no dumper: the file *looks*
authoritative.

The other half is the limit. `/mix/<out>/input/<in>` is reported and
`/mix/<out>/playback/<pb>` is not (ADR 0002), so a dump reproduces
monitoring paths and cannot reproduce software routing. A tool that
stayed quiet about that would lose half a config on the first use.
"""

import pytest

from oscmix_desk import devices, reconcile


def observed_from(config, session_mod, *, linked=True):
    """What a device would report after applying this config.

    Built from the same message shapes the apply sends, which is the
    point: the round trip is only meaningful if the observation is what
    the device would actually say.
    """
    seen = {}
    for entry in reconcile.desired(config):
        seen[entry.path] = entry.args
    return seen


def route(session_mod, **kwargs):
    defaults = dict(name="r", input=(1, 2), output=(5, 6), level=0.0)
    defaults.update(kwargs)
    return session_mod.Route(**defaults)


def config_of(session_mod, *routes):
    return session_mod.Config(device_name="Fireface UCX II", routes=list(routes))


# --------------------------------------------------------------------------
# The round trip.
# --------------------------------------------------------------------------

ROUND_TRIP = [
    dict(input=(1, 2), output=(5, 6), level=0.0),
    dict(input=(1, 2), output=(5, 6), level=-6.0),
    dict(input=(3,), output=(9,), level=-12.0),
    dict(input=(1, 2), output=(5, 6), level=-6.0, stereo=False),
]


@pytest.mark.parametrize("kwargs", ROUND_TRIP,
                         ids=lambda k: "in%s-out%s%s" % (
                             k["input"], k["output"],
                             "" if k.get("stereo", True) else "-split"))
def test_dump_apply_dump_is_a_fixed_point(session_mod, kwargs):
    """Point 3 of the bar in the roadmap, for the input matrix.

    Apply a route, observe what the device would report, reconstruct
    the route from that, observe again -- the second observation must
    equal the first. Anything else means the dumper and the applier
    disagree about what the registers mean.
    """
    original = config_of(session_mod, route(session_mod, **kwargs))
    first = observed_from(original, session_mod)

    recovered = config_of(session_mod, *reconcile.routes_from_observed(first))
    second = observed_from(recovered, session_mod)

    assert second == first, (
        "the dump does not reproduce the state it was read from")


@pytest.mark.parametrize("kwargs", ROUND_TRIP, ids=lambda k: str(k["output"]))
def test_the_recovered_route_carries_the_same_meaning(session_mod, kwargs):
    # Names are derived from channels and so will differ; everything
    # that changes the device must not.
    original = route(session_mod, **kwargs)
    recovered, = reconcile.routes_from_observed(
        observed_from(config_of(session_mod, original), session_mod))
    assert recovered.source == original.source
    assert recovered.output == original.output
    assert recovered.stereo == original.stereo
    assert abs(recovered.level - original.level) < 0.05


def test_several_routes_round_trip_together(session_mod):
    original = config_of(
        session_mod,
        route(session_mod, name="a", input=(1, 2), output=(5, 6), level=-3.0),
        route(session_mod, name="b", input=(3, 4), output=(7, 8), level=-9.0),
        route(session_mod, name="c", input=(5,), output=(9,), level=0.0))
    first = observed_from(original, session_mod)
    recovered = config_of(session_mod, *reconcile.routes_from_observed(first))
    assert len(recovered.routes) == 3
    assert observed_from(recovered, session_mod) == first


def test_the_rendered_file_parses_back(session_mod, tmp_path):
    # The round trip has to survive the text, not just the objects.
    original = config_of(
        session_mod,
        route(session_mod, input=(1, 2), output=(5, 6), level=-6.0),
        route(session_mod, name="split", input=(3, 4), output=(7, 8),
              level=-3.0, stereo=False))
    recovered = config_of(session_mod, *reconcile.routes_from_observed(
        observed_from(original, session_mod)))
    text = reconcile.render_config(recovered, devices.UCX2)

    path = tmp_path / "routing.conf"
    path.write_text(text)
    reparsed = session_mod.load_config(path)
    assert observed_from(reparsed, session_mod) == observed_from(original,
                                                                 session_mod)


def test_a_muted_cell_is_not_a_route(session_mod):
    # The device reports every matrix cell, and almost all of them are
    # -inf. Treating those as routes would emit a config with hundreds
    # of them.
    seen = {"/output/5/stereo": (1,), "/input/1/stereo": (1,)}
    for out in range(1, 21):
        for src in range(1, 21):
            seen["/mix/%d/input/%d" % (out, src)] = (float("-inf"), 0)
    seen["/mix/5/input/1"] = (-6.0, 0)
    routes = reconcile.routes_from_observed(seen)
    assert len(routes) == 1
    assert routes[0].output == (5, 6)


def test_the_order_is_deterministic(session_mod):
    # A fixed point that reorders on every run is not a fixed point.
    original = config_of(
        session_mod,
        route(session_mod, name="b", input=(3, 4), output=(7, 8)),
        route(session_mod, name="a", input=(1, 2), output=(5, 6)))
    seen = observed_from(original, session_mod)
    first = [r.name for r in reconcile.routes_from_observed(seen)]
    second = [r.name for r in reconcile.routes_from_observed(dict(seen))]
    assert first == second == sorted(first)


# --------------------------------------------------------------------------
# What it refuses to pretend.
# --------------------------------------------------------------------------

def test_the_playback_matrix_is_named_as_unrecoverable(session_mod):
    assert reconcile.unrecoverable(devices.UCX2) == \
        ("/mix/{out}/playback/{pb}",)
    text = reconcile.render_config(config_of(session_mod), devices.UCX2)
    assert "does not report" in text
    assert "/mix/{out}/playback/{pb}" in text
    assert "Merge, do not replace" in text


def test_a_playback_route_cannot_be_recovered(session_mod):
    """The limit, stated as a test so it cannot be forgotten.

    A playback route writes /mix/<out>/playback/<pb>, which no dump
    carries. Feeding the applier's own output back in recovers nothing
    -- which is correct, and is why the rendered file says so at the
    top rather than looking complete.
    """
    original = config_of(session_mod, session_mod.Route(
        name="soft", playback=(1, 2), output=(5, 6)))
    seen = observed_from(original, session_mod)
    # The device would not report the playback matrix at all.
    seen = {p: a for p, a in seen.items() if "/playback/" not in p}
    assert reconcile.routes_from_observed(seen) == ()


def test_volume_is_not_pinned_by_a_dump(session_mod):
    # ADR 0003: a route that declares volume forces that level on every
    # start. A dump has no way to tell "I meant this" from "this is
    # where I left it", so it declares neither.
    original = config_of(session_mod,
                         route(session_mod, output=(5, 6), volume=-10.0))
    recovered, = reconcile.routes_from_observed(
        observed_from(original, session_mod))
    assert recovered.volume is None
    text = reconcile.render_config(config_of(session_mod, recovered),
                                   devices.UCX2)
    assert "volume" not in text.split("[route:")[1]
    # The header now states the general rule rather than singling volume
    # out: pinned options are emitted as config, remembered ones as
    # comments, and the register table decides which is which.
    assert "remembers them" in text
    assert "cannot tell" in text


def test_an_empty_device_says_so_rather_than_looking_broken(session_mod):
    text = reconcile.render_config(config_of(session_mod), devices.UCX2)
    assert "No input routing was reported" in text
    assert "not an error" in text
