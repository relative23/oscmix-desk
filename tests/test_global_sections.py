"""Families with no channel dimension, and the section that sets them.

The 0.4.0 plan splits its 1466 registers in two: 42 have no channel and
need one new section each, and 1424 are nested per-channel and need a
config-format decision first. This is the first half, proven on `[echo]`
-- seven registers that between them exercise every value type the model
has: a bool, two enums, a bounded quantity, an unbounded one, and a dB
range shared with the faders.

Everything here is checked against `tests/data/refresh-dump.json` or
against upstream's node table, never against what looked plausible.
"""

import json

import pytest
from support import repo_file

from oscmix_desk.config import load_config
from oscmix_desk.devices import device_for_name
from oscmix_desk.model import GlobalSetting
from oscmix_desk.registers import (
    ENABLE_OPTION,
    GLOBAL,
    global_families,
    settable_globals,
)

UCX2 = device_for_name("Fireface UCX II")


@pytest.fixture(scope="module")
def warm():
    return json.loads(repo_file("tests", "data", "refresh-dump.json").read_text())


# --------------------------------------------------------------------------
# The vocabulary comes from the table, not from the parser.
# --------------------------------------------------------------------------

def test_the_section_names_come_from_the_register_table():
    """Derived from the table, so declaring a family is what enables it.

    Sorted, so the assertion reads as a set rather than as an order
    somebody could change by moving a row.
    """
    assert global_families(UCX2) == ("clock", "controlroom", "echo",
                                     "hardware", "reverb")
    assert global_families(None) == ()


def test_the_options_are_the_last_path_segments():
    """Derived, so a row added to the table is settable without a second edit.

    A list kept in the parser is a second place to disagree with the
    device -- the rule this project already applies to channel sections.
    """
    known = settable_globals(UCX2, "echo")
    assert sorted(known) == ["delay", ENABLE_OPTION, "feedback", "highcut",
                             "type", "volume", "width"]
    for option, register in known.items():
        if option == ENABLE_OPTION:
            assert register.template == "/echo"
        else:
            assert register.template == "/echo/" + option


def test_the_enable_option_is_the_only_invented_word():
    """`/echo` is a node with a value *and* a subtree.

    Its switch has no path segment, so the device has no name for it and
    this one is ours. Every other option is the last segment of a real
    path. Asserting that keeps the exception single and visible instead
    of letting a second invented name in quietly.
    """
    invented = [option for option, register in settable_globals(UCX2, "echo").items()
                if not register.template.endswith("/" + option)]
    assert invented == [ENABLE_OPTION]


# --------------------------------------------------------------------------
# Parsing.
# --------------------------------------------------------------------------

def _conf(tmp_path, body):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n" + body)
    return path


def test_a_global_section_parses_into_settings(tmp_path):
    config = load_config(_conf(tmp_path, """
[echo]
enabled = true
type = Pong Echo
delay = 0.35
volume = -12.0
highcut = 8kHz
"""))
    got = {s.option: (s.value, s.path) for s in config.globals}
    assert got["enabled"] == (1, "/echo")
    assert got["type"] == ("Pong Echo", "/echo/type")
    assert got["delay"] == (0.35, "/echo/delay")
    assert got["volume"] == (-12.0, "/echo/volume")
    assert got["highcut"] == ("8kHz", "/echo/highcut")


@pytest.mark.parametrize(("body", "why"), [
    ("[echo]\nshimmer = 1\n", "no such option"),
    ("[echo]\ndelay = 5.0\n", "outside the declared 0..2 s"),
    ("[echo]\ntype = Hall\n", "not one of the device's names"),
    ("[echo]\nenabled = perhaps\n", "not a boolean"),
])
def test_a_bad_global_value_is_refused(tmp_path, body, why):
    from oscmix_desk import ConfigError

    with pytest.raises(ConfigError):
        load_config(_conf(tmp_path, body))


def test_an_unbounded_option_takes_what_upstream_takes(tmp_path):
    """`feedback` is `setint` with no min or max in oscmix.c.

    So the model declares none, and a config may say what the device
    would accept. Inventing 0..100 here would look tidy and reject
    values that work.
    """
    config = load_config(_conf(tmp_path, "[echo]\nfeedback = 250\n"))
    assert [(s.option, s.value) for s in config.globals] == [("feedback", 250.0)]


