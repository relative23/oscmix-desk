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
from profile_desk import GOOD, TRACKING, desk
from support import write_config

from oscmix_desk import outcome as outcome_mod
from oscmix_desk import paths as paths_mod
from oscmix_desk import profiles

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
# What could not be checked, said for what it is.
# --------------------------------------------------------------------------


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
# A switch that is asked not to check.
# --------------------------------------------------------------------------


def test_a_switch_can_be_asked_not_to_check(tmp_path, recording_backend):
    path = desk(tmp_path, tracking=TRACKING)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=recording_backend, verify=False)
    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert outcome.name == "tracking"
    assert outcome.reason == outcome_mod.NOT_CHECKED
    assert outcome.persisted is True, "the marker was written either way"
    assert outcome.unverified == sorted(
        profiles.expected_registers(profiles.load_profile("tracking", path)))


# --------------------------------------------------------------------------
# One switch at a time.
# --------------------------------------------------------------------------


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
    path = desk(tmp_path, tracking=TRACKING, mixdown=GOOD)
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
# Which interface a switch is for, and whether it can be reached.
# --------------------------------------------------------------------------

def test_the_device_key_names_the_box_not_the_file(tmp_path):
    from support import fake_proc

    from oscmix_desk.discovery import resolve_device

    proc = fake_proc(tmp_path / "proc", boxes=[(24, "24216011")])
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc).key == \
        "2a39-3fd9-24216011"
    # No interface to be had: the model alone.
    empty = fake_proc(tmp_path / "empty")
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", empty).key == \
        "2a39-3fd9-unknown"


# --------------------------------------------------------------------------
# No writing to an absent device (ADR 0023).
# --------------------------------------------------------------------------


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
