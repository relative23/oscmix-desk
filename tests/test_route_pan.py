"""Offline route-balance contracts; this is not a hardware qualification."""

import math
from dataclasses import replace

import pytest
from backend_doubles import RecordingBackend, echo_link_flags_only
from support import routing_conf

from oscmix_desk import desk as desk_mod
from oscmix_desk import dump, reads, reconcile, routing, verify
from oscmix_desk.backend import OSCMIX
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.model import Config, Route
from oscmix_desk.registers import PIN, REESTABLISHED, VERIFIABLE, register_at


@pytest.fixture(autouse=True)
def offline_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(RecordingBackend, "traits", OSCMIX)
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(tmp_path / "empty-proc"))
    monkeypatch.setenv("OSCMIX_SEQ_DEV", str(tmp_path / "no-seq-device"))


def balanced(**options):
    values = dict(name="balanced", input=(13, 14), output=(1, 2), level=-40.0, pan=1)
    values.update(options)
    return Route(**values)


def observed(pan=1, level=-40.0):
    return {"/input/13/stereo": (1,), "/input/14/stereo": (1,),
            "/output/1/stereo": (1,), "/output/2/stereo": (1,),
            "/mix/1/input/13": (level, pan)}


@pytest.mark.parametrize("kind", ["input", "playback"])
@pytest.mark.parametrize("pan", [-100, -99, -1, 0, 1, 99, 100])
def test_integer_balance_runs_through_real_parser_and_generic_wire_plan(tmp_path, kind, pan):
    source = "13/14" if kind == "input" else "1/2"
    config = load_config(routing_conf(tmp_path,
                        f"[route:balanced]\n{kind} = {source}\noutput = 1/2\n"
                        f"level = -40\npan = {pan}\n"))
    route, = config.routes
    assert type(route.pan) is int
    assert route.pan == pan
    path = "/mix/1/%s/%d" % (kind, route.source[1][0])
    expected = (path, "fi", (-40.0, pan))
    assert reconcile.mix_messages(route) == [expected]
    assert verify.expected_registers(config)[path] == expected[1:]
    backend = RecordingBackend(reports=echo_link_flags_only)
    routing.apply_routing(config, backend)
    assert expected in backend.sent
    assert [message for message in backend.sent if message[0].startswith("/mix/")] == [expected]


@pytest.mark.parametrize("raw", ["true", "false", "1.0", "1.5", "nan", "inf", "-inf",
                                  "101", "-101", "2147483648"])
def test_file_pan_rejects_bool_float_nonfinite_or_out_of_range(tmp_path, raw):
    with pytest.raises(ConfigError, match="pan"):
        load_config(routing_conf(tmp_path, "[route:r]\ninput = 13/14\noutput = 1/2\n"
                                f"level = -40\npan = {raw}\n"))


@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, math.nan, math.inf, -math.inf,
                                    "1", -101, 101, 2 ** 31])
def test_programmatic_pan_rejects_malformed_value_at_construction_before_io(value):
    backend = RecordingBackend()
    with pytest.raises(ValueError, match="pan"):
        Config(routes=(balanced(pan=value),))
    assert backend.sent == []
    assert backend.dumps == 0
    assert backend.operations == []


@pytest.mark.parametrize("values", [
    dict(input=(13,), output=(1,)), dict(stereo=False),
    dict(input=(14, 15)), dict(input=(14, 13)), dict(output=(2, 3)),
    dict(output=(2, 1)), dict(input=(13, 15)), dict(output=(1, 3)),
    dict(input=(-1, 0)), dict(output=(63, 65)), dict(input=(True, 2)),
])
def test_nonzero_pan_requires_real_linked_odd_even_pairs(values):
    with pytest.raises(ValueError, match=r"linked.*stereo pairs"):
        balanced(**values)


@pytest.mark.parametrize("sources", [
    dict(input=(13, 14), playback=(1, 2)), dict(input=(), playback=()),
])
def test_nonzero_pan_requires_exactly_one_programmatic_source(sources):
    with pytest.raises(ValueError, match="exactly one input or playback source"):
        balanced(**sources)


@pytest.mark.parametrize("kind", ["input", "playback"])
def test_adding_other_source_cannot_bypass_pan_preflight_before_io(monkeypatch, kind):
    route = balanced(input=(13, 14) if kind == "input" else (),
                     playback=(1, 2) if kind == "playback" else ())
    config = Config(routes=(route,))
    object.__setattr__(route, "playback" if kind == "input" else "input", (3, 4))
    backend = RecordingBackend()
    calls = []
    monkeypatch.setattr(desk_mod.Desk, "_lock_device", lambda _self: calls.append("lock"))
    monkeypatch.setattr(desk_mod, "connect_backend",
                        lambda *_args, **_kwargs: calls.append("connect"))
    operation = desk_mod._operation(config, backend=backend)
    with pytest.raises(ValueError, match="exactly one input or playback source"):
        operation._open()
    assert calls == []
    assert backend.operations == ["close"]
    assert backend.sent == []
    assert backend.dumps == 0


