"""The profile switch, written from its contract rather than its parser.

The roadmap states the promise this file exists to hold:

    the switch has to state its outcome: applied and verified, applied
    but unverifiable (with the list), or refused before anything was
    written because the config did not parse. Never "partly, and here is
    a traceback".

These tests were written before `profiles.py` existed, on purpose. The
last two defects in this project both came from building the mechanism
first and describing it afterwards -- `[input:N]` parsed, validated,
appeared in --dry-run and never reached the device, and CI was green
across all ten jobs on the commit that did nothing. A test written after
the code tends to assert what the code does.

The property that matters is not "a good config applies". It is that a
*bad* config changes nothing -- the whole point of switching profiles on
a live desk is that a typo costs you a message, not your monitoring.
"""

import os
import shutil
import stat

import pytest
from conftest import write_config

from oscmix_desk import locking, profiles
from oscmix_desk import marker as marker_mod
from oscmix_desk import notices as notices_mod
from oscmix_desk import outcome as outcome_mod
from oscmix_desk import paths as paths_mod


def _key(path):
    """The device key the code under test derives for this config.

    From the same resolution the code uses, against the /proc the suite
    points it at (ADR 0024) -- not against the machine's own card list.
    """
    import os
    from pathlib import Path

    from oscmix_desk.discovery import resolve_device
    from oscmix_desk.profiles import load_config

    config = load_config(path)
    return resolve_device(config.usb_id, config.device_name, config.serial,
                          Path(os.environ["OSCMIX_PROC_ROOT"])).key

GOOD = """
[route:main]
output = 1/2
playback = 1/2
level = 0.0

[output:1]
volume = -10.0
"""


# --------------------------------------------------------------------------
# Outcome 3 first: refused, and nothing written.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("broken", "why"), [
    ("[route:x]\noutput = 99\nplayback = 1\n", "channel out of range"),
    ("[route:x]\nplayback = 1\n", "no destination"),
    ("[route:x]\noutput = 1\nplayback = 1\nlevel = wat\n", "not a number"),
    ("[route:x]\noutput = 1\nplayback = 1\nnonsense = 1\n", "unknown option"),
    ("[output:1]\nreflevel = +99dBu\n", "not a valid option value"),
    ("[output:99]\nvolume = 0.0\n", "no such channel"),
    ("[output:1]\n48v = on\n", "an option with no value domain"),
    ("not ini at all", "unparseable"),
])
def test_a_config_that_does_not_parse_is_refused_with_nothing_on_the_wire(
        tmp_path, broken, why, recording_backend):
    """The promise that makes profile switching safe on a live desk.

    Refused means refused *before* the first datagram, not half-applied
    and then rolled back -- there is no rollback for a mixer. Every row
    here is a different way to be wrong, and all of them cost zero
    messages.
    """
    path = write_config(tmp_path / "profiles" / "broken.conf", broken)
    outcome = profiles.switch_profile("broken",
                                      config_path=tmp_path / "routing.conf",
                                      backend=recording_backend)

    assert outcome.state == outcome_mod.REFUSED, why
    assert outcome.applied is False
    assert recording_backend.sent == [], (
        "%s sent %d datagram(s) before refusing"
        % (why, len(recording_backend.sent)))
    assert outcome.reason, "a refusal has to say why"
    # The name lives on the Outcome and in describe(); the reason is the
    # parser's own message, so it is not repeated there.
    assert outcome.name == "broken"
    assert path.name.startswith(outcome.name)


def test_a_missing_profile_is_refused_not_crashed(tmp_path, recording_backend):
    outcome = profiles.switch_profile("nosuch",
                                      config_path=tmp_path / "routing.conf",
                                      backend=recording_backend)
    assert outcome.state == outcome_mod.REFUSED
    assert recording_backend.sent == []
    assert "nosuch" in outcome.reason


def test_an_unknown_section_applies_without_it_rather_than_refusing(
        tmp_path, recording_backend):
    """The one deliberate exception, and it is not this module's to make.

    ADR 0006: an unknown *section* warns and is ignored, an unknown
    *option* fails. A profile is a config, so it inherits that rule
    rather than inventing a stricter one -- a format that means two
    different things depending on which command read it is worse than
    either meaning.

    Worth stating plainly because the trade-off is genuinely closer here
    than at boot. Refusing a profile switch is cheap (the desk keeps the
    old state), while refusing at boot means no routing at all -- which
    is the case ADR 0006 argued from. The rule still wins on consistency.
    """
    write_config(tmp_path / "profiles" / "odd.conf",
                 GOOD + "\n[from-a-newer-version]\nx = 1\n")
    outcome = profiles.switch_profile("odd",
                                      config_path=tmp_path / "routing.conf",
                                      backend=recording_backend, verify=False)
    assert outcome.applied is True
    assert recording_backend.sent, "the sections it did understand still apply"


def test_a_name_that_escapes_the_directory_is_refused(tmp_path,
                                                      recording_backend):
    # ../../etc/something.conf would parse fine as an ini file.
    outcome = profiles.switch_profile("../evil",
                                      config_path=tmp_path / "routing.conf",
                                      backend=recording_backend)
    assert outcome.state == outcome_mod.REFUSED
    assert recording_backend.sent == []


# --------------------------------------------------------------------------
# Outcome 1: applied and verified.
# --------------------------------------------------------------------------

def test_a_good_profile_applies_everything_the_config_asks_for(
        tmp_path, recording_backend):
    """Same guard as test_apply_routing's wire test, for the same reason.

    That one exists because comparing *orderings* could not see a whole
    category of register going missing. A profile switch is a second
    write path, so it gets the same check rather than trusting that it
    shares a code path with the first.
    """
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    outcome = profiles.switch_profile("tracking",
                                      config_path=tmp_path / "routing.conf",
                                      backend=recording_backend, verify=False)

    assert outcome.applied is True
    paths = {path for path, _tags, _args in recording_backend.sent}
    assert "/mix/1/playback/1" in paths
    assert "/output/1/volume" in paths, "the [output:1] section never landed"


def test_verified_means_the_device_confirmed_it(tmp_path, confirming_backend):
    """The fully-verified outcome, and the only shape that can reach it.

    A profile with routes can never be APPLIED_VERIFIED, because
    `/mix/<out>/playback/<pb>` is never reported back -- measured, and
    declared as a trait. So the state is reachable exactly for profiles
    that pin channel state without touching the matrix: "set my monitor
    levels and reference levels, leave the routing alone", which is a
    real thing to want and the one case where "verified" can mean it.
    """
    write_config(tmp_path / "profiles" / "levels.conf",
                 "[output:1]\nvolume = -10.0\n\n[output:5]\nvolume = 0.0\n")
    outcome = profiles.switch_profile("levels",
                                      config_path=tmp_path / "routing.conf",
                                      backend=confirming_backend)
    assert outcome.state == outcome_mod.APPLIED_VERIFIED
    assert outcome.applied is True
    assert outcome.unverified == []


# --------------------------------------------------------------------------
# Outcome 2: applied, but it could not be checked -- with the list.
# --------------------------------------------------------------------------

def test_an_unverifiable_switch_names_what_it_could_not_confirm(
        tmp_path, silent_backend, monkeypatch):
    """The outcome the desktop actually hits.

    The mixer GUI holds UDP 8222 whenever its window is open, so the
    read-back cannot bind and the switch is blind. That is not a failure
    -- the registers went out -- but reporting it as success would make
    the word "verified" mean nothing on the machine where it matters
    most.
    """
    # A backend that cannot be listened to also cannot release the link
    # barrier, so this is the one profile test that pays a real wait.
    # Shortened here because the outcome is under test, not the
    # duration -- tests/test_apply_routing.py owns the timing.
    from oscmix_desk import routing as routing_mod
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.01)

    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    outcome = profiles.switch_profile("tracking",
                                      config_path=tmp_path / "routing.conf",
                                      backend=silent_backend)

    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert outcome.applied is True
    assert outcome.unverified, "unverifiable without a list is not an outcome"
    assert "/output/1/volume" in outcome.unverified
    # Nobody looked, so the line must not read like a read-back that ran
    # and came up short ("N register(s) unconfirmed", until 0.6.11).
    assert outcome.read_back is False
    assert outcome.describe() == (
        "applied 'tracking'; not read back (receive port in use (mixer GUI "
        "running?)), so none of its %d register(s) is confirmed"
        % len(outcome.unverified))


