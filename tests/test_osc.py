"""OSC 1.0 encoding: golden bytes and error handling."""

import struct

import pytest


def test_mix_message_golden_bytes(session_mod):
    # /mix/5/playback/1 ,fi 0.0 -100 -- the exact message the old
    # shell implementation sent, byte for byte.
    expected = (
        b"/mix/5/playback/1\x00\x00\x00"  # 17 chars + NUL, padded to 20
         b",fi\x00"
        + struct.pack(">f", 0.0)
        + struct.pack(">i", -100)
    )
    assert session_mod.encode_osc("/mix/5/playback/1", "fi", 0.0, -100) == expected


def test_path_length_multiple_of_four_gets_full_pad(session_mod):
    # A 3-char path needs exactly one NUL; a 4-char path needs four
    # (OSC strings always carry at least one NUL terminator).
    assert session_mod.encode_osc("/ab") == b"/ab\x00,\x00\x00\x00"
    assert session_mod.encode_osc("/abc") == b"/abc\x00\x00\x00\x00,\x00\x00\x00"


def test_int_argument(session_mod):
    msg = session_mod.encode_osc("/output/5/stereo", "i", 1)
    assert msg.endswith(struct.pack(">i", 1))
    assert b",i\x00\x00" in msg


def test_float_argument_big_endian(session_mod):
    msg = session_mod.encode_osc("/output/5/volume", "f", -6.5)
    assert msg.endswith(struct.pack(">f", -6.5))


def test_string_argument(session_mod):
    msg = session_mod.encode_osc("/x", "s", "abc")
    assert msg.endswith(b"abc\x00")


def test_message_length_is_multiple_of_four(session_mod):
    for path in ("/a", "/ab", "/abc", "/abcd", "/mix/10/playback/12"):
        assert len(session_mod.encode_osc(path, "fi", 1.5, 2)) % 4 == 0


def test_argument_count_mismatch_raises(session_mod):
    with pytest.raises(ValueError):
        session_mod.encode_osc("/x", "fi", 1.0)


def test_unsupported_type_tag_raises(session_mod):
    with pytest.raises(ValueError):
        session_mod.encode_osc("/x", "b", b"blob")


def test_decode_roundtrip(session_mod):
    message = session_mod.encode_osc("/mix/5/playback/1", "fi", -3.0, -100)
    path, tags, args = session_mod.decode_osc(message)
    assert path == "/mix/5/playback/1"
    assert tags == "fi"
    assert abs(args[0] - -3.0) < 1e-6
    assert args[1] == -100


def test_decode_string_argument(session_mod):
    message = session_mod.encode_osc("/x", "s", "hello")
    assert session_mod.decode_osc(message) == ("/x", "s", ("hello",))


def test_decode_no_arguments(session_mod):
    assert session_mod.decode_osc(session_mod.encode_osc("/refresh")) == \
        ("/refresh", "", ())


def test_decode_rejects_malformed_input(session_mod):
    with pytest.raises(ValueError):
        session_mod.decode_osc(b"no-nul-terminator")
    with pytest.raises(ValueError):
        session_mod.decode_osc(b"/x\x00\x00garbage-tags\x00")


def test_decode_truncated_arguments_raise_value_error(session_mod):
    # A valid ',f' tag with only two argument bytes must raise ValueError,
    # not leak struct.error past the documented contract.
    with pytest.raises(ValueError):
        session_mod.decode_osc(b"/x\x00\x00,f\x00\x00" + b"\x00\x00")


def test_bundle_unwrapping(session_mod):
    inner = [
        session_mod.encode_osc("/output/5/stereo", "i", 1),
        session_mod.encode_osc("/mix/5/playback/1", "fi", 0.0, 0),
    ]
    bundle = b"#bundle\x00" + b"\x00" * 8
    for message in inner:
        bundle += struct.pack(">i", len(message)) + message
    assert list(session_mod.iter_osc_messages(bundle)) == inner


def test_plain_datagram_yields_itself(session_mod):
    message = session_mod.encode_osc("/refresh")
    assert list(session_mod.iter_osc_messages(message)) == [message]


def test_deeply_nested_bundles_do_not_exhaust_the_python_stack(session_mod):
    from conftest import osc_bundle

    message = session_mod.encode_osc("/output/1/volume", "f", -30.0)
    nested = message
    for _ in range(1500):
        nested = osc_bundle([nested])
    assert len(nested) < 65507, "the packet fits in a real UDP datagram"
    assert list(session_mod.iter_osc_messages(
        osc_bundle([nested, message]))) == [message, message]


def test_truncated_bundle_stops_cleanly(session_mod):
    message = session_mod.encode_osc("/x", "i", 1)
    bundle = (b"#bundle\x00" + b"\x00" * 8
              + struct.pack(">i", len(message) + 100) + message)
    assert list(session_mod.iter_osc_messages(bundle)) == []
