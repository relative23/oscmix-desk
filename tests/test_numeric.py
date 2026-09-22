"""Numeric meaning, independent of the implementation under test."""

import math
import struct

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk import dump, numeric, reads, reconcile, routing, verify
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.model import Config, Route
from oscmix_desk.registers import register_at


def config_at(tmp_path, body):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n" + body)
    return load_config(path)


@pytest.mark.parametrize(("section", "key", "value"), [
    ("roomeq:output:1", "delay", "nan"),
    ("echo", "width", "NaN"),
    ("reverb", "time", "inf"),
    ("reverb", "time", "-inf"),
    ("reverb", "time", "1e100"),
    ("reverb", "predelay", "nan"),
    ("reverb", "predelay", "2147483648"),
    ("reverb", "predelay", "32768"),
    ("reverb", "predelay", "-32769"),
    ("eq:input:1", "band1freq", "80.9"),
])
def test_invalid_quantity_is_refused_by_the_parser(tmp_path, section, key, value):
    with pytest.raises(ConfigError, match=key):
        config_at(tmp_path, "[%s]\n%s = %s\n" % (section, key, value))


@pytest.mark.parametrize(("section", "key", "path", "want", "got"), [
    ("roomeq:output:1", "delay", "/output/1/roomeq/delay", .1, 0),
    ("roomeq:output:1", "delay", "/output/1/roomeq/delay", .425, 0),
    ("echo", "delay", "/echo/delay", .2, .001),
    ("echo", "width", "/echo/width", .37, 0),
    ("dynamics:input:1", "compratio", "/input/1/dynamics/compratio", 1.4, 1),
    ("dynamics:input:1", "expthres", "/input/1/dynamics/expthres", -80, -math.inf),
])
def test_another_quantity_is_not_confirmed(tmp_path, monkeypatch,
                                          section, key, path, want, got):
    config = config_at(tmp_path, "[%s]\n%s = %s\n" % (section, key, want))
    got = struct.unpack("!f", struct.pack("!f", got))[0]
    plan = reconcile.plan(reconcile.desired(config), {path: (got,)}, UCX2)
    assert plan.confirmed == ()
    assert [write.path for write in plan.writes] == [path]
    backend = RecordingBackend(reports=lambda _sent: [(path, "f", (got,))])
    monkeypatch.setattr(verify.time, "sleep", lambda _seconds: None)
    result = verify.verify_routing(verify.expected_registers(config), 0, 0,
                                  timeout=.002, device_model=UCX2, backend=backend)
    assert result.confirmed == []
    assert result.mismatched == [path]


@pytest.mark.parametrize(("tags", "want", "got"), [
    ("f", (1.0,), (math.nan,)), ("f", (math.nan,), (1.0,)),
    ("f", (math.inf,), (math.inf,)), ("i", (1,), (math.inf,)),
    ("i", (1,), (-math.inf,)), ("i", (1,), (1.5,)),
    ("i", (1,), ("1",)), ("f", (1.0,), ("1",)),
])
def test_malformed_report_is_never_equal_or_an_exception(tags, want, got):
    assert not reconcile.matches(tags, want, got)


@pytest.mark.parametrize(("path", "requested", "raw", "reported"), [
    ("/output/1/volume", -10.05, -101, -10.100000381469727),
    ("/output/1/roomeq/delay", .00425, 4, .004000000189989805),
    ("/output/1/roomeq/delay", .425, 425, .42500001192092896),
    ("/echo/width", .375, 38, .3799999952316284),
    ("/input/1/gain", .19, 1, .10000000149011612),
    ("/input/1/gain", 1.29, 12, 1.2000000476837158),
    ("/input/1/eq/band1q", 9.9, 99, 9.90000057220459),
])
def test_the_pinned_c_conversion_has_these_wire_values(path, requested, raw, reported):
    # Literals from the pinned C operations, not derived by the matcher.
    register = register_at(UCX2, path)
    assert numeric.raw_value(requested, register) == raw
    assert numeric.expected_report(requested, register) == reported
    assert reconcile.matches("f", (requested,), (reported,), register=register)


