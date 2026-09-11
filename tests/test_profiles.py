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

import pytest
from conftest import write_config

from oscmix_desk import profiles

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

    assert outcome.state == profiles.REFUSED, why
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
    assert outcome.state == profiles.REFUSED
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
    assert outcome.state == profiles.REFUSED
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
    assert outcome.state == profiles.APPLIED_VERIFIED
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

    assert outcome.state == profiles.APPLIED_UNVERIFIED
    assert outcome.applied is True
    assert outcome.unverified, "unverifiable without a list is not an outcome"
    assert "/output/1/volume" in outcome.unverified


def test_the_three_states_are_the_only_three(tmp_path):
    # A fourth state would be the "partly, and here is a traceback" the
    # roadmap forbids, arriving by accretion.
    assert set(profiles.STATES) == {profiles.APPLIED_VERIFIED,
                                    profiles.APPLIED_UNVERIFIED,
                                    profiles.REFUSED}


def test_every_outcome_answers_whether_the_device_was_written_to(tmp_path):
    # `applied` is the field a script branches on; it must never be
    # ambiguous, whatever the state.
    for state in profiles.STATES:
        outcome = profiles.Outcome(state=state, name="x", reason="",
                                   unverified=[])
        assert isinstance(outcome.applied, bool)
        assert outcome.applied is (state != profiles.REFUSED)


# --------------------------------------------------------------------------
# Discovery.
# --------------------------------------------------------------------------

def test_profiles_are_listed_by_name_sorted(tmp_path):
    for name in ("mixdown", "tracking", "podcast"):
        write_config(tmp_path / "profiles" / ("%s.conf" % name), GOOD)
    from oscmix_desk import config as config_mod
    assert config_mod.list_profiles(tmp_path / "routing.conf") == [
        "mixdown", "podcast", "tracking"]


def test_no_profiles_directory_is_empty_not_an_error(tmp_path):
    from oscmix_desk import config as config_mod
    assert config_mod.list_profiles(tmp_path / "routing.conf") == []


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
    from oscmix_desk import config as config_mod

    path = config_mod.profile_path("tracking", tmp_path / "routing.conf")
    assert path == tmp_path / "profiles" / "tracking.conf"


def test_the_outcome_describes_itself_in_one_line(tmp_path):
    for state in profiles.STATES:
        line = profiles.Outcome(state=state, name="tracking", reason="why",
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


def test_stating_the_default_explicitly_still_counts_as_stating_it(tmp_path):
    # "equals the default" cannot distinguish "said 7222" from "said
    # nothing", which is why the check reads the file.
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

    assert outcome.state == profiles.APPLIED_UNVERIFIED
    assert outcome.unverified == ["/mix/1/playback/1"]
    assert outcome.unverifiable == ["/mix/1/playback/1"]
    line = outcome.describe()
    assert "cannot report" in line
    assert "unconfirmed" not in line


def test_a_genuine_miss_reads_differently_from_an_uncheckable_one(tmp_path):
    """The distinction the message exists to make."""
    both = profiles.Outcome(
        state=profiles.APPLIED_UNVERIFIED, name="x",
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

    from oscmix_desk.config import Config

    # `policies` is the desk's, not the machine's: "should my monitor
    # faders come back after a restart" is a statement about how this
    # set of routing is meant to behave, so a profile brings its own.
    # `globals` likewise -- an echo send is part of a mix, not part of
    # the box it runs on.
    desk = {"routes", "channels", "policies", "globals"}
    machine = {f.name for f in dataclasses.fields(Config)} - desk
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
    assert outcome.state == profiles.APPLIED_UNVERIFIED
    assert "not checked" in outcome.describe()
    assert "unconfirmed" not in outcome.describe()


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
    assert profiles.active_profile(path) == "tracking"


def test_a_refused_switch_remembers_nothing(tmp_path, recording_backend):
    path = _desk(tmp_path, broken="[route:x]\noutput = 99\nplayback = 1\n")
    assert not profiles.switch_profile("broken", config_path=path,
                                       backend=recording_backend).applied
    assert not (tmp_path / "active-profile").exists()
    assert profiles.active_profile(path) is None


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
                                                       recording_backend):
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=recording_backend)
    assert outcome.applied
    assert outcome.name == "routing.conf"
    assert not (tmp_path / "active-profile").exists()
    written = {p for p, _t, _a in recording_backend.sent}
    assert "/output/1/stereo" in written
    assert "/output/5/stereo" not in written


def test_a_refused_restore_keeps_the_profile(tmp_path, recording_backend):
    path = write_config(tmp_path / "routing.conf", "[route:x]\nplayback = 1\n")
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=recording_backend)
    assert not outcome.applied
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
    assert "not remembered" in caplog.text


def test_restore_main_can_be_asked_not_to_check(tmp_path, recording_backend):
    # verify=False is the switch's contract too (NOT_CHECKED): everything
    # expected goes in the list, and the marker is still forgotten.
    path = _desk(tmp_path, tracking=TRACKING)
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(path, backend=recording_backend,
                                    verify=False)
    assert outcome.state == profiles.APPLIED_UNVERIFIED
    assert outcome.reason == profiles.NOT_CHECKED
    assert outcome.unverified == sorted(
        profiles.expected_registers(profiles.load_config(path)))
    assert not (tmp_path / "active-profile").exists()


def test_the_marker_functions_answer_nothing_without_a_config(tmp_path):
    # No routing.conf, no profiles directory, no marker: None and False,
    # never an AttributeError on a path that does not exist.
    assert profiles.active_profile_path(None) is None
    assert profiles.active_profile(None) is None
    assert profiles.remember_active_profile("tracking", None) is False
    profiles.forget_active_profile(None)          # nothing to forget
    path = _desk(tmp_path, tracking=TRACKING)
    assert profiles.remember_active_profile("tracking", path) is True
    assert (tmp_path / "active-profile").read_text() == "tracking\n"
    profiles.forget_active_profile(path)
    profiles.forget_active_profile(path)          # twice is fine
    assert not (tmp_path / "active-profile").exists()