def test_zero_pan_keeps_legacy_programmatic_input_precedence():
    route = balanced(input=(13, 14), playback=(1, 2), pan=0)
    assert route.source == ("input", (13, 14))
    assert reconcile.mix_messages(route) == [("/mix/1/input/13", "fi", (-40., 0))]


@pytest.mark.parametrize("topology", [
    "input = 13\noutput = 1\n", "playback = 1\noutput = 1\n",
    "input = 13/14\noutput = 1/2\nstereo = false\n",
    "input = 14/15\noutput = 1/2\n",
])
def test_parser_refuses_nonzero_pan_on_mono_split_or_nonphysical_pair(tmp_path, topology):
    with pytest.raises(ConfigError, match=r"pan.*linked.*stereo pairs"):
        load_config(routing_conf(tmp_path, "[route:r]\n" + topology + "level = -40\npan = 1\n"))


@pytest.mark.parametrize("value", [-65.0, -66.0, -math.inf, math.nan, math.inf, True, "-40", 6.1])
def test_nonzero_pan_refuses_silent_or_invalid_level_without_claiming_it_can_be_restored(value):
    with pytest.raises(ValueError, match=r"neither preserved nor reported.*silent"):
        balanced(level=value)


def test_parser_explains_why_nonzero_pan_at_mute_floor_is_refused(tmp_path):
    with pytest.raises(ConfigError, match=r"pan.*neither preserved nor reported.*silent"):
        load_config(routing_conf(tmp_path, "[route:r]\ninput = 13/14\noutput = 1/2\n"
                                "level = -65\npan = 1\n"))


@pytest.mark.parametrize("kind", ["input", "playback"])
@pytest.mark.parametrize(("output", "source", "stereo", "level", "matrix"), [
    ((1, 2), (13, 14), True, -40., [("/mix/1/{kind}/13", "fi", (-40., 0))]),
    ((1,), (13,), True, -40., [("/mix/1/{kind}/13", "fi", (-40., 0))]),
    ((1, 2), (13, 14), False, -65., [("/mix/1/{kind}/13", "fi", (-65., -100)),
                                                    ("/mix/2/{kind}/13", "fi", (-65., 100))]),
    ((1, 2), (13, 14), True, -65., [("/mix/1/{kind}/13", "fi", (-65., 0))]),
])
def test_omitted_pan_and_explicit_zero_keep_existing_wire_shapes(kind, output, source,
                                                              stereo, level, matrix):
    route = Route(name="legacy", output=output, stereo=stereo, level=level, **{kind: source})
    assert route.pan == 0
    assert reconcile.mix_messages(route) == [(path.format(kind=kind), tags, args)
                                             for path, tags, args in matrix]
    assert replace(route, pan=0) == route


@pytest.mark.parametrize("intent", list(reconcile.ApplyIntent))
def test_whole_input_crosspoint_stays_pinned_for_each_apply_intent(intent):
    config = Config(routes=(balanced(),), policies={("output", "pan"): "remember"})
    path = "/mix/1/input/13"
    assert reconcile.policy_for(path, UCX2, config.policies) is PIN
    assert register_at(UCX2, path).verify is VERIFIABLE
    plan = reconcile.application_plan(config, intent)
    assert [write.message() for write in plan.mix()] == [(path, "fi", (-40.0, 1))]


def test_playback_pan_is_reestablished_without_inventing_device_feedback():
    route = balanced(input=(), playback=(1, 2), pan=25)
    path = "/mix/1/playback/1"
    assert register_at(UCX2, path).verify is REESTABLISHED
    entry = next(item for item in reconcile.desired(Config(routes=(route,))) if item.path == path)
    plan = reconcile.plan((entry,), {path: entry.args}, UCX2)
    assert plan.confirmed == ()
    assert plan.unverifiable == (path,)
    assert plan.writes[0].message() == (path, "fi", (-40.0, 25))


def test_no_independent_route_pan_ownership_syntax_is_invented(tmp_path):
    with pytest.raises(ConfigError, match=r"route.pan.*unknown family"):
        load_config(routing_conf(tmp_path,
                    "[route:r]\ninput = 13/14\noutput = 1/2\nlevel = -40\npan = 1\n"
                    "[pin]\nroute.pan = remember\n"))


