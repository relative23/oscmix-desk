"""The mix matrix is written again once the device has reported the
links, and blind when it cannot be asked (ADR 0001).
"""

import socket

import oracle
from oscmix_fakes import DumpingOscmix, make_config, make_route
from support import free_udp_port

from oscmix_desk import osc, routing


def run_verify_and_repair(session_mod, routes, dump, recv_port=None,
                          blocked=False):
    send_port = free_udp_port()
    recv_port = recv_port or free_udp_port()
    blocker = None
    if blocked:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", recv_port))
    device = DumpingOscmix(session_mod, send_port, recv_port, dump)
    device.start()
    try:
        session_mod.verify_and_repair(
            make_config(session_mod, routes, send_port, recv_port))
        device.drain()
    finally:
        if blocker is not None:
            blocker.close()
        device.stop()
        device.join(timeout=5)
        device.sock.close()
    return device

def full_dump(session_mod, routes):
    return [osc.encode_osc(path, types, *args)
            for route in routes
            for path, types, args in oracle.route_messages(route)]

def test_mix_is_reapplied_once_the_dump_reports_the_link_state(session_mod):
    # The dump is what teaches oscmix the device's real link state, so the
    # matrix is rewritten off the back of it rather than from a guess.
    routes = [make_route(session_mod, volume=0.0)]
    device = run_verify_and_repair(session_mod, routes,
                                   full_dump(session_mod, routes))
    assert device.order == ["/refresh", "/mix/5/playback/1",
                            "/output/5/volume", "/output/6/volume"]

def test_reapply_repeats_no_link_message(session_mod):
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
    that rewrites the links. `DumpingOscmix` now mirrors upstream by
    returning the dump in one OSC bundle, so this loss-free test cannot
    stumble into that branch because of scheduling between datagrams.
    Deliberate report loss remains in tests/test_faults.py, where it is
    injected before bundling and its degraded result is asserted.
    """
    routes = [make_route(session_mod, volume=0.0)]
    device = run_verify_and_repair(session_mod, routes,
                                   full_dump(session_mod, routes))
    assert [p for p in device.order if p.endswith("/stereo")] == [], (
        "links were rewritten -- either the re-apply path re-links (a "
        "real defect) or verification took its retry branch because a "
        "register went unconfirmed (see this test's docstring)")

def test_mix_is_reapplied_even_when_the_dump_omits_the_links(verify_mod, session_mod,
                                                             monkeypatch):
    # Degraded beats silent: without the link report the state is unknown,
    # but leaving the matrix as written at startup is the worse option.
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.3)
    routes = [make_route(session_mod)]
    dump = [osc.encode_osc(path, types, *args)
            for path, types, args in oracle.route_messages(routes[0])
            if path != "/output/5/stereo"]
    device = run_verify_and_repair(session_mod, routes, dump)
    assert device.order.count("/mix/5/playback/1") >= 1

def test_blind_reapply_when_the_receive_port_is_taken(routing_mod, session_mod,
                                                      monkeypatch):
    # The mixer GUI holds the port: nothing can be observed, so /refresh
    # still goes out to sync oscmix and the matrix follows after a wait.
    monkeypatch.setattr(routing_mod, "LINK_SYNC_BLIND_DELAY", 0.05)
    routes = [make_route(session_mod, volume=0.0)]
    device = run_verify_and_repair(session_mod, routes, [], blocked=True)
    assert device.order == ["/refresh", "/mix/5/playback/1",
                            "/output/5/volume", "/output/6/volume"]

def test_routes_without_pairs_still_verify(session_mod):
    # A mono-only routing has no links to wait for; the re-apply must not
    # block on a report that can never come.
    routes = [make_route(session_mod, playback=(1,), output=(9,))]
    device = run_verify_and_repair(session_mod, routes,
                                   full_dump(session_mod, routes))
    assert device.order[0] == "/refresh"

def test_send_mix_writes_the_matrix_without_the_links(session_mod):
    # The re-apply path used after the dump: only the matrix, because
    # re-linking would restart the very race it repairs.
    send_port, recv_port = free_udp_port(), free_udp_port()
    device = DumpingOscmix(session_mod, send_port, recv_port, [])
    device.start()
    config = make_config(session_mod, [make_route(session_mod, volume=0.0)],
                         send_port, recv_port)
    try:
        routing.send_mix(config)
        device.drain()
    finally:
        device.stop()
        device.join(timeout=3)
        device.sock.close()
    assert device.order == ["/mix/5/playback/1", "/output/5/volume",
                            "/output/6/volume"]

def test_send_mix_writes_a_register_two_routes_share_once(session_mod):
    """The re-apply goes through the planner now, so it deduplicates.

    Two routes feeding the same output pair both declare that pair's
    volume; the old route-by-route walk sent /output/5/volume and
    /output/6/volume twice. A state holds each register once, and the
    position is the first route's -- the same rule the apply follows.
    """
    send_port, recv_port = free_udp_port(), free_udp_port()
    device = DumpingOscmix(session_mod, send_port, recv_port, [])
    device.start()
    routes = [make_route(session_mod, name="a", volume=0.0),
              make_route(session_mod, name="b", playback=(3, 4), volume=0.0)]
    config = make_config(session_mod, routes, send_port, recv_port)
    try:
        routing.send_mix(config)
        device.drain()
    finally:
        device.stop()
        device.join(timeout=3)
        device.sock.close()
    assert device.order == ["/mix/5/playback/1", "/output/5/volume",
                            "/output/6/volume", "/mix/5/playback/3"]
    assert not any(path.endswith("/stereo") for path in device.order)

def test_blind_reapply_asks_for_a_dump_then_writes(session_mod, routing_mod,
                                                   monkeypatch):
    # Used when the mixer GUI holds the receive port: the dump still has
    # to go out, because it is what teaches oscmix the link state.
    monkeypatch.setattr(routing_mod, "LINK_SYNC_BLIND_DELAY", 0.05)
    send_port, recv_port = free_udp_port(), free_udp_port()
    device = DumpingOscmix(session_mod, send_port, recv_port, [])
    device.start()
    config = make_config(session_mod, [make_route(session_mod)], send_port,
                         recv_port)
    try:
        routing.blind_reapply_mix(config)
        device.drain()
    finally:
        device.stop()
        device.join(timeout=3)
        device.sock.close()
    assert device.order == ["/refresh", "/mix/5/playback/1"]

def test_verify_result_separates_the_three_verdicts(session_mod):
    # The type the read-back reports through: confirmed, mismatched and
    # unobserved mean different things and must not be conflated.
    result = session_mod.VerifyResult(confirmed=["/output/5/stereo"],
                                      mismatched=["/output/5/volume"],
                                      unobserved=["/mix/5/playback/1"])
    assert result.confirmed == ["/output/5/stereo"]
    assert result.mismatched == ["/output/5/volume"]
    assert result.unobserved == ["/mix/5/playback/1"]
