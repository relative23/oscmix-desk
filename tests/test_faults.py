"""Report loss, reordering and interrupted operations over the real control client.

The device can omit or repeat individual register reports. Loss of the control
connection is different: it invalidates the operation and forbids further writes.
"""

import random
import threading
import time

import oracle
import pytest
from support import osc_bundle

from oscmix_desk import osc
from oscmix_desk.errors import ReceivePortError, WriteFailed


def make_route(session_mod):
    return session_mod.Route(name="monitors", playback=(1, 2), output=(5, 6),
                             level=0.0, volume=0.0, stereo=True)


def full_dump(session_mod, route):
    return [osc.encode_osc(path, types, *args)
            for path, types, args in oracle.route_messages(route)]


def run_under(session_mod, wire_peer, *, drop=0., duplicate=False, reorder=False, seed=1):
    route = make_route(session_mod)
    messages = full_dump(session_mod, route)
    rng = random.Random(seed)
    if duplicate:
        messages += list(messages)
    if reorder:
        rng.shuffle(messages)
    messages = [m for m in messages if rng.random() >= drop]
    config = session_mod.Config(routes=[route])
    with wire_peer(reports=[osc_bundle(messages)]) as (device, peer):
        if drop == 1.0:
            with pytest.raises(WriteFailed, match="retained partner") as failure:
                session_mod.verify_and_repair(config, device)
            assert failure.value.written == ()
            assert "/output/5/stereo" in failure.value.unwritten
        else:
            session_mod.verify_and_repair(config, device)
    return peer


@pytest.mark.parametrize("drop", [0., .3, .7, 1.])
def test_loss_preserves_remember_and_refuses_unsafe_link_repair(
        session_mod, verify_mod, monkeypatch, drop, wire_peer):
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", .3)
    peer = run_under(session_mod, wire_peer, drop=drop)
    assert ("/mix/5/playback/1" in peer.order) == (drop < 1.0)
    assert not any(path.endswith("/volume") for path in peer.order)


def test_duplicated_reports_do_not_confuse_the_read_back(session_mod, wire_peer):
    peer = run_under(session_mod, wire_peer, duplicate=True)
    assert peer.order.count("/mix/5/playback/1") == 1


def test_reordered_reports_still_verify(session_mod, wire_peer):
    peer = run_under(session_mod, wire_peer, reorder=True, seed=7)
    assert "/mix/5/playback/1" in peer.order


def test_applying_routing_survives_a_device_that_never_answers(
        session_mod, routing_mod, monkeypatch, wire_peer):
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", .3)
    config = session_mod.Config(routes=[make_route(session_mod)])
    started = time.monotonic()
    with wire_peer() as (device, peer):
        session_mod.apply_routing(config, device)
        assert "/mix/5/playback/1" in peer.order
    assert time.monotonic() - started < 3., "the barrier did not time out"


def test_a_closed_connection_refuses_all_writes(session_mod, wire_peer):
    config = session_mod.Config(routes=[make_route(session_mod)])
    with wire_peer() as (device, peer):
        device.close()
        with pytest.raises(WriteFailed) as failure:
            session_mod.apply_routing(config, device)
        assert failure.value.written == ()
        assert set(failure.value.unwritten) == {
            p for p, _, _ in oracle.route_messages(config.routes[0])}
        assert peer.writes == []


def test_verification_survives_a_flood_of_unrelated_registers(
        session_mod, verify_mod, monkeypatch, wire_peer):
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 1.)
    route = make_route(session_mod)
    noise = [osc.encode_osc("/input/%d/gain" % channel, "f", 12.) for channel in range(1, 200)]
    started = time.monotonic()
    with wire_peer(reports=[osc_bundle(noise + full_dump(session_mod, route))]) as (device, peer):
        session_mod.verify_and_repair(session_mod.Config(routes=[route]), device)
        assert "/mix/5/playback/1" in peer.order
    assert time.monotonic() - started < 3.


def test_backend_loss_after_links_records_partial_apply_and_no_later_phase(session_mod, wire_peer):
    config = session_mod.Config(routes=[make_route(session_mod)])
    with wire_peer(disconnect_after="/output/5/stereo") as (device, peer):
        with pytest.raises(WriteFailed, match="disconnected") as failure:
            session_mod.apply_routing(config, device)
        assert peer.order == ["/playback/1/stereo", "/output/5/stereo"]
        assert failure.value.written == tuple(peer.order)
        assert failure.value.unwritten == ("/mix/5/playback/1", "/output/5/volume",
                                           "/output/6/volume")


