"""The dynamics family: 320 registers, eight options on 40 channels.

Second nested family of 0.4.0, and the first one added after the shape
existed -- so most of this file checks the *row*, not the machinery.
`[dynamics:input:3]` parses, dumps and round-trips because ADR 0014's
sections are generic; what is new is eight bounds, two wire types and a
read-only meter that must stay out of the config.

The register shapes are checked against `tests/data/refresh-dump.json`.
The bounds cannot be: the recording deliberately stores arrival times
and type tags, never values. They come from upstream's node table at the
pinned revision, quoted below with the scale arithmetic shown, because
`build/` is not in the repository and a test cannot read it.
"""

import json

import pytest
from support import repo_file

from oscmix_desk import dump
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.registers import (
    BOOL,
    ENABLE_OPTION,
    NUMBER,
    declared_paths,
    nested_families,
    register_policy,
    settable_nested,
)

#: upstream `dynamicstree`, oscmix.c at 55802a6. `.min`/`.max` are raw
#: register units; `setfixed` divides the OSC value by `.scale` on the
#: way in, so a config sees min*scale .. max*scale. `setint` has no
#: scale and its bounds are already the config's.
UPSTREAM = {
    #  option:      (set,        scale, raw min, raw max)
    "gain":         ("setfixed", 0.1,   -300,    300),
    "attack":       ("setint",   None,     0,    200),
    "release":      ("setint",   None,   100,    999),
    "compthres":    ("setfixed", 0.1,   -600,      0),
    "compratio":    ("setfixed", 0.1,     10,    100),
    "expthres":     ("setfixed", 0.1,   -990,    200),
    "expratio":     ("setfixed", 0.1,     10,    100),
}


@pytest.fixture(scope="module")
def reported():
    """Path -> type tag, for everything the device reported under dynamics."""
    raw = json.loads(repo_file("tests", "data", "refresh-dump.json").read_text())
    return {path: value[0] for path, value in raw["registers"].items()
            if "/dynamics" in path}


# --------------------------------------------------------------------------
# The row, against the recording.
# --------------------------------------------------------------------------

def test_it_is_declared_for_both_channel_families():
    assert "dynamics" in nested_families(UCX2, "input")
    assert "dynamics" in nested_families(UCX2, "output")


def test_the_declared_paths_are_the_ones_the_device_reports(reported):
    """320 of the 322 reported. The other two are meters -- see below."""
    declared = {p for p in declared_paths(UCX2) if "/dynamics" in p}
    assert declared == {p for p in reported if not p.endswith("/meter")}
    assert len(declared) == 320


def test_the_meter_is_not_a_setting(reported):
    """`{NULL, DYNAMICS_METER, .new=newmeter}` -- reported, no `.set`.

    It is in the recording for the two channels that happened to be
    moving, which is what a streamed register looks like. Declaring it
    would put a level reading in a config file.
    """
    assert [p for p in reported if p.endswith("/meter")]
    assert not [p for p in declared_paths(UCX2) if p.endswith("/dynamics/meter")]
    assert ENABLE_OPTION in settable_nested(UCX2, "dynamics", "input")
    assert "meter" not in settable_nested(UCX2, "dynamics", "input")


@pytest.mark.parametrize("family", ["input", "output"])
def test_the_wire_type_matches_what_the_device_reports(reported, family):
    """The lesson from `band1freq`: a `,f` written to a `setint` register
    is dropped without a word -- parsed, validated, on the wire, device
    unchanged. The tag has to come from the device, not from the value.
    """
    for register in settable_nested(UCX2, "dynamics", family).values():
        path = register.template.format(ch=1)
        assert register.tags == reported[path], (
            "%s declared %r, device reports %r"
            % (path, register.tags, reported[path]))


# --------------------------------------------------------------------------
# The bounds, against upstream -- the one thing a factor of ten hides in.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("option", sorted(UPSTREAM))
def test_the_bounds_are_upstreams_after_the_scale(option):
    _, scale, raw_lo, raw_hi = UPSTREAM[option]
    register = settable_nested(UCX2, "dynamics", "input")[option]
    factor = scale if scale is not None else 1
    assert register.lo == pytest.approx(raw_lo * factor)
    assert register.hi == pytest.approx(raw_hi * factor)


