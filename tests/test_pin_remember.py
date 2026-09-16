"""Who wins after the initial write, and why the answer is per register.

Built on a measurement rather than on the words. On a UCX II, of every
register a config can set, exactly one is *pushed* to listeners when it
changes -- `/output/{ch}/stereo`, which the device echoes over MIDI.
`volume`, `mute`, `hi-z`, `gain`, `reflevel` and `/playback/{ch}/stereo`
all change silently; only a `/refresh` shows them.

That kills the strong reading of "pin". Nothing can snap back on a GUI
change, because nothing reports one, and polling means a 2252-register
dump against a device already streaming ~880 meter datagrams a second.
What pin can honestly mean is: the config wins for as long as this
session is still looking.

What it replaces was an accident of timing. A fader turned 0.5 s after a
restart came back at the config's value; the same turn at 1.5, 3 and 6
seconds survived -- and the 0.5 s case was overwritten by the ordinary
start-up apply, not by the verifier. The cut-off was how long the apply
took, which is nobody's decision.
"""

import pytest

from oscmix_desk import verify
from oscmix_desk.registers import (
    PIN,
    POLICIES,
    REMEMBER,
    device_for_name,
    register_policy,
    settable_options,
)

UCX2 = device_for_name("Fireface UCX II")


# --------------------------------------------------------------------------
# The table.
# --------------------------------------------------------------------------

def test_every_register_declares_a_policy_from_the_known_set():
    for register in UCX2.registers:
        assert register.policy in POLICIES, register.template


@pytest.mark.parametrize("path", [
    "/output/1/stereo", "/playback/1/stereo", "/input/1/stereo",
    "/mix/1/playback/1", "/mix/1/input/1",
    "/input/3/hi-z", "/input/3/gain", "/input/3/reflevel",
    "/output/1/reflevel", "/input/1/48v",
])
def test_the_installation_and_the_routing_are_pinned(path):
    """Two kinds of register the config has to keep winning.

    The routing *is* the config -- a mix level or a link flag that drifts
    is the feature not working. And a reference level, a hi-Z switch or a
    phantom power setting describes what is physically plugged in: wrong
    there is a signal problem, not a matter of taste.
    """
    assert register_policy(UCX2, path) == PIN


@pytest.mark.parametrize("path", [
    "/output/1/volume", "/output/1/mute", "/input/1/mute",
    "/output/1/phase", "/input/1/phase",
])
def test_what_a_person_reaches_for_during_a_session_is_remembered(path):
    """ADR 0003's rule, now stated per register instead of implied.

    The example config once carried `volume = 0.0` in a monitor block,
    and every restart forced a hand-set -20 dB back to unity. Declaring
    the value is still allowed -- it is how a fixed installation pins its
    levels -- but the default for a fader is that the person turning it
    wins.
    """
    assert register_policy(UCX2, path) == REMEMBER


def test_an_unmodelled_register_is_remembered_not_pinned():
    # The safe side of the default: this project does not start insisting
    # on something it has no row for.
    assert register_policy(UCX2, "/some/future/register") == REMEMBER
    assert register_policy(None, "/output/1/volume") == REMEMBER


def test_every_settable_option_has_a_deliberate_policy():
    """No config-reachable option may sit on the field default by accident.

    The default is REMEMBER, so a forgotten row is silently the weaker
    promise. This lists what a config can actually set and requires each
    to be named in one of the two lists above -- so adding an option
    forces the decision rather than inheriting one.
    """
    decided = {
        "stereo": PIN, "hi-z": PIN, "gain": PIN, "reflevel": PIN,
        "volume": REMEMBER, "mute": REMEMBER, "phase": REMEMBER,
        # A headphone listening preference, not installation state: it
        # changes how a mix sounds to one listener and breaks nothing if
        # somebody turns it. ADR 0003's default, decided rather than
        # inherited -- which is what this test is for.
        "crossfeed": REMEMBER,
    }
    for family in ("input", "output"):
        for option, register in settable_options(UCX2, family).items():
            assert option in decided, (
                "%s/%s is settable but its policy was never decided"
                % (family, option))
            assert register.policy == decided[option], (
                "%s/%s is %s, the table says %s"
                % (family, option, register.policy, decided[option]))


# --------------------------------------------------------------------------
# Where it changes behaviour.
# --------------------------------------------------------------------------

