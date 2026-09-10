"""Applying a routing: channel linking must precede the mix matrix.

The device stand-in here models the one oscmix behaviour that makes the
order load-bearing: ``/output/<n>/stereo`` does not change oscmix's own
link state, only the *device echo* of that register does
(``newoutputstereo()`` in oscmix.c). A ``/mix`` write that overtakes the
echo is evaluated unlinked and never reaches the pair's right channel,
which silences every even output.
"""

import socket
import threading
import time

import oracle
from conftest import free_udp_port, osc_bundle, repo_file


def make_route(session_mod, **kwargs):
    defaults = dict(name="monitors", playback=(1, 2), output=(5, 6),
                    level=0.0, volume=None, stereo=True)
    defaults.update(kwargs)
    return session_mod.Route(**defaults)


class FakeOscmix(threading.Thread):
    """oscmix + device, reduced to the stereo-link state machine.

    ``echo_delay`` stands in for the MIDI round-trip: the link only
    becomes effective once the echo has been sent back.
    """

    def __init__(self, session_mod, send_port, recv_port, echo_delay=0.05,
                 echo=True):
        super().__init__(daemon=True)
        self.session_mod = session_mod
        self.recv_port = recv_port
        self.echo_delay = echo_delay
        self.echo = echo
        self.linked = set()          # output pairs oscmix considers linked
        self.stereo_playback = set()
        self.mix_writes = []         # (path, was_linked_when_written)
        self.order = []              # every path, in arrival order
        self.arrivals = []           # (path, monotonic) for timing assertions
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", send_port))
        # 0.2 s, like the fakes in test_faults.py, and a timeout keeps
        # the loop alive instead of ending it. At 3.0 s with `return`,
        # stop() was only noticed when the next recvfrom expired, so
        # every test here paid up to three seconds in join() -- time
        # that asserted nothing, multiplied by every mutant these tests
        # cover.
        self.sock.settimeout(0.2)
        self.stopping = threading.Event()
        self.timers = []             # pending echo timers, cancelled on stop

    def stop(self):
        self.stopping.set()
        # An echo timer that fires after teardown writes to a closed
        # socket; cancelling here keeps the fake from outliving its test.
        for timer in self.timers:
            timer.cancel()

    def record(self, path):
        self.order.append(path)
        self.arrivals.append((path, time.monotonic()))
        parts = path.split("/")
        if path.startswith("/output/") and path.endswith("/stereo"):
            channel = int(parts[2])
            pair = channel - (channel - 1) % 2
            if self.echo:
                # The echo is what updates oscmix's state; it arrives
                # only after the device round-trip.
                timer = threading.Timer(self.echo_delay, self.send_link_echo,
                                        [pair, path])
                timer.daemon = True
                self.timers.append(timer)
                timer.start()
        elif path.startswith("/playback/") and path.endswith("/stereo"):
            # setinputstereo() updates oscmix's state synchronously.
            self.stereo_playback.add(int(parts[2]))
        elif path.startswith("/mix/"):
            channel = int(parts[2])
            pair = channel - (channel - 1) % 2
            self.mix_writes.append((path, pair in self.linked))

    def send_link_echo(self, pair, path):
        if self.stopping.is_set():
            return               # cancel() lost the race with the timer
        self.linked.add(pair)
        try:
            self.sock.sendto(self.session_mod.encode_osc(path, "i", 1),
                             ("127.0.0.1", self.recv_port))
        except OSError:
            pass                 # socket already closed by teardown

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            for message in self.session_mod.iter_osc_messages(data):
                try:
                    path, _tags, _args = self.session_mod.decode_osc(message)
                except ValueError:
                    continue
                self.record(path)


def run_apply(session_mod, routes, **kwargs):
    send_port, recv_port = free_udp_port(), free_udp_port()
    device = FakeOscmix(session_mod, send_port, recv_port, **kwargs)
    device.start()
    try:
        session_mod.apply_routing(session_mod.Config(routes=list(routes)),
                                  send_port, recv_port)
        _drain(device)
    finally:
        device.stop()
        device.join(timeout=3)
        device.sock.close()
    return device