@pytest.mark.parametrize(("path", "value", "text"), [
    ("/output/1/roomeq/delay", .001, "0.001"),
    ("/output/1/roomeq/delay", .123, "0.123"),
    ("/output/1/roomeq/delay", .425, "0.425"),
    ("/echo/width", .37, "0.37"),
    ("/echo/delay", .123, "0.123"),
    ("/input/1/gain", 12.3, "12.3"),
])
def test_export_preserves_reported_units_and_wire_value(tmp_path, path, value, text):
    # Readback noise must not hide a lost decimal place. Check the actual
    # first export, including remembered comments, then re-encode it.
    register = register_at(UCX2, path)
    value = struct.unpack("!f", struct.pack("!f", value))[0]
    assert dump._render_value(value, register) == text
    seen = {path: (value,)}
    config = Config(channels=dump.channels_from_observed(seen, UCX2),
                    globals=dump.globals_from_observed(seen, UCX2))
    rendered = dump.render_config(config, UCX2)
    assert "= " + text in rendered
    # Explicitly adopt the initial values in the remembered comments.
    adopted = "\n".join(line[2:] if line.startswith("# ") and " = " in line
                        else line for line in rendered.splitlines())
    file = tmp_path / "export.conf"
    file.write_text(adopted)
    entry, = reconcile.desired(load_config(file))
    assert entry.path == path
    assert numeric.raw_value(entry.args[0], register) == numeric.raw_value(value, register)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1e100, "nan"])
def test_an_invalid_gain_report_is_not_an_active_config(value):
    warnings = []
    channels = dump.channels_from_observed({"/input/1/gain": (value,)}, UCX2, warnings)
    assert channels == ()
    text = dump.render_config(Config(channels=channels), UCX2, warnings)
    assert "INCOMPLETE EXPORT" in text
    assert "/input/1/gain" in text
    assert not any(line.startswith("gain =") for line in text.splitlines())


def test_snapshot_keeps_sub_tenth_changes_visible(monkeypatch, capsys):
    path = "/output/1/roomeq/delay"
    value = struct.unpack("!f", struct.pack("!f", .001))[0]
    monkeypatch.setattr(reads, "_read_device", lambda _config: {path: (value,)})
    assert reads._snapshot(Config()) == 0
    row = capsys.readouterr().out.splitlines()[-1]
    assert row.startswith(path + " ")
    assert float(row.split()[1]) == value


def test_the_highest_q_report_exports_as_the_highest_q(tmp_path):
    seen = {"/input/1/eq/band1q": (9.90000057220459,)}
    warnings = []
    settings = dump.channels_from_observed(seen, UCX2, warnings)
    assert warnings == []
    assert len(settings) == 1
    text = dump.render_config(Config(channels=settings), UCX2)
    assert "band1q = 9.9" in text


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1.9, "1", 2 ** 31])
def test_invalid_link_reports_do_not_release_either_barrier(monkeypatch, value):
    path = "/output/5/stereo"
    backend = RecordingBackend(reports=lambda _sent: [(path, "i", (value,))])
    assert routing.await_link_echo({path: 1}, 0, .01, backend=backend) is routing.LinkEcho.SILENT
    pending, reapplied, sent = {path: 1}, {"done": False}, []
    monkeypatch.setattr(verify, "send_mix", lambda config: sent.append(config))
    callback = verify._link_sync_observer(Config(), pending, reapplied, lambda: False)
    callback(path, (value,))
    assert pending == {path: 1}
    assert reapplied == {"done": False}
    assert sent == []


@pytest.mark.parametrize(("path", "args"), [
    ("/input/1/mute", (2,)), ("/echo", (-1,)),
    ("/clock/source", (1, "Internal")), ("/clock/source", (99, "Internal")),
    ("/input/1/gain", ()), ("/echo/delay", ()),
])
def test_scalar_export_does_not_invent_valid_values_for_invalid_reports(path, args):
    warnings = []
    seen = {path: args}
    assert dump.channels_from_observed(seen, UCX2, warnings) == ()
    assert dump.globals_from_observed(seen, UCX2, warnings) == ()
    assert len(warnings) == 1
    assert path in warnings[0]


