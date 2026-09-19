"""The reconciler, held against the path it is meant to replace.

An abstraction that produces *almost* what the old code produced is
worse than no abstraction, and this is the one place in the project
where "almost" is inaudible until somebody is listening to it: every
defect shipped here was invisible at message level.

So the load-bearing test is not that `plan()` looks reasonable. It is
that `plan(desired(config))` against an empty observation is the datagram
sequence `routing_plan()` produces today -- same messages, same order,
same values -- with exactly one stated difference: a register two routes
share is written once rather than twice. That difference is pinned by
two tests rather than absorbed, because it is a change on the wire in
the audible path. Until both hold, nothing switches over.
"""

import oracle
import pytest

from oscmix_desk import devices, reconcile


def make_config(session_mod, *routes, device="Fireface UCX II"):
    return session_mod.Config(device_name=device, routes=list(routes))


def route(session_mod, **kwargs):
    defaults = dict(name="r", playback=(1, 2), output=(5, 6),
                    level=0.0, volume=None, stereo=True)
    defaults.update(kwargs)
    return session_mod.Route(**defaults)


# --------------------------------------------------------------------------
# Equivalence with the path in the runtime today. This is the whole point.
# --------------------------------------------------------------------------

CONFIGS = [
    [dict(name="main", playback=(1, 2), output=(1, 2))],
    [dict(name="main", playback=(1, 2), output=(1, 2), volume=0.0)],
    [dict(name="mono", playback=(3,), output=(9,), level=-6.0)],
    [dict(name="split", playback=(1, 2), output=(5, 6), stereo=False)],
    # More than one route: the case where walking route by route and
    # walking the routing diverge, and the reason routing_plan exists.
    [dict(name="main", playback=(1, 2), output=(1, 2)),
     dict(name="phones", playback=(3, 4), output=(7, 8))],
    [dict(name="a", playback=(1, 2), output=(1, 2), volume=-10.0),
     dict(name="b", playback=(3, 4), output=(7, 8)),
     dict(name="c", playback=(5,), output=(9,), level=-12.0)],
    # A pair and a mono route sharing an output pair.
    [dict(name="pair", playback=(1, 2), output=(5, 6)),
     dict(name="also", playback=(7, 8), output=(5, 6))],
]


def without_repeats(messages):
    """The sequence with each register kept only at its first position."""
    seen, kept = set(), []
    for message in messages:
        if message[0] in seen:
            continue
        seen.add(message[0])
        kept.append(message)
    return kept


@pytest.mark.parametrize("routes", CONFIGS, ids=lambda r: "+".join(
    d["name"] for d in r))
def test_a_blind_plan_is_what_the_apply_sends_today_minus_repeats(
        session_mod, routes):
    """Equivalent, and the one difference is stated rather than absorbed.

    `routing_plan` walks every route and emits that route's links, so a
    register two routes share goes out twice. The reconciler asks what
    *state* the config wants, and a state holds each register once.

    That is a change on the wire, in the audible path, so it is pinned
    here rather than allowed to pass as "equivalent": the plan must be
    exactly today's sequence with repeats removed, at the same positions.
    """
    config = make_config(session_mod, *[route(session_mod, **r) for r in routes])
    old = list(oracle.routing_plan(config.routes).messages())
    new = list(reconcile.plan(reconcile.desired(config)).messages())
    assert new == without_repeats(old), (
        "the reconciler diverges from routing_plan by more than repeats; "
        "nothing may switch over while this differs")


@pytest.mark.parametrize("routes", CONFIGS, ids=lambda r: "+".join(
    d["name"] for d in r))
def test_every_dropped_message_was_a_repeat_of_the_same_value(
        session_mod, routes):
    """Why dropping them is safe, asserted rather than argued.

    A repeat can only be harmless if it carried the value already in the
    plan. Two routes sharing an output pair must agree on its link state
    -- `_check_link_agreement` rejects configs where they do not -- and
    `/playback/N/stereo` is always 1. If either ever stops holding, the
    dedup silently changes device state, and this fails first.
    """
    config = make_config(session_mod, *[route(session_mod, **r) for r in routes])
    old = list(oracle.routing_plan(config.routes).messages())
    kept = {m[0]: m for m in without_repeats(old)}
    repeats = [m for m in old if m is not kept.get(m[0]) and m[0] in kept]
    for message in repeats:
        assert message == kept[message[0]], (
            "%s is sent twice with different values; dropping the repeat "
            "would change device state" % message[0])


@pytest.mark.parametrize("routes", CONFIGS, ids=lambda r: "+".join(
    d["name"] for d in r))
def test_desired_is_exactly_what_the_verifier_expects_today(session_mod, routes):
    config = make_config(session_mod, *[route(session_mod, **r) for r in routes])
    old = session_mod.expected_registers(config)
    new = {e.path: (e.tags, e.args) for e in reconcile.desired(config)}
    assert new == old


def test_the_barrier_is_per_routing_not_per_route(session_mod):
    # The defect that silenced every even output, stated on the plan:
    # every link precedes every mix, across all routes.
    config = make_config(
        session_mod,
        route(session_mod, name="main", playback=(1, 2), output=(1, 2)),
        route(session_mod, name="phones", playback=(3, 4), output=(7, 8)))
    writes = reconcile.plan(reconcile.desired(config)).writes
    phases = [w.phase for w in writes]
    assert phases == sorted(phases), "a mix write precedes a link write"
    assert {w.path for w in writes if w.phase == reconcile.PHASE_LINK} == {
        "/playback/1/stereo", "/output/1/stereo",
        "/playback/3/stereo", "/output/7/stereo"}


# --------------------------------------------------------------------------
# What the plan does with an observation.
# --------------------------------------------------------------------------