def _drain(device, quiet=0.25, limit=3.0):
    """Wait until the fake has stopped receiving, before stopping it.

    `apply_routing` sends the mix as its last act and returns; the fake's
    thread checks `stopping` at the top of its loop, so calling stop()
    immediately can end it with that datagram still unread. The
    assertions then look at a device that never saw the write.

    The race is real and is removed here regardless of what it has
    caused. What is **not** established is that it caused the one
    failure that prompted this:
    `test_routing_is_applied_even_when_the_echo_never_arrives` failed
    once during a full `make coverage`, and did not come back in 6
    further full coverage runs, 40 plain repeats of this file without
    the drain, or 30 instrumented repeats of it without the drain. Each
    of those reproductions ran a lighter load than the full suite under
    instrumentation, which is the condition that produced it, so they
    narrow the possibilities without settling them.

    Kept because waiting for quiet is what the *product* code does for
    the same reason, and it costs a quarter of a second only while
    something is still arriving -- not because it is known to be the
    fix.
    """
    deadline = time.monotonic() + limit
    seen = len(device.order)
    last = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.02)
        if len(device.order) != seen:
            seen, last = len(device.order), time.monotonic()
        elif time.monotonic() - last >= quiet:
            return


def test_device_fakes_avoid_private_thread_names(session_mod):
    """The fakes share a namespace with threading.Thread's internals.

    ``Thread._stop`` is a method on every version and 3.13 added
    ``Thread._handle``; shadowing either breaks the thread machinery on
    exactly the interpreters that define it. Both slipped through a green
    local run, so the rule is now mechanical: these classes use no
    single-underscore names at all.
    """
    inherited = set(vars(threading.Thread(daemon=True)))
    for cls in (FakeOscmix, DumpingOscmix):
        device = cls(session_mod, free_udp_port(), free_udp_port(),
                     *([] if cls is FakeOscmix else [[]]))
        try:
            names = (set(vars(device)) - inherited) | {
                n for n in vars(cls) if not n.startswith("__")}
        finally:
            device.sock.close()
        private = sorted(n for n in names
                         if n.startswith("_") and not n.startswith("__"))
        assert private == [], (
            "%s uses private names that may collide with threading.Thread: %s"
            % (cls.__name__, private))


def test_mix_is_written_only_after_the_device_confirmed_the_link(session_mod):
    # The regression: with a single-phase send every mix write lands on an
    # unlinked pair and the right channel is never touched.
    device = run_apply(session_mod, [make_route(session_mod)])
    assert device.mix_writes, "no mix message was sent at all"
    unlinked = [path for path, linked in device.mix_writes if not linked]
    assert unlinked == [], (
        "mix written before the device confirmed the stereo link: %s" % unlinked
    )


def test_all_routes_are_linked_before_any_mix_is_written(session_mod):
    # Routes share output pairs, so the barrier has to be global rather
    # than per route -- otherwise route 2's link races route 1's mix.
    routes = [
        make_route(session_mod, name="main", playback=(1, 2), output=(1, 2)),
        make_route(session_mod, name="phones", playback=(1, 2), output=(7, 8)),
        make_route(session_mod, name="direct", playback=(7, 8), output=(7, 8)),
    ]
    device = run_apply(session_mod, routes)
    first_mix = next(i for i, p in enumerate(device.order)
                     if p.startswith("/mix/"))
    later_links = [p for p in device.order[first_mix:]
                   if p.endswith("/stereo")]
    assert later_links == [], (
        "link message sent after the mix phase started: %s" % later_links
    )
    assert all(linked for _, linked in device.mix_writes)


def test_every_stereo_route_links_both_pairs(session_mod):
    route = make_route(session_mod, playback=(7, 8), output=(3, 4))
    links = {path: args
             for path, _t, args in session_mod.link_messages(route)}
    assert links == {"/playback/7/stereo": (1,), "/output/3/stereo": (1,)}