def test_the_three_states_are_the_only_three(tmp_path):
    # A fourth state would be the "partly, and here is a traceback" the
    # roadmap forbids, arriving by accretion.
    assert set(outcome_mod.STATES) == {outcome_mod.APPLIED_VERIFIED,
                                    outcome_mod.APPLIED_UNVERIFIED,
                                    outcome_mod.REFUSED}


def test_every_outcome_answers_whether_the_device_was_written_to(tmp_path):
    # `applied` is the field a script branches on; it must never be
    # ambiguous, whatever the state.
    for state in outcome_mod.STATES:
        outcome = outcome_mod.Outcome(state=state, name="x", reason="",
                                   unverified=[])
        assert isinstance(outcome.applied, bool)
        assert outcome.applied is (state != outcome_mod.REFUSED)


# --------------------------------------------------------------------------
# Discovery.
# --------------------------------------------------------------------------

def test_profiles_are_listed_by_name_sorted(tmp_path):
    for name in ("mixdown", "tracking", "podcast"):
        write_config(tmp_path / "profiles" / ("%s.conf" % name), GOOD)
    assert paths_mod.list_profiles(tmp_path / "routing.conf") == [
        "mixdown", "podcast", "tracking"]


def test_no_profiles_directory_is_empty_not_an_error(tmp_path):
    assert paths_mod.list_profiles(tmp_path / "routing.conf") == []


# --------------------------------------------------------------------------
# The rest of the public surface.
# --------------------------------------------------------------------------

def test_load_profile_parses_without_touching_a_device(tmp_path):
    """Separate from the switch so --dry-run on a profile can be honest.

    A profile that can only be checked by applying it is not something
    anyone will check before a session.
    """
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    config = profiles.load_profile("tracking", tmp_path / "routing.conf")
    assert [route.output for route in config.routes] == [(1, 2)]
    assert [(c.family, c.channel, c.option) for c in config.channels] == [
        ("output", 1, "volume")]


def test_load_profile_raises_for_a_bad_one(tmp_path):
    # switch() turns this into an Outcome; the raising form is what makes
    # that translation a single place rather than a convention.
    from oscmix_desk import ConfigError

    write_config(tmp_path / "profiles" / "bad.conf",
                 "[route:x]\noutput = 99\nplayback = 1\n")
    with pytest.raises(ConfigError):
        profiles.load_profile("bad", tmp_path / "routing.conf")


def test_describe_profiles_summarises_each_one(tmp_path):
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    lines = profiles.describe_profiles(tmp_path / "routing.conf")
    assert len(lines) == 1
    assert "tracking" in lines[0]
    assert "1 route" in lines[0]


def test_describe_profiles_reports_a_broken_one_instead_of_raising(tmp_path):
    """Listing is the command you run *because* something is wrong.

    One unparseable profile hiding the other four would make it useless
    at exactly the moment it is needed.
    """
    write_config(tmp_path / "profiles" / "good.conf", GOOD)
    write_config(tmp_path / "profiles" / "bad.conf",
                 "[route:x]\noutput = 99\nplayback = 1\n")
    lines = list(profiles.describe_profiles(tmp_path / "routing.conf"))
    assert len(lines) == 2
    assert any("BROKEN" in line and "bad" in line for line in lines)
    assert any("good" in line and "BROKEN" not in line for line in lines)


def test_profile_path_maps_a_name_to_a_file(tmp_path):

    path = paths_mod.profile_path("tracking", tmp_path / "routing.conf")
    assert path == tmp_path / "profiles" / "tracking.conf"


def test_the_outcome_describes_itself_in_one_line(tmp_path):
    for state in outcome_mod.STATES:
        line = outcome_mod.Outcome(state=state, name="tracking", reason="why",
                                unverified=["/output/1/volume"]).describe()
        assert "tracking" in line
        assert "\n" not in line


# --------------------------------------------------------------------------
# Machine settings versus desk settings.
# --------------------------------------------------------------------------

def test_a_profile_without_an_osc_section_uses_the_main_config_port(tmp_path):
    """Written after this cost a real device its state.

    The profile below states no ``[osc] port``. Before the fix it fell
    back to the compiled-in default, 7222 -- which on the development
    machine was the *live backend*, so a unit test writing a profile
    reached a Fireface and moved a fader on it. It passed, because
    everything it asserted was true.

    The general rule it produced: a profile describes the desk, the main
    config describes the machine. Ports and the device name are the
    machine's.
    """
    write_config(tmp_path / "routing.conf",
                 "[osc]\nport = 9001\nrecv-port = 9002\n"
                 "[device]\nname = Fireface UFX III\n")
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)

    config = profiles.load_profile("tracking", tmp_path / "routing.conf")
    assert config.osc_port == 9001
    assert config.osc_recv_port == 9002
    assert config.device_name == "Fireface UFX III"


def test_a_profile_that_states_a_port_keeps_its_own(tmp_path):
    # The machine with two backends is exactly the machine whose
    # profiles are per-backend, so stating it has to win.
    write_config(tmp_path / "routing.conf", "[osc]\nport = 9001\n")
    write_config(tmp_path / "profiles" / "other.conf",
                 "[osc]\nport = 9500\n" + GOOD)
    assert profiles.load_profile("other", tmp_path / "routing.conf"
                                 ).osc_port == 9500


#: A value for each machine setting that is neither the default nor main's.
_STATED = {"port": "9500", "recv-port": "9600", "name": "Fireface 802",
           "usb-id": "2a39:3fb0", "serial": "11223344"}


@pytest.mark.parametrize(("section", "option", "attr"),
                         profiles.MACHINE_SETTINGS)
