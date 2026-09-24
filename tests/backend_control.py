"""Qualification of the actual patched C backend, using simulated MIDI only.

Run after building the versioned backend patches:
  OSCMIX_CONTROL_BINARY=/absolute/path/oscmix python -m pytest tests/backend_control.py
Missing binaries fail qualification; these tests never open a hardware device.
"""

import os
import time
from pathlib import Path

import pytest
from control_peer import (
    BEGIN,
    BUSY,
    DERIVED,
    DESK,
    DEVICE,
    END,
    GUI,
    HEADER,
    INVALID,
    KEEPALIVE,
    NOT_OWNER,
    OK,
    READER,
    REFRESH,
    WRITE,
    Peer,
    SimulatedMidi,
    wait_for,
)

from oscmix_desk.osc import decode_osc, encode_osc, iter_osc_messages


@pytest.fixture(scope='module')
def binary():
    path = Path(os.environ['OSCMIX_CONTROL_BINARY']).resolve()
    assert path.is_file()
    assert os.access(path, os.X_OK)
    return path


@pytest.fixture
def midi(binary, tmp_path):
    target = SimulatedMidi(binary, tmp_path / 'c.sock')
    try:
        yield target
    finally:
        target.close()


@pytest.fixture
def desk(midi):
    peer = midi.connect()
    try:
        yield peer
    finally:
        peer.close()


def messages(packet):
    return [decode_osc(raw) for raw in iter_osc_messages(packet[4])]


def test_identity_and_hardware_reports_are_shared_without_echoing_sends(midi, desk):
    with_peer = midi.connect(GUI)
    try:
        assert desk.pid == midi.child.pid == with_peer.pid
        assert desk.epoch == with_peer.epoch
        assert len(desk.epoch) == 16
        assert desk.epoch != bytes(16)
        assert desk.hello[4][24:] == b'Fireface UCX II (00000000)\0'
        assert desk.request(BEGIN)[2] == OK
        assert desk.request(WRITE, payload=encode_osc('/output/5/volume', 'f', -6))[2] == OK
        wait_for(lambda: (0x600, 0xffc4) in midi.registers())
        assert desk.observations == []  # processing ACK is not a value report
        midi.inject((0x600, -300))  # an independent knob setting of -30 dB
        left = desk.observation()
        right = with_peer.observation()
        assert left == right
        assert messages(left) == [('/output/5/volume', 'f', (-30.0,))]
    finally:
        with_peer.close()


def test_bundle_order_keeps_a_final_contradiction(midi, desk):
    midi.inject((0x604, 1), (0x684, 1), (0x604, 0))
    packet = desk.observation()
    assert messages(packet) == [
        ('/output/5/stereo', 'i', (1,)), ('/output/6/stereo', 'i', (1,)),
        ('/output/7/stereo', 'i', (1,)), ('/output/8/stereo', 'i', (1,)),
        ('/output/5/stereo', 'i', (0,)), ('/output/6/stereo', 'i', (0,))]


def test_refresh_cache_is_never_labelled_as_hardware(desk, midi):
    assert desk.request(BEGIN)[2] == OK
    assert desk.request(WRITE, payload=encode_osc('/playback/1/stereo', 'i', 1))[2] == OK
    assert desk.request(REFRESH)[2] == OK
    packet = desk.observation(DERIVED)
    assert ('/playback/1/stereo', 'i', (1,)) in messages(packet)
    assert all('/playback/' in path for path, _, _ in messages(packet))
    wait_for(lambda: (0x3e04, 0x67cd) in midi.registers())
    assert desk.request(REFRESH)[2] == BUSY
    assert midi.registers().count((0x3e04, 0x67cd)) == 1


def test_operation_blocks_gui_and_second_desk_until_explicit_release(midi, desk):
    gui, second = midi.connect(GUI), midi.connect(DESK)
    command = encode_osc('/output/5/volume', 'f', -40)
    try:
        old = gui.token
        assert desk.request(BEGIN)[2] == OK
        assert second.request(BEGIN)[2] == BUSY
        assert gui.request(WRITE, payload=command)[2] == BUSY
        assert desk.request(WRITE, payload=command)[2] == OK
        assert desk.request(END)[2] == OK
        assert gui.request(WRITE, token=old, payload=command)[2] == BUSY
        assert gui.request(WRITE, payload=command)[2] == OK
        wait_for(lambda: len(midi.registers()) == 2)
        assert midi.registers() == [(0x600, 0xfe70)] * 2
        assert second.request(BEGIN)[2] == OK
        assert second.request(END)[2] == OK
    finally:
        gui.close()
        second.close()