def test_unlinked_route_states_the_unlink_explicitly(session_mod):
    # The regression: leaving /output/5/stereo unsent assumed the pair was
    # already unlinked. Against a linked pair the two hard-panned messages
    # address the same register, the second overwrites the first, and one
    # half of the pair goes silent -- measured on a UCX II.
    route = make_route(session_mod, stereo=False)
    links = [(path, args) for path, _t, args in
             session_mod.link_messages(route)]
    assert links == [("/playback/1/stereo", (1,)), ("/output/5/stereo", (0,))]
    # ... and its mix writes use the hard-panned pair balance.
    mixes = [(path, args[1])
             for path, _t, args in session_mod.mix_messages(route)]
    assert mixes == [("/mix/5/playback/1", -100),
                     ("/mix/6/playback/1", 100)]


def test_mono_route_needs_no_linking(session_mod):
    route = make_route(session_mod, playback=(1,), output=(9,))
    assert session_mod.link_messages(route) == []
    assert [p for p, _t, _a in session_mod.mix_messages(route)] == \
        ["/mix/9/playback/1"]


def test_route_messages_is_the_two_phases_in_order(session_mod):
    # expected_registers() and the verification build on this identity.
    route = make_route(session_mod, volume=-3.0)
    assert oracle.route_messages(route) == (
        session_mod.link_messages(route) + session_mod.mix_messages(route))


def test_routing_is_applied_even_when_the_echo_never_arrives(routing_mod, session_mod,
                                                             monkeypatch):
    # A device that stays silent must not cost more than the timeout, and
    # the mix has to be sent regardless -- degraded beats no audio.
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.2)
    device = run_apply(session_mod, [make_route(session_mod)], echo=False)
    assert [p for p, _ in device.mix_writes] == ["/mix/5/playback/1"]


def test_falls_back_to_a_fixed_wait_when_the_port_is_taken(routing_mod, session_mod,
                                                           monkeypatch):
    # The mixer GUI holds the receive port; the echo is then unobservable
    # and apply_routing waits blind instead of skipping the barrier.
    # 0.3, not a token 0.05: the assertion below is a timing one and the
    # fake's arrival stamps carry scheduling jitter. A wait that dwarfs
    # the jitter is what makes "did it wait" answerable.
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 0.3)
    send_port, recv_port = free_udp_port(), free_udp_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", recv_port))
    device = FakeOscmix(session_mod, send_port, recv_port, echo_delay=0.01)
    device.start()
    try:
        session_mod.apply_routing(session_mod.Config(routes=[make_route(session_mod)]), send_port,
                                  recv_port)
    finally:
        blocker.close()
        device.stop()
        device.join(timeout=3)
        device.sock.close()
    assert [p for p, _ in device.mix_writes] == ["/mix/5/playback/1"]

    # Asserted on the gap the *product* controls, not on the fake's link
    # state. That state is set by a `threading.Timer`, and a 10 ms timer
    # measured here fires 1 ms late at the median, 12 ms at p95 and
    # 440 ms at the worst of 400 trials, 11 of them past 40 ms. Under
    # `make flake`, which runs the whole suite five times over, that
    # starvation is routine and the proxy assertion failed. Raising
    # LINK_SETTLE did not help, because the length of the wait was never
    # the problem.
    #
    # What this test is about is that `apply_routing` waits LINK_SETTLE
    # before the mix phase when the echo cannot be observed, and the
    # fake sees that directly: the mix datagram arrives that much later
    # than the link one.
    when = dict(device.arrivals)
    gap = when["/mix/5/playback/1"] - when["/output/5/stereo"]
    assert gap >= 0.15, (
        "mix arrived %.3f s after the link; a blind 0.3 s wait puts it "
        "far beyond half that, and skipping the barrier puts it near "
        "zero" % gap)


def test_await_link_echo_reports_port_unavailable(session_mod):
    recv_port = free_udp_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", recv_port))
    try:
        result = session_mod.await_link_echo({"/output/5/stereo": 1},
                                             recv_port, timeout=0.1)
    finally:
        blocker.close()
    assert result is None


def test_await_link_echo_times_out_without_echo(session_mod):
    assert session_mod.await_link_echo({"/output/5/stereo": 1},
                                       free_udp_port(), timeout=0.1) is False


