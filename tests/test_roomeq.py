"""Room EQ: 640 registers on the outputs, and the last family of 0.4.0.

It was blocked for the whole release. `device_ffucxii.c` recombined each
output's Room EQ offset with `|` against a base whose low five bits were
already set, so offsets 16..31 collided and folded onto the lower half:
320 registers reported where 640 exist, every one of them a double
report of another. That is michaelforney/oscmix#32, filed from here with
a measurement, fixed upstream in 55802a6, and declared only after the
pin moved and the fix was measured on the device -- 640 reported.

The generic checks are in `test_sub_families.py`, which now sweeps six
sub-families. This file is what only Room EQ knows.
"""

import pytest

from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.registers import (
    declared_paths,
    nested_families,
    settable_nested,
)


def test_it_is_an_output_family_only():
    """Upstream hangs `roomeq` off the output tree and nowhere else, and
    the recording agrees: 640 paths, all under `/output/`."""
    assert "roomeq" in nested_families(UCX2, "output")
    assert "roomeq" not in nested_families(UCX2, "input")
    paths = [p for p in declared_paths(UCX2) if "roomeq" in p]
    assert len(paths) == 640
    assert all(p.startswith("/output/") for p in paths)


def test_it_has_nine_bands_where_the_channel_eq_has_three():
    import re
    paths = {p for p in declared_paths(UCX2) if "/output/1/roomeq" in p}
    bands = {m.group(1) for m in
             (re.search(r"/band(\d)", p) for p in paths) if m}
    assert bands == set("123456789")
    assert len(paths) == 32            # switch + delay + 9x3 + 3 types


@pytest.mark.parametrize("band", [1, 8, 9])
def test_only_three_bands_have_a_filter_type(band):
    """Band 1 offers a Low shelf and the last two a High shelf -- the
    same odd-one-out that made the channel EQ worth generating from a
    table. Checked on the declared paths, since none of them is
    settable."""
    paths = {p for p in declared_paths(UCX2) if "roomeq" in p}
    assert "/output/5/roomeq/band%dtype" % band in paths


@pytest.mark.parametrize("band", [2, 3, 4, 5, 6, 7])
def test_the_middle_bands_have_no_type(band):
    paths = {p for p in declared_paths(UCX2) if "roomeq" in p}
    assert "/output/5/roomeq/band%dtype" % band not in paths


# --------------------------------------------------------------------------
# Settable since the pin moved to f2fdd5e. The measurement that decided
# it, both times.
# --------------------------------------------------------------------------

def test_every_room_eq_register_carries_a_value_domain():
    """Until `f2fdd5e` every row here was read-only, measured: oscmix
    sent writes to the `0x35D0` block it reads the family from, and the
    UCX II takes Room EQ writes at `0x3400` (upstream #33, fixed
    2026-08-27). Measured again at the new pin the same night:

        /output/1/roomeq/band1gain  -6.0  ->  setreg 3403, reads back -6.0

    where it had always read 0.0. So now the opposite holds: every one
    of the 640 rows carries a domain, and the section surface exists.
    """
    for register in UCX2.registers:
        if "roomeq" in register.template:
            assert register.domain is not None, register.template
    options = settable_nested(UCX2, "roomeq", "output")
    assert len(options) == 32          # switch + delay + 9x3 + 3 types


def test_a_room_eq_section_loads_and_resolves(tmp_path):
    """The refusal this replaces was itself a replacement: `[roomeq:...]`
    was once accepted and silently produced nothing, then refused while
    the device ignored the writes. Now it loads and resolves."""
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n"
                    "[roomeq:output:5]\nband1gain = -6.0\n")
    config = load_config(path)
    assert [(c.family, c.option, c.channel) for c in config.channels] == [
        ("output", "roomeq/band1gain", 5)]


def test_an_unmodelled_device_still_gets_no_opinion(tmp_path):
    """The half that must not change. An empty settable set means "this
    family is read-only" for a device we model, and "no opinion" for one
    we do not -- opposite answers from the same empty dict."""
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Some Other Interface\n\n"
                    "[roomeq:output:5]\nband1gain = -6.0\n")
    assert load_config(path).channels == []