def test_a_profile_states_one_machine_setting_and_inherits_the_other_four(
        tmp_path, section, option, attr):
    """Option by option, not section by section: a profile that states
    `[device] serial` keeps the main config's `usb-id`, which sits in the
    same section. Since 0.6.11 that rests on the parser's fallbacks
    rather than on a second look at the file, so it is held here for
    every row of the table, against a main config whose five values are
    all non-default."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Some Box\nusb-id = 1111:2222\n"
                        "serial = 99887766\n\n"
                        "[osc]\nport = 9001\nrecv-port = 9002\n")
    write_config(tmp_path / "profiles" / "one.conf",
                 "[%s]\n%s = %s\n\n[route:x]\nplayback = 1/2\noutput = 1/2\n"
                 % (section, option, _STATED[option]))
    main = profiles.load_config(path)
    profile = profiles.load_profile("one", path)
    stated = int(_STATED[option]) if section == "osc" else _STATED[option]
    assert getattr(profile, attr) == stated
    for _section, _option, other in profiles.MACHINE_SETTINGS:
        if other != attr:
            assert getattr(profile, other) == getattr(main, other), other


def test_stating_the_default_explicitly_still_counts_as_stating_it(tmp_path):
    # "equals the default" cannot distinguish "said 7222" from "said
    # nothing". The parser can: it takes what the file says, and falls
    # back to what it was read onto only when the file says nothing.
    from oscmix_desk.constants import DEFAULT_OSC_PORT

    write_config(tmp_path / "routing.conf", "[osc]\nport = 9001\n")
    write_config(tmp_path / "profiles" / "pinned.conf",
                 "[osc]\nport = %d\n" % DEFAULT_OSC_PORT + GOOD)
    assert profiles.load_profile("pinned", tmp_path / "routing.conf"
                                 ).osc_port == DEFAULT_OSC_PORT


def test_the_playback_matrix_is_named_as_uncheckable_not_as_a_miss(
        tmp_path, confirming_backend):
    """The outcome every real routing produces, and what it must read like.

    `/mix/<out>/playback/<pb>` is never reported by this backend --
    measured, and declared as `Traits.dumps_playback_matrix`. So a
    switch that worked perfectly still cannot reach APPLIED_VERIFIED,
    and saying "could not confirm 1 register" about it would train
    people to ignore the one message that matters when something is
    actually wrong.
    """
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    outcome = profiles.switch_profile("tracking",
                                      config_path=tmp_path / "routing.conf",
                                      backend=confirming_backend)

    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert outcome.unverified == ["/mix/1/playback/1"]
    assert outcome.unverifiable == ["/mix/1/playback/1"]
    line = outcome.describe()
    assert "cannot report" in line
    assert "unconfirmed" not in line


def test_a_genuine_miss_reads_differently_from_an_uncheckable_one(tmp_path):
    """The distinction the message exists to make."""
    both = outcome_mod.Outcome(
        state=outcome_mod.APPLIED_UNVERIFIED, name="x",
        unverified=["/mix/1/playback/1", "/output/1/volume"],
        unverifiable=["/mix/1/playback/1"])
    assert "1 register(s) unconfirmed" in both.describe()
    assert "/output/1/volume" in both.describe()
    assert "plus 1 this backend cannot report" in both.describe()


def test_every_machine_level_field_on_config_is_inherited(tmp_path):
    """The table cannot silently miss one.

    A `Config` field is machine-level when no `[route]` and no channel
    section can write it -- those are the desk. Everything else
    describes the box the desk is plugged into, and a profile that
    reverted it to the compiled-in default would be wrong on any machine
    that had set it. `usb-id` was missing from the first version of the
    table for exactly that reason: nothing pointed at it.
    """
    import dataclasses

    from oscmix_desk.model import Config

    # `policies` is the desk's, not the machine's: "should my monitor
    # faders come back after a restart" is a statement about how this
    # set of routing is meant to behave, so a profile brings its own.
    # `globals` likewise -- an echo send is part of a mix, not part of
    # the box it runs on.
    desk = {"routes", "channels", "policies", "globals"}
    # Neither: `loaded` records the machine settings the file resolved to,
    # `main` what a profile's routing.conf resolved to, and `overrides`
    # what the command line replaced -- no file's to state, and carried
    # along by `keep_machine_settings` beside the table (0.6.11).
    record = {"loaded", "main", "overrides"}
    machine = {f.name for f in dataclasses.fields(Config)} - desk - record
    covered = {attr for _section, _option, attr in profiles.MACHINE_SETTINGS}
    assert machine == covered, (
        "not inherited by a profile switch: %s" % sorted(machine - covered))


def test_usb_id_is_inherited_like_the_ports(tmp_path):
    write_config(tmp_path / "routing.conf",
                 "[device]\nname = Fireface UCX II\nusb-id = 2a39:3fd9\n")
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    assert profiles.load_profile("tracking", tmp_path / "routing.conf"
                                 ).usb_id == "2a39:3fd9"


def test_not_checking_reads_differently_from_checking_and_missing(
        tmp_path, recording_backend):
    """Same state, different fact, and the line has to say which.

    `unverified` holds every expected register in both cases. In one it
    means "looked for and absent"; in the other it means "nobody
    looked". APPLIED_UNVERIFIED is correct for both -- the registers did
    go out either way -- so the wording is the only place the difference
    can live.
    """
    write_config(tmp_path / "profiles" / "tracking.conf", GOOD)
    outcome = profiles.switch_profile("tracking",
                                      config_path=tmp_path / "routing.conf",
                                      backend=recording_backend, verify=False)
    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert "not checked" in outcome.describe()
    assert "unconfirmed" not in outcome.describe()
    assert outcome.read_back is False, "nobody looked, so the field says so"


# --------------------------------------------------------------------------
# The active profile survives a start (ADR 0018).
# --------------------------------------------------------------------------

def _desk(tmp_path, main=GOOD, **named):
    """routing.conf with free ports, plus named profiles beside it."""
    from conftest import free_udp_port

    for name, text in named.items():
        write_config(tmp_path / "profiles" / ("%s.conf" % name), text)
    return write_config(tmp_path / "routing.conf",
                        "[osc]\nport = %d\nrecv-port = %d\n%s"
                        % (free_udp_port(), free_udp_port(), main))


TRACKING = """
[route:direct]
output = 5/6
playback = 5/6
"""


def test_an_applied_switch_is_remembered_beside_the_config(tmp_path,
                                                          recording_backend):
    path = _desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=recording_backend)
    assert outcome.applied
    assert (tmp_path / "active-profile").read_text().strip() == "tracking"
    assert marker_mod.active_profile(path) == "tracking"


def test_a_refused_switch_remembers_nothing(tmp_path, recording_backend):
    path = _desk(tmp_path, broken="[route:x]\noutput = 99\nplayback = 1\n")
    assert not profiles.switch_profile("broken", config_path=path,
                                       backend=recording_backend).applied
    assert not (tmp_path / "active-profile").exists()
    assert marker_mod.active_profile(path) is None


def test_effective_config_is_the_remembered_profile(tmp_path):
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    config, name = profiles.effective_config(path)
    assert name == "tracking"
    assert [r.output for r in config.routes] == [(5, 6)]
    # Machine settings still come from routing.conf (ADR 0011).
    assert config.osc_port != 7222


def test_without_a_marker_the_effective_config_is_routing_conf(tmp_path):
    path = _desk(tmp_path, tracking=TRACKING)
    config, name = profiles.effective_config(path)
    assert name is None
    assert [r.output for r in config.routes] == [(1, 2)]


@pytest.mark.parametrize(("marker", "why"), [
    ("gone\n", "names a profile that does not exist"),
    ("broken\n", "names a profile that does not parse"),
    ("../../etc/passwd\n", "is not a profile name"),
])
def test_a_marker_that_cannot_be_honoured_falls_back_with_a_warning(
        tmp_path, caplog, marker, why):
    # The desk must come up; a refused start over a file nobody edited is
    # the failure ADR 0006 exists to prevent. The marker stays, so the
    # warning stays until somebody decides.
    path = _desk(tmp_path, tracking=TRACKING,
                 broken="[route:x]\noutput = 99\nplayback = 1\n")
    (tmp_path / "active-profile").write_text(marker)
    with caplog.at_level("WARNING"):
        config, name = profiles.effective_config(path)
    assert name is None, why
    assert [r.output for r in config.routes] == [(1, 2)]
    assert "ignoring" in caplog.text or "not usable" in caplog.text
    assert (tmp_path / "active-profile").exists(), "the choice is kept"


def test_a_broken_routing_conf_still_refuses_the_start(tmp_path):
    path = write_config(tmp_path / "routing.conf", "[route:x]\nplayback = 1\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    with pytest.raises(profiles.ConfigError):
        profiles.effective_config(path)


def test_restore_main_applies_routing_conf_and_forgets(tmp_path,
                                                       confirming_backend):
    # A device that answers: with one that only echoes the link flags the
    # read-back waited out its whole 10 s window, twice per run of the
    # suite and once per covering mutant (0.6.10).
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=confirming_backend)
    assert outcome.applied
    assert outcome.name == "routing.conf"
    assert outcome.reason != outcome_mod.NOT_CHECKED, "a restore checks by default"
    assert confirming_backend.dumps == 1, \
        "the read-back asks the backend it was given, not a socket of its own"
    assert not (tmp_path / "active-profile").exists()
    written = {p for p, _t, _a in confirming_backend.sent}
    assert "/output/1/stereo" in written
    assert "/output/5/stereo" not in written


def test_a_refused_restore_keeps_the_profile(tmp_path, recording_backend):
    path = write_config(tmp_path / "routing.conf", "[route:x]\nplayback = 1\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=recording_backend)
    assert not outcome.applied
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "routing.conf"
    with pytest.raises(profiles.ConfigError) as parsed:
        profiles.load_config(path)
    assert outcome.reason == str(parsed.value), "the reason is the parse error"
    assert recording_backend.sent == []
    assert (tmp_path / "active-profile").read_text().strip() == "tracking"


def test_the_listing_marks_the_active_profile(tmp_path):
    path = _desk(tmp_path, tracking=TRACKING, mixdown=GOOD)
    (tmp_path / "active-profile").write_text("mixdown\n")
    lines = profiles.describe_profiles(path)
    assert [line.startswith("mixdown") and line.endswith("(active)")
            for line in lines] == [True, False]


def test_a_marker_that_cannot_be_written_does_not_change_the_outcome(
        tmp_path, recording_backend, caplog, monkeypatch):
    # Not a fourth state: the device has the profile, ADR 0011.
    path = _desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.mkdir()          # a directory where the file should be
    with caplog.at_level("WARNING"):
        outcome = profiles.switch_profile("tracking", config_path=path,
                                          backend=recording_backend)
    assert outcome.applied
    assert outcome.name == "tracking"
    assert "not remembered" in caplog.text


def test_a_switch_can_be_asked_not_to_check(tmp_path, recording_backend):
    path = _desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=recording_backend, verify=False)
    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert outcome.name == "tracking"
    assert outcome.reason == outcome_mod.NOT_CHECKED
    assert outcome.persisted is True, "the marker was written either way"
    assert outcome.unverified == sorted(
        profiles.expected_registers(profiles.load_profile("tracking", path)))


def test_restore_main_can_be_asked_not_to_check(tmp_path, recording_backend):
    # verify=False is the switch's contract too (NOT_CHECKED): everything
    # expected goes in the list, and the marker is still forgotten.
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=recording_backend,
                                    verify=False)
    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert outcome.name == "routing.conf"
    assert outcome.reason == outcome_mod.NOT_CHECKED
    assert outcome.read_back is False
    assert outcome.persisted is True, "the marker was removed either way"
    assert outcome.unverified == sorted(
        profiles.expected_registers(profiles.load_config(path)))
    assert not (tmp_path / "active-profile").exists()


def test_the_marker_functions_answer_nothing_without_a_config(tmp_path):
    # No routing.conf, no profiles directory, no marker: None and False,
    # never an AttributeError on a path that does not exist.
    assert marker_mod.active_profile_path(None) is None
    assert marker_mod.active_profile(None) is None
    # In effect and durable, both, each time: `durable` alone went
    # unasserted outside a switch (survivors, 0.6.11).
    assert marker_mod.remember_active_profile("tracking", None) == (False, False)
    assert marker_mod.forget_active_profile(None) == (True, True)
    path = _desk(tmp_path, tracking=TRACKING)
    assert marker_mod.remember_active_profile("tracking", path) == (True, True)
    assert (tmp_path / "active-profile").read_text() == "tracking\n"
    assert marker_mod.forget_active_profile(path) == (True, True)
    assert marker_mod.forget_active_profile(path) == (True, True), "twice is fine"
    assert not (tmp_path / "active-profile").exists()


# --------------------------------------------------------------------------
# Third review round: a marker that is never half written, and one
# switch at a time.
# --------------------------------------------------------------------------

def test_a_marker_write_that_fails_leaves_the_old_marker_whole(tmp_path,
                                                              monkeypatch,
                                                              caplog):
    path = _desk(tmp_path, tracking=TRACKING, mixdown=GOOD)
    (tmp_path / "active-profile").write_text("tracking\n")

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(profiles.os, "replace", refuse)
    with caplog.at_level("WARNING"):
        assert marker_mod.remember_active_profile("mixdown", path) == (False,
                                                                     False)
    assert (tmp_path / "active-profile").read_text() == "tracking\n"
    assert list(tmp_path.glob("*.tmp")) == []
    assert "not remembered" in caplog.text


def test_the_marker_goes_through_a_temporary_file_and_a_rename(tmp_path,
                                                              monkeypatch):
    path = _desk(tmp_path, tracking=TRACKING)
    import os

    renames = []
    real_replace = marker_mod.os.replace

    def record(src, dst):
        assert os.path.dirname(src) == str(tmp_path), \
            "beside the marker: a rename does not cross file systems"
        renames.append((os.path.basename(src), os.path.basename(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(profiles.os, "replace", record)
    umask = os.umask(0o022)
    try:
        assert marker_mod.remember_active_profile("tracking", path).in_effect is True
    finally:
        os.umask(umask)
    # A temporary name of its own, beside the marker. It was the fixed
    # `active-profile.tmp` until 0.6.11, which two switches holding
    # different device locks shared: one could rename the file the other
    # was still writing.
    (source, target), = renames
    assert target == "active-profile"
    assert source.startswith("active-profile.")
    assert source.endswith(".tmp")
    assert source != "active-profile.tmp"
    assert (tmp_path / "active-profile").read_text() == "tracking\n"
    # World-readable like before, not mkstemp's 0600: the launcher reads it.
    assert stat.S_IMODE((tmp_path / "active-profile").stat().st_mode) == 0o644
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_switch_refuses_when_another_holds_the_lock_too_long(
        tmp_path, recording_backend, monkeypatch, caplog):
    path = _desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    umask = os.umask(0o022)
    try:
        with locking._switch_lock(path, _key(path)) as held:
            assert held
            with caplog.at_level("INFO"):
                outcome = profiles.switch_profile("tracking", config_path=path,
                                                  backend=recording_backend)
    finally:
        os.umask(umask)
    lock = locking.device_lock_path(path, _key(path))
    # A plain file every writer of the interface can open -- owner and
    # group, the group being the shared directory's (ADR 0024).
    assert stat.S_IMODE(lock.stat().st_mode) == 0o660
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "tracking"
    assert "holds the device lock" in outcome.reason
    assert recording_backend.sent == [], "a refused switch writes nothing"
    assert not (tmp_path / "active-profile").exists()
    # Once, not once per poll: the loop checks every 0.1 s for up to
    # SWITCH_LOCK_WAIT, and a line per check would be 300 of them.
    assert caplog.text.count(
        "another writer holds the device lock; waiting") == 1
    # And once the lock is free, the same switch goes through.
    assert profiles.switch_profile("tracking", config_path=path,
                                   backend=recording_backend).applied


def test_two_switches_do_not_interleave_on_the_wire(tmp_path, routing_mod,
                                                    monkeypatch):
    """Terminal A: --profile tracking. Terminal B: --profile mixdown.

    Each switch is a link phase, a barrier and a mix phase; interleaved,
    the second's links could land between the first's links and mix,
    which is the ordering ADR 0001 exists to guarantee. The lock makes
    one finish before the other starts, whichever wins.
    """
    import threading
    import time

    from oscmix_desk import backend as backend_mod

    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.05)
    path = _desk(tmp_path, tracking=TRACKING, mixdown=GOOD)
    wire = []

    class SlowBackend:
        traits = backend_mod.OSCMIX

        def __init__(self, tag):
            self.tag = tag

        def send(self, messages):
            for message in messages:
                wire.append((self.tag, message[0]))
                time.sleep(0.02)          # long enough to interleave

        def request_dump(self):
            pass

        def listen(self):
            return None                   # the desktop case: unverified

    outcomes = {}

    def switch(name):
        outcomes[name] = profiles.switch_profile(
            name, config_path=path, backend=SlowBackend(name), verify=False)

    threads = [threading.Thread(target=switch, args=(n,))
               for n in ("tracking", "mixdown")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(o.applied for o in outcomes.values())
    tags = [tag for tag, _path in wire]
    first = tags[0]
    boundary = tags.index(next(t for t in tags if t != first))
    assert all(t == first for t in tags[:boundary])
    assert all(t != first for t in tags[boundary:]), \
        "the second switch's datagrams sit inside the first's"
    # The marker names whichever wrote last, and only that one.
    assert (tmp_path / "active-profile").read_text().strip() == tags[-1]


# --------------------------------------------------------------------------
# The error branches of the marker and the lock, named by a coverage probe.
# --------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_marker_is_ignored_with_a_warning(tmp_path, caplog):
    path = _desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.write_text("tracking\n")
    marker.chmod(0)
    try:
        with caplog.at_level("WARNING"):
            assert marker_mod.active_profile(path) is None
    finally:
        marker.chmod(0o600)
    assert "ignoring" in caplog.text


def test_a_marker_that_cannot_be_removed_is_a_warning_not_a_crash(tmp_path,
                                                                 caplog):
    path = _desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.mkdir()
    (marker / "child").write_text("")               # unlink raises
    with caplog.at_level("WARNING"):
        assert marker_mod.forget_active_profile(path) == (False, False)
    assert "cannot remove %s (" % marker in caplog.text


def test_a_marker_change_that_could_not_be_synced_names_the_directory(
        tmp_path, monkeypatch, caplog):
    path = _desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(marker_mod, "_fsync_directory", lambda _d: False)
    with caplog.at_level("WARNING"):
        assert marker_mod.remember_active_profile("tracking", path) == (True,
                                                                      False)
        assert marker_mod.forget_active_profile(path) == (True, False)
    assert ("profile 'tracking' remembered, but %s could not be synced"
            % tmp_path) in caplog.text
    assert ("marker removed, but %s could not be synced" % tmp_path) \
        in caplog.text


def test_fsync_of_the_directory_reports_what_it_did(tmp_path, monkeypatch):
    # Never raises, because some filesystems refuse it. It says so
    # instead, and the caller turns that into a warning (0.6.6).
    assert marker_mod._fsync_directory(tmp_path) is True
    assert marker_mod._fsync_directory(tmp_path / "does-not-exist") is False

    def refuse(fd):
        raise OSError("fsync unsupported")

    monkeypatch.setattr(profiles.os, "fsync", refuse)
    assert marker_mod._fsync_directory(tmp_path) is False


def test_without_a_config_there_is_nothing_to_lock():
    with locking._switch_lock(None) as held:
        assert held is True


def test_no_profile_refuses_when_another_switch_holds_the_lock(
        tmp_path, recording_backend, monkeypatch):
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    with locking._switch_lock(path, _key(path)):
        outcome = profiles.restore_main(path, backend=recording_backend)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "routing.conf"
    assert "holds the device lock" in outcome.reason
    assert recording_backend.sent == []
    assert (tmp_path / "active-profile").read_text().strip() == "tracking", \
        "a refused restore keeps the profile in effect"


def test_an_empty_marker_means_no_profile(tmp_path):
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("\n")
    assert marker_mod.active_profile(path) is None


def test_a_profile_is_validated_for_the_desk_s_device_not_the_default(
        tmp_path, caplog):
    """Measured before the fix, on a desk for an interface nobody modelled:
    a profile routing to output 25/26 was refused because "channel 25 does
    not exist on a Fireface UCX II", and a profile's `[output:1] volume`
    was accepted through the UCX II's table and would have been written
    -- while the same section in routing.conf has been ignored with a
    warning since 0.6.2. The profile was parsed while it still named the
    default device, and given the desk's name afterwards (0.6.11)."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Some Box\n\n"
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "far.conf",
                 "[route:far]\nplayback = 1/2\noutput = 25/26\n")
    write_config(tmp_path / "profiles" / "vol.conf",
                 "[route:main]\nplayback = 1/2\noutput = 1/2\n\n"
                 "[output:1]\nvolume = -10.0\n")
    far = profiles.load_profile("far", path)
    assert far.device_name == "Some Box"
    assert [route.output for route in far.routes] == [(25, 26)]
    with caplog.at_level("WARNING"):
        vol = profiles.load_profile("vol", path)
    assert vol.channels == [], "nothing of it may reach a device nobody modelled"
    assert "ignoring [output:1]: no register model for 'Some Box'" in caplog.text