@pytest.mark.parametrize("kind", ["input", "playback"])
def test_a_muted_split_route_writes_digital_zero_on_both_sides(kind):
    route = Route(name="silent", output=(5, 6), level=-65., stereo=False,
                  **{kind: (1, 2)})
    messages = reconcile.mix_messages(route)
    assert messages == [("/mix/5/%s/1" % kind, "fi", (-65., -100)),
                        ("/mix/6/%s/1" % kind, "fi", (-65., 100))]
    for _path, _tags, (level, _pan) in messages:
        # Pinned C: setmix uses zero at <=-65, otherwise powf; the
        # unlinked stereo setlevel divides it by two before setmixlevel.
        amplitude = 0 if level <= -65 else 10 ** (level / 20) / 2
        assert round(amplitude * 0x8000) == 0


@pytest.mark.parametrize("kind", ["input", "playback"])
def test_an_unachievable_boost_on_a_split_pair_is_refused(tmp_path, kind):
    with pytest.raises(ConfigError, match=r"unlinked.*0"):
        config_at(tmp_path, "[route:boost]\n%s = 1/2\noutput = 5/6\n"
                  "stereo = false\nlevel = 1\n" % kind)


def test_muted_split_input_readback_has_no_defined_balance():
    route = Route(name="silent", input=(1, 2), output=(5, 6), level=-65., stereo=False)
    entries = reconcile.desired(Config(routes=(route,)))
    seen = {entry.path: entry.args for entry in entries}
    seen["/mix/5/input/1"] = (-math.inf, 0)
    seen["/mix/6/input/1"] = (-math.inf, 0)
    assert reconcile.plan(entries, seen, UCX2).writes == ()
    seen["/mix/5/input/1"] = (-65., -100)
    assert [write.path for write in reconcile.plan(entries, seen, UCX2).writes] == [
        "/mix/5/input/1"]


@pytest.mark.parametrize(("path", "wanted", "adjacent"), [
    ("/output/1/volume", 0., .10000000149011612),
    ("/echo/width", 0., .009999999776482582),
    ("/echo/delay", 0., .0010000000474974513),
])
def test_even_one_wrong_raw_step_is_drift(path, wanted, adjacent):
    register = register_at(UCX2, path)
    assert reconcile.matches('f', (wanted,), (wanted,), register=register)
    assert not reconcile.matches('f', (wanted,), (adjacent,), register=register)


@pytest.mark.parametrize("value", [-2147483648, 2147483647])
def test_osc_integer_includes_both_endpoints(value):
    assert numeric.integer(value) == value


@pytest.mark.parametrize("value", [-2147483649, 2147483648])
def test_osc_integer_refuses_the_first_values_beyond_its_endpoints(value):
    with pytest.raises(ValueError, match='Int32'):
        numeric.integer(value)


@pytest.mark.parametrize("value", [-32768, 32767])
def test_unbounded_integer_setting_keeps_the_full_signed_storage_range(tmp_path, value):
    config = config_at(tmp_path, '[reverb]\npredelay = %s\n' % value)
    entry, = reconcile.desired(config)
    assert entry.args == (value,)
    assert numeric.raw_value(value, register_at(UCX2, '/reverb/predelay')) == value


def test_positive_half_step_rounds_away_from_zero():
    register = register_at(UCX2, '/output/1/roomeq/delay')
    assert numeric.raw_value(.0005, register) == 1
    assert numeric.expected_report(.0005, register) == .0010000000474974513


def test_measured_mix_tolerance_includes_its_boundary_only():
    register = register_at(UCX2, '/mix/1/input/1')
    assert numeric.equal_float(-20., -20.5, register)
    assert not numeric.equal_float(-20., -20.51, register)