def test_a_device_vanishing_mid_dump_invalidates_the_read_and_prevents_repair(
        session_mod, verify_mod, monkeypatch, wire_peer):
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", .5)
    route = make_route(session_mod)
    partial = full_dump(session_mod, route)[:1]
    started = time.monotonic()
    with wire_peer(reports=partial, close_after_dump=True) as (device, peer):
        with pytest.raises(ReceivePortError, match="disconnected"):
            session_mod.verify_and_repair(session_mod.Config(routes=[route]), device)
        assert peer.order == ["/refresh"]
        assert peer.writes == []
    assert time.monotonic() - started < 2.


def test_backend_loss_between_verification_and_retry_has_no_blind_fallback(
        session_mod, verify_mod, monkeypatch, wire_peer):
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", .1)
    route = session_mod.Route(name="main", playback=(1, 2), output=(5, 6))
    real_verify = verify_mod.verify_routing

    def verify_then_disconnect(registers, device, *args, **kwargs):
        result = real_verify(registers, device, *args, **kwargs)
        device.close()
        return result

    monkeypatch.setattr(verify_mod, "verify_routing", verify_then_disconnect)
    with wire_peer() as (device, peer):
        with pytest.raises(WriteFailed) as failure:
            session_mod.verify_and_repair(session_mod.Config(routes=[route]), device)
        assert failure.value.written == ()
        assert peer.order == ["/refresh"]
        assert peer.writes == []


@pytest.mark.parametrize("cycle", range(12))
def test_applying_the_routing_is_repeatable(session_mod, wire_peer, cycle):
    with wire_peer(echo=True) as (device, peer):
        session_mod.apply_routing(session_mod.Config(routes=[make_route(session_mod)]), device)
        assert "/mix/5/playback/1" in peer.order
        assert peer.order.index("/output/5/stereo") < peer.order.index("/mix/5/playback/1")


def test_a_stop_between_the_phases_prevents_every_further_write(
        session_mod, verify_mod, wire_peer):
    with wire_peer() as (device, peer):
        complete = verify_mod.verify_and_repair(
            session_mod.Config(routes=[make_route(session_mod)]), device, lambda: True)
        assert complete is False
        assert peer.order == []


def test_the_verifier_runs_normally_when_nothing_asks_it_to_stop(session_mod, wire_peer):
    peer = run_under(session_mod, wire_peer)
    assert peer.order == ["/refresh", "/mix/5/playback/1"]


def test_operation_wait_is_abandoned_promptly_on_stop(routing_mod, wire_peer):
    with wire_peer() as (device, peer):
        started = time.monotonic()
        deadline = started + .2
        assert routing_mod.wait_unless_stopped(5., lambda: time.monotonic() > deadline,
                                               device) is True
        assert time.monotonic() - started < 1.
        assert peer.writes == []


def test_the_wait_returns_promptly_and_reports_why(routing_mod):
    started = time.monotonic()
    assert routing_mod.wait_unless_stopped(10., lambda: True) is True
    assert time.monotonic() - started < .5
    started = time.monotonic()
    assert routing_mod.wait_unless_stopped(.3, routing_mod.never_stop) is False
    assert time.monotonic() - started >= .3


def test_the_dump_window_ends_when_a_stop_arrives(session_mod, verify_mod, wire_peer):
    registers = verify_mod.expected_registers(session_mod.Config(routes=[make_route(session_mod)]))
    with wire_peer() as (device, peer):
        started = time.monotonic()
        result = verify_mod.verify_routing(registers, device, timeout=10.,
                                            should_stop=lambda: True)
        assert time.monotonic() - started < 1.
        assert result.confirmed == []
        assert peer.order == []


def test_the_session_waits_for_the_verifier_before_exiting(session_mod):
    from oscmix_desk import constants, session

    # The grace has to fit inside TimeoutStopSec next to CHILD_STOP_GRACE,
    # or systemd kills the session during exactly the wait that exists to
    # stop it being killed.
    assert (constants.VERIFIER_STOP_GRACE + constants.CHILD_STOP_GRACE
            < 10.0)

    # A thread that refuses to stop is bounded, not waited on forever.
    forever = threading.Event()
    thread = threading.Thread(target=forever.wait, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        session._await_verifier(thread)
        elapsed = time.monotonic() - started
        assert constants.VERIFIER_STOP_GRACE <= elapsed < \
            constants.VERIFIER_STOP_GRACE + 1.0
    finally:
        forever.set()
        thread.join(timeout=2)

    # A verifier that already finished costs nothing.
    done = threading.Thread(target=lambda: None, daemon=True)
    done.start()
    done.join(timeout=2)
    started = time.monotonic()
    session._await_verifier(done)
    session._await_verifier(None)
    assert time.monotonic() - started < 0.5
