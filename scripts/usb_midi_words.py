"""Register words in a usbmon capture of the interface's MIDI traffic; no live I/O.

A capture (``dumpcap -i usbmonN``, LINKTYPE_USB_LINUX or _MMAPPED) holds URBs. The host's
writes are the submitted bulk OUT URBs, the device's reports the completed bulk IN URBs. Their
USB-MIDI event packets carry SysEx ``F0 00 20 0D 10 <sub> <5-byte groups> F7``; sub ID 0 holds
32-bit register words: an odd-parity bit 31, the register in bits 16-30, the value below.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

#: Bytes a USB-MIDI event packet carries for each code index number.
CIN_BYTES = {0x4: 3, 0x5: 1, 0x6: 2, 0x7: 3, 0x8: 3, 0x9: 3, 0xA: 3, 0xB: 3,
             0xC: 2, 0xD: 2, 0xE: 3, 0xF: 1}
HEADER = {189: 48, 220: 64}
RME = b'\xf0\x00\x20\x0d\x10'


class Word(NamedTuple):
    time: float
    direction: str
    register: int
    value: int
    parity: bool


def urbs(path: Path, bus: int, device: int) -> Iterator[tuple[float, str, bytes]]:
    """(time, direction, payload) of the device's bulk URBs that carry data."""
    data = path.read_bytes()
    if len(data) < 24 or struct.unpack_from('<I', data)[0] != 0xA1B2C3D4:
        raise ValueError('not a microsecond little-endian pcap file')
    header = HEADER.get(struct.unpack_from('<I', data, 20)[0])
    if header is None:
        raise ValueError('not a usbmon capture')
    offset = 24
    while offset + 16 <= len(data):
        seconds, micros, captured, _length = struct.unpack_from('<IIII', data, offset)
        frame = data[offset + 16:offset + 16 + captured]
        offset += 16 + captured
        if len(frame) < header:
            raise ValueError('truncated usbmon record')
        (_id, kind, transfer, endpoint, number, busnum, _setup, _flag, _sec, _usec, _status,
         _len, kept) = struct.unpack_from('<QBBBBHBBqiiII', frame)
        payload = frame[header:header + kept]
        if busnum != bus or number != device or transfer != 3 or not payload:
            continue
        inward = bool(endpoint & 0x80)
        if kind == (ord('C') if inward else ord('S')):
            yield seconds + micros / 1e6, 'in' if inward else 'out', payload


def messages(path: Path, bus: int, device: int) -> Iterator[tuple[float, str, bytes]]:
    """Complete SysEx messages per direction and cable; one cut by a new F0 is dropped."""
    pending: dict[tuple[str, int], bytearray] = {}
    for when, direction, payload in urbs(path, bus, device):
        for start in range(0, len(payload) - 3, 4):
            count = CIN_BYTES.get(payload[start] & 0x0F)
            if count is None:
                continue
            buffer = pending.setdefault((direction, payload[start] >> 4), bytearray())
            for byte in payload[start + 1:start + 1 + count]:
                if byte == 0xF0:
                    buffer.clear()
                if buffer or byte == 0xF0:
                    buffer.append(byte)
                if byte == 0xF7 and buffer:
                    yield when, direction, bytes(buffer)
                    buffer.clear()


def parity(word: int) -> bool:
    """Whether the 32-bit word has the odd parity the device uses."""
    return (word & 0xFFFFFFFF).bit_count() % 2 == 1


def register_words(path: Path, bus: int, device: int) -> Iterator[Word]:
    """Every register word (sub ID 0) of the capture, in capture order."""
    for when, direction, message in messages(path, bus, device):
        if message[:5] != RME or len(message) < 7 or message[5] != 0:
            continue
        body = message[6:-1]
        if len(body) % 5:
            raise ValueError('register message of %d bytes is not whole words' % len(body))
        for start in range(0, len(body), 5):
            word = 0
            for shift, byte in enumerate(body[start:start + 5]):
                word |= (byte & 0x7F) << (7 * shift)
            word &= 0xFFFFFFFF
            yield Word(when, direction, word >> 16 & 0x7FFF, word & 0xFFFF, parity(word))


def bulks(path: Path, bus: int, device: int) -> Iterator[tuple[float, list[tuple[int, int]]]]:
    """The host's register messages, each as (time, [(register, value)])."""
    for when, direction, message in messages(path, bus, device):
        if direction != 'out' or message[:5] != RME or len(message) < 7 or message[5] != 0:
            continue
        body = message[6:-1]
        words = []
        for start in range(0, len(body) - 4, 5):
            word = 0
            for shift, byte in enumerate(body[start:start + 5]):
                word |= (byte & 0x7F) << (7 * shift)
            words.append((word >> 16 & 0x7FFF, word & 0xFFFF))
        yield when, words
