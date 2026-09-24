"""Real coordinated backend with anonymous simulated MIDI pipes; no ALSA access.

Qualification helpers, also shared with the actual GTK lifecycle run. The
wire peer deliberately does not use desk's client, so it can challenge that
client's protocol assumptions independently.
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

HEADER = struct.Struct('>4sIIIQ')
REPLY = 0x80000000
HELLO, BEGIN, END, KEEPALIVE, WRITE, REFRESH = range(1, 7)
EVENT, OBSERVATION = 16, 17
OK, BUSY, INVALID, EXPIRED, SHUTDOWN, OVERFLOW, NOT_OWNER = range(7)
DESK, GUI, READER = 1, 2, 3
DEVICE, DERIVED, METERS = 1, 2, 4


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
        self.socket.sendall(HEADER.pack(b'ODK1', kind, self.request_id, code,
                                       self.token if token is None else token) + payload)
        return self.request_id

    def receive(self):
        packet = self.socket.recv(8217)
        if not packet:
            raise EOFError('backend disconnected')
        magic, kind, request, code, sequence = HEADER.unpack_from(packet)
        assert magic == b'ODK1'
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
