"""Independent matrix observations that a route cannot faithfully describe."""

import math

import pytest

from oscmix_desk import dump, reads
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


def test_missing_mono_links_cannot_be_silently_assumed():
    warnings = []
    assert dump.routes_from_observed({"/mix/5/input/1": (-6., 0)}, UCX2, warnings) == ()
    assert "missing" in warnings[0]


def test_command_keeps_the_omissions_next_to_the_export(monkeypatch, capsys):
    seen = dict(stereo(), **{"/mix/5/input/1": (-6., 50)})
    monkeypatch.setattr(reads, "_read_device", lambda _config: seen)
    assert reads._dump_config(Config()) == 0
    text = capsys.readouterr().out
    assert "INCOMPLETE EXPORT" in text
    assert "/mix/5/input/1" in text
    assert "[route:" not in text
    assert "Merge, do not replace" in text
    assert "prove that direct monitoring is absent" in text
