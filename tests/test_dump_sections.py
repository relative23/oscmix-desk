"""`--dump-config` for the sections 0.4.0 added.

Before this, a dump of a fully configured device emitted `[device]`,
`[osc]` and forty flat channel sections -- 124 settings out of the 604
the device reports. Every global register and all 480 EQ registers fell
out silently, so the round trip that point 3 of the roadmap's bar asks
for was open for everything 0.4.0 had just declared.

The round trip is the test, as it is for the input matrix: render, parse
what was rendered, render that, and require the two texts to be equal.
It earned its place immediately -- see the `mainout` case at the bottom,
where a dump of a working device produced a file that would not load.

Values here are synthesised from the register table rather than taken
from `tests/data/refresh-dump.json`, which records register shape and
arrival times but deliberately not values (they are the user's mixer
state). The shapes still come from the device; only the numbers are ours.
"""

import pytest

from oscmix_desk import reconcile
from oscmix_desk.config import load_config
from oscmix_desk.devices import device_for_name
from oscmix_desk.model import Config
from oscmix_desk.registers import (
    BOOL,
    ENUM,
    global_families,
    nested_families,
    register_policy,
    settable_globals,
    settable_nested,
    settable_options,
)

UCX2 = device_for_name("Fireface UCX II")


def a_value_for(register):
    """A value the device could plausibly report for this register.

    Taken from the register's own declared domain, so it stays valid if
    the table changes -- a hand-written constant would rot into a test
    that passes against a domain no longer declared.
    """
    if register.domain == ENUM:
        return (register.choices.index(register.choices[0]), register.choices[0])
    if register.domain == BOOL:
        return (1,)
    if register.tags.startswith("f"):
        return (float(register.lo if register.lo is not None else 0.0),)
    return (int(register.lo if register.lo is not None else 0),)


@pytest.fixture(scope="module")
def seen():
    """What a device with every declared register set would report."""
    reported = {}
    for family in global_families(UCX2):
        for register in settable_globals(UCX2, family).values():
            reported[register.template] = a_value_for(register)
    for family in ("input", "output"):
        for sub in nested_families(UCX2, family):
            for register in settable_nested(UCX2, sub, family).values():
                for channel in (1, 3):
                    reported[register.template.format(ch=channel)] = \
                        a_value_for(register)
    # One flat pinned option as well: the file has to hold both section
    # shapes at once, because that is what a real dump looks like and
    # the two are rendered by different code paths.
    for channel in (1, 3):
        register = settable_options(UCX2, "input")["gain"]
        reported[register.template.format(ch=channel)] = a_value_for(register)
    return reported


def dumped(seen, device=UCX2):
    """`seen` as the file `--dump-config` would print."""
    config = Config(device_name="Fireface UCX II",
                    channels=list(reconcile.channels_from_observed(seen, device)),
                    globals=list(reconcile.globals_from_observed(seen, device)))
    return reconcile.render_config(config, device)


# --------------------------------------------------------------------------
# What the dump now carries.
# --------------------------------------------------------------------------

def test_every_settable_global_register_is_recovered(seen):
    """The gap this closes: 42 registers reported, none of them written."""
    recovered = reconcile.globals_from_observed(seen, UCX2)
    expected = {register.template
                for family in global_families(UCX2)
                for register in settable_globals(UCX2, family).values()}
    assert {s.path for s in recovered} == expected
    assert len(expected) == 38          # 42 declared, 4 of them read-only


def test_a_nested_option_is_recovered_under_its_sub_family(seen):
    """`band1gain` comes back keyed `eq/band1gain`, not `band1gain`.

    The key is what tells the renderer which section the setting belongs
    in. Flattened, it would land in `[input:3]` and the file would set a
    register that section has no option for.
    """
    recovered = reconcile.channels_from_observed(seen, UCX2)
    keys = {s.option for s in recovered if s.family == "input" and s.channel == 3}
    assert "eq/band1gain" in keys


def test_each_family_gets_its_own_section(seen):
    text = dumped(seen)
    for family in global_families(UCX2):
        assert "[%s]" % family in text


def test_a_nested_section_is_headed_by_sub_family_and_channel(seen):
    """ADR 0014: `[eq:input:3]`, not options folded into `[input:3]`."""
    text = dumped(seen)
    assert "[eq:input:3]" in text
    assert "[eq:input:1]" in text


def test_the_sub_family_switch_is_written_as_enabled(seen):
    """`/input/3/eq` is the section's own switch, and has no option name
    of its own -- it is spelled `enabled` inside `[eq:input:3]`."""
    section = section_of(dumped(seen), "[eq:input:3]")
    assert any(line.lstrip("# ").startswith("enabled =") for line in section)


def section_of(text, header):
    """The lines of one section, header excluded."""
    lines = text.splitlines()
    start = lines.index(header) + 1
    end = next((i for i in range(start, len(lines))
                if lines[i].startswith("[")), len(lines))
    return [line for line in lines[start:end] if line.strip()]


# --------------------------------------------------------------------------
# Point 3 of the bar: the round trip is a fixed point.
# --------------------------------------------------------------------------

def test_every_setting_survives_a_dump_and_a_reload(seen, tmp_path):
    """Render, read back what was rendered, render again: same settings.

    Anything the parser drops, renames, reorders or spells differently
    shows up here, which is the only cheap way to catch a dumper that
    quietly disagrees with the file format it writes. It found two:
    `mainout = -1` and `wckout = true` coming back as `1`.
    """
    first = dumped(seen)
    second = reconcile.render_config(reloaded(first, tmp_path), UCX2)
    assert settings_in(second) == settings_in(first)