def test_a_profile_on_an_802_desk_is_held_to_the_802_s_channels(tmp_path):
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Fireface 802\n\n"
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "far.conf",
                 "[route:far]\nplayback = 1/2\noutput = 29/30\n")
    write_config(tmp_path / "profiles" / "gone.conf",
                 "[route:gone]\nplayback = 1/2\noutput = 31/32\n")
    assert profiles.load_profile("far", path).routes[0].output == (29, 30)
    with pytest.raises(profiles.ConfigError, match="Fireface 802"):
        profiles.load_profile("gone", path)


def test_a_profile_that_names_its_own_device_is_held_to_that_one(tmp_path):
    """A profile may state a machine setting, and then it wins -- for the
    validation too, which is the half the old order got right by luck."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Some Box\n\n"
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "ucx.conf",
                 "[device]\nname = Fireface UCX II\n\n"
                 "[route:far]\nplayback = 1/2\noutput = 25/26\n")
    with pytest.raises(profiles.ConfigError, match="Fireface UCX II"):
        profiles.load_profile("ucx", path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_config_directory_that_cannot_be_written_is_a_warning(tmp_path,
                                                               caplog):
    # The temporary file never exists, so the clean-up has nothing to
    # remove; that has to be as quiet as the write failing was loud.
    path = _desk(tmp_path, tracking=TRACKING)
    tmp_path.chmod(0o500)
    try:
        with caplog.at_level("WARNING"):
            assert marker_mod.remember_active_profile("tracking", path).in_effect is False
    finally:
        tmp_path.chmod(0o700)
    assert not (tmp_path / "active-profile").exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert "not remembered" in caplog.text


# --------------------------------------------------------------------------
# One lock for every writer of the device (ADR 0019).
# --------------------------------------------------------------------------

def test_the_device_lock_is_exclusive_and_released(tmp_path):
    path = _desk(tmp_path, tracking=TRACKING)
    lock = locking.take_device_lock(path, _key(path))
    assert lock is not None
    assert locking.device_lock_path(path, _key(path)).exists()
    assert locking.take_device_lock(path, _key(path), wait=0.2) is None, \
        "a second writer must not hold it at the same time"
    lock.release()
    second = locking.take_device_lock(path, _key(path), wait=0.2)
    assert second is not None
    second.release()
    lock.release()          # releasing twice is not an error


def test_a_writer_without_a_config_gets_a_stand_in(tmp_path):
    # Nothing to lock, and no caller should have to branch on that.
    lock = locking.take_device_lock(None)
    assert lock is not None
    lock.release()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_the_unit_locks_a_file_it_cannot_open_for_writing(tmp_path):
    """`ProtectHome=read-only` is the unit's world: flock needs no write."""
    path = _desk(tmp_path, tracking=TRACKING)
    lock_file = tmp_path / "active-profile.lock"
    lock_file.write_text("")
    lock_file.chmod(0o444)
    try:
        lock = locking.take_device_lock(path, _key(path))
        assert lock is not None
        assert locking.take_device_lock(path, _key(path), wait=0.2) is None
        lock.release()
    finally:
        lock_file.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
