"""Malformed delivery tails cannot authorize writes through a valid prefix."""

import os
import struct

import pytest
from control_peer import ScriptedControl
from support import osc_bundle

from oscmix_desk import osc, routing, verify
from oscmix_desk.backend import Control
from oscmix_desk.errors import ReceivePortError, WriteFailed
from oscmix_desk.model import Config, Route

A, B = '/output/5/stereo', '/output/7/stereo'


def malformed_delivery(kind):
    prefix = [osc.encode_osc(path, 'i', 1) for path in (A, B)]
    wrong = osc.encode_osc(A, 'i', 0)
    if kind == 'missing-argument':
        return osc_bundle(prefix + [wrong[:-4]])
    tail = osc_bundle([]) + struct.pack('>i', len(wrong) + 4) + wrong
    if kind == 'truncated-nested-bundle':
        return osc_bundle(prefix + [tail])
    return osc_bundle(prefix) + tail[16:]


@pytest.mark.parametrize('kind', [
    'missing-argument', 'truncated-element', 'truncated-nested-bundle'])
@pytest.mark.parametrize('action', ['delivery', 'apply', 'verify-and-repair'])
def test_invalid_tail_refuses_the_whole_delivery_and_dependent_writes(
        tmp_path, monkeypatch, kind, action):
    monkeypatch.setattr(verify, 'VERIFY_TIMEOUT', .05)
    peer = ScriptedControl(tmp_path / 'c.sock', reports=[malformed_delivery(kind)])
    device = Control(peer.path, os.getpid())
    desk = Config(routes=[Route('a', playback=(1, 2), output=(5, 6)),
                          Route('b', playback=(1, 2), output=(7, 8))])
    try:
        device.begin()
        if action == 'verify-and-repair':
            with pytest.raises(ReceivePortError, match='invalid OSC delivery'):
                verify.verify_and_repair(desk, device)
            assert peer.writes == []
        else:
            device.request_dump()
            if action == 'delivery':
                with pytest.raises(ReceivePortError, match='invalid OSC delivery'):
                    next(device.messages(.5))  # No valid prefix escapes to an observer.
            else:
                with pytest.raises(WriteFailed, match='invalid OSC delivery') as failed:
                    routing.apply_routing(desk, device)
                assert failed.value.written == ('/playback/1/stereo', A, B)
                assert failed.value.unwritten == ('/mix/5/playback/1', '/mix/7/playback/1')
                assert [osc.decode_osc(raw)[0] for raw in peer.writes] == list(
                    failed.value.written)
        with pytest.raises(ReceivePortError, match='no longer valid'):
            device.heartbeat()
    finally:
        device.close()
        peer.close()