# --------------------------------------------------------------------------
# And it has to reach the wire.
# --------------------------------------------------------------------------

def test_every_global_setting_becomes_a_register_to_write(tmp_path):
    """The guard this release has needed three times.

    Parsed, validated, shown in --dry-run and never sent is the shape of
    two defects already fixed here. A new section is exactly where it
    happens again.
    """
    from oscmix_desk.reconcile import desired

    config = load_config(_conf(tmp_path, """
[echo]
enabled = true
type = Pong Echo
delay = 0.35
"""))
    paths = {e.path for e in desired(config)}
    assert {"/echo", "/echo/type", "/echo/delay"} <= paths


def test_an_enum_goes_out_as_its_index(tmp_path):
    """Same rule as channel sections, and now the same code.

    Upstream takes an int for some enums and either for others; writing
    a name where only an int is read is ignored silently, since a write
    draws no reply. One encoder for both section kinds is what keeps
    that rule in one place.
    """
    from oscmix_desk.reconcile import desired

    config = load_config(_conf(tmp_path, "[echo]\ntype = Pong Echo\n"))
    entry, = [e for e in desired(config) if e.path == "/echo/type"]
    assert (entry.tags, entry.args) == ("i", (2,))


def test_global_settings_are_read_back_like_any_other(tmp_path):
    from oscmix_desk.verify import expected_registers

    config = load_config(_conf(tmp_path, "[echo]\nvolume = -12.0\n"))
    assert "/echo/volume" in expected_registers(config)


# --------------------------------------------------------------------------
# Against the recording.
# --------------------------------------------------------------------------

def test_the_declared_echo_registers_are_all_in_the_dump(warm):
    declared = {r.template for r in UCX2.registers if r.channels == GLOBAL}
    assert declared, "no global registers declared"
    assert sorted(declared - set(warm["registers"])) == []


def test_nothing_in_the_dump_is_left_undeclared(warm):
    declared = {r.template for r in UCX2.registers}
    reported = {p for p in warm["registers"] if p.startswith("/echo")}
    assert sorted(reported - declared) == []


def test_a_global_setting_knows_its_own_path():
    assert GlobalSetting("echo", ENABLE_OPTION, 1).path == "/echo"
    assert GlobalSetting("echo", "delay", 0.5).path == "/echo/delay"


# --------------------------------------------------------------------------
# The two families that followed, and what each of them settled.
# --------------------------------------------------------------------------

def test_reverb_declares_no_bounds_because_upstream_declares_none():
    """The trap this family sets, and the reason to read the source.

    `/echo/volume` is `.scale=0.1, .min=-650, .max=60`. `/reverb/volume`
    is `.scale=0.1` with **no** min or max. The two look like the same
    control and are not, and copying the echo's -65..+6 onto the reverb
    would have looked consistent while rejecting values the device takes.

    Every reverb number is unbounded upstream. Ten of the twelve are
    unbounded here too, and the two exceptions were not reasoned into
    existence -- `width` and `smooth` were bracketed against the device
    after the write sweep found them silently refusing out-of-range
    values. The rule stands for everything nobody measured.
    """
    from oscmix_desk.registers import ENUM, NUMBER

    numbers = [r for r in UCX2.registers
               if r.template.startswith("/reverb/") and r.domain == NUMBER]
    assert len(numbers) == 12
    measured = {"/reverb/width", "/reverb/smooth"}
    for register in numbers:
        if register.template in measured:
            continue
        assert register.lo is None, register.template
        assert register.hi is None, register.template
    assert {r.template for r in numbers} >= measured
    kind, = [r for r in UCX2.registers if r.template == "/reverb/type"]
    assert kind.domain == ENUM
    assert len(kind.choices) == 15