@pytest.mark.skipif(os.geteuid() == 0, reason="root opens anything")
def test_a_lock_that_cannot_be_opened_is_a_refusal(tmp_path, monkeypatch,
                                                   caplog):
    """No lock, no write (ADR 0022).

    Until 0.6.7 this warned and wrote anyway, which made "every writer
    holds one lock" true only while nothing went wrong. Every caller
    refuses now: a switch says so, a reconcile stands down, and a start
    fails so systemd can try again.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = _desk(tmp_path, tracking=TRACKING)
    tmp_path.chmod(0o500)
    try:
        with caplog.at_level("ERROR"):
            lock = locking.take_device_lock(path, _key(path))
    finally:
        tmp_path.chmod(0o700)
    assert lock is None
    assert str(tmp_path / "active-profile.lock") in caplog.text, \
        "the error has to name the lock it could not open"


def test_a_switch_that_cannot_remember_says_so_in_the_outcome(
        tmp_path, recording_backend, caplog):
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").mkdir()
    with caplog.at_level("WARNING"):
        outcome = profiles.switch_profile("tracking", config_path=path,
                                          backend=recording_backend)
    assert outcome.applied
    assert outcome.persisted is False, \
        "the caller decides about the reload, and needs the fact to do it"
    assert "not remembered" in outcome.describe()
    assert "next reload or start" in outcome.describe()


def test_a_restore_that_cannot_forget_says_so_in_the_outcome(
        tmp_path, confirming_backend):
    path = _desk(tmp_path, tracking=TRACKING)
    marker = tmp_path / "active-profile"
    marker.mkdir()
    (marker / "child").write_text("")
    outcome = profiles.restore_main(path, backend=confirming_backend)
    assert outcome.applied
    assert outcome.persisted is False


def test_forgetting_reports_whether_the_marker_is_gone(tmp_path):
    path = _desk(tmp_path, tracking=TRACKING)
    assert marker_mod.forget_active_profile(path).in_effect is True, "nothing to remove"
    (tmp_path / "active-profile").write_text("tracking\n")
    assert marker_mod.forget_active_profile(path).in_effect is True
    assert marker_mod.forget_active_profile(None).in_effect is True


def test_an_applied_switch_that_was_remembered_stays_persisted(
        tmp_path, recording_backend):
    path = _desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=recording_backend)
    assert outcome.persisted is True
    assert "not remembered" not in outcome.describe()


def test_a_short_write_is_finished_rather_than_truncated(tmp_path,
                                                         monkeypatch):
    """write(2) may write less than it was given without failing.

    The marker is renamed over a correct one, so a truncated name would
    replace a good desk with a profile that does not exist.
    """
    real_write = marker_mod.os.write

    def one_byte_at_a_time(fd, data):
        return real_write(fd, data[:1])

    path = _desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(profiles.os, "write", one_byte_at_a_time)
    assert marker_mod.remember_active_profile("tracking", path).in_effect is True
    assert (tmp_path / "active-profile").read_text() == "tracking\n"


def test_a_directory_that_cannot_be_synced_warns_on_both_paths(
        tmp_path, monkeypatch, caplog):
    # The marker is in effect either way; what is not guaranteed is that
    # it survives a power cut, and that has to be said rather than
    # swallowed.
    path = _desk(tmp_path, tracking=TRACKING)
    monkeypatch.setattr(marker_mod, "_fsync_directory", lambda _d: False)
    with caplog.at_level("WARNING"):
        assert marker_mod.remember_active_profile("tracking", path).in_effect is True
    assert "may not survive a power cut" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert marker_mod.forget_active_profile(path).in_effect is True
    assert "may come back after a power cut" in caplog.text


# --------------------------------------------------------------------------
# The lock names the device (ADR 0022).
# --------------------------------------------------------------------------

def test_the_device_key_names_the_box_not_the_file(tmp_path):
    from conftest import fake_proc

    from oscmix_desk.discovery import resolve_device

    proc = fake_proc(tmp_path / "proc", boxes=[(24, "24216011")])
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc).key == \
        "2a39-3fd9-24216011"
    # No interface to be had: the model alone.
    empty = fake_proc(tmp_path / "empty")
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", empty).key == \
        "2a39-3fd9-unknown"


def test_the_lock_lives_in_the_runtime_directory(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    path = _desk(tmp_path, tracking=TRACKING)
    assert locking.device_lock_path(path, "2a39-3fd9-24216011") == \
        runtime / "oscmix-desk" / "2a39-3fd9-24216011.lock"
    assert (runtime / "oscmix-desk").is_dir(), "and it is created"


def test_without_a_runtime_directory_the_lock_stays_beside_the_config(
        tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = _desk(tmp_path, tracking=TRACKING)
    assert locking.device_lock_path(path, "2a39-3fd9-24216011") == \
        tmp_path / "active-profile.lock"
    assert locking.device_lock_path(None, "2a39-3fd9-24216011") is None


def test_two_configs_over_one_device_take_the_same_lock(tmp_path, monkeypatch):
    """The point of keying on the hardware.

    Two config directories describing one interface are two desks on one
    device. Until 0.6.7 they held two different lock files and wrote at
    the same time.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    first = _desk(tmp_path / "a", tracking=TRACKING)
    second = _desk(tmp_path / "b", tracking=TRACKING)
    key = "2a39-3fd9-24216011"
    assert locking.device_lock_path(first, key) == \
        locking.device_lock_path(second, key)
    held = locking.take_device_lock(first, key)
    assert held is not None
    assert locking.take_device_lock(second, key, wait=0.2) is None
    held.release()


