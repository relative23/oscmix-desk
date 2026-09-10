"""The seam between this project and whatever speaks to the device.

That is upstream oscmix over OSC on loopback. Six places used to
open their own socket and know the address, which made the dependency on
oscmix's *behaviour* invisible: it was spread through the control flow
as timing constants and barriers, with nothing naming what they were
working around.

Two reasons this is worth a module beyond tidiness.

**The workarounds get a name.** ``LINK_ECHO_TIMEOUT``, ``LINK_SETTLE``
and ``LINK_SYNC_BLIND_DELAY`` exist for one upstream implementation
detail -- ``setbool`` does not update oscmix's own view of the stereo
flag, so a ``/mix`` write arriving before the device echo is evaluated
against a stale value. That is a property of *this backend*, so it is
declared as one (``Traits.reports_link_state_on_write``) rather than
inferred from the shape of the code. When the patch offered as
michaelforney/oscmix#31 lands and the pin moves, flipping that flag is
the change; hunting for the barrier is not.

**It keeps an option open.** The one worth keeping is not a competing
mixer, it is an own *state path*: writing and reading the two dozen
registers this project actually pins directly over SysEx while oscmix
keeps the GUI and metering. That would remove the dual-writer problem
and kill the cache race at the root. It is not worth doing while one
device is modelled -- it
means owning register decoding for devices nobody here can test -- and
this is what makes it cheap to keep possible.

The seam is deliberately narrow: send a burst, ask for a dump, listen.
Everything about *when* stays with the caller. A backend that decided
timing would just be the old control flow with an extra indirection.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from types import TracebackType
from typing import Iterable, Iterator, Optional, Sequence, Tuple, Type

from .osc import decode_osc, encode_osc, iter_osc_messages

Message = Tuple[str, str, Tuple[object, ...]]

#: Datagrams larger than this are not produced by anything upstream
#: sends; the size is the socket read buffer, not a protocol limit.
READ_SIZE = 65536


@dataclass(frozen=True)
class Traits:
    """What a backend does that the control flow has to work around.

    Every field is a statement about the backend that can be *checked*,
    and ``tests/test_backend.py`` checks each one against a recording or
    a measurement rather than against belief. A trait nobody can verify
    does not belong here.
    """

    #: Whether writing a stereo flag updates the backend's own view of
    #: it, or whether that view only changes when the device echoes the
    #: register back. False for upstream oscmix at the pinned revision,
    #: measured by logging ``out->stereo`` inside ``setlevel()``:
    #: unpatched it reads 0 for a pair that was just linked.
    #:
    #: False is what makes the two-phase apply and its barrier
    #: necessary. See patches/0001 and michaelforney/oscmix#31.
    reports_link_state_on_write: bool

    #: Whether a state dump carries ``/mix/<out>/playback/<pb>``. False:
    #: confirmed absent from a full recorded dump, which is why the
    #: playback matrix is re-established rather than verified (ADR 0002).
    dumps_playback_matrix: bool

    #: Whether the device reports a register that did not change. False:
    #: writing a value it already holds produces no report, so "wait for
    #: the echo" cannot be the only synchronisation mechanism.
    reports_unchanged_registers: bool


#: Upstream oscmix at the pinned revision. Every value measured.
OSCMIX = Traits(
    reports_link_state_on_write=False,
    dumps_playback_matrix=False,
    reports_unchanged_registers=False,
)


class Listener:
    """A bound receive port, yielding decoded messages until it is done.

    Exists as an object because binding is the operation that can fail
    in a way the caller must handle: the mixer GUI holds the receive
    port whenever its window is open, and that is a normal state, not an
    error. ``Backend.listen`` returns None for it.
    """

    def __init__(self, sock: "socket.socket") -> None:
        self._sock = sock

    def messages(self, timeout: float) -> Iterator[Tuple[str, str,
                                                         Sequence[object]]]:
        """Decoded messages from one datagram, or nothing on timeout.

        Malformed messages are skipped rather than raised on: this reads
        off a socket, and one bad message must not end a dump that is
        otherwise confirming registers.
        """
        self._sock.settimeout(timeout)
        try:
            datagram, _ = self._sock.recvfrom(READ_SIZE)
        except (socket.timeout, OSError):
            return
        for raw in iter_osc_messages(datagram):
            try:
                path, tags, args = decode_osc(raw)
            except (ValueError, struct.error):
                continue
            yield path, tags, args

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "Listener":
        return self

    def __exit__(self, _kind: Optional[Type[BaseException]],
                 _value: Optional[BaseException],
                 _traceback: Optional[TracebackType]) -> None:
        # The three arguments are the protocol's, not ours: a listener
        # closes whether the block left cleanly or by exception.
        self.close()


class Backend:
    """Where the registers go, and where the reports come back from."""

    traits = OSCMIX

    def __init__(self, host: str, send_port: int, recv_port: int) -> None:
        self.host = host
        self.send_port = send_port
        self.recv_port = recv_port

    def send(self, messages: Iterable[Message]) -> None:
        """Put a burst of registers on the wire, in the order given.

        One socket for the burst: the order is the caller's, and this
        must not reorder or coalesce it.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for path, tags, args in messages:
                sock.sendto(encode_osc(path, tags, *args),
                            (self.host, self.send_port))
        finally:
            sock.close()

    def request_dump(self) -> None:
        """Ask the backend to report its entire register state."""
        self.send([("/refresh", "", ())])

    def listen(self) -> Optional[Listener]:
        """Bind the receive port, or None when something else holds it.

        No ``SO_REUSEADDR`` on purpose: a bind that succeeded alongside
        the mixer GUI would split the backend's datagrams between both
        readers and produce quietly wrong numbers on both sides.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.recv_port))
        except OSError:
            sock.close()
            return None
        return Listener(sock)


def loopback(send_port: int, recv_port: int) -> Backend:
    """The backend this project actually talks to."""
    return Backend("127.0.0.1", send_port, recv_port)
