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
from oscmix_fakes import DumpingOscmix, make_config, make_route
from support import free_udp_port


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


def test_the_barrier_waits_for_the_echo_on_the_port_it_was_given(
        session_mod, silent_backend, routing_mod, monkeypatch):
    """Extracted from `apply_routing` in 0.6.11, and its call of the echo
    wait was stubbed wherever it was reached: the receive port and the
    timeout could be dropped from it in silence (survivors)."""
    asked = []
    monkeypatch.setattr(routing_mod, "await_link_echo",
                        lambda *a, **k: asked.append((a, k)) or True)
    config = make_config(session_mod, [make_route(session_mod)], 7222, 8222)
    routing_mod._cross_the_barrier(config, 9123, silent_backend)
    assert asked == [((routing_mod.output_link_state(config.routes), 9123,
                       routing_mod.LINK_ECHO_TIMEOUT),
                      {"backend": silent_backend})]


