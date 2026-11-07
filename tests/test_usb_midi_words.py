"""The usbmon decoder of the playback-mute and refresh-echo probes, on synthetic captures."""

import importlib.util
import struct

import pytest
from support import repo_file


@pytest.fixture(scope="module")
def words():
    spec = importlib.util.spec_from_file_location(
        "usb_midi_words", repo_file("scripts", "usb_midi_words.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def regword(reg, value):
    word = (reg & 0x7FFF) << 16 | value & 0xFFFF
    return word | (0 if (word.bit_count() % 2) else 1) << 31


def sysex(*pairs):
    body = b"".join(bytes((regword(r, v) >> (7 * i)) & 0x7F for i in range(5)) for r, v in pairs)
    return b"\xf0\x00\x20\x0d\x10\x00" + body + b"\xf7"


def packets(message, cable=1):
    """USB-MIDI event packets of one SysEx message."""
    out = bytearray()
    for start in range(0, len(message), 3):
        chunk = message[start:start + 3]
        cin = 0x4 if start + 3 < len(message) else 0x4 + len(chunk)
        out += bytes([cable << 4 | cin]) + chunk.ljust(3, b"\0")
    return bytes(out)


def record(when, payload, *, inward=False, device=4, bus=5):
    kind = ord("C" if inward else "S")
    endpoint = 0x86 if inward else 0x07
    header = struct.pack("<QBBBBHBBqiiII", 1, kind, 3, endpoint, device, bus, 0, 0,
                         int(when), 0, 0, len(payload), len(payload)).ljust(64, b"\0")
    frame = header + payload
    seconds = int(when)
    return struct.pack("<IIII", seconds, round((when - seconds) * 1e6), len(frame),
                       len(frame)) + frame


def capture(tmp_path, *records):
    path = tmp_path / "capture.pcap"
    head = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 220)
    path.write_bytes(head + b"".join(records))
    return path


def test_host_writes_and_device_reports_are_told_apart(words, tmp_path):
    path = capture(tmp_path,
                   record(10.5, packets(sysex((0x4020, 0x9000), (0x4021, 0)))),
                   record(10.6, packets(sysex((0x2000, 0x7D76))), inward=True),
                   record(10.7, packets(sysex((0x4020, 0))), device=9))
    found = list(words.register_words(path, 5, 4))
    assert [(w.direction, w.register, w.value) for w in found] == [
        ("out", 0x4020, 0x9000), ("out", 0x4021, 0), ("in", 0x2000, 0x7D76)]
    assert all(w.parity for w in found)
    assert found[0].time == pytest.approx(10.5)
    assert list(words.bulks(path, 5, 4)) == [(pytest.approx(10.5), [(0x4020, 0x9000),
                                                                     (0x4021, 0)])]


def test_interleaved_cables_keep_their_own_messages(words, tmp_path):
    first, second = packets(sysex((0x4020, 1)), cable=1), packets(sysex((0x4021, 2)), cable=0)
    mixed = b"".join(first[i:i + 4] + second[i:i + 4] for i in range(0, len(first), 4))
    path = capture(tmp_path, record(1.0, mixed))
    assert sorted(w.register for w in words.register_words(path, 5, 4)) == [0x4020, 0x4021]


def test_a_message_cut_by_a_new_start_is_dropped(words, tmp_path):
    cut = packets(sysex((0x4020, 1)))[:8]
    path = capture(tmp_path, record(1.0, cut + packets(sysex((0x4021, 2)))))
    assert [w.register for w in words.register_words(path, 5, 4)] == [0x4021]


def test_a_word_with_wrong_parity_is_reported_as_such(words, tmp_path):
    message = bytearray(sysex((0x4020, 1)))
    message[6 + 4] ^= 0x08  # flip bit 31
    path = capture(tmp_path, record(1.0, packets(bytes(message))))
    found, = words.register_words(path, 5, 4)
    assert found.parity is False


def test_a_file_that_is_no_usbmon_capture_is_refused(words, tmp_path):
    path = tmp_path / "other.pcap"
    path.write_bytes(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    with pytest.raises(ValueError, match="usbmon"):
        list(words.register_words(path, 5, 4))
