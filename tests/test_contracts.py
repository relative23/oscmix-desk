"""The invariants this project holds itself to, checked as properties.

The tests elsewhere pin down behaviour for chosen inputs. These state the
rules that must hold for *every* input, and hypothesis goes looking for
the counterexample. Two of them exist because of specific incidents:

* the OSC decoder reads datagrams off a socket but was only ever tested
  against the encoder in the same file -- it had never seen a hostile byte
* `a route writes exactly the registers it declares` is the rule the
  `volume` bug taught us, and it was nowhere expressed as a rule
"""

import math
import os
import struct

import oracle
import pytest

from oscmix_desk import constants, osc, reconcile

# A missing dev dependency should say so, not abort collection for the
# whole suite: `make test` on a fresh checkout is a reasonable thing to
# try before reading requirements-dev.txt.
#
# But a silent skip means "all passed" while nothing in this file ran,
# which is a worse lie than a collection error. Two things prevent that:
# OSCMIX_REQUIRE_CONTRACTS=1 turns the skip into a hard failure (CI sets
# it, so the gate is mechanical there), and conftest prints a banner at
# the end of any run in which this module was skipped.
try:
    import hypothesis
except ImportError as exc:  # pragma: no cover -- depends on the environment
    if os.environ.get("OSCMIX_REQUIRE_CONTRACTS") == "1":
        raise RuntimeError(
            "OSCMIX_REQUIRE_CONTRACTS=1 but hypothesis is not installed -- "
            "the contract tests would have been skipped silently. "
            "pip install -r requirements-dev.txt"
        ) from exc
    pytest.skip("property tests need hypothesis "
                "(pip install -r requirements-dev.txt)",
                allow_module_level=True)

given, settings = hypothesis.given, hypothesis.settings
st = hypothesis.strategies

# OSC paths as oscmix uses them: ASCII, no NUL, no comma-leading weirdness.
osc_paths = st.builds(
    lambda parts: "/" + "/".join(parts),
    st.lists(st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126,
                                            blacklist_characters="\0"),
                     min_size=1, max_size=8),
             min_size=1, max_size=4),
)
osc_ints = st.integers(min_value=-(2 ** 31), max_value=2 ** 31 - 1)
osc_floats = st.floats(allow_nan=False, allow_infinity=False,
                       min_value=-1e6, max_value=1e6, width=32)
osc_strings = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126),
                      max_size=16)


@st.composite
def osc_messages(draw):
    """A path plus matching type tags and arguments."""
    tags = draw(st.lists(st.sampled_from("ifs"), max_size=5))
    args = []
    for tag in tags:
        if tag == "i":
            args.append(draw(osc_ints))
        elif tag == "f":
            args.append(draw(osc_floats))
        else:
            args.append(draw(osc_strings))
    return draw(osc_paths), "".join(tags), tuple(args)


def as_float32(value):
    """What a float becomes once it has been through the wire format."""
    return struct.unpack(">f", struct.pack(">f", value))[0]


@given(message=osc_messages())
def test_encoding_then_decoding_returns_the_message(session_mod, message):
    path, tags, args = message
    decoded_path, decoded_tags, decoded_args = osc.decode_osc(
        osc.encode_osc(path, tags, *args))
    assert decoded_path == path
    assert decoded_tags == tags
    assert len(decoded_args) == len(args)
    for tag, sent, got in zip(tags, args, decoded_args):
        if tag == "f":
            assert got == pytest.approx(as_float32(sent), rel=1e-6, abs=1e-9)
        else:
            assert got == sent


@given(message=osc_messages())
def test_an_encoded_message_is_always_four_byte_aligned(session_mod, message):
    # OSC 1.0 requires it, and oscmix's parser assumes it. A message that
    # is not aligned desynchronises everything after it in a bundle.
    path, tags, args = message
    assert len(osc.encode_osc(path, tags, *args)) % 4 == 0


@settings(max_examples=400)
@given(data=st.binary(max_size=256))
def test_decoding_hostile_bytes_fails_cleanly(session_mod, data):
    # The decoder reads from a UDP socket: anything can arrive. It may
    # reject, it may parse, but it must not raise something the callers do
    # not catch -- they only ever guard ValueError and struct.error.
    try:
        path, tags, args = osc.decode_osc(data)
    except (ValueError, struct.error):
        return
    assert isinstance(path, str)
    assert isinstance(tags, str)
    assert isinstance(args, tuple)
    assert len(tags) == len(args)


@settings(max_examples=400)
@given(data=st.binary(max_size=256))
def test_iterating_hostile_datagrams_never_raises(session_mod, data):
    # iter_osc_messages splits bundles by a length prefix taken straight
    # from the datagram; a hostile length must not escape as an exception
    # or spin forever.
    for message in osc.iter_osc_messages(data):
        assert isinstance(message, bytes)


@given(level=st.floats(min_value=-65.0, max_value=6.0,
                       allow_nan=False, allow_infinity=False),
       stereo=st.booleans(),
       volume=st.one_of(st.none(), st.floats(min_value=-65.0, max_value=6.0,
                                             allow_nan=False,
                                             allow_infinity=False)))
def test_a_route_writes_only_what_it_declares(session_mod, level, stereo,
                                              volume):
    # The rule the `volume` bug taught us: an undeclared register belongs
    # to the user. A route with no `volume` must never touch a fader.
    route = session_mod.Route(name="r", playback=(1, 2), output=(5, 6),
                              level=level, volume=volume, stereo=stereo)
    written = {path for path, _t, _a in oracle.route_messages(route)}
    declared = {"/playback/1/stereo", "/output/5/stereo",
                "/mix/5/playback/1", "/mix/6/playback/1"}
    if volume is not None:
        declared |= {"/output/5/volume", "/output/6/volume"}
    assert written <= declared
    if volume is None:
        assert not any("volume" in path for path in written)


