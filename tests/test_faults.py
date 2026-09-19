"""Behaviour when the environment misbehaves.

Every failure this project has actually shipped was a timing or delivery
problem, not a logic error: a link message overtaken by its own mix, two
teardown races, a stub that installed its signal handler too late. So the
adversary here is the transport and the clock, not the input.

OSC over UDP has no delivery guarantee, while an overloaded backend can
also lose individual register events before it builds a reply. These
tests inject dropped, duplicated, reordered and delayed reports and
assert the property that has to survive all of it: the routing is still
written, and the process still exits cleanly.
"""

import random
import socket
import struct
import threading
import time

import oracle
import pytest
from support import free_udp_port, osc_bundle

from oscmix_desk import osc


class LossyDevice(threading.Thread):
    """A device stand-in that mistreats reports before bundling them.

    ``drop`` discards a fraction, ``duplicate`` reports registers twice,
    and ``reorder`` shuffles reports within the reply bundle.
    """

    def __init__(self, session_mod, send_port, recv_port, dump=(),
                 drop=0.0, duplicate=False, reorder=False, seed=1):
        super().__init__(daemon=True)
        self.session_mod = session_mod
        self.recv_port = recv_port
        self.dump = list(dump)
        self.drop = drop
        self.duplicate = duplicate
        self.reorder = reorder
        self.random = random.Random(seed)
        self.received = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", send_port))
        self.sock.settimeout(0.2)
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def drain(self, quiet=0.3, limit=5.0):
        deadline = time.monotonic() + limit
        seen = -1
        while time.monotonic() < deadline:
            if len(self.received) == seen:
                return
            seen = len(self.received)
            time.sleep(quiet)

    def reply(self):
        messages = list(self.dump)
        if self.duplicate:
            messages += list(self.dump)
        if self.reorder:
            self.random.shuffle(messages)
        messages = [message for message in messages
                    if self.random.random() >= self.drop]
        try:
            self.sock.sendto(osc_bundle(messages),
                             ("127.0.0.1", self.recv_port))
        except OSError:
            return

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            for message in osc.iter_osc_messages(data):
                try:
                    path, _tags, _args = osc.decode_osc(message)
                except (ValueError, struct.error):
                    continue
                self.received.append(path)
                if path == "/refresh":
                    self.reply()


def make_route(session_mod):
    return session_mod.Route(name="monitors", playback=(1, 2), output=(5, 6),
                             level=0.0, volume=0.0, stereo=True)


def full_dump(session_mod, route):
    return [osc.encode_osc(path, types, *args)
            for path, types, args in oracle.route_messages(route)]


def run_under(session_mod, **device_options):
    send_port, recv_port = free_udp_port(), free_udp_port()
    route = make_route(session_mod)
    device = LossyDevice(session_mod, send_port, recv_port,
                         dump=full_dump(session_mod, route), **device_options)
    device.start()
    config = session_mod.Config(routes=[route], osc_port=send_port,
                                osc_recv_port=recv_port)
    try:
        session_mod.verify_and_repair(config)
        device.drain()
    finally:
        device.stop()
        device.join(timeout=5)
        device.sock.close()
    return device


@pytest.mark.parametrize("drop", [0.0, 0.3, 0.7, 1.0])
def test_the_mix_is_re_applied_however_much_the_dump_loses(session_mod,
                                                           verify_mod,
                                                           monkeypatch, drop):
    # Losing the dump must never mean losing the routing. With everything
    # dropped the link state is unknown, and the mix still has to go out:
    # degraded beats silent.
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.4)
    device = run_under(session_mod, drop=drop)
    assert "/mix/5/playback/1" in device.received


def test_duplicated_reports_do_not_confuse_the_read_back(session_mod):
    # A register reported twice is still one register.
    device = run_under(session_mod, duplicate=True)
    assert device.received.count("/mix/5/playback/1") == 1


def test_reordered_reports_still_verify(session_mod):
    # Nothing may depend on the dump's order: it is a stream of registers
    # with no promised sequence.
    device = run_under(session_mod, reorder=True, seed=7)
    assert "/mix/5/playback/1" in device.received