def test_a_register_already_at_its_value_is_not_written(session_mod):
    config = make_config(session_mod, route(session_mod, output=(5, 6)))
    entries = reconcile.desired(config)
    seen = {e.path: e.args for e in entries}
    result = reconcile.plan(entries, seen, devices.UCX2)
    written = {w.path for w in result.writes}
    # The playback matrix is rewritten regardless -- it is unverifiable.
    assert written == {"/mix/5/playback/1"}
    assert "/output/5/stereo" in result.confirmed
    assert "/mix/5/playback/1" in result.unverifiable


def test_a_mismatched_register_is_written_and_says_so(session_mod):
    config = make_config(session_mod, route(session_mod, output=(5, 6)))
    entries = reconcile.desired(config)
    seen = {e.path: e.args for e in entries}
    seen["/output/5/stereo"] = (0,)
    result = reconcile.plan(entries, seen, devices.UCX2)
    reasons = {w.path: w.reason for w in result.writes}
    assert reasons["/output/5/stereo"] == reconcile.MISMATCHED


def test_a_register_the_dump_never_mentioned_is_written(session_mod):
    config = make_config(session_mod, route(session_mod, output=(5, 6)))
    entries = reconcile.desired(config)
    result = reconcile.plan(entries, {}, devices.UCX2)
    reasons = {w.path: w.reason for w in result.writes}
    assert reasons["/output/5/stereo"] == reconcile.MISSING


def test_the_playback_matrix_is_never_confirmed_only_rewritten(session_mod):
    # ADR 0002: a /mix write draws no reply and the dump omits it, so it
    # is re-established from a known link state rather than checked.
    # Even a dump that somehow carried it must not confirm it.
    config = make_config(session_mod, route(session_mod, output=(5, 6)))
    entries = reconcile.desired(config)
    seen = {e.path: e.args for e in entries}
    result = reconcile.plan(entries, seen, devices.UCX2)
    assert "/mix/5/playback/1" not in result.confirmed
    assert [w.reason for w in result.writes
            if w.path == "/mix/5/playback/1"] == [reconcile.REWRITE]


def test_a_blind_plan_writes_everything(session_mod):
    # seen=None is "the dump could not be read" -- the path taken when
    # the mixer GUI holds the receive port, which is the normal desktop
    # case. It must behave exactly like a first apply.
    config = make_config(session_mod, route(session_mod, output=(5, 6)))
    entries = reconcile.desired(config)
    result = reconcile.plan(entries, None, devices.UCX2)
    assert len(result.writes) == len(entries)
    assert {w.reason for w in result.writes} == {reconcile.UNCONDITIONAL}
    assert result.confirmed == ()


def test_floats_compare_with_the_devices_quantisation(session_mod):
    # 0.5 dB, the value the read-back has used since 0.1.2. Without it a
    # confirmed register reads as mismatched on every single run.
    config = make_config(session_mod,
                         route(session_mod, output=(5, 6), volume=-10.0))
    entries = reconcile.desired(config)
    seen = {e.path: e.args for e in entries}
    seen["/output/5/volume"] = (-10.3,)
    assert "/output/5/volume" in reconcile.plan(
        entries, seen, devices.UCX2).confirmed
    seen["/output/5/volume"] = (-12.0,)
    assert "/output/5/volume" not in reconcile.plan(
        entries, seen, devices.UCX2).confirmed


def test_an_unmodelled_device_compares_everything(session_mod):
    # No register model means no opinion, and the reconciler must not
    # invent one: every entry is comparable, as it was before the model.
    config = make_config(session_mod, route(session_mod, output=(5, 6)))
    entries = reconcile.desired(config)
    seen = {e.path: e.args for e in entries}
    result = reconcile.plan(entries, seen, None)
    assert result.writes == ()
    assert len(result.confirmed) == len(entries)


# --------------------------------------------------------------------------
# observed(), and what cannot be verified at all.
# --------------------------------------------------------------------------

def test_observed_is_the_devices_view_as_a_value():
    reports = {"/output/5/stereo": [1], "/output/5/volume": [-10.0, "extra"]}
    assert reconcile.observed(reports) == {
        "/output/5/stereo": (1,), "/output/5/volume": (-10.0, "extra")}


def test_the_module_stays_pure(session_mod):
    """No socket, no clock, no device.

    The reason it can be tested against recordings rather than hardware,
    and the reason `--dump-config` and `--diff` can reuse it without
    inheriting the apply's side effects.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(reconcile))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"socket", "time", "subprocess", "os"}, (
        "reconcile grew a side effect: %s" % sorted(imported))


def test_a_channel_setting_resolves_to_its_own_channels_register():
    """One option name, three rows, and the channel decides which.

    `/input/{ch}/gain` splits by what the device accepts: 0..75 dB on
    the two mic preamps, 0..24 on the instrument channels and (since
    fdc47f7) on Analog 5-8. Resolving by name alone returned whichever
    row came last in the table.

    Today the two settable rows agree on domain and tag, so the entry
    `_encode` builds is the same either way and nothing is visibly
    broken. This test exists for the day they do not agree: two rows
    differing in `choices` would write the wrong enum value, and a write
    draws no reply to notice it by.

    `channel_entries` resolves through `option_register` directly; the
    `_register_for` wrapper it went through was removed when its nested
    fallback turned out to be dead code (17 surviving mutants, all in a
    branch no loadable config can reach).
    """
    from oscmix_desk.devices import UCX2
    from oscmix_desk.registers import option_register

    assert option_register(UCX2, "input", "gain", 1).hi == 75.0
    assert option_register(UCX2, "input", "gain", 3).hi == 24.0
    assert option_register(UCX2, "input", "gain", 5).hi == 24.0
