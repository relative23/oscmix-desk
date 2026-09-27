"""Independent matrix observations that a route cannot faithfully describe."""

import math

import pytest

from oscmix_desk import dump, reads
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.model import Config


def stereo():
    return {"/input/1/stereo": (1,), "/output/5/stereo": (1,),
            "/mix/5/input/1": (-6.0, 0)}


@pytest.mark.parametrize("change", [
    {"/mix/5/input/1": (-6., 50)},
    {"/mix/5/input/1": (-6.,)},
    {"/mix/5/input/1": (-6., 0.5)},
    {"/mix/5/input/1": (math.nan, 0)},
    {"/mix/5/input/1": (math.inf, 0)},
    {"/mix/5/input/1": (7., 0)},
    {"/input/1/stereo": ()},
    {"/input/1/stereo": (0,)},
    {"/input/1/stereo": (math.nan,)},
    {"/input/2/stereo": (0,)},
    {"/output/5/stereo": ()},
    {"/output/6/stereo": (0,)},
])
def test_unrepresentable_or_incomplete_stereo_is_omitted_with_reason(change):
    warnings = []
    assert dump.routes_from_observed(dict(stereo(), **change), UCX2, warnings) == ()
    assert len(warnings) == 1
    assert "/mix/5/input/1" in warnings[0]


@pytest.mark.parametrize(("out", "src"), [(0, 1), (21, 1), (5, 21), (5, 0), (6, 1), (5, 2)])
def test_unknown_channel_or_even_stereo_anchor_is_not_invented(out, src):
    seen = stereo()
    del seen["/mix/5/input/1"]
    seen["/mix/%d/input/%d" % (out, src)] = (-6., 0)
    warnings = []
    assert dump.routes_from_observed(seen, UCX2, warnings) == ()
    assert warnings


@pytest.mark.parametrize(("left", "right"), [
    ((0., -100), (0., -100)), ((0., 100), (0., -100)),
    ((0., -100), (-3., 100)), ((0., -100), (0., 50)),
    ((-64., -100), (-64., 100)),
])
def test_split_requires_both_hard_pans_equal_levels_and_representable_gain(left, right):
    seen = {"/input/1/stereo": (1,), "/output/5/stereo": (0,),
            "/mix/5/input/1": left, "/mix/6/input/1": right}
    warnings = []
    assert dump.routes_from_observed(seen, UCX2, warnings) == ()
    assert len(warnings) == 2


@pytest.mark.parametrize(('left', 'right'), [(6.04, 6.04), (0., .25)])
def test_split_does_not_round_invalid_or_different_reported_levels_into_a_route(left, right):
    seen = {'/input/1/stereo': (1,), '/output/5/stereo': (0,),
            '/mix/5/input/1': (left, -100), '/mix/6/input/1': (right, 100)}
    warnings = []
    assert dump.routes_from_observed(seen, UCX2, warnings) == ()
    assert len(warnings) == 2


@pytest.mark.parametrize(('source', 'output', 'level', 'expected'), [
    (1, 1, -12.26, 'in1-2-out1-2'),
    (19, 19, -12.26, 'in19-20-out19-20'),
])
def test_first_and_last_linked_pairs_keep_their_names_and_report_precision(
        source, output, level, expected):
    seen = {'/input/%d/stereo' % source: (1,),
            '/input/%d/stereo' % (source + 1): (1,),
            '/output/%d/stereo' % output: (1,),
            '/output/%d/stereo' % (output + 1): (1,),
            '/mix/%d/input/%d' % (output, source): (level, 0)}
    warnings = []
    route, = dump.routes_from_observed(seen, UCX2, warnings)
    assert (route.name, route.input, route.output, route.level, route.stereo) == (
        expected, (source, source + 1), (output, output + 1), -12.3, True)
    assert warnings == []


def test_zero_db_split_consumes_its_partner_without_an_incomplete_export_warning():
    # Float32 report close to the known 6.0206 dB split compensation.
    seen = {'/input/1/stereo': (1,), '/output/5/stereo': (0,),
            '/mix/5/input/1': (6.020600318908691, -100),
            '/mix/6/input/1': (6.020600318908691, 100)}
    warnings = []
    route, = dump.routes_from_observed(seen, UCX2, warnings)
    assert (route.name, route.input, route.output, route.level, route.stereo) == (
        'in1-2-out5-6-split', (1, 2), (5, 6), 0., False)
    assert warnings == []


def test_muted_boundary_cell_does_not_hide_a_later_mono_route_at_maximum_gain():
    seen = {'/input/1/stereo': (0,), '/output/1/stereo': (0,),
            '/output/5/stereo': (0,), '/mix/1/input/1': (-65., 0),
            '/mix/6/input/2': (6., 0)}
    warnings = []
    route, = dump.routes_from_observed(seen, UCX2, warnings)
    assert (route.name, route.input, route.output, route.level) == (
        'in2-out6', (2,), (6,), 6.)
    assert warnings == []


def test_missing_mono_links_cannot_be_silently_assumed():
    warnings = []
    assert dump.routes_from_observed({"/mix/5/input/1": (-6., 0)}, UCX2, warnings) == ()
    assert "missing" in warnings[0]


@pytest.mark.parametrize(('out', 'src'), [(0, 1), (21, 1), (5, 0), (5, 21)])
def test_unlinked_reports_do_not_make_unknown_matrix_channels_exportable(out, src):
    seen = {f'/output/{out - (out - 1) % 2}/stereo': (0,),
            f'/input/{src - (src - 1) % 2}/stereo': (0,),
            f'/mix/{out}/input/{src}': (-6.0, 0)}
    warnings = []
    assert dump.routes_from_observed(seen, UCX2, warnings) == ()
    assert len(warnings) == 1
    assert 'outside the known device map' in warnings[0]


def test_unknown_device_map_cannot_supply_a_route():
    warnings = []
    assert dump.routes_from_observed(stereo(), None, warnings) == ()
    assert len(warnings) == 1
    assert 'outside the known device map' in warnings[0]


def test_one_invalid_route_does_not_hide_a_later_independent_valid_route():
    seen = {'/mix/1/input/1': (-6.0, 0), **stereo()}
    warnings = []
    route, = dump.routes_from_observed(seen, UCX2, warnings)
    assert (route.input, route.output, route.level) == ((1, 2), (5, 6), -6.0)
    assert len(warnings) == 1
    assert '/mix/1/input/1' in warnings[0]


@pytest.mark.parametrize('path', ['/notmix/5/input/1', '/mix/5/notinput/1',
                                  '/mix/5/input/1/extra'])
def test_other_message_shapes_are_not_reconstructed_as_matrix_cells(path):
    seen = stereo()
    del seen['/mix/5/input/1']
    seen[path] = (-6.0, 0)
    warnings = []
    assert dump.routes_from_observed(seen, UCX2, warnings) == ()
    assert warnings == []


def test_command_keeps_the_omissions_next_to_the_export(monkeypatch, capsys, tmp_path):
    seen = dict(stereo(), **{"/mix/5/input/1": (-6., 50)})
    monkeypatch.setattr(reads, "_read_device",
                        lambda _config, _path: reads.DeviceRead(seen, "00000000", "epoch"))
    assert reads._dump_config(Config()) == 0
    text = capsys.readouterr().out
    assert "INCOMPLETE EXPORT" in text
    assert "/mix/5/input/1" in text
    assert "[route:" not in text
    assert "Merge, do not replace" in text
    assert "prove that direct monitoring is absent" in text
    exported = tmp_path / 'incomplete.conf'
    exported.write_text(text)
    assert load_config(exported).routes == ()
