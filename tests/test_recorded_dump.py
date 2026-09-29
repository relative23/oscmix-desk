"""``register_promptly_reported`` against a recorded dump, not a memory.

That function decides whether a missing register is a
warning worth re-sending for or a note. It was a hand-maintained list,
measured once against a UCX II and checked against nothing since -- and
it is about to be the thing 0.3.0's verification classes are derived
from, at ten times the register count.

``tests/data/refresh-dump.json`` is a real ``/refresh`` from the pinned
oscmix revision: which registers arrive, and how long after the request.
No values -- those are the user's mixer state, and the question here is
which registers a dump reports.

Two of these tests state a rule the classification must obey. One states
what the measurement actually found, including where it disagrees with
the prose, so the disagreement cannot quietly become folklore again.
"""

import json

import pytest
from support import repo_file

# Aliased: `registers` is already a local name in the fixtures below,
# for the dict a recording holds.
from oscmix_desk import devices, verify

# Registers this project writes, in the families it cares about.
ROUTED = [
    "/output/1/stereo", "/output/5/stereo", "/output/7/stereo",
    "/playback/1/stereo", "/playback/5/stereo", "/playback/7/stereo",
    "/output/5/volume", "/output/6/volume",
    "/mix/1/playback/1", "/mix/5/playback/1", "/mix/7/playback/1",
]


@pytest.fixture(scope="module")
def dump():
    return json.loads(repo_file("tests", "data", "refresh-dump.json")
                      .read_text())


def test_the_fixture_names_the_revision_it_was_taken_against(dump):
    # A dump is evidence about one build of oscmix. One that
    # does not say which build is not evidence.
    assert len(dump["oscmix_revision"]) == 40
    assert dump["device"] == "Fireface UCX II"
    assert dump["registers"], "empty fixture"


def test_the_pinned_revision_is_the_one_the_dump_came_from(dump):
    # If the pin moves without a re-record, the fixture describes a
    # backend that is no longer shipped -- the exact failure the pin
    # exists to prevent, in the artifact rather than in the release.
    install = repo_file("install.sh").read_text()
    pinned = next(line.split('"')[1].split(":-")[1].rstrip("}")
                  for line in install.splitlines()
                  if line.startswith("OSCMIX_REF="))
    assert dump["oscmix_revision"] == pinned, (
        "tests/data/refresh-dump.json was recorded against %s but "
        "install.sh pins %s -- re-record with scripts/record-dump.py"
        % (dump["oscmix_revision"][:12], pinned[:12]))


def test_a_register_the_dump_never_reports_is_never_called_prompt(
        session_mod, dump):
    """The rule that must hold, or verification retries what it cannot win.

    A register classified prompt but absent from the dump becomes a
    *problem*: the verifier warns and re-sends the whole routing, every
    single run, forever. So absence in the recording is binding.
    """
    reported = set(dump["registers"])
    for path in ROUTED:
        if path not in reported:
            assert not verify.register_promptly_reported(path), (
                "%s is not in the recorded dump, so classifying it as "
                "promptly reported makes every run warn and re-send"
                % path)


def test_a_register_called_prompt_really_does_arrive_in_the_window(
        session_mod, dump):
    """The other direction: a prompt register has to be observable.

    ``VERIFY_TIMEOUT`` bounds the observation window. A register
    classified prompt that arrives after it would be reported as lost on
    every run -- a warning about nothing.
    """
    from oscmix_desk import constants

    registers = dump["registers"]
    for path in ROUTED:
        if path in registers and verify.register_promptly_reported(path):
            _tags, first_seen = registers[path]
            assert first_seen < constants.VERIFY_TIMEOUT, (
                "%s is classified prompt but arrived %.1fs into the dump, "
                "past the %.0fs window" % (path, first_seen,
                                           constants.VERIFY_TIMEOUT))


def test_the_playback_mix_matrix_is_absent_as_documented(dump):
    # The constraint the whole two-phase design rests on: a /mix write
    # draws no reply and the dump omits the playback matrix, so it can
    # only be re-established from a known link state, never verified.
    matrix = [path for path in dump["registers"]
              if path.startswith("/mix/") and "/playback/" in path]
    assert matrix == [], (
        "the dump now reports the playback mix matrix: %s -- if upstream "
        "started dumping it, the matrix becomes verifiable and its "
        "re-established class needs revisiting" % matrix[:5])


def test_the_input_mix_matrix_is_present_as_0_3_0_assumes(dump):
    # The finding 0.3.0's feature set rests on: /mix/<out>/input/<in>
    # *does* appear, so almost all of the new surface is verifiable,
    # unlike the playback matrix this project started with.
    inputs = [path for path in dump["registers"]
              if path.startswith("/mix/") and "/input/" in path]
    assert len(inputs) >= 100, "only %d input mix registers" % len(inputs)


