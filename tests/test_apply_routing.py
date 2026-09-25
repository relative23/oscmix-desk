"""Applying a routing: channel linking must precede the mix matrix.

The device stand-in here models the one oscmix behaviour that makes the
order load-bearing: ``/output/<n>/stereo`` does not change oscmix's own
link state, only the *device echo* of that register does
(``newoutputstereo()`` in oscmix.c). A ``/mix`` write that overtakes the
echo is evaluated unlinked and never reaches the pair's right channel,
which silences every even output.
"""

import time

import oracle
import pytest
from backend_doubles import RecordingBackend
from oscmix_fakes import make_config, make_route

from oscmix_desk import reconcile, routing
from oscmix_desk.backend import OSCMIX
from oscmix_desk.errors import ReceivePortError, WriteFailed
from oscmix_desk.routing import LinkEcho


class EchoBackend(RecordingBackend):
    """Link cache changes only after a delayed device report, never on send."""

    traits = OSCMIX

    def __init__(self, echo_delay=0.05, echo=True):
        super().__init__()
        self.echo_delay, self.echo = echo_delay, echo
        self.linked = set()
        self.pending = []
        self.mix_writes = []
        self.order = []
        self.arrivals = []

    def send(self, messages):
        for path, tags, args in messages:
            self.sent.append((path, tags, args))
            self.order.append(path)
            self.arrivals.append((path, time.monotonic()))
            if path.startswith('/output/') and path.endswith('/stereo') and self.echo:
                self.pending.append((time.monotonic() + self.echo_delay, (path, tags, args)))
            elif path.startswith('/mix/'):
                output = int(path.split('/')[2])
                self.mix_writes.append((path, output in self.linked))

    def messages(self, timeout):
        if not self.pending:
            time.sleep(timeout)
            return
        time.sleep(min(timeout, max(0, self.pending[0][0] - time.monotonic())))
        ready = [item for item in self.pending if item[0] <= time.monotonic()]
        self.pending = [item for item in self.pending if item not in ready]
        for _when, report in ready:
            if report[2] == (1,):
                self.linked.add(int(report[0].split('/')[2]))
            yield report


def run_apply(session_mod, routes, **kwargs):
    device = EchoBackend(**kwargs)
    session_mod.apply_routing(session_mod.Config(routes=list(routes)), device)
    return device


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
             for path, _t, args in reconcile.link_messages(route)}
    assert links == {"/playback/7/stereo": (1,), "/output/3/stereo": (1,)}


def test_unlinked_route_states_the_unlink_explicitly(session_mod):
    # The regression: leaving /output/5/stereo unsent assumed the pair was
    # already unlinked. Against a linked pair the two hard-panned messages
    # address the same register, the second overwrites the first, and one
    # half of the pair goes silent -- measured on a UCX II.
    route = make_route(session_mod, stereo=False)
    links = [(path, args) for path, _t, args in
             reconcile.link_messages(route)]
    assert links == [("/playback/1/stereo", (1,)), ("/output/5/stereo", (0,))]
    # ... and its mix writes use the hard-panned pair balance.
    mixes = [(path, args[1])
             for path, _t, args in reconcile.mix_messages(route)]
    assert mixes == [("/mix/5/playback/1", -100),
                     ("/mix/6/playback/1", 100)]


def test_mono_route_unlinks_both_pairs(session_mod):
    route = make_route(session_mod, playback=(1,), output=(9,))
    assert reconcile.link_messages(route) == [
        ("/playback/1/stereo", "i", (0,)),
        ("/output/9/stereo", "i", (0,)),
    ]
    assert [p for p, _t, _a in reconcile.mix_messages(route)] == \
        ["/mix/9/playback/1"]


def test_route_messages_is_the_two_phases_in_order(session_mod):
    # expected_registers() and the verification build on this identity.
    route = make_route(session_mod, volume=-3.0)
    assert oracle.route_messages(route) == (
        reconcile.link_messages(route) + reconcile.mix_messages(route))


def test_routing_is_applied_even_when_the_echo_never_arrives(routing_mod, session_mod,
                                                             monkeypatch):
    # A device that stays silent must not cost more than the timeout, and
    # the mix has to be sent regardless -- degraded beats no audio.
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 0.2)
    device = run_apply(session_mod, [make_route(session_mod)], echo=False)
    assert [p for p, _ in device.mix_writes] == ["/mix/5/playback/1"]


def test_connection_failure_refuses_the_mix_instead_of_a_blind_wait(
        recording_backend, monkeypatch):
    def failed(_timeout):
        raise ReceivePortError(104, 'backend disconnected')
    monkeypatch.setattr(recording_backend, 'messages', failed)
    from oscmix_desk.model import Config, Route
    config = Config(routes=[Route('route', playback=(1, 2), output=(5, 6))])
    with pytest.raises(WriteFailed, match='disconnected') as failure:
        routing.apply_routing(config, recording_backend)
    assert failure.value.written == ('/playback/1/stereo', '/output/5/stereo')
    assert failure.value.unwritten == ('/mix/5/playback/1',)
    assert not any(path.startswith('/mix/') for path, _, _ in recording_backend.sent)