@pytest.mark.parametrize(("requested", "reported", "confirmed"), [
    ((-40., 1), (-40.1, 1), True), ((-40., 1), (-40., 0), False),
    ((-40., 1), (-39., 1), False), ((-40., 1), (-40., True), False),
    ((-40., 1), (-40., 1.0), False), ((-40., 1), (-40., math.inf), False),
    ((-40., 1), (-40., 101), False), ((-40., True), (-40., 1), False),
])
def test_fi_verification_compares_both_numeric_components_without_bool_coercion(
        requested, reported, confirmed):
    row = register_at(UCX2, "/mix/1/input/13")
    assert reconcile.matches("fi", requested, reported, register=row) is confirmed


@pytest.mark.parametrize("reported", [1, 0, True, 1.0, math.nan])
def test_completed_observation_keeps_pan_mismatch_and_invalid_feedback_distinct(reported):
    path = "/mix/1/input/13"
    backend = RecordingBackend(reports=lambda _sent: [(path, "fi", (-40., reported))])
    result = verify.verify_routing({path: ("fi", (-40., 1))}, backend,
                                  timeout=.001, device_model=UCX2)
    expected_match = type(reported) is int and reported == 1
    expected_invalid = type(reported) is not int
    assert result.confirmed == ([path] if expected_match else [])
    assert result.mismatched == ([] if expected_match else [path])
    assert result.invalid == ([path] if expected_invalid else [])


@pytest.mark.parametrize("pan", [-100, -1, 1, 50, 100])
def test_finite_linked_input_balance_exports_and_roundtrips_both_components(tmp_path, pan):
    warnings = []
    route, = dump.routes_from_observed(observed(pan), UCX2, warnings)
    assert warnings == []
    assert (route.input, route.output, route.level, route.pan) == ((13, 14), (1, 2), -40., pan)
    rendered = dump.render_config(Config(routes=(route,)), UCX2)
    assert f"\npan = {pan}\n" in rendered
    recovered = load_config(routing_conf(tmp_path, rendered))
    assert reconcile.mix_messages(recovered.routes[0]) == [("/mix/1/input/13", "fi", (-40., pan))]
    recovered_observation = dict(observed(pan), **{
        entry.path: entry.args for entry in reconcile.desired(recovered)})
    assert dump.render_config(Config(routes=dump.routes_from_observed(
        recovered_observation, UCX2)), UCX2) == rendered


@pytest.mark.parametrize(("out", "src"), [(1, 13), (1, 14), (2, 13), (2, 14)])
def test_matching_linked_nonzero_alias_can_export_its_canonical_pair(out, src):
    seen = observed(25)
    del seen["/mix/1/input/13"]
    seen[f"/mix/{out}/input/{src}"] = (-40., 25)
    warnings = []
    route, = dump.routes_from_observed(seen, UCX2, warnings)
    assert warnings == []
    assert reconcile.mix_messages(route) == [("/mix/1/input/13", "fi", (-40., 25))]


@pytest.mark.parametrize("alias", [(-40., 0), (-39., 25), (-40., -25)])
def test_contradictory_linked_alias_does_not_export_a_selected_balance(alias):
    seen = dict(observed(25), **{"/mix/2/input/14": alias})
    warnings = []
    assert dump.routes_from_observed(seen, UCX2, warnings) == ()
    assert len(warnings) == 1
    assert "contradictory linked level/pan aliases" in warnings[0]


@pytest.mark.parametrize("value", [True, 1.0, 1.5, math.inf, math.nan, "1", 101])
def test_invalid_observed_pan_is_never_exported_as_an_active_route(value):
    warnings = []
    assert dump.routes_from_observed(observed(value), UCX2, warnings) == ()
    assert len(warnings) == 1
    assert "pan" in warnings[0]


@pytest.mark.parametrize("field", ["/input/13/stereo", "/input/14/stereo",
                                  "/output/1/stereo", "/output/2/stereo"])
@pytest.mark.parametrize("value", [True, 1.0, 0, math.nan])
def test_invalid_or_contradictory_link_flags_do_not_authorize_pan_export(field, value):
    warnings = []
    assert dump.routes_from_observed(dict(observed(), **{field: (value,)}), UCX2, warnings) == ()
    assert warnings


@pytest.mark.parametrize("pan", [-100, 0, 1, 100])
@pytest.mark.parametrize("level", [-math.inf, -65.0])
def test_silent_cells_do_not_invent_recoverable_balance_or_routes(pan, level):
    warnings = []
    assert dump.routes_from_observed(observed(pan, level), UCX2, warnings) == ()
    assert warnings == []


def test_export_refuses_rounding_nonzero_balance_into_digital_mute():
    warnings = []
    assert dump.routes_from_observed(observed(1, -64.99), UCX2, warnings) == ()
    assert "round level to digital mute" in warnings[0]


def test_zero_pan_export_and_mute_verification_keep_previous_behavior():
    route, = dump.routes_from_observed(observed(0), UCX2)
    assert "\npan = " not in dump.render_config(Config(routes=(route,)), UCX2)
    row = register_at(UCX2, "/mix/1/input/13")
    assert reconcile.matches("fi", (-65., 0), (-math.inf, 100), register=row)