def report_after(session_mod, recv_port, value, delay=0.1):
    """Report a link value once await_link_echo has had time to bind.

    Sending before the bind would drop the datagram, which makes a
    "still waiting" assertion pass for the wrong reason.
    """
    def send():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(session_mod.encode_osc("/output/5/stereo", "i", value),
                        ("127.0.0.1", recv_port))
        finally:
            sock.close()

    timer = threading.Timer(delay, send)
    timer.daemon = True
    timer.start()
    return timer


def test_await_link_echo_rejects_the_opposite_value(session_mod):
    # A report of the value we are not waiting for is not a confirmation:
    # an unlinked route waits for 0 and must ignore a stale 1, and the
    # other way round.
    for want, stale in ((1, 0), (0, 1)):
        recv_port = free_udp_port()
        timer = report_after(session_mod, recv_port, stale)
        try:
            assert session_mod.await_link_echo({"/output/5/stereo": want},
                                               recv_port, timeout=0.4) is False
        finally:
            timer.cancel()


def test_await_link_echo_accepts_either_link_value(session_mod):
    # Symmetry with the above: the matching report does end the wait, so
    # the rejection test cannot be passing merely because nothing arrived.
    for want in (1, 0):
        recv_port = free_udp_port()
        timer = report_after(session_mod, recv_port, want)
        try:
            assert session_mod.await_link_echo({"/output/5/stereo": want},
                                               recv_port, timeout=2.0) is True
        finally:
            timer.cancel()


def test_await_link_echo_without_paths_is_immediate(session_mod):
    assert session_mod.await_link_echo({}, free_udp_port()) is True


def test_output_link_state_carries_the_expected_value(session_mod):
    routes = [
        make_route(session_mod, name="a", playback=(1, 2), output=(7, 8)),
        make_route(session_mod, name="b", playback=(7, 8), output=(7, 8)),
        make_route(session_mod, name="c", playback=(1, 2), output=(1, 2),
                   stereo=False),
        make_route(session_mod, name="mono", playback=(1,), output=(9,)),
    ]
    assert session_mod.output_link_state(routes) == {"/output/7/stereo": 1,
                                                     "/output/1/stereo": 0}


def make_config(session_mod, routes, port, recv_port):
    return session_mod.Config(routes=routes, osc_port=port,
                              osc_recv_port=recv_port)