def test_await_link_echo_propagates_receiver_failure(unbindable_backend):
    with pytest.raises(ReceivePortError):
        routing.await_link_echo({'/output/5/stereo': 1}, unbindable_backend, timeout=0.1)


def test_await_link_echo_times_out_without_echo(silent_backend):
    assert routing.await_link_echo({'/output/5/stereo': 1}, silent_backend,
                                   timeout=0.01) is LinkEcho.SILENT


@pytest.mark.parametrize('want', [0, 1])
def test_await_link_echo_rejects_the_opposite_value(want):
    backend = RecordingBackend(lambda _: [('/output/5/stereo', 'i', (1 - want,))])
    assert routing.await_link_echo({'/output/5/stereo': want}, backend,
                                   timeout=0.01) is LinkEcho.CONTRADICTED


@pytest.mark.parametrize('want', [0, 1])
def test_await_link_echo_accepts_either_link_value(want):
    backend = RecordingBackend(lambda _: [('/output/5/stereo', 'i', (want,))])
    assert routing.await_link_echo({'/output/5/stereo': want}, backend,
                                   timeout=0.01) is LinkEcho.CONFIRMED


def test_await_link_echo_without_paths_is_immediate(unbindable_backend):
    assert routing.await_link_echo({}, unbindable_backend) is LinkEcho.CONFIRMED


def test_output_link_state_carries_the_expected_value(session_mod):
    routes = [
        make_route(session_mod, name="a", playback=(1, 2), output=(7, 8)),
        make_route(session_mod, name="b", playback=(7, 8), output=(7, 8)),
        make_route(session_mod, name="c", playback=(1, 2), output=(1, 2),
                   stereo=False),
        make_route(session_mod, name="mono", playback=(11,), output=(9,)),
    ]
    assert routing.output_link_state(routes) == {"/output/7/stereo": 1,
                                                "/output/1/stereo": 0,
                                                "/output/9/stereo": 0}


def test_unlinked_route_compensates_the_halved_gain(session_mod):
    # oscmix halves the gain on the unlinked path (setlevel: ll = vol / 2),
    # measured on a UCX II as an exact 6 dB deficit. `level` has to mean
    # the same thing on both paths, so the request is raised by 6.02 dB.
    linked = {p: a for p, _t, a in
              reconcile.mix_messages(make_route(session_mod))}
    unlinked = {p: a for p, _t, a in
                reconcile.mix_messages(make_route(session_mod,
                                                    stereo=False))}
    assert linked["/mix/5/playback/1"] == (0.0, 0)
    sent, pan = unlinked["/mix/5/playback/1"]
    assert pan == -100
    assert abs(sent - 6.0206) < 0.001


def test_unlinked_compensation_tracks_the_requested_level(session_mod):
    route = make_route(session_mod, stereo=False, level=-12.0)
    sent = {p: a for p, _t, a in reconcile.mix_messages(route)}
    assert abs(sent["/mix/5/playback/1"][0] - (-12.0 + 6.0206)) < 0.001


def test_unlinked_route_cannot_be_pushed_above_unity(session_mod):
    # oscmix clamps the gain it derives at 2.0, which is exactly the
    # offset, so positive levels saturate instead of scaling. Sending more
    # would only pretend to be louder.
    route = make_route(session_mod, stereo=False, level=6.0)
    sent = {p: a for p, _t, a in reconcile.mix_messages(route)}
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
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 3.0)
    config = make_config(session_mod, [make_route(session_mod)], 7222, 8222)
    started = time.monotonic()
    session_mod.apply_routing(config, silent_backend)
    assert time.monotonic() - started < 1.0, "the barrier ran anyway"
    paths = [p for p, _t, _a in silent_backend.sent]
    assert paths.index("/output/5/stereo") < paths.index("/mix/5/playback/1")


@pytest.mark.parametrize(("echo", "said"), [
    (LinkEcho.SILENT, ("no link change reported within 1.5s; mix matrix will be "
                       "re-applied after the register sync")),
    (LinkEcho.CONFIRMED, "channel pairs linked and confirmed by the device"),
])
def test_silence_and_confirmation_are_reported_distinctly(
        session_mod, silent_backend, routing_mod, monkeypatch, caplog, echo, said):
    monkeypatch.setattr(routing_mod, "await_link_echo", lambda *a, **k: echo)
    monkeypatch.setattr(routing_mod, "LINK_ECHO_TIMEOUT", 1.5)
    config = make_config(session_mod, [make_route(session_mod)], 7222, 8222)
    with caplog.at_level("INFO"):
        routing_mod._cross_the_barrier(config, silent_backend)
    assert [r.getMessage() for r in caplog.records] == [said]


def test_barrier_borrows_the_operation_connection_and_timeout(
        session_mod, silent_backend, routing_mod, monkeypatch):
    asked = []
    monkeypatch.setattr(routing_mod, "await_link_echo",
                        lambda *a, **k: asked.append((a, k)) or LinkEcho.CONFIRMED)
    config = make_config(session_mod, [make_route(session_mod)], 7222, 8222)
    routing_mod._cross_the_barrier(config, silent_backend)
    assert asked == [((routing_mod.output_link_state(config.routes), silent_backend,
                       routing_mod.LINK_ECHO_TIMEOUT),
                      {"should_stop": routing_mod.never_stop})]
