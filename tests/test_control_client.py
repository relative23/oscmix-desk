"""The desk client fails closed on lost replies, malformed peers and stale data."""

import errno
import os
import socket
import struct
from dataclasses import replace

import pytest
from control_peer import (
    BEGIN,
    BUSY,
    DERIVED,
    DEVICE,
    END,
    EVENT,
    HELLO,
    KEEPALIVE,
    METERS,
    OBSERVATION,
    REFRESH,
    REPLY,
    WRITE,
    ScriptedControl,
)

from oscmix_desk import backend
from oscmix_desk.diagnostics import BackendStatus
from oscmix_desk.discovery import Device
from oscmix_desk.errors import ReceivePortError, WriteFailed
from oscmix_desk.model import Config
from oscmix_desk.osc import encode_osc

EPOCH = bytes(range(16))


def ready(server):
    return BackendStatus('ready', 'checked', Device('2a39:3fd9', '00000000', 24),
                         os.getpid(), str(server.path), os.getuid(), os.getgid())


@pytest.fixture
def peer(tmp_path):
    active = []

    def start(**options):
        server = ScriptedControl(tmp_path / ('c%d.sock' % len(active)), **options)
        active.append(server)
        return server

    yield start
    for server in active:
        server.close()


@pytest.fixture
def control(peer):
    server = peer()
    client = backend.Control(server.path, os.getpid(), expected_uid=os.getuid())
    try:
        yield server, client
    finally:
        client.close()


def test_acknowledgement_loss_retains_the_uncertain_write_and_never_replays(peer):
    server = peer(lose_reply=WRITE)
    client = backend.Control(server.path, os.getpid())
    try:
        client.begin()
        with pytest.raises(WriteFailed) as failed:
            client.send([('/output/5/volume', 'f', (-40,)),
                         ('/output/7/volume', 'f', (-30,))])
        assert failed.value.written == ('/output/5/volume',)
        assert failed.value.unwritten == ('/output/7/volume',)
        assert server.writes == [encode_osc('/output/5/volume', 'f', -40)]
        with pytest.raises(ReceivePortError, match='no longer valid'):
            client.finish()
        assert server.requests == [HELLO, BEGIN, WRITE]
    finally:
        client.close()


@pytest.mark.parametrize('refused', [1, 2, 6])
def test_definitely_refused_write_is_pending_not_claimed_as_applied(peer, refused):
    server = peer(refuse_write=refused)
    client = backend.Control(server.path, os.getpid())
    try:
        client.begin()
        with pytest.raises(WriteFailed) as failed:
            client.send([('/output/5/volume', 'f', (-40,))])
        assert failed.value.written == ()
        assert failed.value.unwritten == ('/output/5/volume',)
        assert server.writes == []
    finally:
        client.close()


def test_lost_finish_acknowledgement_cannot_be_a_successful_operation(peer):
    server = peer(lose_reply=END)
    client = backend.Control(server.path, os.getpid())
    try:
        client.begin()
        client.send([('/output/5/volume', 'f', (-40,))])
        with pytest.raises(ReceivePortError, match='disconnected'):
            client.finish()
    finally:
        client.close()


@pytest.mark.parametrize('handshake', [
    b'', EPOCH, EPOCH + struct.pack('>II', os.getpid(), 2) + b'name\0',
    EPOCH + struct.pack('>II', os.getpid() + 1, 1) + b'name\0',
    EPOCH + struct.pack('>II', os.getpid(), 1) + b'name',
    EPOCH + struct.pack('>II', os.getpid(), 1) + b'a\0b\0',
    EPOCH + struct.pack('>II', os.getpid(), 1) + b'\xff\0',
])
def test_incompatible_handshake_fails_before_acquisition_or_writes(peer, handshake):
    server = peer(handshake=handshake)
    with pytest.raises(OSError, match=r'handshake|incompatible'):
        backend.Control(server.path, os.getpid())
    assert server.requests == [HELLO]
    assert server.writes == []


