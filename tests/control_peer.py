"""Real coordinated backend with anonymous simulated MIDI pipes; no ALSA access.

Qualification helpers, also shared with the actual GTK lifecycle run. The
wire peer deliberately does not use desk's client, so it can challenge that
client's protocol assumptions independently. It shares only the wire values,
which tests/test_protocol.py holds to control.h.
"""

import fcntl
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time

from oscmix_desk.protocol import (
    HEADER,
    MAGIC,
    PAYLOAD,
    REPLY,
    Event,
    Request,
    Role,
    Source,
    Status,
)

HELLO, BEGIN, END, KEEPALIVE, WRITE, REFRESH = Request
EVENT, OBSERVATION, WINDOW = Event
OK, BUSY, INVALID, EXPIRED, SHUTDOWN, OVERFLOW, NOT_OWNER = Status
DESK, GUI, READER = Role
DEVICE, DERIVED, METERS = Source.DEVICE, Source.DERIVED, Source.METERS


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError('condition did not become true within %.1fs' % timeout)


def sysex(registers):
    data = bytearray(b'\xf0\x00\x20\x0d\x10\x00')
    for address, value in registers:
        word = address << 16 | (value & 0xffff)
        word |= (1 - bin(word).count('1') % 2) << 31
        data.extend((word >> shift) & 0x7f for shift in range(0, 35, 7))
    return bytes(data) + b'\xf7'


class SimulatedMidi:
    def __init__(self, binary, path, *, drain=True):
        self.path = path
        self.packets = []
        self.failure = None
        self.done = threading.Event()
        read_fd, self.inject_fd = os.pipe()
        self.capture_fd, write_fd = os.pipe()
        if not drain:
            fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
        # The child wrapper's source fds cannot alias its required fds 6/7.
        read_high = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 10)
        write_high = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(read_fd)
        os.close(write_fd)
        self.transcript = path.with_suffix('.log').open('w+b')
        try:
            self.child = subprocess.Popen(
                [sys.executable, '-c',
                 ('import os,sys; os.dup2(int(sys.argv[1]),6); '
                 'os.dup2(int(sys.argv[2]),7); '
                 'os.execv(sys.argv[3],sys.argv[3:])'),
                 str(read_high), str(write_high), str(binary), '-l', '-p',
                 'Fireface UCX II (00000000)', '-c', str(path)],
                pass_fds=(read_high, write_high), stdout=self.transcript,
                stderr=subprocess.STDOUT)
        finally:
            os.close(read_high)
            os.close(write_high)
        self.thread = threading.Thread(target=self._read, daemon=True) if drain else None
        if self.thread is not None:
            self.thread.start()

    def _read(self):
        pending = b''
        try:
            while not self.done.is_set():
                if not select.select([self.capture_fd], [], [], 0.05)[0]:
                    continue
                data = os.read(self.capture_fd, 8192)
                if not data:
                    return
                pending += data
                while b'\xf7' in pending:
                    packet, pending = pending.split(b'\xf7', 1)
                    self.packets.append(packet + b'\xf7')
        except OSError as exc:
            self.failure = exc

    def connect(self, role=DESK, sources=DEVICE | DERIVED | METERS):
        wait_for(lambda: self.path.exists() or self.child.poll() is not None)
        assert self.child.poll() is None, self.log()
        return Peer(self.path, role, sources)

    def inject(self, *registers):
        os.write(self.inject_fd, sysex(registers))

    def log(self):
        self.transcript.flush()
        return self.path.with_suffix('.log').read_text()

    def registers(self):
        result = []
        for packet in list(self.packets):
            if not packet.startswith(b'\xf0\x00\x20\x0d\x10\x00'):
                continue
            for start in range(6, len(packet) - 1, 5):
                word = sum(value << (7 * shift)
                           for shift, value in enumerate(packet[start:start + 5]))
                address = (word >> 16) & 0x7fff
                if address != 0x3f00:  # keepalive is not a register mutation
                    result.append((address, word & 0xffff))
        return result

    def close(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=4)
                raise AssertionError('backend did not stop') from None
        self.done.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            assert not self.thread.is_alive()
        for fd in (self.inject_fd, self.capture_fd):
            if fd >= 0:
                os.close(fd)
        self.transcript.close()
        assert self.failure is None


class Peer:
    def __init__(self, path, role, sources):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.socket.settimeout(2)
        self.socket.connect(str(path))
        self.request_id = 0
        self.token = 0
        self.events = []
        self.observations = []
        try:
            self.hello = self.request(HELLO, code=role, payload=struct.pack('>I', sources))
        except BaseException:
            self.socket.close()
            raise
        self.epoch = self.hello[4][:16]
        self.pid = struct.unpack_from('>I', self.hello[4], 16)[0]

    def send(self, kind, *, code=0, payload=b'', token=None):
        self.request_id += 1
        self.socket.sendall(HEADER.pack(MAGIC, kind, self.request_id, code,
                                       self.token if token is None else token) + payload)
        return self.request_id

    def receive(self):
        packet = self.socket.recv(HEADER.size + PAYLOAD + 1)
        if not packet:
            raise EOFError('backend disconnected')
        magic, kind, request, code, sequence = HEADER.unpack_from(packet)
        assert magic == MAGIC
        result = kind, request, code, sequence, packet[HEADER.size:]
        if kind == EVENT:
            self.token = sequence
            self.events.append(result)
        elif kind == OBSERVATION:
            self.observations.append(result)
        return result

    def request(self, kind, **kwargs):
        request = self.send(kind, **kwargs)
        while True:
            result = self.receive()
            if result[0] == kind | REPLY and result[1] == request:
                self.token = result[3]
                return result

    def observation(self, source=DEVICE):
        while True:
            result = self.receive()
            if result[0] == OBSERVATION and result[2] == source:
                return result

    def close(self):
        self.socket.close()

    def drain(self):
        while True:
            self.receive()


