"""Everything the config asks for reaches the wire, in one order, and
the dry run prints exactly those datagrams.
"""

import socket
import threading
import time

import oracle
from oscmix_fakes import make_route
from support import free_udp_port, repo_file

from oscmix_desk import osc


class CapturingBackend(threading.Thread):
    """A socket that only records, in arrival order, what reaches it.

    FakeOscmix above models the link state machine and reports paths.
    This one keeps the decoded message whole -- path, type tags and
    arguments -- because that is what the dry run prints and therefore
    what has to match.
    """

    def __init__(self, session_mod, port):
        super().__init__(daemon=True)
        self.session_mod = session_mod
        self.received = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        # Short, and a timeout continues rather than ends the thread.
        # It used to be 3.0 s with `return` on timeout, which meant
        # stop() was only noticed after the next recvfrom expired -- so
        # every test using this paid a 3-second join it was not
        # measuring anything with. That is per test, per mutant.
        self.sock.settimeout(0.05)
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
                    self.received.append(osc.decode_osc(message))
                except ValueError:
                    continue

def test_the_dry_run_prints_exactly_the_datagrams_the_apply_sends(
        session_mod, routing_mod, monkeypatch, capsys):
    """Roadmap item G: the printed sequence *is* the sent sequence.

    Two routes, because a single route cannot exhibit the bug class this
    check exists for: walking route by route and printing link, mix,
    link, mix agrees with the real order only when there is one route.
    CI grepped that output to guard the defect that silenced every even
    output, so for two routes it was inspecting an artifact nothing sends.
    """
    from oscmix_desk import session as session_module

    routes = [
        make_route(session_mod, name="main", playback=(1, 2), output=(1, 2),
                   volume=-10.0),
        make_route(session_mod, name="phones", playback=(3, 4), output=(7, 8)),
        make_route(session_mod, name="talkback", playback=(5,), output=(9,)),
    ]
    port, recv_port = free_udp_port(), free_udp_port()
    config = session_mod.Config(osc_port=port, osc_recv_port=recv_port,
                                routes=routes)

    session_module._print_dry_run(42, config)
    printed = [line[len("would send: "):]
               for line in capsys.readouterr().out.splitlines()
               if line.startswith("would send: ")]

    # No echo will arrive on an unbound recv port, so the barrier would
    # burn LINK_ECHO_TIMEOUT; the order under test does not depend on it.
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.05)
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.05)
    backend = CapturingBackend(session_mod, port)
    backend.start()
    try:
        session_mod.apply_routing(session_mod.Config(routes=list(routes)), port, recv_port)
        # The apply returns as soon as the last sendto did; give the
        # reader a moment to drain the socket buffer.
        deadline = time.monotonic() + 3.0
        while len(backend.received) < len(printed) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        backend.stop()
        backend.join(timeout=3)
        backend.sock.close()

    sent = ["%s ,%s %s" % (path, tags, " ".join(map(str, args)))
            for path, tags, args in backend.received]
    assert printed == sent

def test_the_plan_puts_every_link_before_every_mix(session_mod):
    # The property routing_plan exists for, stated without a socket:
    # the barrier is per routing, not per route.
    routes = [
        make_route(session_mod, name="main", playback=(1, 2), output=(1, 2)),
        make_route(session_mod, name="phones", playback=(3, 4), output=(7, 8)),
    ]
    plan = oracle.routing_plan(routes)
    assert all(path.endswith("/stereo") for path, _t, _a in plan.links)
    assert not any(path.endswith("/stereo") for path, _t, _a in plan.mix)
    assert plan.messages() == plan.links + plan.mix
    # ... and it is the same set of messages route_messages declares,
    # only ordered for the wire rather than per route.
    declared = [m for route in routes
                for m in oracle.route_messages(route)]
    assert sorted(plan.messages()) == sorted(declared)

def test_everything_the_config_asks_for_reaches_the_wire(session_mod,
                                                        monkeypatch):
    """The general form of a defect that shipped twice in two shapes.

    First as roadmap item G: `--dry-run` walked route by route while the
    apply walked the routing, so the printed order was not the sent
    order. Fixed by giving both one source.

    Then again, in the commit that added `[input:N]` and `[output:N]`:
    `apply_routing` took a list of routes and rebuilt a Config from it,
    so channel state parsed, validated, appeared in `--dry-run` and
    never reached the device. The dry run and the apply were reading
    different sources *again* -- and the earlier fix did not catch it
    because it compared the two orderings, not the two contents.

    So this asserts the property directly: every register `desired()`
    produces is a datagram the device receives. Adding a section that
    the apply forgets fails here, whatever shape the forgetting takes.
    """
    # Real sockets on purpose -- the claim is about datagrams, not about
    # what a double was handed. But the barrier is not what is being
    # tested, and at its shipped 1.5 s it was the whole cost of this
    # test; the timing tests below own that number.
    from oscmix_desk import routing as routing_mod
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.05)

    send_port, recv_port = free_udp_port(), free_udp_port()
    config = session_mod.Config(
        device_name="Fireface UCX II",
        osc_port=send_port, osc_recv_port=recv_port,
        routes=(make_route(session_mod, volume=-6.0),
                session_mod.Route(name="mon", input=(1, 2), output=(7, 8))),
        channels=(
            session_mod.ChannelSetting("output", 5, "mute", 0),
            session_mod.ChannelSetting("input", 3, "gain", 12.0),
            session_mod.ChannelSetting("output", 5, "reflevel", "+4dBu"),
        ))

    device = CapturingBackend(session_mod, send_port)
    device.start()
    try:
        session_mod.apply_routing(config, send_port, recv_port)
        deadline = time.monotonic() + 3.0
        from oscmix_desk import reconcile

        wanted = [e.path for e in reconcile.desired(config)]
        while (len({p for p, _t, _a in device.received}) < len(wanted)
               and time.monotonic() < deadline):
            time.sleep(0.02)
    finally:
        device.stop()
        device.join(timeout=3)
        device.sock.close()

    sent = {path for path, _tags, _args in device.received}
    missing = [p for p in wanted if p not in sent]
    assert missing == [], (
        "the config asks for these and the apply never sent them: %s" % missing)

def test_nothing_takes_a_part_of_the_config_and_rebuilds_the_rest(session_mod):
    """The guard for a defect this project has now shipped twice.

    Both had the same shape: a function took `config.routes`, rebuilt
    `Config(routes=...)` internally, and silently dropped
    `config.channels`. The first was on the write path -- every
    `[input:N]` and `[output:N]` parsed, validated, showed up in
    --dry-run and never reached the device. The second was the mirror on
    the read path: the same registers were written, then left out of the
    read-back, so the run logged "routing verified" without having looked
    at one of them.

    Neither was visible in what it *did* report, which is why neither a
    green suite nor a green CI noticed. The tests that catch each one
    individually exist; this catches the third instance, in whatever
    function it turns up in next.
    """
    import ast

    package = repo_file("src", "oscmix_desk")
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Config"):
                continue
            # A Config built from every field it has is a real config;
            # one built from a strict subset is a config with holes.
            given = {kw.arg for kw in node.keywords if kw.arg}
            if given and given < {"routes"} | {"channels"} and "channels" not in given:
                offenders.append("%s:%d" % (path.name, node.lineno))
    assert offenders == [], (
        "these rebuild a Config from routes alone, dropping channel "
        "sections -- pass the whole Config instead: %s" % offenders)