def test_kernel_uid_is_checked_before_hello(peer):
    server = peer()
    with pytest.raises(OSError, match='peer does not match'):
        backend.Control(server.path, os.getpid(), expected_uid=os.getuid() + 1)
    assert server.requests == []


def test_meter_subscription_preserves_origin_but_never_confirms_registers(peer):
    server = peer()
    client = backend.Control(server.path, os.getpid(), sources=7)
    try:
        packet = encode_osc('/output/5/level', 'f', -60.)
        server.emit(OBSERVATION, code=METERS, sequence=1, payload=packet)
        delivery = client.next_delivery(.5)
        assert (delivery.origin, delivery.sequence, delivery.payload) == (4, 1, packet)
        server.emit(OBSERVATION, code=METERS, sequence=2, payload=packet)
        assert list(client.messages(.5)) == []
        assert not server.writes
    finally:
        client.close()


@pytest.mark.parametrize('sources', [0, 8, -1])
def test_invalid_subscription_is_refused_before_connecting(peer, sources):
    server = peer()
    with pytest.raises(ValueError, match='observation sources'):
        backend.Control(server.path, os.getpid(), sources=sources)
    assert server.requests == []


def test_symbolic_link_endpoint_is_refused(control, tmp_path):
    server, _client = control
    alias = tmp_path / 'alias'
    alias.symlink_to(server.path)
    with pytest.raises(OSError, match='symbolic link'):
        backend.Control(alias, os.getpid())


def test_no_udp_fallback_for_absent_socket(tmp_path):
    with pytest.raises(FileNotFoundError):
        backend.Control(tmp_path / 'absent.sock', os.getpid())


def test_shared_origin_order_and_epoch_are_preserved(control):
    server, client = control
    hardware = encode_osc('/output/5/volume', 'f', -30)
    cached = encode_osc('/playback/1/stereo', 'i', 1)
    server.emit(OBSERVATION, code=DEVICE, sequence=40, payload=hardware)
    server.emit(OBSERVATION, code=DERIVED, sequence=48, payload=cached)
    assert client.next_delivery(0.5) == backend.Delivery(DEVICE, 40, EPOCH, hardware)
    assert client.next_delivery(0.5) == backend.Delivery(DERIVED, 48, EPOCH, cached)
    assert client.next_delivery(0.01) is None


@pytest.mark.parametrize(('kind', 'number', 'code', 'sequence', 'payload'), [
    (OBSERVATION, 0, 4, 1, encode_osc('/input/1/level', 'f', -50)),
    (OBSERVATION, 0, DEVICE, 1, b'bad'),
    (OBSERVATION, 2, DEVICE, 1, encode_osc('/output/5/volume', 'f', -30)),
    (EVENT, 0, 0, 0, b''),
    (EVENT, 0, 2, 1, b''),
    (EVENT, 0, 0, 1, b'extra'),
    (18, 0, 8, 1, b''),
    (18, 0, 0, 1, b'extra'),
    (99, 0, 0, 1, b''),
    (HELLO | REPLY, 1, 0, 1, b''),
])
def test_bad_events_invalidate_connection_instead_of_becoming_silence(
        control, kind, number, code, sequence, payload):
    server, client = control
    server.emit(kind, number, code, sequence, payload)
    with pytest.raises(ReceivePortError):
        client.next_delivery(0.5)
    with pytest.raises(ReceivePortError, match='no longer valid'):
        client.next_delivery(0.5)


@pytest.mark.parametrize('packet', [b'', b'ODK2' + bytes(20), bytes(9000)])
def test_truncated_or_incompatible_frames_invalidate_connection(control, packet):
    server, client = control
    if packet:
        server.peer.sendall(packet)
    else:
        server.peer.shutdown(socket.SHUT_WR)
    with pytest.raises(ReceivePortError):
        client.next_delivery(0.5)


