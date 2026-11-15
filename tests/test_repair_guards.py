"""What the verifier and the mix repair do with what they observed.

The mutation run of 0.10.1 found these untested: the retry's settle wait could drop the
operation's connection, and with it the lease's KEEPALIVEs; the verifier could fall back to a
default timeout; the reapply could lose what the verifier had observed; and the mix repair could
read what it was given, plan without it, refuse without naming what it kept back, or log
nothing, all without a failing test. Each double here has the signature of what it replaces.
"""

import logging
import time

import oracle
import pytest
from backend_doubles import RecordingBackend, echo_link_flags_only
from oscmix_fakes import make_route
from support import osc_bundle

from oscmix_desk import osc, prechecks, routing
from oscmix_desk import verify as verify_api
from oscmix_desk.errors import WriteFailed
from oscmix_desk.model import ChannelSetting, Config, Route
from oscmix_desk.registers import PIN

MAIN = Route("main", playback=(1, 2), output=(5, 6))
#: An owned (PIN) playback mute, the only one a matrix repair writes, beside an input whose
#: AutoSet is declared off: its pair's flags and link are what the repair has to know first.
MUTED = Config(routes=[MAIN], policies={("playback", "mute"): PIN},
               channels=(ChannelSetting("playback", 1, "mute", 1),
                         ChannelSetting("playback", 2, "mute", 1),
                         ChannelSetting("input", 1, "autoset", 0)))


def paths(backend):
    return [path for path, _tags, _args in backend.sent]


def mono_without_its_link(session_mod):
    """Two mono routes and a dump that lacks the link state of their outputs."""
    routes = [make_route(session_mod, name="a", playback=(1,), output=(9,)),
              make_route(session_mod, name="b", playback=(2,), output=(11,))]
    dump = [osc.encode_osc(p, t, *a)
            for route in routes for p, t, a in oracle.route_messages(route)]
    return routes, dump


def test_a_link_the_dump_never_reports_is_retried_on_a_live_lease_and_logged(
        session_mod, verify_mod, monkeypatch, wire_peer, caplog):
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.3)
    monkeypatch.setattr(verify_mod, "VERIFY_SETTLE", 0.05)
    waits = []
    real = verify_mod.wait_unless_stopped

    def wait_unless_stopped(seconds, should_stop, backend=None):
        waits.append(backend)
        return real(seconds, should_stop, backend)

    monkeypatch.setattr(verify_mod, "wait_unless_stopped", wait_unless_stopped)
    routes, dump = mono_without_its_link(session_mod)
    started = time.monotonic()
    with caplog.at_level(logging.INFO), \
            wire_peer(reports=[osc_bundle(dump)], echo=True) as (device, _peer):
        verify_api.verify_and_repair(session_mod.Config(routes=routes), device)
        assert waits
        assert all(backend is device for backend in waits), "the settle wait kept no lease"
    assert time.monotonic() - started < 5, "the verifier waited a default timeout, not its own"
    said = [record.getMessage() for record in caplog.records]
    links = "/output/11/stereo, /output/9/stereo"
    for line in ("2 register(s) unconfirmed (%s); re-sending routing" % links,
                 "dump never reported %s; re-applying mix without confirmation" % links,
                 "mix matrix re-applied against the synchronized link state",
                 "unconfirmed after retry: %s" % links):
        assert line in said, said


def test_the_mix_reapply_takes_what_the_verifier_observed(session_mod, verify_mod, monkeypatch,
                                                          wire_peer):
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.3)
    monkeypatch.setattr(verify_mod, "VERIFY_SETTLE", 0.05)
    reapplied = []

    def send_mix(config, backend, *, confirmed=(), observed=None):
        reapplied.append(observed)

    monkeypatch.setattr(verify_mod, "send_mix", send_mix)
    routes, dump = mono_without_its_link(session_mod)
    with wire_peer(reports=[osc_bundle(dump)], echo=True) as (device, _peer):
        verify_api.verify_and_repair(session_mod.Config(routes=routes), device)
    assert reapplied
    assert all(isinstance(observed, dict) and observed for observed in reapplied)


def test_a_plain_mix_repair_reads_nothing_and_says_so(recording_backend, caplog):
    with caplog.at_level(logging.INFO):
        routing.send_mix(Config(routes=[MAIN]), recording_backend)
    assert recording_backend.dumps == 0
    assert paths(recording_backend)
    assert all(path.startswith("/mix/") for path in paths(recording_backend))
    assert "mix matrix re-applied against the synchronized link state" in caplog.text