def test_a_mismatch_on_a_pinned_register_is_a_problem():
    result = verify.VerifyResult(confirmed=[], mismatched=["/input/3/gain"],
                                 unobserved=[])
    assert verify._unconfirmed(result, UCX2) == ["/input/3/gain"]
    assert verify._kept_by_the_device(result, UCX2) == []


def test_a_mismatch_on_a_remembered_register_is_the_user():
    """The one place the distinction changes what the device sees.

    Re-sending here would undo a fader move while the person was still
    holding the knob -- and the read-back runs for up to a minute after
    start, so the window is not theoretical.
    """
    result = verify.VerifyResult(confirmed=[], mismatched=["/output/1/volume"],
                                 unobserved=[])
    assert verify._unconfirmed(result, UCX2) == []
    assert verify._kept_by_the_device(result, UCX2) == ["/output/1/volume"]


def test_absence_is_judged_by_reportability_not_by_policy():
    """Two independent axes, and it is worth showing they stay independent.

    Policy answers "who wins when we disagree". Reportability answers
    "would we even hear about it". A missing register is not the user
    changing something, it is a write that may have been lost -- so
    remember has nothing to say about it.

    `/output/1/stereo` is pinned *and* reported completely after a cold
    plug, so its absence is a lost write worth re-sending for.
    `/input/3/gain` is equally pinned and is channel state, which a cold
    plug delivers raggedly -- 1234 of 1932 registers, measured -- so its
    absence is a note. Same policy, different verdicts.
    """
    for path, expected in (("/output/1/stereo", ["/output/1/stereo"]),
                           ("/input/3/gain", [])):
        result = verify.VerifyResult(confirmed=[], mismatched=[],
                                     unobserved=[path])
        assert register_policy(UCX2, path) == PIN
        assert verify._unconfirmed(result, UCX2) == expected, path


def test_pinned_and_remembered_mismatches_are_separated_not_merged():
    result = verify.VerifyResult(
        confirmed=[], mismatched=["/output/1/volume", "/input/3/gain"],
        unobserved=[])
    assert verify._unconfirmed(result, UCX2) == ["/input/3/gain"]
    assert verify._kept_by_the_device(result, UCX2) == ["/output/1/volume"]


# --------------------------------------------------------------------------
# The config override.
# --------------------------------------------------------------------------

def test_a_config_can_pin_what_the_table_remembers(tmp_path):
    """The fixed installation that really does want its faders pinned.

    A venue rack where the levels are set once and must survive anyone
    poking at the mixer is the case the table's default is wrong for.
    The default is chosen for the desk in front of a person, so this has
    to be overridable rather than argued about.
    """
    from oscmix_desk.config import load_config
    from oscmix_desk.reconcile import policy_for

    path = tmp_path / "routing.conf"
    path.write_text("[pin]\noutput.volume = pin\n")
    config = load_config(path)

    assert policy_for("/output/5/volume", UCX2, config.policies) == PIN
    # Untouched options keep the table's answer.
    assert policy_for("/output/5/mute", UCX2, config.policies) == REMEMBER


def test_a_config_can_remember_what_the_table_pins(tmp_path):
    # The other direction: a studio that rides input gain by hand does
    # not want the session putting it back.
    from oscmix_desk.config import load_config
    from oscmix_desk.reconcile import policy_for

    path = tmp_path / "routing.conf"
    path.write_text("[pin]\ninput.gain = remember\n")
    config = load_config(path)
    assert policy_for("/input/3/gain", UCX2, config.policies) == REMEMBER


def test_the_override_reaches_the_decision_that_re_sends(tmp_path):
    """Parsed is not applied -- this release has been bitten by that twice.

    `[input:N]` parsed, validated, showed in --dry-run and never reached
    the device; then the same registers were written and left out of the
    read-back. So the override is asserted where it changes behaviour,
    not where it is stored.
    """
    from oscmix_desk.config import load_config

    path = tmp_path / "routing.conf"
    path.write_text("[pin]\noutput.volume = pin\n")
    overrides = load_config(path).policies

    result = verify.VerifyResult(confirmed=[], mismatched=["/output/1/volume"],
                                 unobserved=[])
    assert verify._unconfirmed(result, UCX2, overrides) == ["/output/1/volume"]
    assert verify._kept_by_the_device(result, UCX2, overrides) == []