def test_without_a_runtime_directory_the_config_path_is_the_lock(
        tmp_path, monkeypatch):
    """The fallback the runtime directory usually hides.

    With `$XDG_RUNTIME_DIR` set, the config path no longer decides where
    the lock lives, so nothing observes it being passed. A session
    without a runtime directory is the case where it still does.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = _desk(tmp_path, tracking=TRACKING)
    lock = locking.take_device_lock(path, "2a39-3fd9-24216011")
    assert lock is not None
    assert (tmp_path / "active-profile.lock").exists()
    assert locking.take_device_lock(path, "2a39-3fd9-24216011",
                                     wait=0.2) is None
    lock.release()
    # And with no config either there is nothing to contend over.
    assert locking.take_device_lock(None, "2a39-3fd9-24216011") is not None


def test_a_runtime_directory_that_cannot_be_made_falls_back_and_says_so(
        tmp_path, monkeypatch, caplog):
    # A file where the directory should be: mkdir raises, and the lock
    # has to land beside the config rather than nowhere.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(blocker))
    path = _desk(tmp_path, tracking=TRACKING)
    with caplog.at_level("WARNING"):
        where = locking.device_lock_path(path, "2a39-3fd9-24216011")
    assert where == tmp_path / "active-profile.lock"
    assert str(blocker / "oscmix-desk") in caplog.text, \
        "the warning names the directory it could not use"


def test_a_switch_without_a_runtime_directory_locks_beside_the_config(
        tmp_path, recording_backend, monkeypatch):
    """The config path still decides where the lock lives, sometimes.

    With a runtime directory in play it does not, so nothing observes
    the switch passing it down. Without one it does, and a switch must
    then contend with a lock held there.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    path = _desk(tmp_path, tracking=TRACKING)
    held = locking.take_device_lock(path, _key(path))
    assert held is not None
    assert (tmp_path / "active-profile.lock").exists()
    try:
        outcome = profiles.switch_profile("tracking", config_path=path,
                                          backend=recording_backend)
    finally:
        held.release()
    assert outcome.state == outcome_mod.REFUSED
    assert recording_backend.sent == []


# --------------------------------------------------------------------------
# 0.6.8: one lock path for every writer, and no writing to an absent
# device (ADR 0023). Each of these is a way the 0.6.7 lock came apart,
# measured on a live UCX II before it was fixed.
# --------------------------------------------------------------------------


