"""The ODK1 control protocol, as the coordinated backend defines it.

Every value mirrors ``control.h`` from patches/0003; tests/test_protocol.py
parses that header and fails when one side changes without the other.
This module holds wire facts only. How the desk uses them (timeouts,
queue limits, retries) stays with the client in backend.py.
"""

import struct
from enum import IntEnum, IntFlag

MAGIC = b"ODK1"
#: Protocol implementation version reported in the HELLO reply.
VERSION = 1
#: magic, kind, request number, code, sequence (a lease token on requests).
HEADER = struct.Struct(">4sIIIQ")
#: Set in ``kind`` on the reply to a request of that kind.
REPLY = 0x80000000
PAYLOAD = 8192
#: HELLO reply payload: epoch, backend pid, VERSION, then the device name
#: terminated by one NUL.
HELLO_REPLY = struct.Struct(">16sII")


class Request(IntEnum):
    """Request kinds a client sends; each gets exactly one reply."""

    HELLO = 1
    BEGIN = 2
    END = 3
    KEEPALIVE = 4
    WRITE = 5
    REFRESH = 6


class Event(IntEnum):
    """Unsolicited packets; their request number is always 0."""

    #: The operation lease changed: code OK (free) or BUSY (owned),
    #: sequence the new lease generation. ``CONTROL_EVENT`` in control.h.
    LEASE = 16
    #: One OSC delivery: code its Source, sequence the delivery number.
    OBSERVATION = 17
    #: A refresh window opened (code BUSY) or closed (OK); sequence
    #: counts windows.
    WINDOW = 18


class Status(IntEnum):
    """Reply codes."""

    OK = 0
    BUSY = 1
    INVALID = 2
    EXPIRED = 3
    SHUTDOWN = 4
    OVERFLOW = 5
    NOT_OWNER = 6


class Role(IntEnum):
    """Sent as the HELLO code. Only a DESK may take the operation lease."""

    DESK = 1
    GUI = 2
    READER = 3


class Source(IntFlag):
    """Where an observation came from; a client subscribes to a subset."""

    #: The MIDI register handler: what the device reported.
    DEVICE = 1
    #: The command and cache handler: what the backend derived or echoed.
    DERIVED = 2
    #: Level metering.
    METERS = 4
