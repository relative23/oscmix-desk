"""Performance: growth order, not wall-clock.

Nothing here was known to be slow, which is not the same as knowing it is
fast. But a hard millisecond budget on a shared runner mostly measures
that runner, and this project's failures are timing bugs already -- a
gate that flakes under load would be a new source of exactly the thing it
is meant to guard against.

So these assert the property that survives a busy machine: **how the cost
grows**. Doubling the input must not quadruple the time. A slow afternoon
moves both measurements together and changes nothing; an accidental
quadratic shows up whatever the load.

The one absolute bound left is deliberately absurd (seconds where the
measurement is milliseconds). It catches a hang, not a slowdown.
"""

import os
import time

import oracle
import pytest

ABSURD_SECONDS = 10.0        # a hang detector, not a budget


def timed(work, repeats=5):
    """Best of N: the fastest run is the least contaminated by scheduling."""
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


def assert_scales_linearly(small, large, factor, tolerance=3.0):
    """The cost of ``factor`` times the work must not blow up.

    Generous by design: constant overheads dominate at small sizes, so a
    linear routine routinely measures better than ``factor``. What this
    rejects is quadratic, which at factor 10 would show ~100x.
    """
    if small <= 0:
        return
    growth = large / small
    if growth >= factor * tolerance and _host_is_overloaded():
        # A timing ratio on an overloaded host is noise, not a verdict.
        # Measured 2026-09-05: the bundle-walking test read 39.6x for 10x
        # the work with a load average of 36 on 8 cores, from unrelated
        # suites running beside it, and 9.3x on the next quiet run. CI
        # runners are never in this state, so the gate stays strict
        # there; only a host that could not have produced a meaningful
        # number skips.
        pytest.skip("host load %.0f on %d cores: the timing ratio is noise"
                    % (os.getloadavg()[0], os.cpu_count() or 1))
    assert growth < factor * tolerance, (
        "%.1fx the work took %.1fx the time (linear would be ~%dx)"
        % (factor, growth, factor))


def _host_is_overloaded():
    try:
        return os.getloadavg()[0] > 2 * (os.cpu_count() or 1)
    except OSError:
        return False


def test_encoding_routes_scales_linearly(session_mod):
    def encode(count):
        routes = [session_mod.Route(name="r%d" % n, playback=(1, 2),
                                    output=(1, 2), level=0.0)
                  for n in range(count)]

        def work():
            for route in routes:
                for path, types, args in oracle.route_messages(route):
                    session_mod.encode_osc(path, types, *args)
        return timed(work)

    assert_scales_linearly(encode(50), encode(500), factor=10)


def test_decoding_a_dump_scales_linearly(session_mod):
    # A real /refresh dump is several thousand registers. Anything worse
    # than linear here turns verification into a timeout.
    def decode(count):
        messages = [session_mod.encode_osc("/input/%d/gain" % (n % 20 + 1),
                                           "f", 0.0)
                    for n in range(count)]

        def work():
            for message in messages:
                session_mod.decode_osc(message)
        return timed(work)

    assert_scales_linearly(decode(200), decode(2000), factor=10)


def test_bundle_walking_scales_linearly(session_mod):
    # iter_osc_messages walks a bundle by length prefixes. Re-scanning the
    # buffer per message would be quadratic and invisible until a real
    # dump arrives.
    import struct

    def walk(count):
        body = b"".join(
            struct.pack(">i", len(m)) + m
            for m in (session_mod.encode_osc("/output/%d/level" % (n % 20 + 1),
                                             "f", -20.0)
                      for n in range(count)))
        datagram = b"#bundle\x00" + b"\x00" * 8 + body
        return timed(lambda: list(session_mod.iter_osc_messages(datagram)))

    assert_scales_linearly(walk(200), walk(2000), factor=10)


def test_config_parsing_scales_linearly(session_mod, tmp_path):
    def parse(count):
        path = tmp_path / ("routing%d.conf" % count)
        path.write_text("".join(
            "[route:r%d]\nplayback = 1/2\noutput = %d/%d\nlevel = 0.0\n\n"
            % (n, (n % 10) * 2 + 1, (n % 10) * 2 + 2)
            for n in range(count)))
        assert len(session_mod.load_config(path).routes) == count
        return timed(lambda: session_mod.load_config(path))

    assert_scales_linearly(parse(40), parse(400), factor=10)


def test_expected_registers_scales_linearly(session_mod):
    def build(count):
        routes = [session_mod.Route(name="r%d" % n, playback=(1, 2),
                                    output=(1, 2), level=0.0)
                  for n in range(count)]
        return timed(lambda: session_mod.expected_registers(session_mod.Config(routes=routes)))

    assert_scales_linearly(build(50), build(500), factor=10)


@pytest.mark.parametrize("count", [1, 2000])
def test_nothing_hangs_on_a_large_dump(session_mod, count):
    # The absolute bound: not a budget, a hang detector.
    messages = [session_mod.encode_osc("/input/1/gain", "f", 0.0)] * count
    started = time.perf_counter()
    for message in messages:
        session_mod.decode_osc(message)
    assert time.perf_counter() - started < ABSURD_SECONDS