class ScriptedControl:
    def __init__(self, path, *, handshake=None, refuse_write=0, lose_reply=0,
                 begin_busy=False, refresh_busy=False, reports=(), echo=False,
                 disconnect_after=None, close_after_dump=False, hello_code=OK,
                 busy_begins=0, silent=(), delay=None, after_hello=(), after_begin=(),
                 derived=()):
        # silent: kinds never answered while the connection stays open.
        # delay: {kind: seconds} before a reply. after_hello/after_begin: raw
        # packets (bytes) or (kind, code, sequence, payload) frames sent after
        # that reply; a sequence of None means the current generation.
        # derived: payloads a refresh answers from the backend's own view,
        # before the device reports.
        self.path = path
        self.derived = derived
        self.hello_code = hello_code
        self.busy_begins = busy_begins
        self.silent = silent
        self.delay = delay or {}
        self.after_hello = after_hello
        self.after_begin = after_begin
        self.reports = reports
        self.echo = echo
        self.disconnect_after = disconnect_after
        self.close_after_dump = close_after_dump
        self.sequence = 0
        self.handshake = handshake
        self.refuse_write = refuse_write
        self.lose_reply = lose_reply
        self.begin_busy = begin_busy
        self.refresh_busy = refresh_busy
        self.generation = 1
        self.writes = []
        self.order = []
        self.requests = []
        self.wire = []  # (kind, request, code, token, payload) as received
        self.error = None
        self.done = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.peer = None
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def emit(self, kind, request=0, code=0, sequence=None, payload=b''):
        self.peer.sendall(HEADER.pack(MAGIC, kind, request, code,
                                     self.generation if sequence is None else sequence) + payload)

    def run(self):
        try:
            while not self.done.is_set():
                try:
                    self.peer, _ = self.listener.accept()
                except socket.timeout:
                    continue
                self.peer.settimeout(0.1)
                try:
                    self.serve()
                finally:
                    self.peer.close()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            self.error = exc

    def serve(self):
        while not self.done.is_set():
            try:
                data = self.peer.recv(10000)
            except socket.timeout:
                continue
            if not data:
                return
            magic, kind, request, request_code, token = HEADER.unpack_from(data)
            assert magic == MAGIC
            self.requests.append(kind)
            self.wire.append((kind, request, request_code, token, data[HEADER.size:]))
            code = OK
            payload = b''
            if kind == HELLO:
                payload = (bytes(range(16)) + struct.pack('>II', os.getpid(), 1)
                           + b'Fireface UCX II (00000000)\0')
                if self.handshake is not None:
                    payload = self.handshake
                code = self.hello_code
            elif kind == BEGIN:
                code = BUSY if self.begin_busy else OK
                if self.busy_begins:
                    self.busy_begins -= 1
                    code = BUSY
                if code == OK:
                    self.generation += 1
            elif kind == END:
                self.generation += 1
                self.emit(EVENT)
            elif kind == WRITE:
                code = self.refuse_write
                if not code:
                    from oscmix_desk.osc import decode_osc
                    self.writes.append(data[HEADER.size:])
                    self.order.append(decode_osc(data[HEADER.size:])[0])
            elif kind == REFRESH:
                code = BUSY if self.refresh_busy else OK
                if not code:
                    self.order.append('/refresh')
            if (kind == self.lose_reply or (kind == WRITE and self.order
                                           and self.order[-1] == self.disconnect_after)):
                return
            if kind in self.silent:
                continue
            time.sleep(self.delay.get(kind, 0))
            self.emit(kind | REPLY, request, code, payload=payload)
            follow = (self.after_hello if kind == HELLO else
                      self.after_begin if kind == BEGIN and code == OK else ())
            for frame in follow:
                if isinstance(frame, bytes):
                    self.peer.sendall(frame)
                else:
                    event, event_code, sequence, data = frame
                    self.emit(event, 0, event_code, sequence, data)
            if kind == REFRESH and code == OK:
                for report in self.derived:
                    self.sequence += 1
                    self.emit(OBSERVATION, code=DERIVED, sequence=self.sequence, payload=report)
                for report in self.reports:
                    self.sequence += 1
                    self.emit(OBSERVATION, code=DEVICE, sequence=self.sequence, payload=report)
                if self.close_after_dump:
                    return
            elif kind == WRITE and code == OK and self.echo:
                self.sequence += 1
                self.emit(OBSERVATION, code=DEVICE, sequence=self.sequence,
                          payload=data[HEADER.size:])

    def close(self):
        self.done.set()
        self.thread.join(timeout=3)
        self.listener.close()
        if self.peer is not None:
            self.peer.close()
        assert not self.thread.is_alive()
        assert self.error is None, self.error