@pytest.mark.parametrize('role', [GUI, READER])
def test_only_a_desk_can_acquire_an_operation_lease(midi, role):
    peer = midi.connect(role)
    try:
        assert peer.request(BEGIN)[2] == NOT_OWNER
        assert peer.request(KEEPALIVE)[2] == NOT_OWNER
        if role == READER:
            assert peer.request(WRITE, payload=encode_osc('/output/5/volume', 'f', -40))[2] == BUSY
        assert midi.registers() == []
    finally:
        peer.close()


@pytest.mark.parametrize('payload', [
    encode_osc('/refresh'), encode_osc('/refresh/'), encode_osc('/register', 'ii', 0x3e04, 0x67cd),
    encode_osc('/*'), encode_osc('/output/{5,7}/volume', 'f', -40), b'bad',
    b'/output/5/volume\0\0\0\0,f\0\0', encode_osc('/output/5/volume', 'f', float('nan')),
    encode_osc('/output/5/volume', 'f', float('inf')),
])
def test_invalid_requests_cannot_bypass_arbitration_or_reach_midi(desk, midi, payload):
    assert desk.request(BEGIN)[2] == OK
    assert desk.request(WRITE, payload=payload)[2] == INVALID
    assert midi.registers() == []


@pytest.mark.parametrize('packet', [
    b'invalid', HEADER.pack(b'ODK2', BEGIN, 2, 0, 0),
    HEADER.pack(b'ODK1', BEGIN, 1, 0, 0),  # replayed request id
    HEADER.pack(b'ODK1', BEGIN, 3, 0, 0),  # omitted request id
    HEADER.pack(b'ODK1', BEGIN, 2, 1, 0),
    HEADER.pack(b'ODK1', BEGIN, 2, 0, 0) + b'extra',
    HEADER.pack(b'ODK1', WRITE, 2, 0, 0) + bytes(8193),
])
def test_malformed_protocol_disconnects_without_any_write(desk, midi, packet):
    desk.socket.sendall(packet)
    with pytest.raises((EOFError, ConnectionResetError)):
        desk.drain()
    assert midi.registers() == []


def test_disconnect_releases_lease_and_old_generation_does_not_authorize(midi, desk):
    assert desk.request(BEGIN)[2] == OK
    token = desk.token
    desk.close()
    new = midi.connect()
    try:
        assert new.epoch == desk.epoch
        assert new.request(KEEPALIVE, token=token)[2] == NOT_OWNER
        assert new.request(BEGIN)[2] == OK
        assert new.token != token
    finally:
        new.close()


def test_idle_expiry_closes_connection_and_releases_writer(midi, desk):
    assert desk.request(BEGIN)[2] == OK
    desk.socket.settimeout(6)
    started = time.monotonic()
    with pytest.raises(EOFError):
        desk.drain()
    assert 4.5 <= time.monotonic() - started < 6
    second = midi.connect()
    try:
        assert second.request(BEGIN)[2] == OK
    finally:
        second.close()


def test_hard_deadline_cannot_be_renewed(midi, desk):
    assert desk.request(BEGIN)[2] == OK
    started = time.monotonic()
    def keep_alive():
        while time.monotonic() - started < 94:
            time.sleep(2)
            assert desk.request(KEEPALIVE)[2] == OK
    with pytest.raises((EOFError, BrokenPipeError, ConnectionResetError)):
        keep_alive()
    assert 89 <= time.monotonic() - started < 94
    assert 'operation lease expired' in midi.log()


def test_gui_refresh_requests_coalesce_until_operation_and_window_end(midi, desk):
    gui = midi.connect(GUI)
    try:
        assert desk.request(BEGIN)[2] == OK
        assert desk.request(REFRESH)[2] == OK
        started = time.monotonic()
        for _ in range(10):
            assert gui.request(REFRESH)[2] == BUSY
        assert desk.request(END)[2] == OK
        wait_for(lambda: len(midi.registers()) == 2, timeout=11)
        assert time.monotonic() - started >= 9.5
        assert midi.registers() == [(0x3e04, 0x67cd)] * 2
    finally:
        gui.close()