def test_applying_routing_survives_a_device_that_never_answers(session_mod,
                                                               routing_mod,
                                                               monkeypatch):
    # The barrier waits for a report that may never come. It must time
    # out and write the mix anyway, within its own timeout.
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.3)
    send_port, recv_port = free_udp_port(), free_udp_port()
    route = make_route(session_mod)
    device = LossyDevice(session_mod, send_port, recv_port, drop=1.0)
    device.start()
    started = time.monotonic()
    try:
        session_mod.apply_routing(session_mod.Config(routes=[route]), send_port, recv_port)
        device.drain()
    finally:
        device.stop()
        device.join(timeout=5)
        device.sock.close()
    assert "/mix/5/playback/1" in device.received
    assert time.monotonic() - started < 3.0, "the barrier did not time out"


def test_a_dead_backend_port_does_not_raise(session_mod, routing_mod,
                                            monkeypatch):
    # Nothing is listening at all: sending to a closed UDP port can raise
    # ECONNREFUSED on Linux once an ICMP error has been seen. Applying a
    # routing must not turn that into a crashed service.
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.1)
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.05)
    route = make_route(session_mod)
    for _ in range(3):
        session_mod.apply_routing(session_mod.Config(routes=[route]),
                                  free_udp_port(), free_udp_port())


def test_verification_survives_a_flood_of_unrelated_registers(session_mod,
                                                              verify_mod,
                                                              monkeypatch):
    # A real dump is thousands of registers we never asked about. The
    # read-back must not slow to a crawl or mistake one for an answer.
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 1.0)
    send_port, recv_port = free_udp_port(), free_udp_port()
    route = make_route(session_mod)
    noise = [osc.encode_osc("/input/%d/gain" % channel, "f", 12.0)
             for channel in range(1, 200)]
    device = LossyDevice(session_mod, send_port, recv_port,
                         dump=noise + full_dump(session_mod, route))
    device.start()
    config = session_mod.Config(routes=[route], osc_port=send_port,
                                osc_recv_port=recv_port)
    started = time.monotonic()
    try:
        session_mod.verify_and_repair(config)
        device.drain()
    finally:
        device.stop()
        device.join(timeout=5)
        device.sock.close()
    assert time.monotonic() - started < 8.0
    assert "/mix/5/playback/1" in device.received


# --------------------------------------------------------------------------
# Faults that tear state rather than packets.
#
# Everything above disturbs the transport and leaves the process intact.
# Every defect this project has actually shipped had the opposite shape:
# something changed underneath a half-finished transaction. These are the
# three windows where that can happen.
# --------------------------------------------------------------------------


class DyingDevice(threading.Thread):
    """A backend that disappears partway through a sequence.

    ``die_after`` names the OSC path whose arrival is the last thing it
    handles; the socket closes immediately afterwards, the way a killed
    process stops answering mid-conversation.
    """

    def __init__(self, session_mod, send_port, recv_port, die_after,
                 dump=()):
        super().__init__(daemon=True)
        self.session_mod = session_mod
        self.recv_port = recv_port
        self.die_after = die_after
        self.dump = list(dump)
        self.received = []
        self.died_after = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", send_port))
        self.sock.settimeout(0.2)
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            for message in osc.iter_osc_messages(data):
                try:
                    path, _tags, _args = osc.decode_osc(message)
                except (ValueError, struct.error):
                    continue
                self.received.append(path)
                if path == "/refresh":
                    for register in self.dump:
                        try:
                            self.sock.sendto(register,
                                             ("127.0.0.1", self.recv_port))
                        except OSError:
                            return
                if path == self.die_after:
                    self.died_after = path
                    self.sock.close()
                    return


def test_a_backend_killed_between_the_phases_does_not_crash_the_session(
        session_mod, routing_mod, monkeypatch):
    # The one window where the routing is knowingly half-applied: links
    # written, mix not yet. Losing the backend here must end as a logged
    # failure, not as an exception out of apply_routing -- the session
    # still has to reach its exit-code decision.
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.2)
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.05)
    send_port, recv_port = free_udp_port(), free_udp_port()
    device = DyingDevice(session_mod, send_port, recv_port,
                         die_after="/output/5/stereo")
    device.start()
    try:
        session_mod.apply_routing(session_mod.Config(routes=[make_route(session_mod)]), send_port,
                                  recv_port)
    finally:
        device.stop()
        device.join(timeout=5)
    assert device.died_after == "/output/5/stereo"
    assert "/playback/1/stereo" in device.received