@pytest.mark.parametrize(("broken", "why"), [
    ("[pin]\noutput.volume = vielleicht\n", "not a policy"),
    ("[pin]\nvolume = pin\n", "no family"),
    ("[pin]\nmidi.volume = pin\n", "no such family"),
    ("[pin]\noutput.wat = pin\n", "no such option"),
    ("[pin]\noutput.48v = pin\n", "not settable, so not overridable"),
])
def test_a_typo_in_the_pin_section_is_an_error(tmp_path, broken, why):
    """Silent by nature, so it has to fail loudly.

    A wrong key here changes nothing visible: the routing still applies,
    and the only symptom is a fader that does or does not come back,
    weeks later, on a machine nobody is watching.
    """
    from oscmix_desk import ConfigError
    from oscmix_desk.config import load_config

    path = tmp_path / "routing.conf"
    path.write_text(broken)
    with pytest.raises(ConfigError):
        load_config(path)


def test_an_old_version_would_ignore_the_section_not_reject_the_file(tmp_path):
    """Why [pin] is a section and not an option in [output:N].

    ADR 0006: an unknown *option* in a known section is an error, an
    unknown *section* is a warning. Putting this in [output:N] would mean
    every config using it is rejected whole by 0.2.x -- the routing gone,
    on a machine that upgraded a config before it upgraded the package.
    As a section it degrades to "the table defaults apply", which is
    exactly what those versions already do.
    """
    from oscmix_desk.config import _KNOWN_OPTIONS

    for kind, options in _KNOWN_OPTIONS.items():
        assert not any(o.startswith("pin") for o in options), kind


# --------------------------------------------------------------------------
# What a dump writes down.
# --------------------------------------------------------------------------

def test_a_dump_emits_pinned_options_and_comments_out_remembered_ones():
    """The other half of the same question, and the reason it is one model.

    A dump reads a value off the device. Whether that value belongs in a
    config is exactly "does the config win here" asked backwards, so the
    answer comes from the same column rather than from a rule inside the
    writer.
    """
    from oscmix_desk.config import ChannelSetting, Config
    from oscmix_desk.reconcile import render_config

    config = Config(device_name="Fireface UCX II", channels=[
        ChannelSetting("output", 5, "volume", -12.0),    # remembered
        ChannelSetting("output", 5, "reflevel", "+4dBu"),  # pinned
        ChannelSetting("input", 3, "hi-z", True),          # pinned
    ])
    text = render_config(config, UCX2)

    assert "reflevel = +4dBu" in text
    assert "hi-z = true" in text
    # Present, visible, and not applied until a person says so.
    assert "# volume = -12.0" in text
    assert "remembered" in text


def test_a_dumped_remembered_value_is_still_shown():
    """Commented, not dropped.

    Seeing what the device holds is most of why anyone runs a dump.
    Omitting remembered options would make a channel with a hand-set
    fader look like a channel with no state at all.
    """
    from oscmix_desk.config import ChannelSetting, Config
    from oscmix_desk.reconcile import render_config

    text = render_config(Config(device_name="Fireface UCX II", channels=[
        ChannelSetting("output", 5, "volume", -12.0)]), UCX2)
    assert "[output:5]" in text
    assert "-12.0" in text


def test_a_dumped_config_round_trips_through_the_parser(tmp_path):
    """A dump has to be a config, not something that looks like one.

    The commented half is the risk: a stray '#' in the wrong place, or a
    value rendered in a format the parser rejects, turns a dump into a
    file that fails at the next boot -- and the person finds out then.
    """
    from oscmix_desk.config import ChannelSetting, Config, load_config
    from oscmix_desk.reconcile import render_config

    config = Config(device_name="Fireface UCX II", channels=[
        ChannelSetting("output", 5, "volume", -12.0),
        ChannelSetting("output", 5, "reflevel", "+4dBu"),
        ChannelSetting("input", 3, "hi-z", True),
        ChannelSetting("input", 3, "gain", 12.0),
    ])
    path = tmp_path / "dumped.conf"
    path.write_text(render_config(config, UCX2))
    parsed = load_config(path)

    got = {(c.family, c.channel, c.option): c.value for c in parsed.channels}
    assert got == {
        ("output", 5, "reflevel"): "+4dBu",
        ("input", 3, "hi-z"): True,
        ("input", 3, "gain"): 12.0,
    }, "only the pinned options should survive a round trip"


# --------------------------------------------------------------------------
# A wait that came out of this work, and the recording behind it.
# --------------------------------------------------------------------------