@pytest.mark.parametrize("second", [dict(pan=-1), dict(pan=0), dict(level=-30.)])
def test_conflicting_nonzero_level_pan_claims_are_rejected_before_io(second):
    backend = RecordingBackend()
    with pytest.raises(ValueError, match="disagree on level/pan"):
        Config(routes=(balanced(name="first"), balanced(name="second", **second)))
    assert backend.sent == []
    assert backend.dumps == 0


def test_parser_names_conflicting_compound_claims_before_returning_config(tmp_path):
    with pytest.raises(ConfigError, match=r"first.*second.*disagree on level/pan.*mix/1/input/13"):
        load_config(routing_conf(tmp_path,
                    "[route:first]\ninput = 13/14\noutput = 1/2\nlevel = -40\npan = 1\n"
                    "[route:second]\ninput = 13/14\noutput = 1/2\nlevel = -40\npan = -1\n"))


@pytest.mark.parametrize(("source", "output"), [((14, 13), (1, 2)),
                                               ((13, 14), (2, 1)), ((14, 13), (2, 1))])
def test_old_even_alias_cannot_overwrite_new_nonzero_physical_crosspoint(source, output):
    alias = Route(name="alias", input=source, output=output, level=-40.)
    with pytest.raises(ValueError, match=r"disagree on level/pan.*mix/1/input/13"):
        Config(routes=(balanced(), alias))


def test_identical_nonzero_claims_deduplicate_and_other_source_banks_stay_independent():
    panned = balanced()
    playback = balanced(input=(), playback=(13, 14))
    config = Config(routes=(panned, replace(panned, name="duplicate"), playback))
    matrix = [entry for entry in reconcile.desired(config) if entry.path.startswith("/mix/")]
    assert [(entry.path, entry.args) for entry in matrix] == [
        ("/mix/1/input/13", (-40., 1)), ("/mix/1/playback/13", (-40., 1))]


def test_pan_zero_level_duplicates_keep_existing_file_order():
    first = balanced(pan=0, level=-40.)
    second = balanced(pan=0, level=-30.)
    config = Config(routes=(first, second))
    entry = next(item for item in reconcile.desired(config) if item.path == "/mix/1/input/13")
    assert entry.args == (-30., 0)


def test_mutated_route_is_rechecked_before_connect_lock_or_begin(monkeypatch):
    route = balanced()
    config = Config(routes=(route,))
    object.__setattr__(route, "pan", True)
    backend = RecordingBackend()
    calls = []
    monkeypatch.setattr(desk_mod.Desk, "_lock_device", lambda _self: calls.append("lock"))
    monkeypatch.setattr(desk_mod, "connect_backend",
                        lambda *_args, **_kwargs: calls.append("connect"))
    operation = desk_mod._operation(config, backend=backend)
    with pytest.raises(ValueError, match="pan must be an integer"):
        operation._open()
    assert calls == []
    assert backend.operations == ["close"]
    assert backend.sent == []
    assert backend.dumps == 0


def test_later_route_list_mutation_cannot_bypass_conflict_preflight(monkeypatch):
    routes = [balanced()]
    config = Config(routes=routes)
    routes.append(balanced(pan=-1))
    calls = []
    monkeypatch.setattr(desk_mod.Desk, "_lock_device", lambda _self: calls.append("lock"))
    backend = RecordingBackend()
    operation = desk_mod._operation(config, backend=backend)
    with pytest.raises(ValueError, match="disagree on level/pan"):
        operation._open()
    assert calls == []
    assert backend.operations == ["close"]
    assert backend.sent == []
    assert backend.dumps == 0


def test_device_specific_pan_channels_are_checked_before_begin(monkeypatch):
    config = Config(routes=(balanced(input=(21, 22)),))
    calls = []
    monkeypatch.setattr(desk_mod.Desk, "_lock_device", lambda _self: calls.append("lock"))
    backend = RecordingBackend()
    operation = desk_mod._operation(config, backend=backend)
    with pytest.raises(ValueError, match=r"pan.*input channels do not exist"):
        operation._open()
    assert calls == []
    assert backend.operations == ["close"]
    assert backend.sent == []


def test_dataclass_equality_and_diff_include_pan_as_the_second_component():
    left, right = balanced(pan=-1), balanced(pan=1)
    assert left != right
    assert replace(left, pan=1) == right
    entry = next(item for item in reconcile.desired(Config(routes=(right,)))
                 if item.path == "/mix/1/input/13")
    plan = reconcile.plan((entry,), {entry.path: (-40., -1)}, UCX2)
    write, = plan.writes
    line = reads._diff_line(write, {entry.path: (-40., -1)})
    assert "-40.0, 1" in line
    assert "device -40.0, -1" in line
    assert "mismatched" in line