def test_duplicate_observation_is_not_a_new_confirmation(control):
    server, client = control
    value = encode_osc('/output/5/stereo', 'i', 1)
    server.emit(OBSERVATION, code=DEVICE, sequence=5, payload=value)
    assert client.next_delivery(0.5).sequence == 5
    server.emit(OBSERVATION, code=DEVICE, sequence=5, payload=value)
    with pytest.raises(ReceivePortError, match='order'):
        client.next_delivery(0.5)


def test_overflow_is_visible_and_clears_queued_confirmations(control, monkeypatch):
    server, client = control
    monkeypatch.setattr(backend, 'CONTROL_QUEUE_BYTES', 60)
    value = encode_osc('/output/5/stereo', 'i', 1)
    for sequence in (1, 2):
        server.emit(OBSERVATION, code=DEVICE, sequence=sequence, payload=value)
    with pytest.raises(ReceivePortError, match='overflowed'):
        client.wait(0.5)
    with pytest.raises(ReceivePortError, match='no longer valid'):
        client.next_delivery(0.1)


@pytest.mark.parametrize('phase', ['begin', 'refresh'])
def test_busy_operation_or_window_has_bounded_wait_and_no_write(peer, phase):
    server = peer(begin_busy=phase == 'begin', refresh_busy=phase == 'refresh')
    client = backend.Control(server.path, os.getpid())
    try:
        action = client.begin if phase == 'begin' else client.request_dump
        with pytest.raises(OSError, match=r'lease|window') as failed:
            action(timeout=0)
        assert failed.value.errno == errno.EBUSY
        assert server.writes == []
    finally:
        client.close()


def test_heartbeat_loss_invalidates_current_lease(peer, monkeypatch):
    server = peer(lose_reply=KEEPALIVE)
    client = backend.Control(server.path, os.getpid())
    try:
        client.begin()
        monkeypatch.setattr(backend, 'CONTROL_HEARTBEAT', 0)
        with pytest.raises(ReceivePortError, match='disconnected'):
            client.wait(0.1)
        assert server.requests == [HELLO, BEGIN, KEEPALIVE]
    finally:
        client.close()


def test_cancellation_before_any_request_leaves_no_write(peer):
    server = peer()
    with pytest.raises(ReceivePortError, match='cancelled'):
        backend.Control(server.path, os.getpid(), should_stop=lambda: True)
    assert server.writes == []


def test_bounded_request_number_and_payload_cannot_wrap_or_truncate(control):
    _server, client = control
    client.begin()
    client._request_id = 0xffffffff
    with pytest.raises(WriteFailed) as failed:
        client.send([('/output/5/volume', 'f', (-40,))])
    assert failed.value.written == ()
    assert failed.value.unwritten == ('/output/5/volume',)


def test_backend_factory_binds_checked_identity_to_kernel_peer(peer, monkeypatch):
    server = peer()
    identity = ready(server)
    checked = []
    def status(config, proc_root, config_path):
        checked.append((config, proc_root, config_path))
        return identity
    monkeypatch.setattr(backend, 'backend_status', status)
    client = backend.connect_backend(Config(serial='00000000'))
    try:
        assert len(checked) == 2
        assert checked[0] == checked[1]
        assert (client.pid, client.uid, client.gid) == (
            os.getpid(), os.getuid(), os.getgid())
        assert client.epoch == EPOCH
        assert server.requests == [HELLO]
    finally:
        client.close()


@pytest.mark.parametrize('change', ['serial', 'pid', 'bridge', 'socket'])
def test_factory_refuses_identity_change_during_connect(peer, monkeypatch, change):
    server = peer()
    first = ready(server)
    later = {'serial': replace(first, device=Device('2a39:3fd9', '99999999', 24)),
             'pid': replace(first, pid=123456),
             'bridge': replace(first, device=Device('2a39:3fd9', '00000000', 25)),
             'socket': replace(first, endpoint='/another.control')}[change]
    results = iter([first, later])
    monkeypatch.setattr(backend, 'backend_status', lambda *_a: next(results))
    with pytest.raises(OSError, match='identity changed'):
        backend.connect_backend(Config())
    assert server.requests == [HELLO]
    assert server.writes == []


