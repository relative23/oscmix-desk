"""One checked connection to the backend that owns MIDI and all OSC writers.

The versioned ODK1 envelope preserves delivery boundaries and provenance.
Operation owners take the device file lock, connect, acquire the lease, and
keep this connection through every phase, verification and repair. Helpers
borrow it; they never reconnect or use a direct UDP path. A backend reply is
processing acknowledgement, not device confirmation.
"""

from __future__ import annotations

import errno
import os
import socket
import stat
import struct
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Iterable, Iterator, List, NoReturn, Optional, Tuple

from .constants import CONTROL_ACK_TIMEOUT, CONTROL_ACQUIRE_TIMEOUT
from .diagnostics import backend_status
from .discovery import serial_in
from .errors import ReceivePortError, WriteFailed
from .model import Config
from .osc import Message, decode_delivery, encode_osc
from .protocol import (
    HEADER,
    HELLO_REPLY,
    MAGIC,
    PAYLOAD,
    REPLY,
    VERSION,
    Event,
    Request,
    Role,
    Source,
    Status,
)


@dataclass(frozen=True)
class Traits:
    """What a backend does that the control flow has to work around.

    Every field is a statement about the backend that can be *checked*,
    and ``tests/test_backend.py`` checks each one against a recording or
    a measurement rather than against belief. A trait nobody can verify
    does not belong here.

    Which of them the code *reads* is stated per field below, because
    for four releases none of them was: the docstring promised that
    flipping ``reports_link_state_on_write`` would be the change when
    upstream fixed the cache, and no branch consulted it. The barrier
    does now. The other two are facts the register table and the verifier
    already encode; they stay here as the named, tested statement of
    *why* that encoding is what it is, not as a switch.
    """

    #: Whether writing a stereo flag updates the backend's own view of
    #: it, or whether that view only changes when the device echoes the
    #: register back. False for upstream oscmix at the pinned revision,
    #: measured by logging ``out->stereo`` inside ``setlevel()``:
    #: unpatched it reads 0 for a pair that was just linked.
    #:
    #: False is what makes the two-phase apply and its barrier
    #: necessary. See patches/0001 and michaelforney/oscmix#31.
    #: **Read by** ``routing._cross_the_barrier``: True skips the wait.
    reports_link_state_on_write: bool

    #: Whether a state dump carries ``/mix/<out>/playback/<pb>``. False:
    #: confirmed absent from a full recorded dump, which is why the
    #: playback matrix is re-established rather than verified.
    #: Documented, not read: the register table encodes it as the
    #: ``REESTABLISHED`` class of ``/mix/{out}/playback/{pb}``.
    dumps_playback_matrix: bool

    #: Whether the device reports a register that did not change. False:
    #: writing a value it already holds produces no report, so "wait for
    #: the echo" cannot be the only synchronisation mechanism.
    #: Documented, not read: it is why the barrier is opportunistic and
    #: why the verifier's dump re-applies the mix regardless.
    reports_unchanged_registers: bool


#: Upstream oscmix at the pinned revision. Every value measured.
OSCMIX = Traits(
    reports_link_state_on_write=False,
    dumps_playback_matrix=False,
    reports_unchanged_registers=False,
)


CONTROL_QUEUE_BYTES = 256 * 1024
CONTROL_QUEUE_PACKETS = 4096
#: Seconds between KEEPALIVEs while the lease is held. The longest gap is
#: this plus one acknowledgement wait (CONTROL_ACK_TIMEOUT), which has to
#: stay clear of protocol.LEASE_IDLE; 2.0 until 0.8.1 left one second.
CONTROL_HEARTBEAT = 1.0


@dataclass(frozen=True)
class Delivery:
    """One complete OSC delivery and its actual backend origin.

    The register model still determines what an origin can confirm.
    Sequence numbers order deliveries on this connection, not device time.
    """

    origin: Source
    sequence: int
    epoch: bytes
    payload: bytes


