"""OSC 1.0 encoding and decoding -- the subset oscmix understands."""

from __future__ import annotations

import struct
from typing import Iterator, List, Tuple, Union

#: What an argument is on this wire: oscmix speaks ``,i``, ``,f`` and
#: ``,s``, and nothing else is encoded or decoded here. Named once, so
#: that a register value is not an ``object`` every reader has to cast
#: past the type checker (0.7.0; it was, and 13 ``type: ignore`` said so).
Value = Union[int, float, str]
Args = Tuple[Value, ...]
#: One register write or report: path, type tags, arguments.
Message = Tuple[str, str, Args]


def _osc_string(value: str) -> bytes:
    """Encode an OSC string: ASCII, NUL-terminated, padded to 4 bytes."""
    raw = value.encode("ascii") + b"\x00"
    return raw + b"\x00" * (-len(raw) % 4)


def encode_osc(path: str, types: str = "", *args: Value) -> bytes:
    """Encode a single OSC message.

    Supported type tags: ``f`` (float32), ``i`` (int32), ``s`` (string).
    """
    if len(types) != len(args):
        raise ValueError(
            "type tag %r expects %d arguments, got %d" % (types, len(types), len(args))
        )
    data = _osc_string(path) + _osc_string("," + types)
    for tag, value in zip(types, args):
        if tag == "f":
            data += struct.pack(">f", float(value))
        elif tag == "i":
            data += struct.pack(">i", int(value))
        elif tag == "s":
            data += _osc_string(str(value))
        else:
            raise ValueError("unsupported OSC type tag %r" % tag)
    return data


def _decode_string(data: bytes, offset: int) -> Tuple[str, int]:
    end = data.index(b"\x00", offset)
    value = data[offset:end].decode("ascii")
    end += 1
    padded = end + (-end % 4)
    if padded > len(data) or any(data[end:padded]):
        raise ValueError("invalid OSC string padding")
    return value, padded


def decode_osc(data: bytes) -> Message:
    """Decode a single OSC message (type tags ``f``, ``i``, ``s``)."""
    try:
        path, offset = _decode_string(data, 0)
        tags, offset = _decode_string(data, offset)
    except (ValueError, IndexError):
        raise ValueError("truncated OSC message") from None
    if not tags.startswith(","):
        raise ValueError("missing OSC type tag string")
    args: List[Value] = []
    try:
        for tag in tags[1:]:
            value: Value
            if tag == "f":
                (value,) = struct.unpack_from(">f", data, offset)
                offset += 4
            elif tag == "i":
                (value,) = struct.unpack_from(">i", data, offset)
                offset += 4
            elif tag == "s":
                value, offset = _decode_string(data, offset)
            else:
                raise ValueError("unsupported OSC type tag %r" % tag)
            args.append(value)
    except (struct.error, IndexError):
        raise ValueError("truncated OSC arguments") from None
    if offset != len(data):
        raise ValueError("unexpected trailing OSC data")
    return path, tags[1:], tuple(args)


def iter_osc_messages(datagram: bytes) -> Iterator[bytes]:
    """Yield the OSC messages in a datagram, unwrapping #bundle framing.

    Iterative, not recursive: a bundle may contain bundles, and the
    nesting costs four bytes a level on the wire while it costs a Python
    frame here. A datagram that nests a thousand deep fits in a UDP
    packet; a thousand frames do not fit in the interpreter's stack, and
    this reads whatever the network hands it.

    Depth first, in wire order. Invalid bundle framing raises rather than
    silently keeping a prefix. Consumers must finish decoding the delivery
    before using any of its messages; see ``decode_delivery``.
    """
    pending = [datagram]
    while pending:
        current = pending.pop()
        if len(current) % 4:
            raise ValueError("unaligned OSC delivery")
        if not current.startswith(b"#bundle\x00"):
            yield current
            continue
        if len(current) < 16:
            raise ValueError("truncated OSC bundle header")
        offset = 16  # "#bundle\0" plus 8-byte time tag
        elements = []
        while offset + 4 <= len(current):
            (size,) = struct.unpack_from(">i", current, offset)
            offset += 4
            if size <= 0 or size % 4 or offset + size > len(current):
                raise ValueError("invalid OSC bundle element size")
            elements.append(current[offset:offset + size])
            offset += size
        pending.extend(reversed(elements))


def decode_delivery(data: bytes) -> Tuple[Message, ...]:
    """Decode every message before exposing any part of one OSC delivery."""
    return tuple(decode_osc(raw) for raw in iter_osc_messages(data))