def test_factory_refuses_a_handshake_for_another_serial(peer, monkeypatch):
    server = peer()
    identity = replace(ready(server), device=Device('2a39:3fd9', '24216011', 24))
    monkeypatch.setattr(backend, 'backend_status', lambda *_a: identity)
    with pytest.raises(OSError, match='identity changed'):
        backend.connect_backend(Config())
    assert server.requests == [HELLO]


@pytest.mark.parametrize('state', ['absent', 'unknown', 'conflict', 'wrong-child'])
def test_factory_refuses_incompatible_target_before_connect(monkeypatch, state):
    identity = BackendStatus(state if state != 'wrong-child' else 'ready', 'refused',
                             Device('2a39:3fd9', '24216011', 24), 1, '/a.control')
    monkeypatch.setattr(backend, 'backend_status', lambda *_a: identity)
    def forbidden(*_a, **_k):
        pytest.fail('an incompatible target was connected')
    monkeypatch.setattr(backend, 'Control', forbidden)
    with pytest.raises(OSError, match=r'refused|not owned by the started backend'):
        backend.connect_backend(Config(), expected_pid=2)


def test_wrong_kernel_group_refused_before_hello(peer):
    server = peer()
    with pytest.raises(OSError, match='peer does not match'):
        backend.Control(server.path, os.getpid(), expected_gid=os.getgid() + 1)
    assert server.requests == []


def test_hardware_observer_excludes_derived_values_but_preserves_bundle_order(control):
    server, client = control
    value = encode_osc('/output/5/volume', 'f', -6)
    server.emit(OBSERVATION, code=DERIVED, sequence=1, payload=value)
    assert list(client.messages(0.1)) == []
    server.emit(OBSERVATION, code=DEVICE, sequence=2, payload=value)
    assert list(client.messages(0.1)) == [('/output/5/volume', 'f', (-6.,))]


def test_lost_lease_event_invalidates_queued_observations(control):
    server, client = control
    client.begin()
    server.emit(OBSERVATION, code=DEVICE, sequence=1,
                payload=encode_osc('/output/5/stereo', 'i', 1))
    server.emit(EVENT, code=BUSY, sequence=server.generation + 1)
    with pytest.raises(ReceivePortError, match='lease changed'):
        client.wait(.1)
    with pytest.raises(ReceivePortError, match='no longer valid'):
        client.next_delivery(.1)


@pytest.mark.parametrize('fault', ['request', 'kind', 'payload',
                                   'status', 'generation', 'timeout'])
def test_invalid_write_ack_preserves_partial_result_and_closes(control, monkeypatch, fault):
    server, client = control
    client.begin()
    emit = server.emit

    def reply(kind, request=0, code=0, sequence=None, payload=b''):
        if kind == WRITE | REPLY:
            if fault == 'timeout':
                return
            if fault == 'request':
                request -= 1
            elif fault == 'kind':
                kind = BEGIN | REPLY
            elif fault == 'payload':
                payload = b'unexpected'
            elif fault == 'status':
                code = 5
            elif fault == 'generation':
                sequence = server.generation + 1
        emit(kind, request, code, sequence, payload)

    monkeypatch.setattr(server, 'emit', reply)
    if fault == 'timeout':
        monkeypatch.setattr(backend, 'CONTROL_ACK_TIMEOUT', .2)
    with pytest.raises(WriteFailed) as failed:
        client.send([('/output/5/volume', 'f', (-40,)), ('/output/7/volume', 'f', (-30,))])
    assert failed.value.errno == (errno.ETIMEDOUT if fault == 'timeout' else errno.EPROTO)
    assert failed.value.written == ('/output/5/volume',)
    assert failed.value.unwritten == ('/output/7/volume',)
    assert server.writes == [encode_osc('/output/5/volume', 'f', -40)]
    with pytest.raises(ReceivePortError, match='no longer valid'):
        client.next_delivery(.1)
    with pytest.raises(ReceivePortError, match='no longer valid'):
        client.finish()
    assert server.requests == [HELLO, BEGIN, WRITE]