def test_the_scaled_bounds_are_not_the_raw_ones():
    """The failure this guards: declaring -300..300 dB of makeup gain
    because `.scale` was not applied. Every scaled bound would be ten
    times too wide and every out-of-range value would reach the device.
    """
    gain = settable_nested(UCX2, "dynamics", "input")["gain"]
    assert (gain.lo, gain.hi) == (-30.0, 30.0)
    assert gain.unit == "dB"


def test_the_switch_is_a_bool_and_the_rest_are_numbers():
    options = settable_nested(UCX2, "dynamics", "output")
    assert options[ENABLE_OPTION].domain == BOOL
    assert {options[name].domain for name in UPSTREAM} == {NUMBER}


def test_dynamics_is_remembered_rather_than_pinned():
    """ADR 0003's default. A compressor setting is something a person
    reaches for during a session, not installation state like a
    reference level -- so the device's value wins after the first write.
    """
    assert register_policy(UCX2, "/input/3/dynamics/compthres") == "remember"
    assert register_policy(UCX2, "/input/3/dynamics") == "remember"


# --------------------------------------------------------------------------
# The config section, and the round trip. Point 2 and point 3 of the bar.
# --------------------------------------------------------------------------

_HEAD = "[device]\nname = Fireface UCX II\n\n"


def write(tmp_path, text):
    path = tmp_path / "routing.conf"
    path.write_text(_HEAD + text)
    return path


def test_a_section_writes_only_paths_the_model_declares(tmp_path):
    """Point 2 of the bar: written ⊆ declared, the rule the volume bug
    produced."""
    from oscmix_desk import reconcile

    config = load_config(write(tmp_path, "[dynamics:input:3]\n"
                               "enabled = true\ncompthres = -18.0\n"
                               "compratio = 4.0\nattack = 5\n"))
    written = {entry.path for entry in reconcile.desired(config)}
    assert written <= set(declared_paths(UCX2))
    assert "/input/3/dynamics/compthres" in written


def test_the_ratio_is_written_as_a_float_and_the_attack_as_an_int(tmp_path):
    from oscmix_desk import reconcile

    config = load_config(write(tmp_path, "[dynamics:input:3]\n"
                               "compratio = 4.0\nattack = 5\n"))
    by_path = {e.path: e for e in reconcile.desired(config)}
    assert by_path["/input/3/dynamics/compratio"].tags == "f"
    assert by_path["/input/3/dynamics/compratio"].args == (4.0,)
    assert by_path["/input/3/dynamics/attack"].tags == "i"
    assert by_path["/input/3/dynamics/attack"].args == (5,)


@pytest.mark.parametrize(("line", "why"), [
    ("compratio = 12.0", "above upstream's 10.0"),
    ("compthres = 3.0", "above upstream's 0.0"),
    ("attack = 250", "above upstream's 200"),
    ("release = 50", "below upstream's 100"),
    ("gain = -40.0", "below upstream's -30.0"),
])
def test_a_value_outside_the_bounds_is_refused(tmp_path, line, why):
    """And this refusal is the only one there is: oscmix reads `.min`
    and `.max` nowhere at the pinned revision, so an out-of-range value
    that gets past here goes to the register."""
    from oscmix_desk.errors import ConfigError

    with pytest.raises(ConfigError) as raised:
        load_config(write(tmp_path, "[dynamics:input:3]\n%s\n" % line))
    assert "out of range" in str(raised.value), why


def test_the_dump_round_trips_a_pinned_dynamics_section(tmp_path):
    """Point 3 of the bar. Dynamics is REMEMBER, so a dump writes it as
    a comment; pinning it in `[pin]` is what puts it back in the file."""

    seen = {"/input/3/dynamics": (1,),
            "/input/3/dynamics/compthres": (-18.0,),
            "/input/3/dynamics/attack": (5,)}
    config = load_config(write(tmp_path, ""))
    config.channels.extend(dump.channels_from_observed(seen, UCX2))
    text = dump.render_config(config, UCX2)
    assert "[dynamics:input:3]" in text
    assert "# compthres = -18.0" in text
    assert "# enabled = true" in text