def test_a_device_vanishing_mid_dump_still_leaves_the_mix_applied(
        session_mod, verify_mod, monkeypatch):
    # Unplugged while /refresh is still streaming: the read-back gets a
    # truncated dump and no more. It must return a verdict rather than
    # hang, and the matrix must still be written -- an interrupted
    # verification is not a reason to leave the routing half-done.
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.5)
    send_port, recv_port = free_udp_port(), free_udp_port()
    route = make_route(session_mod)
    partial = full_dump(session_mod, route)[:1]      # links only, then gone
    device = DyingDevice(session_mod, send_port, recv_port,
                         die_after="/refresh", dump=partial)
    device.start()
    config = session_mod.Config(routes=[route], osc_port=send_port,
                                osc_recv_port=recv_port)
    started = time.monotonic()
    try:
        session_mod.verify_and_repair(config)
    finally:
        device.stop()
        device.join(timeout=5)
    assert time.monotonic() - started < 10.0, "the read-back did not give up"
    assert device.received.count("/refresh") >= 1


def test_the_receive_port_taken_between_attempts_falls_back_blind(
        session_mod, verify_mod, routing_mod, monkeypatch):
    # The mixer GUI opens *during* verification. The first attempt reads
    # the dump; the retry finds the port gone. That path must end in the
    # blind re-apply, not in an exception -- test_verify.py only covers
    # the port being taken from the start, which returns early and never
    # reaches the retry.
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.3)
    monkeypatch.setattr(routing_mod, "LINK_SYNC_BLIND_DELAY", 0.05)
    send_port, recv_port = free_udp_port(), free_udp_port()
    route = make_route(session_mod)
    device = LossyDevice(session_mod, send_port, recv_port, dump=[])
    device.start()

    blocker = {"sock": None}
    real_verify = verify_mod.verify_routing

    def verify_then_take_the_port(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        if blocker["sock"] is None:          # after the first attempt only
            blocker["sock"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            blocker["sock"].bind(("127.0.0.1", recv_port))
        return result

    monkeypatch.setattr(verify_mod, "verify_routing", verify_then_take_the_port)
    config = session_mod.Config(routes=[route], osc_port=send_port,
                                osc_recv_port=recv_port)
    try:
        session_mod.verify_and_repair(config)
        device.drain()
    finally:
        if blocker["sock"] is not None:
            blocker["sock"].close()
        device.stop()
        device.join(timeout=5)
        device.sock.close()
    assert device.received.count("/refresh") >= 2, "the retry never ran"
    assert "/mix/5/playback/1" in device.received


@pytest.mark.parametrize("cycle", range(12))
def test_applying_the_routing_is_repeatable(session_mod, routing_mod,
                                            monkeypatch, cycle):
    # A soak in miniature: the same routing applied over and over, each
    # time against a fresh peer. Anything that leaks a socket, a thread
    # or a stale barrier state shows up as a failure on a later cycle
    # rather than the first.
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.1)
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.02)
    send_port, recv_port = free_udp_port(), free_udp_port()
    route = make_route(session_mod)
    device = LossyDevice(session_mod, send_port, recv_port)
    device.start()
    try:
        session_mod.apply_routing(session_mod.Config(routes=[route]), send_port, recv_port)
        device.drain(quiet=0.15, limit=3.0)
    finally:
        device.stop()
        device.join(timeout=5)
        device.sock.close()
    assert "/mix/5/playback/1" in device.received
    assert device.received.index("/output/5/stereo") < \
        device.received.index("/mix/5/playback/1"), "phase order broke"


# --------------------------------------------------------------------------
# The verifier's stop contract -- roadmap item I, ADR 0009.
#
# The verifier runs on a daemon thread that may live for two verification
# windows plus a 20 s blind delay after READY=1, and it writes routing at
# three points. It used to read stop_requested and child.poll() exactly
# once, before starting. A `systemctl --user stop` in that window
# therefore had it writing routing at a backend being terminated, with
# the thread cut wherever it happened to be when the process exited.
# --------------------------------------------------------------------------