@pytest.mark.parametrize('phase', ['heartbeat', 'finish'])
@pytest.mark.parametrize('fault', ['generation', 'refused'])
def test_lease_ack_confirms_the_same_operation(control, monkeypatch, phase, fault):
    server, client = control
    client.begin()
    token = server.generation
    emit = server.emit

    def reply(kind, request=0, code=0, sequence=None, payload=b''):
        expected = KEEPALIVE if phase == 'heartbeat' else END
        if kind == expected | REPLY:
            if fault == 'refused':
                code = 6
            else:
                sequence = token + 1 if phase == 'heartbeat' else token
        emit(kind, request, code, sequence, payload)

    monkeypatch.setattr(server, 'emit', reply)
    monkeypatch.setattr(backend, 'CONTROL_HEARTBEAT', 0)
    with pytest.raises(ReceivePortError, match=r'lease lost|did not finish'):
        client.heartbeat() if phase == 'heartbeat' else client.finish()
    with pytest.raises(ReceivePortError, match='no longer valid'):
        client.next_delivery(.1)
    assert not server.writes


def test_operations_cannot_write_without_a_lease_or_acquire_one_twice(control):
    server, client = control
    with pytest.raises(OSError, match='no backend operation lease') as missing:
        client.finish()
    assert missing.value.errno == errno.EPERM
    with pytest.raises(WriteFailed) as refused:
        client.send([('/output/5/volume', 'f', (-40,))])
    assert refused.value.written == ()
    assert refused.value.unwritten == ('/output/5/volume',)
    assert server.requests == [HELLO]
    client.begin()
    with pytest.raises(OSError, match='already owns an operation') as duplicate:
        client.begin()
    assert duplicate.value.errno == errno.EALREADY
    assert server.requests == [HELLO, BEGIN]
    assert not server.writes


def test_refresh_retries_a_busy_window_and_discards_earlier_observations(control, monkeypatch):
    server, client = control
    client.begin()
    server.refresh_busy = True
    server.reports = [encode_osc('/output/5/volume', 'f', -6.0)]
    emit = server.emit

    def reply(kind, request=0, code=0, sequence=None, payload=b''):
        if kind == REFRESH | REPLY and code == BUSY:
            server.sequence += 1
            emit(OBSERVATION, code=DEVICE, sequence=server.sequence,
                 payload=encode_osc('/output/5/volume', 'f', -30.0))
            server.refresh_busy = False
        emit(kind, request, code, sequence, payload)

    monkeypatch.setattr(server, 'emit', reply)
    client.request_dump(timeout=.5)
    assert list(client.messages(.5)) == [('/output/5/volume', 'f', (-6.0,))]
    assert client.next_delivery(0) is None
    assert server.requests == [HELLO, BEGIN, REFRESH, REFRESH]
    assert server.writes == []


def test_a_definite_refresh_refusal_is_not_retried_or_misreported_as_a_dump(control, monkeypatch):
    server, client = control
    emit = server.emit

    def reply(kind, request=0, code=0, sequence=None, payload=b''):
        emit(kind, request, 2 if kind == REFRESH | REPLY else code, sequence, payload)

    monkeypatch.setattr(server, 'emit', reply)
    with pytest.raises(ReceivePortError, match='refresh window') as refused:
        client.request_dump(timeout=.5)
    assert refused.value.errno == errno.EBUSY
    assert server.requests == [HELLO, REFRESH]
    assert server.writes == []
