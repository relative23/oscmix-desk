"""The mix matrix is written again once the device has reported the
links, through the same operation connection (ADR 0001, ADR 0030).
"""

import oracle
import pytest
from control_peer import REFRESH
from oscmix_fakes import make_route
from support import osc_bundle

from oscmix_desk import osc, routing
from oscmix_desk.errors import ReceivePortError


def run_verify_and_repair(session_mod, routes, dump, *, wire_peer):
    with wire_peer(reports=[osc_bundle(dump)], echo=True) as (device, peer):
        session_mod.verify_and_repair(session_mod.Config(routes=routes), device)
    return peer

def full_dump(session_mod, routes):
    return [osc.encode_osc(path, types, *args)
            for route in routes
            for path, types, args in oracle.route_messages(route)]

def test_mix_is_reapplied_once_the_dump_reports_the_link_state(session_mod, wire_peer):
    # The dump is what teaches oscmix the device's real link state, so the
    # matrix is rewritten off the back of it rather than from a guess.
    routes = [make_route(session_mod, volume=0.0)]
    device = run_verify_and_repair(session_mod, routes,
                                   full_dump(session_mod, routes), wire_peer=wire_peer)
    assert device.order == ["/refresh", "/mix/5/playback/1"]

def test_reapply_repeats_no_link_message(session_mod, wire_peer):
    """Only the matrix is rewritten; re-linking could restart the race.

    The old fixture sent five dump registers as separate UDP datagrams.
    It failed once in CI (run 32067475867, repeat 4 of 5), then the same
    mechanism returned in the 0.5.2 mutation gate: one unconfirmed
    register took `verify_and_repair` down its retry branch and rewrote
    the links.

    `/playback/1/stereo` and
    `/output/5/stereo` can reach the device fake here only from the
    *retry* branch of `verify_and_repair`, which fires when an expected
    register stays unconfirmed and then calls `apply_routing` -- and
    that rewrites the links. The scripted peer models one delivery by
    returning the dump in one OSC bundle, so this loss-free test cannot
    stumble into that branch because of scheduling between datagrams.
    Deliberate report loss remains in tests/test_faults.py, where it is
    injected before bundling and its degraded result is asserted.
    """
    routes = [make_route(session_mod, volume=0.0)]
    device = run_verify_and_repair(session_mod, routes,
                                   full_dump(session_mod, routes), wire_peer=wire_peer)
    assert [p for p in device.order if p.endswith("/stereo")] == [], (
        "links were rewritten -- either the re-apply path re-links (a "
        "real defect) or verification took its retry branch because a "
        "register went unconfirmed (see this test's docstring)")

def test_mix_is_reapplied_even_when_the_dump_omits_the_links(verify_mod, session_mod,
                                                             monkeypatch, wire_peer):
    # Degraded beats silent: without the link report the state is unknown,
    # but leaving the matrix as written at startup is the worse option.
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.3)
    routes = [make_route(session_mod)]
    dump = [osc.encode_osc(path, types, *args)
            for path, types, args in oracle.route_messages(routes[0])
            if path != "/output/5/stereo"]
    device = run_verify_and_repair(session_mod, routes, dump, wire_peer=wire_peer)
    assert device.order.count("/mix/5/playback/1") >= 1

def test_refresh_disconnect_cannot_trigger_blind_reapply(session_mod, wire_peer):
    routes = [make_route(session_mod, volume=0.0)]
    with wire_peer(lose_reply=REFRESH) as (device, peer):
        with pytest.raises(ReceivePortError, match="disconnected"):
            session_mod.verify_and_repair(session_mod.Config(routes=routes), device)
        assert peer.order == ["/refresh"]
        assert peer.writes == []

def test_routes_without_pairs_still_verify(session_mod, wire_peer):
    # A mono-only routing has no links to wait for; the re-apply must not
    # block on a report that can never come.
    routes = [make_route(session_mod, playback=(1,), output=(9,))]
    device = run_verify_and_repair(session_mod, routes,
                                   full_dump(session_mod, routes), wire_peer=wire_peer)
    assert device.order[0] == "/refresh"

def test_send_mix_writes_the_matrix_without_the_links(session_mod, wire_peer):
    config = session_mod.Config(routes=[make_route(session_mod, volume=0.0)])
    with wire_peer() as (device, peer):
        routing.send_mix(config, device)
        assert peer.order == ["/mix/5/playback/1"]


def test_send_mix_deduplicates_two_routes_without_channel_writes(session_mod, wire_peer):
    routes = [make_route(session_mod, name="a", volume=0.0),
              make_route(session_mod, name="b", playback=(3, 4), volume=0.0)]
    config = session_mod.Config(routes=routes)
    with wire_peer() as (device, peer):
        routing.send_mix(config, device)
        assert peer.order == ["/mix/5/playback/1", "/mix/5/playback/3"]
        assert not any(path.endswith("/stereo") for path in peer.order)

def test_verify_result_separates_the_three_verdicts(session_mod):
    # The type the read-back reports through: confirmed, mismatched and
    # unobserved mean different things and must not be conflated.
    result = session_mod.VerifyResult(confirmed=["/output/5/stereo"],
                                      mismatched=["/output/5/volume"],
                                      unobserved=["/mix/5/playback/1"])
    assert result.confirmed == ["/output/5/stereo"]
    assert result.mismatched == ["/output/5/volume"]
    assert result.unobserved == ["/mix/5/playback/1"]
