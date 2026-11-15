"""A write sweep cut short by a playback stream still gives the device its values back.

On 2026-11-15 a browser began to play through the UCX II six minutes before the end of a release
sweep. The changed USB playback mode ended the measurement, as it must, but the same check then
refused the restoration, and 32 registers and their linked partners kept their probe values: the
input auto level on twenty inputs and the output expanders. The interface was the same one; only
its stream had changed.
"""

import importlib.util

import pytest
from support import repo_file, signed

from oscmix_desk.discovery import Device
from oscmix_desk.streams import PlaybackMode, PlaybackState

CHANGED = "USB playback mode or identity changed during the measurement"
WRITE = [("/output/1/eq/band1gain", "f", (0.,))]
#: A 48 kHz stream on all 20 USB channels, the browser's on 2026-11-15.
PLAYING = PlaybackMode(48000, 20, 1)


@pytest.fixture(scope="module")
def sweep():
    spec = importlib.util.spec_from_file_location(
        "sweep_writes", repo_file("scripts", "sweep-writes.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backend(sweep, monkeypatch, coordinated):
    """A checked backend whose stream changes after it was opened, and its scripted peer."""
    monkeypatch.setattr(sweep, "build_evidence", signed(sweep.build_evidence, {}))
    with coordinated() as peer:
        checked = sweep.CheckedBackend(Device("2a39:3fd9", "99887766", 28), None, None,
                                       peer.proc)
        try:
            checked.begin()
            yield checked, peer
        finally:
            checked.device.close()


def stream(sweep, monkeypatch, checked, *, card=None, mode=PLAYING):
    """From now on the card plays ``mode``: on the card it was measured on, or on ``card``."""
    state = PlaybackState(checked.playback.card if card is None else card,
                          checked.playback.serial, mode)
    monkeypatch.setattr(sweep, "read_playback", signed(sweep.read_playback, state))


def test_a_changed_stream_ends_the_measurement_but_not_its_restoration(sweep, monkeypatch,
                                                                      backend):
    checked, peer = backend
    stream(sweep, monkeypatch, checked)
    with pytest.raises(ValueError, match=CHANGED):
        checked.send(WRITE)
    assert peer.writes == []
    with sweep.restoring(checked):
        checked.send(WRITE, restoring=True)
        checked.request_dump()
    assert len(peer.writes) == 1
    assert b"/output/1/eq/band1gain" in peer.writes[0]
    assert checked.restoring is False
    with pytest.raises(ValueError, match=CHANGED):
        checked.send(WRITE)


def test_another_card_receives_no_restoration(sweep, monkeypatch, backend):
    checked, peer = backend
    stream(sweep, monkeypatch, checked, card=7)
    with sweep.restoring(checked), pytest.raises(ValueError, match=CHANGED):
        checked.send(WRITE, restoring=True)
    assert peer.writes == []


def test_an_unmeasured_stream_receives_no_restoration(sweep, monkeypatch, backend):
    checked, peer = backend
    stream(sweep, monkeypatch, checked, mode=PlaybackMode(48000, 20, 2))
    with sweep.restoring(checked), pytest.raises(ValueError, match="unmeasured USB"):
        checked.send(WRITE, restoring=True)
    assert peer.writes == []


def test_a_probe_cut_short_by_a_stream_is_put_back(sweep, monkeypatch):
    path, _tags, _args = WRITE[0]
    before = {path: 0.}
    state = dict(before)

    class Checked:
        """Refuses every write once the stream changed, except while restoring."""

        restoring = False

        def send(self, messages, **_options):
            if not self.restoring:
                raise ValueError(CHANGED)
            for name, _tags, args in messages:
                state[name] = args[0]

    def probe(*_args):
        state[path] = -3.
        raise ValueError(CHANGED)

    monkeypatch.setattr(sweep, "sweep", probe)
    monkeypatch.setattr(sweep, "read_all", signed(sweep.read_all, body=lambda: dict(state)))
    monkeypatch.setattr(sweep.time, "sleep", lambda _s: None)
    device = Checked()
    _findings, after, unrestored, error = sweep.measure_and_restore(device, None, [], before)
    assert after == before
    assert unrestored == []
    assert CHANGED in error
    assert "restoration failed" not in error
    assert device.restoring is False