def test_a_stop_between_the_phases_prevents_every_further_write(
        session_mod, routing_mod, verify_mod, monkeypatch):
    send_port, recv_port = free_udp_port(), free_udp_port()
    route = make_route(session_mod)
    config = session_mod.Config(osc_port=send_port, osc_recv_port=recv_port,
                                routes=[route])

    stopped = {"now": False}
    writes = []
    for name in ("send_mix", "blind_reapply_mix", "apply_routing"):
        monkeypatch.setattr(verify_mod, name,
                            lambda *_a, _n=name, **_kw: writes.append(_n))
    # The dump reports nothing, so verify_routing runs its window out and
    # every register comes back unobserved -- the path with the most
    # writes on it.
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.2)

    def should_stop():
        return stopped["now"]

    stopped["now"] = True
    verify_mod.verify_and_repair(config, should_stop)
    assert writes == [], "the verifier wrote after a stop was requested"


def test_the_verifier_runs_normally_when_nothing_asks_it_to_stop(
        session_mod, verify_mod, monkeypatch):
    # The other half of the contract: the check must not be so eager that
    # it breaks the repair path it guards.
    send_port, recv_port = free_udp_port(), free_udp_port()
    config = session_mod.Config(osc_port=send_port, osc_recv_port=recv_port,
                                routes=[make_route(session_mod)])
    writes = []
    for name in ("send_mix", "blind_reapply_mix", "apply_routing"):
        monkeypatch.setattr(verify_mod, name,
                            lambda *_a, _n=name, **_kw: writes.append(_n))
    monkeypatch.setattr(verify_mod, "VERIFY_TIMEOUT", 0.2)
    monkeypatch.setattr(verify_mod, "VERIFY_SETTLE", 0.01)
    verify_mod.verify_and_repair(config)
    assert writes, "the verifier repaired nothing at all"


def test_the_blind_delay_is_abandoned_on_a_stop(session_mod, routing_mod,
                                                monkeypatch):
    # The path a user actually hits: the mixer GUI holds the receive
    # port, so this is the normal desktop case, and it is 20 s long --
    # twice TimeoutStopSec.
    send_port, recv_port = free_udp_port(), free_udp_port()
    config = session_mod.Config(osc_port=send_port, osc_recv_port=recv_port,
                                routes=[make_route(session_mod)])
    sent = []
    monkeypatch.setattr(routing_mod, "send_mix",
                        lambda _c: sent.append("mix"))
    monkeypatch.setattr(routing_mod, "LINK_SYNC_BLIND_DELAY", 5.0)

    started = time.monotonic()
    deadline = started + 0.2
    routing_mod.blind_reapply_mix(config,
                                  lambda: time.monotonic() > deadline)
    elapsed = time.monotonic() - started
    assert sent == [], "the mix was re-applied after a stop"
    assert elapsed < 2.0, (
        "the blind delay took %.1fs to notice the stop; TimeoutStopSec "
        "is 10s and CHILD_STOP_GRACE already claims 5 of it" % elapsed)


def test_the_wait_returns_promptly_and_reports_why(routing_mod):
    started = time.monotonic()
    assert routing_mod.wait_unless_stopped(10.0, lambda: True) is True
    assert time.monotonic() - started < 0.5
    # ... and it still waits when nothing is asking it to stop.
    started = time.monotonic()
    assert routing_mod.wait_unless_stopped(0.3, routing_mod.never_stop) is False
    assert time.monotonic() - started >= 0.3


def test_the_dump_window_ends_when_a_stop_arrives(session_mod, verify_mod):
    # verify_routing binds the receive port and waits out VERIFY_TIMEOUT
    # for a dump that never comes. A stop must end that window at the
    # next loop turn, not at the deadline.
    send_port, recv_port = free_udp_port(), free_udp_port()
    registers = verify_mod.expected_registers(session_mod.Config(routes=[make_route(session_mod)]))
    started = time.monotonic()
    result = verify_mod.verify_routing(registers, send_port, recv_port,
                                       timeout=10.0, should_stop=lambda: True)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, "the dump window ignored the stop for %.1fs" % elapsed
    # What was seen is returned rather than discarded; the caller decides.
    assert result is not None
    assert result.confirmed == []


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