@pytest.mark.parametrize("path", [
    "/input/1/48v", "/input/3/hi-z", "/input/3/reflevel",
    "/output/5/reflevel", "/output/5/mute", "/output/5/phase",
    "/input/1/gain", "/input/1/stereo",
])
def test_the_channel_state_0_3_0_plans_is_verifiable(path, dump):
    # Each of these is a 0.3.0 config option. Their being in the dump is
    # what makes "verified against the device" a promise the next release
    # can keep, and it should fail loudly here if a pin bump removes one.
    assert path in dump["registers"], "%s is not reported by the dump" % path


def test_the_write_only_registers_stay_write_only(dump):
    # Confirmed absent, and therefore unverifiable by construction. The
    # verifier must report these as unverifiable, never as confirmed.
    for path in ("/input/1/name", "/output/1/name", "/output/1/loopback"):
        assert path not in dump["registers"], (
            "%s is reported now; it was write-only when 0.3.0's "
            "verification classes were designed around it" % path)


def test_the_meters_are_the_only_thing_that_streams_unasked(dump):
    # The recorder separates these out; if something else starts
    # streaming, the dump's timing measurements stop meaning what they
    # say and the verifier's early exit may fire on noise.
    unexpected = [path for path in dump["streamed"]
                  if not path.endswith(("/level", "/meter"))]
    assert unexpected == [], "streaming without being asked: %s" % unexpected


def test_the_measured_dump_disagrees_with_the_prose_and_says_so(
        session_mod, dump):
    """The finding this fixture exists to make impossible to forget.

    ``register_promptly_reported`` excludes ``/playback/*`` because "the
    /playback/* section sits near the end of a dump that streams several
    thousand messages over MIDI for many seconds". **Measured against the
    pinned revision, it arrives at 0.0 s and the whole dump takes 1.9 s.**

    That was first measured with only the backend restarted, which left
    a cold *device* as the untested condition and
    LINK_SYNC_BLIND_DELAY=20 with an excuse. It lost the excuse and then
    the value: 5 s since then. See
    tests/data/cold-plug-timeline.json, captured across a real USB
    replug on both OSC ports. /playback/*/stereo arrives at 0.0 s there
    too -- before the session has sent a single message -- and the link
    registers come back 0.01 s after the /refresh that asks for them.

    This test holds the disagreement in place with the number attached,
    so the next person meets a measurement rather than a memory.

    It is also why the discrepancy went unseen: the verify loop exits as
    soon as the *prompt* set matches, so /playback/* never got looked at.
    """
    registers = dump["registers"]
    playback = {path: seen for path, (_tags, seen) in registers.items()
                if path.startswith("/playback/") and path.endswith("/stereo")}
    assert playback, "no /playback/*/stereo in the dump at all"
    assert max(playback.values()) < 1.0, (
        "the prose may be right after all: /playback/*/stereo arrived at "
        "%.1fs" % max(playback.values()))
    assert dump["dump_seconds"] < 5.0, (
        "the dump took %.1fs; the old 15-20s figure may be describing "
        "this after all" % dump["dump_seconds"])
    # At 0.0 s because the backend sends them itself: setrefresh() emits
    # its own view of the playback links before the device answers. The
    # coordinated backend labels that view backend-derived, so it is no
    # hardware evidence and the verifier does not wait for it. See
    # test_playback_links_in_a_cold_plug_come_from_the_backend below.
    assert verify.register_promptly_reported("/playback/1/stereo") is False


# --------------------------------------------------------------------------
# The cold-plug timeline: what the device actually does after a replug.
#
# tests/data/cold-plug-timeline.json was captured with tcpdump on *both*
# OSC ports during a real USB replug, so a request (7222) can be told
# apart from a device push (8222). An earlier capture read only 8222 and
# could not make that distinction, which left a burst of 624 registers
# unattributable.
#
# This is the evidence LINK_SYNC_BLIND_DELAY rests on, and it is the
# condition the prose in this repository has always described without
# ever having measured it.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cold():
    return json.loads(repo_file("tests", "data", "cold-plug-timeline.json")
                      .read_text())


def test_the_cold_plug_timeline_names_its_revision(cold):
    assert len(cold["oscmix_revision"]) == 40
    assert "replug" in cold["condition"]
    assert cold["observed_seconds"] > 120, (
        "too short to say anything about what does *not* happen later")


def test_recorded_output_links_fit_within_the_observation_window(cold):
    # Historical device timing, not a qualification of the new patch series.
    # 0.8.0 has no blind retry for a receive channel held by GTK; the shared
    # backend owns that channel. Contradiction/loss is tested on live Control.
    from oscmix_desk import constants

    links = [t for path, t in cold["first_report_seconds"].items()
             if path.startswith("/output/") and path.endswith("/stereo")]
    assert links
    assert max(links) < constants.VERIFY_TIMEOUT


