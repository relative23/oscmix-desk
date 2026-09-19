"""The three-band EQ, and the first section written the ADR 0014 way.

480 registers, the largest family in 0.4.0 and the first one nested:
`[eq:input:3]` rather than a dotted option inside `[input:3]`, because
an installed 0.3.0 refuses the whole file over the latter.

Two things here are not about EQ at all. The wire type is taken from the
register's declared tag rather than from the Python value, and a
sub-family's own switch is kept out of the flat section. Both were found
while adding this family, and both were silent.
"""

import pytest
from conftest import repo_file

from oscmix_desk import ConfigError
from oscmix_desk.config import load_config
from oscmix_desk.devices import device_for_name
from oscmix_desk.reconcile import desired
from oscmix_desk.registers import (
    ENABLE_OPTION,
    nested_families,
    settable_nested,
    settable_options,
)

UCX2 = device_for_name("Fireface UCX II")


def _conf(tmp_path, body):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n" + body)
    return path


# --------------------------------------------------------------------------
# The table.
# --------------------------------------------------------------------------

def test_the_eq_is_declared_for_both_families():
    from oscmix_desk.registers import declared_paths

    # Membership rather than equality: the tuple grows with every nested
    # family, and pinning it here would make this EQ test fail for
    # reasons that have nothing to do with EQ.
    assert "eq" in nested_families(UCX2, "input")
    assert "eq" in nested_families(UCX2, "output")
    assert len([p for p in declared_paths(UCX2) if "/eq" in p]) == 480


def test_band_one_and_band_three_offer_different_filter_types():
    """The row that made this family worth generating from a table.

    Band 1 has a **Low** Shelf, band 3 a **High** Shelf. Everything else
    about the two bands is identical, which is the condition under which
    hand-copying gets the odd one out wrong -- and a wrong enum name is
    refused by the config rather than silently mis-set, so it would show
    up as "the device does not support this" on somebody's desk.
    """
    by_path = {r.template: r for r in UCX2.registers}
    band1 = by_path["/input/{ch}/eq/band1type"].choices
    band3 = by_path["/input/{ch}/eq/band3type"].choices
    assert "Low Shelf" in band1
    assert "Low Shelf" not in band3
    assert "High Shelf" in band3
    assert "High Shelf" not in band1


def test_the_bounds_are_upstreams():
    """`eqtree` in oscmix.c: freq 20..20000, gain .scale=0.1 min/max
    -200..200, q .scale=0.1 min/max 4..99."""
    by_path = {r.template: r for r in UCX2.registers}
    freq = by_path["/input/{ch}/eq/band2freq"]
    assert (freq.lo, freq.hi, freq.unit) == (20.0, 20000.0, "Hz")
    gain = by_path["/input/{ch}/eq/band2gain"]
    assert (gain.lo, gain.hi, gain.unit) == (-20.0, 20.0, "dB")
    quality = by_path["/input/{ch}/eq/band2q"]
    assert (quality.lo, quality.hi) == (0.4, 9.9)


# --------------------------------------------------------------------------
# The nested option must not leak into the flat section (ADR 0014).
# --------------------------------------------------------------------------

def test_a_flat_section_offers_neither_nested_options_nor_the_switch():
    """Both halves, and the second one is the trap.

    `eq/band1freq` is obviously nested. `/input/{ch}/eq` -- the family's
    own switch -- is flat by path shape, so it would land in
    `[input:3]` as `eq = true` unless something notices it has children.
    Either one in `[input:3]` is a file an installed 0.3.0 refuses whole.
    """
    flat = settable_options(UCX2, "input")
    assert sorted(flat) == ["gain", "hi-z", "mute", "phase", "reflevel"]
    assert not [key for key in flat if "/" in key]
    assert "eq" not in flat
    assert ENABLE_OPTION in settable_nested(UCX2, "eq", "input")


@pytest.mark.parametrize("body", [
    "[input:3]\neq.band1freq = 80\n",
    "[input:3]\neq/band1freq = 80\n",
    "[input:3]\neq = true\n",
])
def test_the_flat_section_refuses_them(tmp_path, body):
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(_conf(tmp_path, body))


# --------------------------------------------------------------------------
# Parsing and the wire.
# --------------------------------------------------------------------------

def test_a_nested_section_parses_into_channel_settings(tmp_path):
    config = load_config(_conf(tmp_path, """
[eq:input:3]
enabled = true
band1freq = 120
band1gain = -4.5
band1type = Low Shelf
"""))
    got = {(s.family, s.channel, s.option): s.value for s in config.channels}
    assert got[("input", 3, "eq")] == 1
    assert got[("input", 3, "eq/band1freq")] == 120.0
    assert got[("input", 3, "eq/band1type")] == "Low Shelf"


