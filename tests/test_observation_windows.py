"""A link match is revocable until its decoded delivery has been drained."""

import itertools

import pytest

from oscmix_desk import routing, verify
from oscmix_desk.backend import OSCMIX
from oscmix_desk.errors import WriteFailed
from oscmix_desk.model import Config, Route

A, B = "/output/5/stereo", "/output/7/stereo"


class Deliveries:
    """One script per listen, each item a complete decoded OSC datagram."""

    traits = OSCMIX

    def __init__(self, windows):
        self.windows = windows
        self.opens = 0
        self.closed = 0
        self.sent = []
        self.dumps = 0

    def send(self, messages):
        self.sent.extend(messages)

    def request_dump(self):
        self.dumps += 1

    def listen(self):
        batches = iter(self.windows[min(self.opens, len(self.windows) - 1)])
        self.opens += 1
        owner = self

        class Listener:
            def messages(self, _timeout):
                batch = next(batches, [])
                if isinstance(batch, OSError):
                    raise batch
                yield from batch

            def close(self):
                owner.closed += 1

        return Listener()


@pytest.fixture
def clocked(monkeypatch):
    ticks = itertools.count(0, .001)
    monkeypatch.setattr(verify.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(verify.time, "sleep", lambda _: None)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", .1)
    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", .1)


def report(path, value):
    return path, "i", (value,)


def desk():
    return Config(routes=[Route("a", playback=(1, 2), output=(5, 6)),
                          Route("b", playback=(1, 2), output=(7, 8))])


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("wanted", [0, 1])
@pytest.mark.parametrize("bad", [None, "invalid", float("nan"), float("inf"), 2, .5])
def test_a_contradiction_revokes_a_link_match(clocked, batched, wanted, bad):
    bad = 1 - wanted if bad is None else bad
    reports = [report(A, wanted), report(A, bad), report(B, wanted)]
    backend = Deliveries([[reports] if batched else [[r] for r in reports]])
    result = routing.await_link_echo({A: wanted, B: wanted}, 0, backend=backend)
    assert result.value == "contradicted"
    assert not result
    assert backend.closed == 1


@pytest.mark.parametrize("wanted", [0, 1])
def test_a_later_valid_value_restores_confirmation(clocked, wanted):
    backend = Deliveries([[[report(A, wanted), report(A, 1 - wanted)],
                           [report(B, wanted)], [report(A, wanted)]]])
    assert routing.await_link_echo({A: wanted, B: wanted}, 0, backend=backend)


def test_final_contradiction_in_a_bundle_prevents_the_mix_phase(clocked):
    backend = Deliveries([[[report(A, 1), report(B, 1), report(A, 0)]]])
    with pytest.raises(WriteFailed) as caught:
        routing.apply_routing(desk(), 0, 0, backend=backend)
    assert caught.value.written == ("/playback/1/stereo", A, B)
    assert caught.value.unwritten == ("/mix/5/playback/1", "/mix/7/playback/1")
    assert all(path.endswith("/stereo") for path, _tags, _args in backend.sent)


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("last", [False, True])
def test_background_sync_never_writes_through_known_wrong_links(
        clocked, monkeypatch, batched, last):
    reports = ([report(A, 1), report(B, 1), report(A, 0)] if last else
               [report(A, 1), report(A, 0), report(B, 1)])
    # Keep the first window open through A's final separate delivery too.
    reports.append(report("/playback/1/stereo", 1))
    backend = Deliveries([[reports] if batched else [[r] for r in reports],
                          [[report(A, 0), report(B, 1)]]])
    monkeypatch.setattr(verify, "loopback", lambda *_: backend)
    monkeypatch.setattr(routing, "loopback", lambda *_: backend)
    try:
        verify.verify_and_repair(desk())
    except WriteFailed:
        pass  # A bounded link repair may itself be refused in part.
    assert [p for p, _t, _a in backend.sent if p.startswith("/mix/")] == []


@pytest.mark.parametrize("conflict", [False, True])
def test_receive_failure_never_allows_a_blind_mix_write(clocked, monkeypatch, conflict):
    from oscmix_desk.errors import ReceivePortError

    batch = [report(A, 1), report(A, 0)] if conflict else []
    backend = Deliveries([[batch, ReceivePortError(100, "receive failed")]])
    monkeypatch.setattr(verify, "loopback", lambda *_: backend)
    monkeypatch.setattr(routing, "loopback", lambda *_: backend)
    with pytest.raises(ReceivePortError, match="receive failed"):
        verify.verify_and_repair(desk())
    assert backend.sent == []
    with pytest.raises(WriteFailed, match="receive failed") as caught:
        routing.apply_routing(desk(), 0, 0, backend=backend)
    assert caught.value.written == ("/playback/1/stereo", A, B)
    assert caught.value.unwritten == ("/mix/5/playback/1", "/mix/7/playback/1")
    assert backend.closed == 2


def test_a_retry_cannot_replace_known_wrong_links_with_silence(clocked, monkeypatch):
    backend = Deliveries([[[report(A, 0), report(B, 1)]], []])
    monkeypatch.setattr(verify, "loopback", lambda *_: backend)
    monkeypatch.setattr(routing, "loopback", lambda *_: backend)
    with pytest.raises(WriteFailed, match="fresh link confirmation required"):
        verify.verify_and_repair(desk())
    assert all(p.endswith("/stereo") for p, _t, _a in backend.sent)


def test_stop_during_a_link_delivery_abandons_dependent_writes(clocked):
    backend = Deliveries([[[report(A, 1), report(B, 1)]]])
    # Stop after the link phase, while the barrier owns its listener.
    with pytest.raises(WriteFailed, match="stop requested") as caught:
        routing.apply_routing(desk(), 0, 0, backend=backend,
                              should_stop=lambda: backend.opens > 0)
    assert caught.value.written == ("/playback/1/stereo", A, B)
    assert caught.value.unwritten == ("/mix/5/playback/1", "/mix/7/playback/1")
    assert backend.closed == 1


def test_empty_decoded_value_revokes_a_match(clocked):
    backend = Deliveries([[[report(A, 1), (A, "", ()), report(B, 1)]]])
    assert (routing.await_link_echo({A: 1, B: 1}, 0, backend=backend)
            is routing.LinkEcho.CONTRADICTED)