def test_the_control_room_reductions_cannot_go_above_unity():
    """`dimreduction` and `recallvolume` are `.min=-650, .max=0`.

    Not the fader range: a *reduction* that could be positive would be a
    boost, and the device does not offer one. The upper bound is the
    part worth asserting, because -65 alone would look like a fader and
    read as one.
    """
    from oscmix_desk.constants import LEVEL_MIN

    by_path = {r.template: r for r in UCX2.registers}
    for path in ("/controlroom/dimreduction", "/controlroom/recallvolume"):
        assert (by_path[path].lo, by_path[path].hi) == (LEVEL_MIN, 0.0), path
        assert by_path[path].unit == "dB"


def test_a_reduction_above_zero_is_refused(tmp_path):
    from oscmix_desk import ConfigError

    with pytest.raises(ConfigError, match=r"out of range -65\.0\.\.0\.0"):
        load_config(_conf(tmp_path, "[controlroom]\ndimreduction = 3.0\n"))


def test_the_control_room_splits_setup_from_buttons():
    """ADR 0012 applied to a family where both kinds sit together.

    How far DIM reduces, what RECALL returns to and which pair the
    section drives are set once for a room. DIM, MONO and mute-enable
    are buttons somebody presses while working, and a session that put
    them back would be arguing with the person at the desk.
    """
    from oscmix_desk.registers import PIN, REMEMBER, register_policy

    for path in ("/controlroom/mainout", "/controlroom/dimreduction",
                 "/controlroom/recallvolume"):
        assert register_policy(UCX2, path) == PIN, path
    for path in ("/controlroom/dim", "/controlroom/mainmono",
                 "/controlroom/muteenable"):
        assert register_policy(UCX2, path) == REMEMBER, path


def test_mainout_declares_the_eleventh_name_the_new_pin_gives_it(warm):
    """This test used to assert the opposite, and the pin move is why.

    The device reports -1 for "no main out". At 2411b12 `oscsendenum`
    had no value list, so -1 matched no name and arrived unnamed -- that
    was upstream #30. e8151cd gave enums an optional value list and
    `mainout` a "None" option, and the pin now sits on 55802a6, so the
    device reports `('is', (-1, 'None'))`.

    The old docstring named the trap this walks into: index 10 is not
    -1, and upstream's `setenum` reads a `,i` argument as the raw value
    rather than as a position. That is what `values` is for, and the
    next test is the one that would fail without it.
    """
    by_path = {r.template: r for r in UCX2.registers}
    choices = by_path["/controlroom/mainout"].choices
    assert choices[0] == "1/2"
    assert choices[-1] == "None"
    assert len(choices) == 11


def test_selecting_no_main_out_writes_minus_one_and_not_ten():
    """The whole reason a register may declare enum *values*.

    `setenum` takes a `,i` argument as the value, not the index. "None"
    sits at position 10 and means -1; written positionally it would set
    the register to 10, which is not a main out and not "none" either --
    accepted, on the wire, device wrong.
    """
    from oscmix_desk.reconcile import _enum_value

    by_path = {r.template: r for r in UCX2.registers}
    mainout = by_path["/controlroom/mainout"]
    assert mainout.values == (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, -1)
    assert _enum_value(mainout, "None") == -1
    assert _enum_value(mainout, "1/2") == 0
    assert _enum_value(mainout, "19/20") == 9


def test_every_other_enum_leaves_its_values_empty():
    """Position and value agree everywhere else, and saying so twice is
    a place for them to disagree."""
    for register in UCX2.registers:
        if register.template == "/controlroom/mainout":
            continue
        assert register.values == (), register.template


def test_the_three_families_are_complete_against_the_recording(warm):
    """Nothing reported and left undeclared, for any family declared.

    Checked per family rather than in total: a family half-declared is
    worse than one not declared at all, because `--dump-config` emits
    the half it knows and the file then reads as though the device had
    no reverb settings.
    """
    declared = {r.template for r in UCX2.registers}
    for family in global_families(UCX2):
        reported = {p for p in warm["registers"]
                    if p == "/" + family or p.startswith("/%s/" % family)}
        assert reported, family
        assert sorted(reported - declared) == [], family


# --------------------------------------------------------------------------
# clock and hardware: where "settable" stopped being a judgement call.
# --------------------------------------------------------------------------