def test_every_nested_setting_reaches_the_wire(tmp_path):
    """Fourth instance of this shape in the release, first caught by looking.

    `channel_entries` looked options up in `settable_options`, which by
    then deliberately excluded nested ones -- so `[eq:input:3]` parsed,
    validated, appeared in the config, and produced no entry at all.
    Every EQ setting was dropped between the file and the device.
    """
    config = load_config(_conf(tmp_path, """
[eq:input:3]
enabled = true
band1freq = 120
band1gain = -4.5
"""))
    paths = {e.path for e in desired(config)}
    assert {"/input/3/eq", "/input/3/eq/band1freq",
            "/input/3/eq/band1gain"} <= paths


def test_the_wire_type_comes_from_the_declared_tag_not_the_value(tmp_path):
    """The silent one, and the reason to read `oscgetint`.

    `band1freq` is `setint` upstream. A float argument makes
    `oscgetint` set "incorrect argument type", `setint` returns without
    writing, and a write draws no reply to notice it by. Every value
    here parses to a Python float, so encoding by value type would send
    `,f` to an int register: parsed, validated, on the wire, and the
    device unchanged.
    """
    config = load_config(_conf(tmp_path,
                               "[eq:input:3]\nband1freq = 120\n"
                               "band1gain = -4.5\nband1type = Peak\n"))
    tags = {e.path: (e.tags, e.args) for e in desired(config)}
    assert tags["/input/3/eq/band1freq"] == ("i", (120,))
    assert tags["/input/3/eq/band1gain"] == ("f", (-4.5,))
    assert tags["/input/3/eq/band1type"] == ("i", (0,))


def test_no_register_is_written_in_a_type_the_device_will_not_read():
    """The general form, over every settable register in the model."""
    from oscmix_desk.reconcile import _encode
    from oscmix_desk.registers import ENUM

    for register in UCX2.registers:
        if register.domain is None:
            continue
        value = register.choices[0] if register.domain == ENUM else 1.0
        entry = _encode("/x", register, value)
        if register.domain == ENUM:
            assert entry.tags == "i", register.template
        else:
            assert entry.tags == register.tags[0], register.template


# --------------------------------------------------------------------------
# Rejections.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("body", "why"), [
    ("[eq:input:3]\nband3type = Low Shelf\n", "Low Shelf is band 1 only"),
    ("[eq:input:3]\nband1freq = 30000\n", "above 20 kHz"),
    ("[eq:input:3]\nband1q = 12.0\n", "above 9.9"),
    ("[eq:input:99]\nband1freq = 80\n", "no such channel"),
    ("[eq:input:3]\nband4gain = 0.0\n", "no fourth band"),
])
def test_a_bad_nested_value_is_refused(tmp_path, body, why):
    with pytest.raises(ConfigError):
        load_config(_conf(tmp_path, body))


def test_the_declared_eq_registers_are_all_in_the_recording():
    import json

    warm = json.loads(repo_file("tests", "data",
                                "refresh-dump.json").read_text())
    from oscmix_desk.registers import declared_paths

    declared = {p for p in declared_paths(UCX2) if "/eq" in p}
    assert sorted(declared - set(warm["registers"])) == []
    reported = {p for p in warm["registers"] if "/eq/" in p or p.endswith("/eq")}
    assert sorted(reported - declared) == []


def test_a_childless_option_is_not_a_sub_family():
    """Found by reading a mutation survivor that was right to survive.

    Dropping the flat lookup in `_register_for` (since removed as
    dead code) changed nothing, because
    the nested fallback answered instead: `settable_nested(device,
    "gain", "input")` returned the *gain* register under
    `ENABLE_OPTION`, its template matching the prefix exactly. The
    behaviour was equivalent and the looseness was real -- only
    `_is_nested_section` stopped `[gain:input:3]` being accepted, which
    is two places having to agree about one fact.

    `settable_nested` now answers for sub-families only, so the two
    cannot drift apart.
    """
    assert settable_nested(UCX2, "gain", "input") == {}
    assert settable_nested(UCX2, "reflevel", "output") == {}
    assert len(settable_nested(UCX2, "eq", "input")) == 12


def test_a_childless_option_cannot_be_written_as_a_section(tmp_path, caplog):
    """`[gain:input:3]` is not a thing, and has to warn rather than parse."""
    with caplog.at_level("WARNING"):
        config = load_config(_conf(tmp_path, "[gain:input:3]\nenabled = 12\n"))
    assert config.channels == []
    assert any("ignoring unknown section" in record.getMessage()
               for record in caplog.records)