def _shared(tmp_path, monkeypatch):
    """A stand-in for /run/oscmix-desk, which tests may not touch."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o1777, exist_ok=True)
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    return shared


def test_the_shared_directory_wins_over_the_runtime_directory(
        tmp_path, monkeypatch):
    shared = _shared(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    path = _desk(tmp_path, tracking=TRACKING)
    assert locking.device_lock_path(path, "2a39-3fd9-24216011") == \
        shared / "2a39-3fd9-24216011.lock"


def test_a_writer_without_a_runtime_directory_computes_the_same_path(
        tmp_path, monkeypatch):
    """The hole this release exists for.

    `$XDG_RUNTIME_DIR` is absent from sudo, cron and a bare ssh command.
    Measured on the desk in 0.6.7: with a holder on the runtime path, the
    same switch run without the variable computed a path beside the
    config, took it in two seconds and wrote the whole routing.
    """
    shared = _shared(tmp_path, monkeypatch)
    path = _desk(tmp_path, tracking=TRACKING)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    with_env = locking.device_lock_path(path, "2a39-3fd9-24216011")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    without_env = locking.device_lock_path(path, "2a39-3fd9-24216011")
    assert with_env == without_env == shared / "2a39-3fd9-24216011.lock"

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    held = locking.take_device_lock(path, "2a39-3fd9-24216011")
    assert held is not None
    try:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        assert locking.take_device_lock(
            path, "2a39-3fd9-24216011", wait=0.2) is None
    finally:
        held.release()


def test_two_user_sessions_over_one_interface_contend(tmp_path, monkeypatch):
    """`/run/user/<uid>` is per user; one piece of hardware is not."""
    shared = _shared(tmp_path, monkeypatch)
    path = _desk(tmp_path, tracking=TRACKING)
    key = "2a39-3fd9-24216011"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-1000"))
    first = locking.take_device_lock(path, key)
    assert first is not None
    try:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-2000"))
        assert locking.device_lock_path(path, key) == \
            shared / ("%s.lock" % key)
        assert locking.take_device_lock(path, key, wait=0.2) is None
    finally:
        first.release()


def test_a_vanishing_runtime_directory_does_not_free_the_lock(
        tmp_path, monkeypatch):
    """It goes with the last logout of a user without lingering.

    In 0.6.7 the holder's file stopped existing and the next writer
    created a fresh inode and took it.
    """
    _shared(tmp_path, monkeypatch)
    runtime = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    path = _desk(tmp_path, tracking=TRACKING)
    held = locking.take_device_lock(path, "2a39-3fd9-24216011")
    assert held is not None
    try:
        shutil.rmtree(runtime, ignore_errors=True)
        assert locking.take_device_lock(
            path, "2a39-3fd9-24216011", wait=0.2) is None
    finally:
        held.release()


def test_a_writer_without_a_config_still_takes_the_lock(tmp_path, monkeypatch):
    """The shared path needs no config directory, so neither does a writer.

    `scripts/sweep-writes.py` is the one that has none, and it is the
    loudest writer in the repository.
    """
    _shared(tmp_path, monkeypatch)
    held = locking.take_device_lock(None, "2a39-3fd9-24216011")
    assert held is not None
    try:
        assert locking.take_device_lock(
            None, "2a39-3fd9-24216011", wait=0.2) is None
    finally:
        held.release()


def test_an_existing_shared_directory_is_never_fallen_back_from(
        tmp_path, monkeypatch, caplog):
    """Falling back from the directory other writers use is the hole itself.

    A lock that cannot be opened there is a refusal, not a reason to
    compute a different path: the writer that quietly locks somewhere
    else is exactly the one that walks past the holder.
    """
    if os.getuid() == 0:
        pytest.skip("root opens anything")
    shared = _shared(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    path = _desk(tmp_path, tracking=TRACKING)
    shared.chmod(0o500)                      # no new file may be created
    try:
        with caplog.at_level("ERROR"):
            lock = locking.take_device_lock(path, "2a39-3fd9-24216011")
    finally:
        shared.chmod(0o1777)
    assert lock is None
    assert str(shared) in caplog.text, "the error names the lock it wanted"
    assert not (tmp_path / "active-profile.lock").exists(), \
        "and it must not quietly lock somewhere else"
    assert not (tmp_path / "run" / "oscmix-desk").exists()


def test_a_switch_to_an_absent_interface_writes_nothing(tmp_path, monkeypatch):
    """0.6.7 reported `applied`, exited 0 and recorded the marker.

    Measured with the UCX II unplugged: eight registers unconfirmed, a
    marker naming a profile that had never been at the device, and the
    next start applying it. No backend is handed in here, because the
    check exists for the caller that opens its own socket.
    """
    _shared(tmp_path, monkeypatch)
    monkeypatch.setenv("OSCMIX_SYSFS_USB", str(tmp_path / "no-usb"))
    path = _desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "tracking", "a refusal says what it refused"
    assert outcome.reason == "2a39:3fd9 is not connected"
    assert not marker_mod.active_profile_path(path).exists(), \
        "and it remembers nothing"


def test_a_restore_to_an_absent_interface_writes_nothing(tmp_path, monkeypatch):
    _shared(tmp_path, monkeypatch)
    monkeypatch.setenv("OSCMIX_SYSFS_USB", str(tmp_path / "no-usb"))
    path = _desk(tmp_path, tracking=TRACKING)
    outcome = profiles.restore_main(config_path=path)
    assert outcome.state == outcome_mod.REFUSED
    assert outcome.name == "routing.conf"
    assert outcome.reason == "2a39:3fd9 is not connected"


def test_a_switch_refuses_when_no_backend_holds_the_port(tmp_path, monkeypatch):
    """Presence in sysfs is not reachability.

    Measured on the desk: `authorized=0` emptied the ALSA card list and
    the sequencer clients while `/sys/bus/usb/devices/5-2` stayed in
    place with `idVendor` readable -- so a check on sysfs alone still
    said the device was there, and the switch still reported `applied`
    for datagrams the kernel dropped. udev stops the unit the moment the
    device goes, and a stopped unit does the same thing by itself.
    """
    _shared(tmp_path, monkeypatch)
    sysfs = tmp_path / "sysfs"
    (sysfs / "5-2").mkdir(parents=True)
    (sysfs / "5-2" / "idVendor").write_text("2a39\n")
    (sysfs / "5-2" / "idProduct").write_text("3fd9\n")
    monkeypatch.setenv("OSCMIX_SYSFS_USB", str(sysfs))
    # A /proc where the interface is visible to ALSA and nothing is bound:
    # the interface is there, the backend is not.
    from conftest import fake_proc

    proc = fake_proc(tmp_path / "proc", boxes=[(24, "24216011")])
    monkeypatch.setenv("OSCMIX_PROC_ROOT", str(proc))
    path = _desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path)
    assert outcome.state == outcome_mod.REFUSED
    assert "nothing is listening" in outcome.reason
    assert not marker_mod.active_profile_path(path).exists()


def test_the_serial_is_inherited_like_the_usb_id(tmp_path):
    """A profile that does not state it keeps the running one.

    Without this a switch would hand the next writer an empty serial,
    the key would be recomputed from the card list, and the pinning done
    at start-up would be undone by the first profile change.
    """
    path = _desk(tmp_path, tracking=TRACKING)
    path.write_text(path.read_text() + "\n[device]\nserial = 24216011\n")
    profile = profiles.load_profile("tracking", path)
    assert profile.serial == "24216011"


def test_the_lock_file_is_openable_by_a_second_user(tmp_path, monkeypatch):
    """A service with umask 077 would otherwise lock everyone else out.

    `flock` holds on a read-only descriptor, so a second writer only
    needs to *open* the file -- but a lock file created 0600 by the unit
    cannot be opened by anyone else at all. Measured on the desk: the
    unit's own lock file came out `-rw-------`. Owner and group since
    0.6.9, the group being the directory's (ADR 0024).
    """
    _shared(tmp_path, monkeypatch)
    umask = os.umask(0o077)
    try:
        held = locking.take_device_lock(None, "2a39-3fd9-24216011")
    finally:
        os.umask(umask)
    assert held is not None
    try:
        info = locking.device_lock_path(None, "2a39-3fd9-24216011").stat()
        assert stat.S_IMODE(info.st_mode) == 0o660, \
            "created 0o%o; a second writer cannot open it" % stat.S_IMODE(info.st_mode)
        assert info.st_gid == (tmp_path / "shared").stat().st_gid
    finally:
        held.release()

def test_a_configured_serial_is_the_key_a_switch_and_a_restore_lock_on(
        tmp_path, monkeypatch, recording_backend):
    """`[device] serial` separates two boxes only if a writer uses it.

    Two identical interfaces without it share one lock; with it, each
    desk contends on its own box's key and nothing else. A switch that
    dropped the configured serial would key on the card list instead --
    another box's number, or `ambiguous` -- and walk past a holder of
    its own box (ADR 0023).
    """
    _shared(tmp_path, monkeypatch)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    path = _desk(tmp_path, main=GOOD + "\n[device]\nserial = 99887766\n",
                 tracking=TRACKING)
    held = locking.take_device_lock(None, "2a39-3fd9-99887766")
    assert held is not None
    try:
        switched = profiles.switch_profile("tracking", config_path=path,
                                           backend=recording_backend)
        restored = profiles.restore_main(config_path=path,
                                         backend=recording_backend)
    finally:
        held.release()
    assert switched.state == outcome_mod.REFUSED
    assert restored.state == outcome_mod.REFUSED
    assert "holds the device lock" in switched.reason
    assert recording_backend.sent == []


def test_a_restore_without_a_runtime_directory_locks_beside_the_config(
        tmp_path, recording_backend, monkeypatch):
    """The config path still decides the lock for a bare session.

    The switch has this test already; the restore takes the same lock by
    a separate call, and nothing observed it passing the path down.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.3)
    path = _desk(tmp_path, tracking=TRACKING)
    held = locking.take_device_lock(path, _key(path))
    assert held is not None
    assert (tmp_path / "active-profile.lock").exists()
    try:
        outcome = profiles.restore_main(config_path=path,
                                        backend=recording_backend)
    finally:
        held.release()
    assert outcome.state == outcome_mod.REFUSED
    assert recording_backend.sent == []