def test_a_slow_consumer_cannot_block_another_writer(midi):
    slow, healthy = midi.connect(GUI, DEVICE), midi.connect(DESK, DERIVED)
    try:
        for index in range(300):
            midi.inject((0x600, -index))
        wait_for(lambda: 'slow consumer' in midi.log())
        assert healthy.request(BEGIN)[2] == OK
        assert healthy.request(WRITE, payload=encode_osc('/output/5/volume', 'f', -40))[2] == OK
        wait_for(lambda: midi.registers() == [(0x600, 0xfe70)])
        with pytest.raises(EOFError):
            slow.drain()
    finally:
        slow.close()
        healthy.close()


def test_midi_eof_invalidates_all_connections(midi, desk):
    os.close(midi.inject_fd)
    midi.inject_fd = -1
    with pytest.raises(EOFError):
        desk.drain()
    assert midi.child.wait(timeout=3) != 0
    assert 'MIDI bridge disconnected' in midi.log()
    assert not midi.path.exists()


def test_oversized_decoded_delivery_cannot_become_partial_confirmation(midi, desk):
    midi.inject(*[(0x600, -index) for index in range(400)])
    with pytest.raises(EOFError):
        desk.drain()
    assert desk.observations == []
    assert midi.child.wait(timeout=3) != 0
    assert 'cannot encode complete OSC delivery' in midi.log()


def test_duplicate_backend_owner_is_refused(binary, midi, desk):
    duplicate = SimulatedMidi(binary, midi.path)
    try:
        assert duplicate.child.wait(timeout=3) != 0
        assert desk.request(BEGIN)[2] == OK
        assert desk.request(END)[2] == OK
    finally:
        duplicate.close()


def test_new_backend_epoch_after_shutdown(binary, midi, desk):
    midi.child.terminate()
    midi.child.wait(timeout=3)
    replacement = SimulatedMidi(binary, midi.path)
    try:
        new = replacement.connect()
        try:
            assert new.epoch != desk.epoch
            assert new.pid != desk.pid
        finally:
            new.close()
    finally:
        replacement.close()


@pytest.mark.parametrize('filename', ['c.sock', 'c.sock.owner'])
def test_socket_and_owner_lock_are_not_symlink_targets(binary, tmp_path, filename):
    victim = tmp_path / 'preserved'
    victim.write_text('untouched')
    path = tmp_path / filename
    path.symlink_to(victim)
    target = SimulatedMidi(binary, tmp_path / 'c.sock')
    try:
        assert target.child.wait(timeout=3) != 0
        assert victim.read_text() == 'untouched'
        assert path.is_symlink()
    finally:
        target.close()


def test_midi_write_stall_is_bounded(binary, tmp_path):
    target = SimulatedMidi(binary, tmp_path / 'c.sock', drain=False)
    try:
        desk = target.connect()
        try:
            assert desk.request(BEGIN)[2] == OK
            desk.socket.settimeout(4)
            started = time.monotonic()
            def fill_pipe():
                for _ in range(2000):
                    desk.request(WRITE, payload=encode_osc('/output/5/volume', 'f', -40))
            with pytest.raises((EOFError, BrokenPipeError, ConnectionResetError)):
                fill_pipe()
            assert time.monotonic() - started < 6
            assert target.child.wait(timeout=3) != 0
            assert 'MIDI bridge write timed out' in target.log()
        finally:
            desk.close()
    finally:
        target.close()


def test_consumer_limit_is_bounded_and_recovers_after_close(midi, desk):
    peers = [midi.connect(GUI) for _ in range(15)]
    try:
        with pytest.raises((EOFError, ConnectionResetError, BrokenPipeError)):
            Peer(midi.path, READER, DEVICE)
        peers.pop().close()
        # Existing connection proves one full dispatch boundary after close.
        assert desk.request(BEGIN)[2] == OK
        last = midi.connect(READER)
        last.close()
    finally:
        for peer in peers:
            peer.close()