def test_the_dump_is_a_fixed_point_from_the_second_render(seen, tmp_path):
    """Byte-for-byte, headers included, once round.

    Not from the *first*: the first render carries the device's
    remembered state as comments (ADR 0012), and a family that is
    entirely remembered -- `[echo]` -- keeps its header while every line
    under it is a comment. The parser is right to drop those, so the
    section is gone next time round. From there on the file is stable,
    which is the property that matters for a config under version
    control.
    """
    second = reconcile.render_config(reloaded(dumped(seen), tmp_path), UCX2)
    third = reconcile.render_config(reloaded(second, tmp_path), UCX2)
    assert third == second


def reloaded(text, tmp_path):
    path = tmp_path / "dumped.conf"
    path.write_text(text)
    return load_config(path)


def settings_in(text):
    """The live `option = value` lines -- what a re-render must reproduce.

    Section headers are excluded because a header is not a setting: a
    remembered-only section has one and holds nothing the parser keeps.
    """
    return [line for line in text.splitlines()
            if line.strip() and not line.startswith("#")
            and not line.startswith("[")]


def test_the_dump_parses_back_without_being_refused(seen, tmp_path):
    """The whole file, not just the parts we thought to check.

    ADR 0006 refuses a file whole over one unknown option, so a single
    misspelled name in the renderer makes every dump unusable.
    """
    path = tmp_path / "dumped.conf"
    path.write_text(dumped(seen))
    config = load_config(path)
    assert config.globals
    assert config.channels


def test_a_pinned_global_survives_the_round_trip(seen, tmp_path):
    path = tmp_path / "dumped.conf"
    path.write_text(dumped(seen))
    recovered = {s.path for s in load_config(path).globals}
    assert "/clock/wckout" in recovered          # a pinned family
    assert "/echo/type" not in recovered         # a remembered one


# --------------------------------------------------------------------------
# Pinned and remembered render differently, and say why.
# --------------------------------------------------------------------------

def test_a_pinned_option_is_a_live_line(seen):
    """`[hardware]` is pinned: the file sets it, the device does not win."""
    assert register_policy(UCX2, "/hardware/opticalout") == "pin"
    section = section_of(dumped(seen), "[hardware]")
    assert any(line.startswith("opticalout =") for line in section)


def test_a_remembered_option_is_commented_with_its_value(seen):
    """`[echo]` is remembered, so the dump shows the state without
    claiming it. A dump that dropped it would read as "no echo set"."""
    assert register_policy(UCX2, "/echo/type") == "remember"
    section = section_of(dumped(seen), "[echo]")
    line = next(line for line in section if "type" in line)
    assert line.startswith("# type =")
    assert "remembered" in line


def test_an_empty_section_is_not_invented(seen):
    """A family the device said nothing about produces no section."""
    text = dumped({k: v for k, v in seen.items()
                   if k != "/reverb" and not k.startswith("/reverb/")})
    assert "[reverb]" not in text
    assert "[echo]" in text


def test_without_a_model_there_are_no_globals(seen):
    """Nothing to reconstruct from: the register table is the only thing
    that says which paths are global."""
    assert reconcile.globals_from_observed(seen, None) == ()


# --------------------------------------------------------------------------
# The defect the round trip found on its first run.
# --------------------------------------------------------------------------

def test_a_value_the_backend_cannot_name_is_not_written_as_config(seen):
    """`/controlroom/mainout` reports -1 for "no main out", and at the
    pinned revision it arrives with no name attached. Written out as
    `mainout = -1` it produced a file the parser refused -- a dump of a
    working device that could not be read back (michaelforney/oscmix#30).

    It stays in the file as a comment: the state is real and worth
    seeing, it just cannot be spelled as a setting yet.
    """
    unnamed = dict(seen, **{"/controlroom/mainout": (-1,)})
    section = section_of(dumped(unnamed), "[controlroom]")
    line = next(line for line in section if "mainout" in line)
    assert line.startswith("# mainout = -1")
    assert "oscmix#30" in line


def test_that_dump_still_parses(seen, tmp_path):
    """The point of commenting it rather than dropping it: the rest of
    the file survives an option the backend cannot name."""
    unnamed = dict(seen, **{"/controlroom/mainout": (-1,)})
    path = tmp_path / "dumped.conf"
    path.write_text(dumped(unnamed))
    assert load_config(path).globals


def test_a_dump_keeps_every_row_of_a_multi_row_option(seen):
    """The bug a dict keyed by option name hides.

    `/input/{ch}/gain` is three rows -- mic 0..75, instrument 0..24, and
    Analog 5-8 with no value domain. `settable_options` is keyed by name
    and so keeps one of them, and walking that dict to build a dump
    iterated only the surviving row's channels. Inputs 1 and 2 vanished
    from `--dump-config` while the device reported them perfectly well:
    1198 channel settings where there should have been 1200.

    Caught against the hardware rather than here, which is why this test
    exists.
    """
    settings = reconcile.channels_from_observed(seen, UCX2)
    gains = sorted(s.channel for s in settings
                   if s.family == "input" and s.option == "gain")
    assert gains == [1, 3], (
        "channel 1 is a mic row and channel 3 an instrument row; "
        "keying by option name keeps one of them and drops the other")