def test_without_an_override_the_lock_directory_is_the_shared_one(
        tmp_path, monkeypatch):
    """`OSCMIX_LOCK_DIR` is for tests; production reads the real path."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o1777)
    monkeypatch.delenv("OSCMIX_LOCK_DIR", raising=False)
    monkeypatch.setattr(locking, "SHARED_LOCK_DIR", str(shared))
    assert locking.device_lock_path(None, "2a39-3fd9-24216011") == \
        shared / "2a39-3fd9-24216011.lock"


def test_reachability_reads_the_real_sysfs_and_proc_by_default(monkeypatch):
    """The overrides are test seams; the defaults are what a desk runs."""
    from pathlib import Path

    from oscmix_desk import Config
    from oscmix_desk.discovery import Device
    from oscmix_desk.process import PortHolder

    monkeypatch.delenv("OSCMIX_SYSFS_USB", raising=False)
    monkeypatch.delenv("OSCMIX_PROC_ROOT", raising=False)
    seen = []
    present, bound = [True], [True]
    holder = [PortHolder(pid=7, oscmix=True, client=24, serial="24216011")]
    monkeypatch.setattr(profiles, "usb_device_present",
                        lambda usb_id, sysfs: seen.append(sysfs) or present[0])
    monkeypatch.setattr(profiles, "udp_port_listening",
                        lambda port, proc: seen.append(proc) or bound[0])
    monkeypatch.setattr(profiles, "port_holder",
                        lambda port, proc: seen.append(proc) or holder[0])
    config = Config()
    box = Device(usb_id="2a39:3fd9", serial="24216011", client=24)
    assert profiles._unreachable(config, box) is None
    assert seen == [Path("/sys/bus/usb/devices"), Path("/proc"), Path("/proc")]

    holder[0] = PortHolder(pid=7, oscmix=True, client=28, serial="99887766")
    assert profiles._unreachable(config, box) == (
        "the backend on UDP 7222 drives the interface 99887766, not 24216011")
    holder[0] = PortHolder(pid=7, oscmix=True, client=28, serial=None)
    assert profiles._unreachable(config, box) == (
        "the backend on UDP 7222 bridges sequencer client 28, not 24")
    holder[0] = PortHolder(pid=7, oscmix=False, client=None, serial=None)
    assert profiles._unreachable(config, box) == (
        "UDP 7222 is held by pid 7, not by an oscmix backend of this user")
    holder[0] = None
    assert profiles._unreachable(config, box) == (
        "UDP 7222 is held by a process that cannot be identified, not by an "
        "oscmix backend of this user")
    bound[0] = False
    assert profiles._unreachable(config, box) == (
        "nothing is listening on UDP 7222, so the backend is not running")
    present[0] = False
    assert profiles._unreachable(config, box) == "2a39:3fd9 is not connected"


def test_a_marker_that_is_not_utf8_is_ignored_with_a_warning(tmp_path, caplog):
    """It raised UnicodeDecodeError past `except OSError` -- on every start."""
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_bytes(b"\xff\xfe\n")
    with caplog.at_level("WARNING"):
        _config, active = profiles.effective_config(path)
    assert active is None
    assert "ignoring" in caplog.text


# --------------------------------------------------------------------------
# 0.6.11: a profile that names its own backend, and what a marker promises.
# --------------------------------------------------------------------------

def _retargeting_desk(tmp_path):
    path = write_config(tmp_path / "routing.conf", "[osc]\nport = 9001\n" + GOOD)
    write_config(tmp_path / "profiles" / "here.conf", GOOD)
    write_config(tmp_path / "profiles" / "same.conf", "[osc]\nport = 9001\n" + GOOD)
    write_config(tmp_path / "profiles" / "there.conf",
                 "[osc]\nport = 9500\n\n[device]\nserial = 99887766\n" + GOOD)
    return path


def test_a_profile_that_names_another_machine_is_told_what_0_7_0_does(
        tmp_path, caplog, recording_backend):
    """It still wins in 0.6.x (ADR 0011). ADR 0026 ends that: one persisted
    profile meant three targets, and two such profiles hold two device
    locks over one marker. Said where the desk is written, once.

    A profile that *restates* routing.conf's values is not told anything:
    `--dump-config > profiles/x.conf`, the documented way to make one,
    writes `[device]` and `[osc]` into every profile. The first cut
    warned about the sections and would have refused them all."""

    path = _retargeting_desk(tmp_path)
    for name in ("here", "same"):
        assert notices_mod.other_machine_warning(
            profiles.load_profile(name, path)) is None, name
    assert notices_mod.other_machine_warning(profiles.load_config(path)) is None
    there = notices_mod.other_machine_warning(profiles.load_profile("there", path))
    assert there.startswith(
        "this profile names another backend or interface than its "
        "routing.conf -- serial '99887766' (not ''), osc port 9500 (not 9001)")
    assert "from 0.7.0 it is refused (ADR 0026)" in there
    with caplog.at_level("WARNING"):
        profiles.switch_profile("there", config_path=path,
                                backend=recording_backend, verify=False)
    assert caplog.text.count("from 0.7.0 it is refused") == 1


def test_a_dumped_config_makes_a_profile_nobody_is_warned_about(tmp_path):
    from oscmix_desk.reconcile import render_config

    path = write_config(tmp_path / "routing.conf", GOOD)
    dumped = render_config(profiles.load_config(path))
    assert "[device]" in dumped, "which is why the sections cannot be the rule"
    assert "[osc]" in dumped
    write_config(tmp_path / "profiles" / "dumped.conf", dumped)
    assert notices_mod.other_machine_warning(
        profiles.load_profile("dumped", path)) is None


def test_a_marker_that_may_not_survive_a_power_cut_says_so_in_the_outcome(
        tmp_path, monkeypatch, recording_backend):
    """The marker is in effect, so the unit is reloaded as usual; what is
    not known is whether the directory entry reached the disk. That was a
    log line only, and `persisted=True` read as more than it meant."""
    path = _retargeting_desk(tmp_path)
    durable = profiles.switch_profile("here", config_path=path,
                                      backend=recording_backend, verify=False)
    assert (durable.persisted, durable.durable) == (True, True)
    assert "power cut" not in durable.describe()
    monkeypatch.setattr(marker_mod, "_fsync_directory", lambda _d: False)
    for outcome in (
            profiles.switch_profile("here", config_path=path,
                                    backend=recording_backend, verify=False),
            profiles.restore_main(config_path=path, backend=recording_backend,
                                  verify=False)):
        assert (outcome.persisted, outcome.durable) == (True, False)
        assert outcome.describe().endswith(
            "; remembered, but it may not survive a power cut")