def test_all_forty_two_global_registers_are_declared():
    """The global half of 0.4.0, complete.

    Counted so that "declared" and "measured" cannot drift: the plan
    sized this half at 42 from `tests/data/refresh-dump.json`, and the
    table now carries the same number.
    """
    assert len([r for r in UCX2.registers if r.channels == GLOBAL]) == 42
    assert global_families(UCX2) == ("clock", "controlroom", "echo",
                                     "hardware", "reverb")


@pytest.mark.parametrize("path", [
    "/clock/samplerate", "/hardware/ccmode",
    "/hardware/dspload", "/hardware/dspvers",
])
def test_a_register_upstream_cannot_write_is_not_settable(path):
    """`domain is None` is decided by oscmix.c, not by taste.

    `samplerate` is `{"samplerate", CLOCK_SAMPLERATE, .new=newsamplerate}`
    -- a reporter with no `.set`. `ccmode` is `.new=newbool` with no
    setter, and `dspload`/`dspvers` come from nameless nodes that only
    report. A config cannot set what oscmix cannot write, and saying so
    in the table beats discovering it as a write that draws no reply.

    This also settles the roadmap's open question about the sample rate:
    it is not "state or event", it is not writable at all.
    """
    by_path = {r.template: r for r in UCX2.registers}
    assert by_path[path].domain is None


def test_the_read_only_registers_are_absent_from_the_sections():
    """Declared for reading, unreachable from a config -- the `48v` shape.

    The register model carries them so the read-back and `--dump-config`
    know they exist; `settable_globals` leaves them out so no section
    can name them.
    """
    assert "samplerate" not in settable_globals(UCX2, "clock")
    for absent in ("ccmode", "dspload", "dspvers"):
        assert absent not in settable_globals(UCX2, "hardware"), absent
    assert sorted(settable_globals(UCX2, "clock")) == [
        "source", "wckout", "wcksingle", "wckterm"]


def test_a_config_cannot_name_a_read_only_register(tmp_path):
    from oscmix_desk import ConfigError

    with pytest.raises(ConfigError, match="unknown option"):
        load_config(_conf(tmp_path, "[clock]\nsamplerate = 44100\n"))


def test_the_box_and_the_clock_are_pinned():
    """Neither family holds anything a person dials during a session.

    Which clock a room runs on, whether the word clock output is
    terminated, what the optical port carries and what the box does with
    no computer attached are all installation, in ADR 0012's sense.
    """
    from oscmix_desk.registers import PIN, register_policy

    for register in UCX2.registers:
        if register.channels != GLOBAL or register.domain is None:
            continue
        if register.template.startswith(("/clock/", "/hardware/")):
            assert register_policy(UCX2, register.template) == PIN, \
                register.template


def test_the_three_registers_whose_bounds_had_to_be_measured():
    """Upstream declares no range for these; the device enforces one.

    The standing rule is that a range invented here would reject values
    the device accepts, so an unbounded register in upstream's node
    table stays unbounded in the model. These three are the measured
    exception, and the reason is the direction of the failure: the
    device *rejects* an out-of-range write outright rather than clamping
    it. `/reverb/width = 1.02` leaves the register at its old value and
    reports nothing at all, so a config asking for it would be silently
    ignored -- which is the failure mode this project exists to prevent.

    Bracketed on a UCX II, 2026-08-25: 0.0 and 1.0 accepted on both
    width registers, -0.01 and 1.02 refused; 0 and 100 accepted on
    smooth, -1 and 101 refused.
    """
    from oscmix_desk.devices import UCX2
    from oscmix_desk.registers import register_at

    for path in ("/echo/width", "/reverb/width"):
        register = register_at(UCX2, path)
        assert (register.lo, register.hi) == (0.0, 1.0), path
    smooth = register_at(UCX2, "/reverb/smooth")
    assert (smooth.lo, smooth.hi) == (0.0, 100.0)


def test_reverb_volume_stays_unbounded():
    """The rule the three exceptions above did not repeal.

    `/reverb/volume` has no bound upstream and none was measured, so it
    keeps none. An `hi` copied from `/echo/volume` because the two look
    alike would reject values this device takes.
    """
    from oscmix_desk.devices import UCX2
    from oscmix_desk.registers import register_at

    assert register_at(UCX2, "/reverb/volume").hi is None