@given(level=st.floats(min_value=-65.0, max_value=6.0,
                       allow_nan=False, allow_infinity=False),
       stereo=st.booleans())
def test_route_messages_is_exactly_its_two_phases(session_mod, level, stereo):
    # expected_registers() and the verification build on this identity;
    # if it ever stops holding, both silently verify the wrong set.
    route = session_mod.Route(name="r", playback=(1, 2), output=(5, 6),
                              level=level, stereo=stereo)
    assert oracle.route_messages(route) == (
        reconcile.link_messages(route) + reconcile.mix_messages(route))


@given(level=st.floats(min_value=-65.0, max_value=6.0,
                       allow_nan=False, allow_infinity=False),
       stereo=st.booleans())
def test_links_always_precede_the_mix_matrix(session_mod, level, stereo):
    # The ordering the device requires, as a property rather than as three
    # example-based tests. Linked or unlinked, the link comes first.
    route = session_mod.Route(name="r", playback=(1, 2), output=(5, 6),
                              level=level, stereo=stereo)
    paths = [path for path, _t, _a in oracle.route_messages(route)]
    first_mix = next(i for i, p in enumerate(paths) if p.startswith("/mix/"))
    assert all(not p.endswith("/stereo") for p in paths[first_mix:])


@given(level=st.floats(min_value=-65.0, max_value=0.0,
                       allow_nan=False, allow_infinity=False))
def test_level_means_the_same_gain_linked_or_not(session_mod, level):
    # oscmix halves the gain on the unlinked path, so the request carries
    # a compensating offset. Measured on a UCX II as an exact 6 dB
    # difference before the compensation existed.
    def mix_level(stereo):
        route = session_mod.Route(name="r", playback=(1, 2), output=(5, 6),
                                  level=level, stereo=stereo)
        return {p: a for p, _t, a in
                reconcile.mix_messages(route)}["/mix/5/playback/1"][0]

    linked, unlinked = mix_level(True), mix_level(False)
    # 20*log10(2) of headroom, which oscmix halves back to `level`.
    if level <= -65:
        assert linked == unlinked == -65
    else:
        assert unlinked - linked == pytest.approx(20.0 * math.log10(2.0), abs=1e-6)


@settings(max_examples=300)
@given(text=st.text(max_size=400))
def test_config_parsing_is_total(session_mod, tmp_path_factory, text):
    # Any file the user hands us either parses or produces a ConfigError
    # naming what is wrong. A traceback from routing.conf would exit with
    # the wrong code and put systemd into a restart loop.
    path = tmp_path_factory.mktemp("cfg") / "routing.conf"
    path.write_text(text)
    try:
        config = session_mod.load_config(path)
    except session_mod.ConfigError as exc:
        message = str(exc)
        assert message, "ConfigError must say what is wrong"
        return
    assert isinstance(config, session_mod.Config)
    for route in config.routes:
        assert len(route.playback) == len(route.output)
        assert constants.LEVEL_MIN <= route.level <= constants.LEVEL_MAX


@given(port=st.integers())
def test_out_of_range_ports_are_always_rejected(session_mod,
                                                tmp_path_factory, port):
    path = tmp_path_factory.mktemp("cfg") / "routing.conf"
    path.write_text("[osc]\nport = %d\n" % port)
    if 1 <= port <= 65535:
        assert session_mod.load_config(path).osc_port == port
    else:
        with pytest.raises(session_mod.ConfigError):
            session_mod.load_config(path)


@st.composite
def corrupted_messages(draw):
    """A valid encoding with bytes flipped, truncated or appended.

    Pure random bytes almost always die on the first string read, so they
    never reach the argument decoder. Starting from something well-formed
    puts the damage where the parsing actually happens.
    """
    from oscmix_desk import osc as osc_mod

    path, tags, args = draw(osc_messages())
    data = bytearray(osc_mod.encode_osc(path, tags, *args))
    if data:
        for _ in range(draw(st.integers(min_value=0, max_value=3))):
            index = draw(st.integers(min_value=0, max_value=len(data) - 1))
            data[index] = draw(st.integers(min_value=0, max_value=255))
    cut = draw(st.integers(min_value=0, max_value=len(data)))
    data = data[:cut] + bytes(draw(st.binary(max_size=8)))
    return bytes(data)


@settings(max_examples=600)
@given(data=corrupted_messages())
def test_decoding_corrupted_messages_fails_cleanly(session_mod, data):
    try:
        path, tags, args = osc.decode_osc(data)
    except (ValueError, struct.error):
        return
    assert isinstance(path, str)
    assert len(tags) == len(args)


@settings(max_examples=400)
@given(sizes=st.lists(st.integers(min_value=-(2 ** 31), max_value=2 ** 31 - 1),
                      max_size=4),
       payload=st.binary(max_size=64))
def test_bundles_with_hostile_sizes_terminate(session_mod, sizes, payload):
    # iter_osc_messages walks a bundle by a length prefix taken straight
    # from the datagram. A negative or oversized length must end the walk,
    # not loop or read past the buffer.
    datagram = b"#bundle\x00" + b"\x00" * 8
    for size in sizes:
        datagram += struct.pack(">i", size) + payload
    assert isinstance(list(osc.iter_osc_messages(datagram)), list)