def test_the_dump_answers_almost_immediately(cold):
    # The /refresh request and the reply that matters, from the same
    # capture: 2.25 s and 2.26 s.
    refresh = [t for t, paths in cold["requests_sent"] if "/refresh" in paths]
    assert len(refresh) == 1, "expected exactly one /refresh: %s" % refresh
    links = [t for p, t in cold["first_report_seconds"].items()
             if p.startswith("/output/") and p.endswith("/stereo")]
    assert min(links) - refresh[0] < 1.0, (
        "the link registers took %.2fs to come back"
        % (min(links) - refresh[0]))


def test_playback_stereo_arrives_first_not_last(cold):
    """Directly contradicts register_promptly_reported's docstring.

    It says the /playback/* section "sits near the end of a dump that
    streams several thousand messages over MIDI for many seconds".
    Measured on a cold replug: 0.0 s, before anything else, and before
    the session had even sent its first message.
    """
    reports = cold["first_report_seconds"]
    playback = {p: t for p, t in reports.items()
                if p.startswith("/playback/") and p.endswith("/stereo")}
    assert playback
    assert max(playback.values()) < 1.0
    first_request = min(t for t, _paths in cold["requests_sent"])
    assert min(playback.values()) < first_request, (
        "the device reported the playback links before anything asked")


def test_the_cold_dump_is_incomplete_and_that_matters_for_0_3_0(cold, dump):
    """The finding that outlives this measurement.

    A cold plug delivers a fraction of the register set within seconds
    and the rest may not arrive for minutes -- 1234 of 1932 non-meter
    registers here, with 276 s of observation and no second burst.

    Everything *this* release verifies is in the fast part, which is why
    nothing has ever noticed. 0.3.0's `[output:N]` sections are not:
    /output/5/reflevel, /output/5/mute and /output/5/phase were never
    reported at all. A verification class derived from the warm dump
    would call them verifiable and then report them unconfirmed on every
    cold boot.
    """
    warm = {p for p in dump["registers"]
            if "/level" not in p and "/meter" not in p}
    cold_seen = set(cold["first_report_seconds"])
    assert len(cold_seen) < len(warm), "the cold dump was complete after all"
    missing = warm - cold_seen
    assert len(missing) > 100, "only %d missing" % len(missing)

    # What this release depends on is present regardless.
    for path in ("/output/1/stereo", "/output/5/stereo", "/output/7/stereo",
                 "/output/1/volume", "/output/2/volume"):
        assert path in cold_seen, "%s missing from a cold plug" % path

    # What 0.3.0 plans to verify is not.
    assert {"/output/5/reflevel", "/output/5/mute"} & missing, (
        "the 0.3.0 channel-state registers came back this time; re-check "
        "whether the warning in this test still applies")






def test_playback_links_in_a_cold_plug_come_from_the_backend(cold):
    """Why /playback/*/stereo is re-established, not verified.

    A cold plug is where the dump is *incomplete* -- 1234 of 1932
    non-meter registers, and `/output/<n>/mute` came back for channels
    1, 2, 3, 8, 9 and 10 but not 4-7 or 11-20. The playback link flags
    came back all twenty, at t=0.00 s, before anything else: no device
    register backs them, and setrefresh() sends the backend's own view
    before the device says anything. The coordinated backend labels that
    view backend-derived, which the desk never counts as confirmation.
    """
    first = cold["first_report_seconds"]
    stereo = {path: seen for path, seen in first.items()
              if path.startswith("/playback/") and path.endswith("/stereo")}
    assert len(stereo) == 20, (
        "only %d of 20 /playback/<n>/stereo came back" % len(stereo))
    assert max(stereo.values()) == 0.0, (
        "the latest arrived at %.2fs, so 'first' is too strong"
        % max(stereo.values()))


def test_no_channel_setting_is_called_prompt_unless_the_cold_plug_proves_it(
        cold):
    """The rule, checked against the recording rather than by hand.

    `register_promptly_reported` decides whether an absent register is
    re-sent. Saying yes for something a cold plug does not deliver means
    re-sending it on every hotplug -- which is the whole reason the rule
    exists, stated in its own docstring.

    It was asked of `settable_options`, which knows only a family's flat
    options, so every nested one fell through to "yes": 480 EQ registers
    of which the recording shows 332 arriving. Written as a sweep over
    the model so the next nested family cannot reintroduce it silently.
    """
    arrived = set(cold["first_report_seconds"])
    wrongly_prompt = []
    for register in devices.UCX2.registers:
        if register.domain is None:
            continue
        if not register.template.startswith(("/input/{ch}/",
                                             "/output/{ch}/")):
            continue
        paths = [register.template.format(ch=channel)
                 for channel in devices.UCX2.channels[register.channels]]
        if all(path in arrived for path in paths):
            continue            # complete after a cold plug, so "yes" is right
        wrongly_prompt += [path for path in paths
                           if verify.register_promptly_reported(
                               path, devices.UCX2)]
    assert wrongly_prompt == [], (
        "%d paths a cold plug does not deliver are still called prompt, "
        "so a hotplug re-sends them: %s"
        % (len(wrongly_prompt), sorted(wrongly_prompt)[:4]))