def test_a_refused_mix_repair_names_every_write_it_kept_back(recording_backend, monkeypatch):
    seen = []

    def retention_problem(config, selected, retained, confirmed, *, observed=None):
        seen.append(observed)
        return "retained link state is not confirmed for /output/5/stereo"

    monkeypatch.setattr(routing, "retention_problem", retention_problem)
    observed = {"/output/5/stereo": ("i", (1,))}
    with pytest.raises(WriteFailed) as refused:
        routing.send_mix(Config(routes=[MAIN]), recording_backend, observed=observed)
    assert seen == [observed]
    assert "retained link state is not confirmed" in str(refused.value)
    assert list(refused.value.written) == []
    assert list(refused.value.unwritten) == ["/mix/5/playback/1"]
    assert recording_backend.sent == []


def test_the_mix_repair_plans_and_keeps_with_what_was_observed(recording_backend, monkeypatch):
    seen = []
    plan, kept = routing.matrix_repair_plan, routing.remembered_paths

    def matrix_repair_plan(config, confirmed=(), *, observed=None):
        seen.append(("plan", observed))
        return plan(config, confirmed, observed=observed)

    def remembered_paths(config, observed=None, *, linked=()):
        seen.append(("kept", observed))
        return kept(config, observed, linked=linked)

    monkeypatch.setattr(routing, "matrix_repair_plan", matrix_repair_plan)
    monkeypatch.setattr(routing, "remembered_paths", remembered_paths)
    observed = {"/output/5/stereo": ("i", (1,))}
    routing.send_mix(Config(routes=[MAIN]), recording_backend, observed=observed)
    assert seen == [("plan", observed), ("kept", observed)]


def device_with_input_pair(sent):
    return [("/input/1/autoset", "i", (0,)), ("/input/2/autoset", "i", (0,)),
            ("/input/1/stereo", "i", (1,)), *echo_link_flags_only(sent)]


def test_a_playback_mute_repair_reads_its_dependencies_on_its_backend(recording_backend):
    backend = RecordingBackend(reports=device_with_input_pair)
    routing.send_mix(MUTED, backend)
    assert backend.dumps >= 1, "the AutoSet and link dependencies were never read"
    assert paths(backend)


def test_a_playback_mute_repair_given_its_reports_reads_none(recording_backend):
    backend = RecordingBackend(reports=device_with_input_pair)
    observed = {"/input/1/autoset": ("i", (0,)), "/input/2/autoset": ("i", (0,)),
                "/input/1/stereo": ("i", (1,))}
    routing.send_mix(MUTED, backend, observed=observed)
    assert backend.dumps == 0
    assert paths(backend)


def test_a_playback_mute_repair_refused_for_gains_sends_nothing(recording_backend, monkeypatch):
    def gain_problem(config, selected, latest, intent, retained, confirmed):
        return "AutoSet is active or not confirmed off for gain writes: /input/1/gain"

    monkeypatch.setattr(prechecks, "gain_problem", gain_problem)
    backend = RecordingBackend(reports=device_with_input_pair)
    with pytest.raises(WriteFailed) as refused:
        routing.send_mix(MUTED, backend)
    assert "AutoSet is active" in str(refused.value)
    assert list(refused.value.written) == []
    assert refused.value.unwritten
    assert backend.sent == []


def test_a_contradicted_link_withholds_the_reapply_and_says_which(session_mod, verify_mod,
                                                                  monkeypatch, wire_peer, caplog):
    # The device reports both output pairs unlinked although the desk linked them: a mix
    # written now would reach one channel of each pair.
    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.2)
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.3)
    monkeypatch.setattr(verify_mod, "VERIFY_SETTLE", 0.05)
    routes = [make_route(session_mod, name="a", output=(5, 6)),
              make_route(session_mod, name="b", playback=(3, 4), output=(7, 8))]
    dump = [osc.encode_osc(p, t, *(0,) if p.startswith("/output/") and p.endswith("/stereo")
                           else a)
            for route in routes for p, t, a in oracle.route_messages(route)]
    with caplog.at_level(logging.INFO), \
            wire_peer(reports=[osc_bundle(dump)], echo=False) as (device, _peer), \
            pytest.raises(WriteFailed, match="fresh link confirmation required"):
        # The repair re-links them and needs the device to confirm that first.
        verify_api.verify_and_repair(session_mod.Config(routes=routes), device)
    said = [record.getMessage() for record in caplog.records]
    assert ("link state contradicted (/output/5/stereo, /output/7/stereo); "
            "mix reapply withheld") in said, said