def test_the_dump_settle_is_bounded_from_both_sides():
    """ADR 0010: a device wait names its measurement and is held to it.

    `DUMP_LISTEN_SETTLE` exists because upstream writes to a *connected*
    UDP socket and `writeosc` ignores ECONNREFUSED. While nothing is
    bound on the receive port, every meter datagram draws an ICMP
    port-unreachable that Linux queues; the next write fails with it and
    is dropped. Bind and ask for a dump in the same breath and the
    casualty is the bundle `setrefresh()` flushes first -- all twenty
    `/playback/<n>/stereo`.

    Measured on a UCX II, twelve trials per gap: 0.0 s delivered them
    4/12, 0.1 s and 0.3 s 12/12. With the wait in place, twelve
    consecutive `verify_routing` calls lost it 0 times, against 5 of 10
    before.

    Bounded from both sides because either mistake is silent. Too small
    and the register is lost again, which now reads as "unconfirmed after
    retry" in the journal. Too large and every verification, every
    profile switch and every --dump-config pays it.
    """
    from oscmix_desk.constants import DUMP_LISTEN_SETTLE

    # One meter datagram at the measured rate (~880/s) is what the
    # mechanism actually needs.
    one_datagram = 1.0 / 880
    assert 2 * one_datagram <= DUMP_LISTEN_SETTLE
    assert 100 * one_datagram >= DUMP_LISTEN_SETTLE
    # And it must stay small against the window it precedes.
    from oscmix_desk.constants import VERIFY_TIMEOUT
    assert DUMP_LISTEN_SETTLE < VERIFY_TIMEOUT / 20


# --------------------------------------------------------------------------
# And the dump has to actually feed the renderer.
# --------------------------------------------------------------------------

def test_channel_state_is_reconstructed_from_a_dump():
    """`render_config` could format channel sections and nothing made any.

    The renderer was reachable only from its own tests -- built,
    correct, and wired to nothing, which is the third time this release
    has produced that shape. So the reconstruction is asserted from the
    reported registers, not from a hand-built Config.
    """
    from oscmix_desk.reconcile import channels_from_observed

    seen = {
        "/output/5/volume": (-12.0,),
        "/output/5/reflevel": (0, "+4dBu"),
        "/input/3/hi-z": (1,),
        "/input/3/gain": (12.0,),
        "/output/5/mute": (0,),
    }
    got = {(c.family, c.channel, c.option): c.value
           for c in channels_from_observed(seen, UCX2)}

    assert got[("output", 5, "reflevel")] == "+4dBu", (
        "an enum has to become its name, not its index")
    assert got[("input", 3, "hi-z")] is True, "a bool has to become a bool"
    assert got[("input", 3, "gain")] == 12.0
    assert got[("output", 5, "volume")] == -12.0
    assert got[("output", 5, "mute")] is False


def test_only_settable_options_are_reconstructed():
    # A dump that invented options would produce a file the parser
    # rejects -- and the person finds out at the next boot.
    from oscmix_desk.reconcile import channels_from_observed

    seen = {"/output/5/name": ("Monitors",), "/input/1/48v": (1,),
            "/output/5/volume": (-3.0,)}
    got = [(c.family, c.channel, c.option)
           for c in channels_from_observed(seen, UCX2)]
    assert got == [("output", 5, "volume")]


def test_the_dump_command_passes_channel_state_to_the_renderer():
    """The wiring itself, asserted at the seam that was missing.

    Reading the registers and formatting them both worked; nothing
    joined them. This checks the join by name so a future refactor
    cannot quietly unhook it again.
    """
    import ast

    from conftest import repo_file

    source = repo_file("src", "oscmix_desk", "cli.py").read_text()
    tree = ast.parse(source)
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "channels_from_observed" in called, (
        "--dump-config reconstructs routes but not channel state, so "
        "render_config's channel sections are unreachable in production")


def test_an_uncommented_remembered_line_parses(tmp_path):
    """The comment carries a second '#', and that has to be harmless.

    A dumped line reads `# volume = -6.0   # remembered: ...`. Someone
    who decides to pin it deletes the leading '#', leaving an inline
    comment -- which the parser must accept, or the dump has handed them
    a file that fails at the next boot.
    """
    from oscmix_desk.config import load_config

    path = tmp_path / "routing.conf"
    path.write_text("[output:5]\n"
                    "volume = -6.0   # remembered: the device's value wins\n")
    config = load_config(path)
    assert [(c.option, c.value) for c in config.channels] == [("volume", -6.0)]
