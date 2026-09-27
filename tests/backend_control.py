"""Qualification of the actual patched C backend, using simulated MIDI only.

Run after building the versioned backend patches:
  OSCMIX_CONTROL_BINARY=/absolute/path/oscmix python -m pytest tests/backend_control.py
Missing binaries fail qualification; these tests never open a hardware device.
"""

import json
import os
import select
import shlex
import signal
import subprocess
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
from support import repo_file

from oscmix_desk.backend import Control
from oscmix_desk.errors import ReceivePortError, WriteFailed
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
        wait_for(lambda: target.path.exists())
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


def test_alsa_input_loss_terminates_the_actual_bridge_reader(binary, tmp_path):
    probe = tmp_path / 'bridge-input-loss'
    subprocess.run(['cc', '-std=c11', '-I', str(binary.parent),
                    str(repo_file('tests/alsaseq_input_loss.c')),
                    '-lasound', '-pthread', '-o', str(probe)],
                   check=True, capture_output=True, timeout=30)
    result = subprocess.run([str(probe)], capture_output=True, timeout=3)
    assert result.returncode == 1
    assert b'snd_seq_event_input:' in result.stderr
    assert result.stdout == b'', 'bridge continued reading after known lost MIDI events'


def test_gtk_observations_do_not_notify_global_connection_properties(binary, tmp_path):
    """Value delivery must not repeatedly rebind the entire GTK window."""
    probe = tmp_path / 'gtk-notifications'
    flags = shlex.split(subprocess.check_output(
        ['pkg-config', '--cflags', '--libs', 'gtk+-3.0'], text=True, timeout=10))
    subprocess.run(['cc', '-std=c11', '-I', str(binary.parent),
                    str(repo_file('tests/gtk_notifications.c')),
                    str(binary.parent / 'gtk/mixer.c'), str(binary.parent / 'osc.c'),
                    '-o', str(probe), *flags, '-lm'],
                   check=True, capture_output=True, timeout=30)
    midi = SimulatedMidi(binary, tmp_path / 'g.sock')
    child = None
    try:
        wait_for(lambda: midi.path.exists())
        child = subprocess.Popen([str(probe), str(midi.path), str(midi.child.pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wait_for(lambda: (0x3e04, 0x67cd) in midi.registers())
        midi.inject((0x600, -330))
        assert select.select([child.stdout], [], [], 3)[0]
        assert child.stdout.readline() == 'READY\n'
        for index in range(100):
            midi.inject((0x600, -300 - index))
        midi.inject((0x600, -440))
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert json.loads(stdout) == {'reports': 101, 'connection_notifications': 0}
        assert 'disconnecting slow consumer' not in midi.log()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        midi.close()


def test_full_queue_recovers_when_the_peer_resumes_reading(binary, tmp_path):
    probe = tmp_path / 'queue-recovery'
    subprocess.run(['cc', '-std=c11', '-I', str(binary.parent),
                    str(repo_file('tests/control_queue_recovery.c')),
                    str(binary.parent / 'osc.c'), '-lm', '-o', str(probe)],
                   check=True, capture_output=True, timeout=30)
    result = subprocess.run([str(probe)], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['recovered_packets'] > 32
    assert observed['slow_peer_disconnected'] is True
    assert result.stderr == 'control: disconnecting slow consumer\n'


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


def test_input_mix_needs_fresh_level_and_pan_not_a_previous_desired_value(midi, desk):
    assert desk.request(BEGIN)[2] == OK
    assert desk.request(WRITE, payload=encode_osc('/mix/5/input/1', 'fi', -6, 0))[2] == OK
    # Link reports are fresh. Only the pan arrives for the matrix: its
    # volume in out->mix is still the previously sent -6 dB, not read-back.
    midi.inject((0x0002, 0), (0x604, 0), (0x2100, 0x8000))
    packet = desk.observation()
    assert not any(path.startswith('/mix/') for path, _, _ in messages(packet))
    # An independent device level now completes the compound observation.
    midi.inject((0x2100, (-300) & 0x7fff))
    assert messages(desk.observation()) == [('/mix/5/input/1', 'fi', (-30.0, 0))]
    # Another command must not let that old device half confirm its new
    # desired half. A new pair of reports is required after a write.
    assert desk.request(WRITE, payload=encode_osc('/mix/5/input/1', 'fi', -12, 0))[2] == OK
    midi.inject((0x0002, 0), (0x604, 0), (0x2100, 0x8000))
    assert not any(path.startswith('/mix/') for path, _, _ in messages(desk.observation()))


def test_linked_input_mix_waits_for_both_physical_members(midi, desk):
    midi.inject((0x0002, 1), (0x604, 0),
                (0x2100, (-360) & 0x7fff), (0x2100, 0x8000))
    assert not any(path.startswith('/mix/') for path, _, _ in messages(desk.observation()))
    midi.inject((0x2101, (-360) & 0x7fff), (0x2101, 0x8000))
    report = messages(desk.observation())[-1]
    assert report[:2] == ('/mix/5/input/1', 'fi')
    assert report[2][0] == pytest.approx(-29.9794, abs=.0001)
    assert report[2][1] == 0


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


def test_midi_hangup_refuses_buffered_reports_and_waiting_writes(midi, desk):
    assert desk.request(BEGIN)[2] == OK
    midi.child.send_signal(signal.SIGSTOP)
    try:
        state = Path('/proc') / str(midi.child.pid) / 'status'
        wait_for(lambda: '\nState:\tT ' in '\n' + state.read_text())
        # Make POLLIN and POLLHUP visible together. Both old observations and
        # a waiting writer must lose authority at this known failure boundary.
        midi.inject((0x604, 1), (0x684, 1))
        os.close(midi.inject_fd)
        midi.inject_fd = -1
        desk.send(WRITE, payload=encode_osc('/output/5/volume', 'f', -40))
    finally:
        midi.child.send_signal(signal.SIGCONT)
    with pytest.raises((EOFError, ConnectionResetError)):
        desk.drain()
    assert midi.child.wait(timeout=3) != 0
    midi.thread.join(timeout=2)
    assert not midi.thread.is_alive()
    assert midi.registers() == [], 'known MIDI hangup still allowed a write'
    assert desk.observations == [], 'buffered reports escaped after known MIDI hangup'
    assert 'MIDI bridge disconnected' in midi.log()


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


def test_desk_client_retains_origin_epoch_and_complete_bundle(midi, desk):
    client = Control(midi.path, midi.child.pid)
    try:
        client.begin()
        assert desk.request(BEGIN)[2] == BUSY
        client.send([('/output/5/stereo', 'i', (1,)), ('/output/7/stereo', 'i', (1,))])
        midi.inject((0x604, 1), (0x684, 1), (0x604, 0))
        delivery = client.next_delivery(2)
        assert delivery is not None
        assert delivery.origin == DEVICE
        assert delivery.epoch == desk.epoch
        reports = [decode_osc(raw) for raw in iter_osc_messages(delivery.payload)]
        assert reports[-2:] == [('/output/5/stereo', 'i', (0,)),
                                ('/output/6/stereo', 'i', (0,))]
        client.finish()
        assert desk.request(BEGIN)[2] == OK
    finally:
        client.close()


def test_desk_client_refresh_keeps_cache_provenance_and_discards_old_reports(midi, desk):
    client = Control(midi.path, midi.child.pid)
    try:
        midi.inject((0x600, -300))
        client.wait(0.1)
        client.request_dump()
        delivery = client.next_delivery(2)
        assert delivery is not None
        assert delivery.origin == DERIVED
        assert all(path.startswith('/playback/') for path, _, _ in
                   (decode_osc(raw) for raw in iter_osc_messages(delivery.payload)))
        assert client.next_delivery(0.05) is None
    finally:
        client.close()


def test_desk_client_wait_maintains_lease_without_extending_its_hard_limit(midi, desk):
    client = Control(midi.path, midi.child.pid)
    try:
        client.begin()
        client.wait(5.2)
        assert desk.request(BEGIN)[2] == BUSY
        client.send([('/output/5/volume', 'f', (-40,))])
        client.finish()
        wait_for(lambda: midi.registers() == [(0x600, 0xfe70)])
    finally:
        client.close()


def test_desk_client_never_writes_without_lease(midi, desk):
    client = Control(midi.path, midi.child.pid)
    try:
        with pytest.raises(WriteFailed) as failed:
            client.send([('/output/5/volume', 'f', (-40,))])
        assert failed.value.written == ()
        assert failed.value.unwritten == ('/output/5/volume',)
        assert midi.registers() == []
    finally:
        client.close()


def test_desk_client_wrong_peer_is_refused_before_protocol_or_hardware(midi, desk):
    with pytest.raises(OSError, match='peer does not match'):
        Control(midi.path, os.getpid())
    assert midi.registers() == []


def test_desk_client_cancel_discards_observation_authority_and_releases_lease(midi, desk):
    cancelled = [False]
    client = Control(midi.path, midi.child.pid, should_stop=lambda: cancelled[0])
    try:
        client.begin()
        midi.inject((0x600, -300))
        client.wait(0.1)
        cancelled[0] = True
        with pytest.raises(ReceivePortError, match='cancelled'):
            client.next_delivery(0.1)
        # EOF is processed by the backend event loop, not synchronously by close().
        wait_for(lambda: desk.request(BEGIN)[2] == OK)
        assert midi.registers() == []
    finally:
        client.close()


def test_desk_client_disconnect_reports_exact_completed_burst_prefix(midi, desk, monkeypatch):
    client = Control(midi.path, midi.child.pid)
    try:
        client.begin()
        original = client._acknowledgement
        def stop_after_first_write(kind, request):
            result = original(kind, request)
            if kind == WRITE:
                midi.child.terminate()
                midi.child.wait(timeout=3)
            return result
        monkeypatch.setattr(client, '_acknowledgement', stop_after_first_write)
        with pytest.raises(WriteFailed) as failed:
            client.send([('/output/5/volume', 'f', (-40,)),
                         ('/output/7/volume', 'f', (-30,))])
        assert failed.value.written == ('/output/5/volume',)
        assert failed.value.unwritten == ('/output/7/volume',)
        wait_for(lambda: midi.registers())
        assert midi.registers() == [(0x600, 0xfe70)]
        with pytest.raises(ReceivePortError, match='no longer valid'):
            client.finish()
    finally:
        client.close()


def test_real_apply_drains_the_final_link_contradiction_before_matrix(midi, monkeypatch):
    from oscmix_desk import routing
    from oscmix_desk.model import Config, Route

    client = Control(midi.path, midi.child.pid)
    original = client.send
    def with_device_response(messages):
        original(messages)
        midi.inject((0x604, 1), (0x684, 1), (0x604, 0))
    monkeypatch.setattr(client, 'send', with_device_response)
    monkeypatch.setattr(routing, 'LINK_ECHO_TIMEOUT', 0.1)
    config = Config(routes=[Route('a', playback=(1, 2), output=(5, 6)),
                            Route('b', playback=(1, 2), output=(7, 8))])
    try:
        client.begin()
        with pytest.raises(WriteFailed, match='contradicted') as failure:
            routing.apply_routing(config, client)
        assert failure.value.written == (
            '/playback/1/stereo', '/output/5/stereo', '/output/7/stereo')
        assert failure.value.unwritten == ('/mix/5/playback/1', '/mix/7/playback/1')
        assert all(register < 0x4000 for register, _ in midi.registers())
        client.finish()
    finally:
        client.close()


@pytest.mark.parametrize('volume', [None, -60, -300])
@pytest.mark.parametrize('pinned', [False, True])
def test_real_reconcile_keeps_remember_with_missing_matching_or_changed_reply(
        midi, monkeypatch, volume, pinned):
    from oscmix_desk import verify
    from oscmix_desk.model import ChannelSetting, Config
    from oscmix_desk.registers import PIN

    client = Control(midi.path, midi.child.pid)
    original = client.request_dump
    def refresh():
        original()
        if volume is not None:
            midi.inject((0x600, volume))
    monkeypatch.setattr(client, 'request_dump', refresh)
    monkeypatch.setattr(verify, 'VERIFY_TIMEOUT', 0.05)
    config = Config(channels=[ChannelSetting('output', 5, 'volume', -6)],
                    policies={('output', 'volume'): PIN} if pinned else {})
    try:
        client.begin()
        assert verify.reconcile_now(config, 'real backend regression', client)
        client.finish()
        wait_for(lambda: midi.registers())
        if pinned:
            wait_for(lambda: (0x600, 0xffc4) in midi.registers())
        volumes = [(reg, value) for reg, value in midi.registers() if reg == 0x600]
        assert volumes == ([(0x600, 0xffc4)] if pinned else [])
    finally:
        client.close()


def test_profile_holds_one_lease_through_readback_and_checks_end_before_marker(
        midi, tmp_path, monkeypatch):
    from oscmix_desk import profiles

    path = tmp_path / 'routing.conf'
    path.write_text('[output:5]\nvolume=-40\n')
    (tmp_path / 'profiles').mkdir()
    (tmp_path / 'profiles/new.conf').write_text('[output:5]\nvolume=-30\n')
    (tmp_path / 'active-profile').write_text('previous\n')
    client = Control(midi.path, midi.child.pid)
    gui = midi.connect(GUI)
    rival = midi.connect()
    original = client.request_dump
    def refresh():
        assert rival.request(BEGIN)[2] == BUSY
        assert gui.request(WRITE, payload=encode_osc('/output/5/volume', 'f', -20))[2] == BUSY
        original()
        midi.inject((0x600, -300))
    monkeypatch.setattr(client, 'request_dump', refresh)
    marked = []
    persist = profiles.remember_active_profile
    def remember(name, config_path):
        assert name == 'new'
        assert config_path == path
        assert rival.request(BEGIN)[2] == OK
        assert rival.request(END)[2] == OK
        marked.append(name)
        return persist(name, config_path)
    monkeypatch.setattr(profiles, 'remember_active_profile', remember)
    try:
        outcome = profiles.switch_profile('new', path, backend=client)
        assert outcome.state == 'applied-verified'
        assert outcome.persisted
        assert marked == ['new']
        assert midi.registers() == [(0x600, 0xfed4), (0x3e04, 0x67cd)]
    finally:
        client.close()
        gui.close()
        rival.close()


@pytest.mark.parametrize('failure_phase', ['request_dump', 'finish'])
def test_profile_lost_backend_never_commits_marker_even_after_all_writes(
        midi, tmp_path, monkeypatch, failure_phase):
    from oscmix_desk import profiles

    path = tmp_path / 'routing.conf'
    path.write_text('[output:5]\nvolume=-40\n')
    (tmp_path / 'profiles').mkdir()
    (tmp_path / 'profiles/new.conf').write_text('[output:5]\nvolume=-30\n')
    marker = tmp_path / 'active-profile'
    marker.write_text('previous\n')
    client = Control(midi.path, midi.child.pid)
    original = getattr(client, failure_phase)
    def lose_backend():
        midi.child.terminate()
        midi.child.wait(timeout=3)
        return original()
    monkeypatch.setattr(client, failure_phase, lose_backend)
    try:
        outcome = profiles.switch_profile('new', path, backend=client,
                                          verify=failure_phase == 'request_dump')
        assert outcome.state == 'applied-unverified'
        assert not outcome.persisted
        assert not outcome.read_back
        assert outcome.unverified == ['/output/5/volume']
        assert 'did not finish' in outcome.reason
        assert marker.read_text() == 'previous\n'
        wait_for(lambda: midi.registers())
        assert midi.registers() == [(0x600, 0xfed4)]
    finally:
        client.close()