class Control:
    """One checked backend connection; never reconnects or replays a request.

    The operation owner drives this object from one thread at a time.
    Reading or waiting here maintains its lease. A caller must use wait()
    instead of a long unserviced sleep while it owns an operation.
    """

    traits = OSCMIX

    def __init__(self, path: Path, expected_pid: int, *,
                 expected_uid: Optional[int] = None,
                 expected_gid: Optional[int] = None,
                 reader: bool = False,
                 sources: Source = Source.DEVICE | Source.DERIVED,
                 should_stop: Optional[Callable[[], bool]] = None) -> None:
        if sources < 1 or int(sources) & ~int(Source.DEVICE | Source.DERIVED | Source.METERS):
            raise ValueError("observation sources must be a nonempty subset of Source")
        self._sources = Source(sources)
        self.path = path
        self.should_stop = should_stop
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._closed = False
        self._request_id = 0
        self._lease: Optional[int] = None
        self._last_heartbeat = 0.0
        self._generation = 0
        self._last_sequence = 0
        self._queue: Deque[Delivery] = deque()
        self._queue_bytes = 0
        self.epoch = b""
        self.device_name = ""
        self.pid = self.uid = self.gid = -1
        try:
            self._connect(expected_pid, expected_uid, expected_gid, reader)
        except (OSError, ValueError, struct.error) as exc:
            self.close()
            if isinstance(exc, OSError):
                raise
            raise OSError(errno.EPROTO, "invalid backend handshake") from exc

    def _connect(self, expected_pid: int, expected_uid: Optional[int],
                 expected_gid: Optional[int], reader: bool) -> None:
        if not stat.S_ISSOCK(self.path.lstat().st_mode):
            raise OSError(errno.EPERM, "control endpoint is not a socket or is a symbolic link")
        self._sock.settimeout(CONTROL_ACK_TIMEOUT)
        self._sock.connect(str(self.path))
        # struct ucred: pid_t is signed, uid_t and gid_t are not.
        peer = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                     struct.calcsize("i2I"))
        self.pid, self.uid, self.gid = struct.unpack("i2I", peer)
        if (self.pid != expected_pid or (expected_uid is not None and self.uid != expected_uid)
                or (expected_gid is not None and self.gid != expected_gid)):
            raise OSError(errno.EPERM,
                          "control socket peer does not match the checked backend")
        role = Role.READER if reader else Role.DESK
        code, generation, payload = self._request(Request.HELLO, code=role,
                                                   payload=struct.pack(">I", self._sources))
        name = payload[HELLO_REPLY.size:-1]
        if (code != Status.OK or len(payload) <= HELLO_REPLY.size
                or not payload.endswith(b"\0") or b"\0" in name):
            raise OSError(errno.EPROTO, "invalid backend handshake")
        epoch, pid, version = HELLO_REPLY.unpack_from(payload)
        if pid != self.pid or version != VERSION:
            raise OSError(errno.EPROTO, "incompatible backend identity or protocol")
        self.epoch = epoch
        self.device_name = name.decode("utf-8", "strict")
        self._generation = generation

    def close(self) -> None:
        self._closed = True
        self._lease = None
        self._queue.clear()
        self._queue_bytes = 0
        self._sock.close()

    def _check(self) -> None:
        if self._closed:
            raise ReceivePortError(errno.ENOTCONN, "backend connection is no longer valid")
        if self.should_stop is not None and self.should_stop():
            self.close()
            raise ReceivePortError(errno.ECANCELED, "backend operation cancelled")

    def _send_request(self, kind: Request, code: int = 0,
                      payload: bytes = b"", token: Optional[int] = None) -> int:
        self._check()
        if len(payload) > PAYLOAD or self._request_id == 0xffffffff:
            self.close()
            raise OSError(errno.EOVERFLOW, "control request exceeds protocol limits")
        self._request_id += 1
        packet = HEADER.pack(
            MAGIC, kind, self._request_id, code,
            self._generation if token is None else token) + payload
        try:
            self._sock.settimeout(CONTROL_ACK_TIMEOUT)
            sent = self._sock.send(packet)
        except OSError:
            self.close()
            raise
        if sent != len(packet):
            self.close()
            raise OSError(errno.EIO, "incomplete control request")
        return self._request_id

    def _receive(self, timeout: float) -> Optional[Tuple[int, int, int, int, bytes]]:
        self._check()
        try:
            self._sock.settimeout(max(0.001, timeout))
            packet, _ancillary, flags, _address = self._sock.recvmsg(
                HEADER.size + PAYLOAD + 1)
        except socket.timeout:
            return None
        except OSError as exc:
            self.close()
            raise ReceivePortError(exc.errno, "backend receive failed: %s" % exc) from exc
        if not packet:
            self.close()
            raise ReceivePortError(errno.ECONNRESET, "backend disconnected; observations invalid")
        if (len(packet) < HEADER.size
                or len(packet) > HEADER.size + PAYLOAD
                or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
            self._protocol_error("invalid control packet size")
        magic, kind, request, code, sequence = HEADER.unpack_from(packet)
        if magic != MAGIC:
            self._protocol_error("incompatible control framing")
        return kind, request, code, sequence, packet[HEADER.size:]

    def _protocol_error(self, reason: str) -> NoReturn:
        self.close()
        raise ReceivePortError(errno.EPROTO, reason)

    def _event(self, frame: Tuple[int, int, int, int, bytes]) -> None:
        kind, request, code, sequence, payload = frame
        if request:
            self._protocol_error("unexpected control reply")
        if kind == Event.LEASE:
            if payload or code not in (Status.OK, Status.BUSY) or sequence < self._generation:
                self._protocol_error("invalid lease event")
            if self._lease is not None and (sequence != self._lease or code != Status.BUSY):
                self._protocol_error("backend operation lease changed unexpectedly")
            self._generation = sequence
        elif kind == Event.WINDOW:
            if payload or code not in (Status.OK, Status.BUSY):
                self._protocol_error("invalid refresh-window event")
        elif kind == Event.OBSERVATION:
            if (code not in (Source.DEVICE, Source.DERIVED, Source.METERS)
                    or not code & self._sources
                    or not payload or len(payload) % 4
                    or sequence <= self._last_sequence or not self.epoch):
                self._protocol_error("invalid observation origin, order or delivery")
            self._last_sequence = sequence
            size = HEADER.size + len(payload)
            if (len(self._queue) >= CONTROL_QUEUE_PACKETS
                    or self._queue_bytes + size > CONTROL_QUEUE_BYTES):
                self.close()
                raise ReceivePortError(errno.ENOBUFS, "backend observation queue overflowed")
            self._queue.append(Delivery(Source(code), sequence, self.epoch, payload))
            self._queue_bytes += size
        else:
            self._protocol_error("unexpected backend event")

    def _acknowledgement(self, kind: Request, request: int) -> Tuple[int, int, bytes]:
        deadline = time.monotonic() + CONTROL_ACK_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ReceivePortError(errno.ETIMEDOUT, "backend acknowledgement timed out")
            frame = self._receive(min(remaining, 0.25))
            if frame is None:
                continue
            received_kind, number, code, sequence, payload = frame
            if received_kind & REPLY:
                if received_kind != kind | REPLY or number != request:
                    self._protocol_error("unexpected backend acknowledgement")
                if kind != Request.HELLO and (payload or code not in (
                        Status.OK, Status.BUSY, Status.INVALID, Status.NOT_OWNER)):
                    self._protocol_error("invalid backend acknowledgement")
                return code, sequence, payload
            self._event(frame)

    def _request(self, kind: Request, code: int = 0, payload: bytes = b"",
                 token: Optional[int] = None) -> Tuple[int, int, bytes]:
        request = self._send_request(kind, code, payload, token)
        return self._acknowledgement(kind, request)

    def _discard(self) -> None:
        self._queue.clear()
        self._queue_bytes = 0

    def begin(self, timeout: float = CONTROL_ACQUIRE_TIMEOUT) -> None:
        if self._lease is not None:
            raise OSError(errno.EALREADY, "this connection already owns an operation")
        deadline = time.monotonic() + timeout
        while True:
            code, generation, _ = self._request(Request.BEGIN)
            self._discard()  # no observation window precedes this lease's receipt boundary
            if code == Status.OK:
                self._lease = generation
                self._generation = generation
                self._last_heartbeat = time.monotonic()
                return
            if code == Status.NOT_OWNER:
                raise OSError(errno.EPERM, "this connection's role cannot take the "
                              "backend operation lease")
            if code != Status.BUSY or time.monotonic() >= deadline:
                raise OSError(errno.EBUSY, "another client holds the backend operation lease")
            self.wait(min(0.1, max(0.0, deadline - time.monotonic())))

    def heartbeat(self) -> None:
        self._check()
        if (self._lease is not None
                and time.monotonic() - self._last_heartbeat >= CONTROL_HEARTBEAT):
            code, generation, _ = self._request(Request.KEEPALIVE, token=self._lease)
            if code or generation != self._lease:
                self._protocol_error("backend operation lease lost")
            self._last_heartbeat = time.monotonic()

    def finish(self) -> None:
        """Check release acknowledgement before committing a successful marker."""
        self._check()
        if self._lease is None:
            raise OSError(errno.EPERM, "no backend operation lease to finish")
        token, self._lease = self._lease, None
        code, generation, _ = self._request(Request.END, token=token)
        if code or generation <= token:
            self._protocol_error("backend operation did not finish")
        self._generation = generation

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            self.heartbeat()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            frame = self._receive(min(0.25, remaining))
            if frame is not None:
                self._event(frame)

    def next_delivery(self, timeout: float) -> Optional[Delivery]:
        deadline = time.monotonic() + timeout
        while True:
            self.heartbeat()
            if self._queue:
                result = self._queue.popleft()
                self._queue_bytes -= HEADER.size + len(result.payload)
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            frame = self._receive(min(remaining, 0.25))
            if frame is not None:
                self._event(frame)

    def messages(self, timeout: float) -> Iterator[Message]:
        """Decode one complete device-origin delivery, excluding cached echoes.

        Callers finish the delivery before selecting writes. A skipped
        backend-derived packet is not a missing device value and cannot
        revoke or confirm one; it simply supplies no hardware evidence.
        """
        delivery = self.next_delivery(timeout)
        if delivery is None or delivery.origin != Source.DEVICE:
            return
        try:
            messages = decode_delivery(delivery.payload)
        except ValueError:
            self._protocol_error("invalid OSC delivery; observations invalid")
        yield from messages

    def request_dump(self, timeout: float = 12.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            self.heartbeat()
            code, _generation, _ = self._request(Request.REFRESH, token=self._lease)
            self._discard()
            if code == Status.OK:
                return
            if code != Status.BUSY or time.monotonic() >= deadline:
                raise ReceivePortError(errno.EBUSY, "no fresh backend refresh window available")
            self.wait(min(0.1, max(0.0, deadline - time.monotonic())))

    def send(self, messages: Iterable[Message]) -> None:
        burst = list(messages)
        paths = [message[0] for message in burst]
        written: List[str] = []
        if self._lease is None:
            raise WriteFailed(OSError(errno.EPERM, "writes require the complete operation lease"),
                              [], paths)
        for index, (path, tags, args) in enumerate(burst):
            submitted = False
            try:
                self.heartbeat()
                request = self._send_request(Request.WRITE,
                                             payload=encode_osc(path, tags, *args),
                                             token=self._lease)
                submitted = True
                code, generation, _ = self._acknowledgement(Request.WRITE, request)
                if code == Status.OK and generation != self._lease:
                    self._protocol_error("backend lease changed during write")
            except OSError as exc:
                if submitted:
                    written.append(path)  # acknowledgement lost: may have reached hardware
                raise WriteFailed(exc, written, paths[index + int(submitted):]) from exc
            if code:  # a definite refusal before processing
                raise WriteFailed(OSError(errno.EBUSY if code == Status.BUSY else errno.EPROTO,
                                  "backend refused write to %s (code %d)" % (path, code)),
                                  written, paths[index:])
            written.append(path)


def connect_backend(config: Config, config_path: Optional[Path] = None, *,
                    reader: bool = False,
                    sources: Source = Source.DEVICE | Source.DERIVED,
                    should_stop: Optional[Callable[[], bool]] = None,
                    expected_pid: Optional[int] = None) -> Control:
    """Pin a read-only identity check to the actual kernel peer connection.

    Connecting never acquires writing permission. The operation takes its
    device file lock first, then calls begin() on this checked connection.
    Neither an old UDP listener nor an unidentifiable bridge is a fallback.
    """
    proc_root = Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc"))
    before = backend_status(config, proc_root, config_path)
    if (before.state != "ready" or before.pid is None or before.endpoint is None
            or before.device is None):
        raise OSError(errno.ENOTCONN, before.detail)
    if expected_pid is not None and before.pid != expected_pid:
        raise OSError(errno.EPERM, "control endpoint is not owned by the started backend")
    connection = Control(Path(before.endpoint), before.pid,
                         expected_uid=before.uid, expected_gid=before.gid,
                         reader=reader, sources=sources, should_stop=should_stop)
    try:
        unchanged = (serial_in(connection.device_name) == before.device.serial
                     and backend_status(config, proc_root, config_path) == before)
    except BaseException:
        connection.close()
        raise
    if not unchanged:
        connection.close()
        raise OSError(errno.EPERM, "backend or device identity changed while connecting")
    return connection