class DumpingOscmix(threading.Thread):
    """Records every write and answers /refresh with a canned dump.

    Whatever the dump contains, oscmix's link state is only correct once
    it has reported ``/output/<n>/stereo`` -- which is what the mix
    re-apply hangs off.
    """

    def __init__(self, session_mod, send_port, recv_port, dump):
        super().__init__(daemon=True)
        self.session_mod = session_mod
        self.recv_port = recv_port
        self.dump = dump
        self.order = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", send_port))
        self.sock.settimeout(0.2)
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def drain(self, quiet=0.3, limit=5.0):
        """Wait until no further datagram arrives for ``quiet`` seconds.

        UDP sends return immediately, so stopping the moment
        verify_and_repair() returns would race the last datagrams and
        make the order assertions flaky.
        """
        deadline = time.monotonic() + limit
        seen = -1
        while time.monotonic() < deadline:
            if len(self.order) == seen:
                return
            seen = len(self.order)
            time.sleep(quiet)

    def run(self):
        while not self.stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue          # keep listening past the verify window
            except OSError:
                return
            for message in self.session_mod.iter_osc_messages(data):
                try:
                    path, _tags, _args = self.session_mod.decode_osc(message)
                except ValueError:
                    continue
                self.order.append(path)
                if path == "/refresh":
                    self.sock.sendto(osc_bundle(self.dump),
                                     ("127.0.0.1", self.recv_port))


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
    return [session_mod.encode_osc(path, types, *args)
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
    dump = [session_mod.encode_osc(path, types, *args)
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


def test_unlinked_route_compensates_the_halved_gain(session_mod):
    # oscmix halves the gain on the unlinked path (setlevel: ll = vol / 2),
    # measured on a UCX II as an exact 6 dB deficit. `level` has to mean
    # the same thing on both paths, so the request is raised by 6.02 dB.
    linked = {p: a for p, _t, a in
              session_mod.mix_messages(make_route(session_mod))}
    unlinked = {p: a for p, _t, a in
                session_mod.mix_messages(make_route(session_mod,
                                                    stereo=False))}
    assert linked["/mix/5/playback/1"] == (0.0, 0)
    sent, pan = unlinked["/mix/5/playback/1"]
    assert pan == -100
    assert abs(sent - 6.0206) < 0.001


def test_unlinked_compensation_tracks_the_requested_level(session_mod):
    route = make_route(session_mod, stereo=False, level=-12.0)
    sent = {p: a for p, _t, a in session_mod.mix_messages(route)}
    assert abs(sent["/mix/5/playback/1"][0] - (-12.0 + 6.0206)) < 0.001


def test_unlinked_route_cannot_be_pushed_above_unity(session_mod):
    # oscmix clamps the gain it derives at 2.0, which is exactly the
    # offset, so positive levels saturate instead of scaling. Sending more
    # would only pretend to be louder.
    route = make_route(session_mod, stereo=False, level=6.0)
    sent = {p: a for p, _t, a in session_mod.mix_messages(route)}
    assert abs(sent["/mix/5/playback/1"][0] - 6.0206) < 0.001


def test_send_mix_writes_the_matrix_without_the_links(session_mod):
    # The re-apply path used after the dump: only the matrix, because
    # re-linking would restart the very race it repairs.
    send_port, recv_port = free_udp_port(), free_udp_port()
    device = DumpingOscmix(session_mod, send_port, recv_port, [])
    device.start()
    config = make_config(session_mod, [make_route(session_mod, volume=0.0)],
                         send_port, recv_port)
    try:
        session_mod.send_mix(config)
        device.drain()
    finally:
        device.stop()
        device.join(timeout=3)
        device.sock.close()
    assert device.order == ["/mix/5/playback/1", "/output/5/volume",
                            "/output/6/volume"]


def test_a_backend_that_updates_link_state_on_write_needs_no_barrier(
        session_mod, silent_backend, routing_mod, monkeypatch):
    """The trait the barrier exists for, read at last.

    `backend.Traits.reports_link_state_on_write` promised since 0.2.0
    that flipping it would be the change when upstream fixed the cache,
    and nothing read it. With the receive port held (the desktop case)
    the barrier is a blind LINK_SETTLE wait; a backend that updates its
    own link state on write has nothing to wait for, and the apply must
    not pay the settle -- while still sending every link before any mix.
    """
    from oscmix_desk import backend as backend_mod

    silent_backend.traits = backend_mod.Traits(
        reports_link_state_on_write=True, dumps_playback_matrix=False,
        reports_unchanged_registers=False)
    monkeypatch.setattr(routing_mod, "LINK_SETTLE", 3.0)
    config = make_config(session_mod, [make_route(session_mod)], 7222, 8222)
    started = time.monotonic()
    session_mod.apply_routing(config, 7222, 8222, backend=silent_backend)
    assert time.monotonic() - started < 1.0, "the barrier ran anyway"
    paths = [p for p, _t, _a in silent_backend.sent]
    assert paths.index("/output/5/stereo") < paths.index("/mix/5/playback/1")


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
        session_mod.send_mix(config)
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
        session_mod.blind_reapply_mix(config)
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
            for message in self.session_mod.iter_osc_messages(data):
                try:
                    self.received.append(self.session_mod.decode_osc(message))
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

    config = session_mod.Config(
        device_name="Fireface UCX II",
        routes=[make_route(session_mod, volume=-6.0),
                session_mod.Route(name="mon", input=(1, 2), output=(7, 8))],
        channels=[
            session_mod.ChannelSetting("output", 5, "mute", 0),
            session_mod.ChannelSetting("input", 3, "gain", 12.0),
            session_mod.ChannelSetting("output", 5, "reflevel", "+4dBu"),
        ])
    send_port, recv_port = free_udp_port(), free_udp_port()
    config.osc_port, config.osc_recv_port = send_port, recv_port

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
