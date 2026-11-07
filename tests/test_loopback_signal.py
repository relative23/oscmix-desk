"""The loopback probe's signal analysis, on synthetic recordings (scripts/loopback_signal.py)."""

import math
import random
import struct
import sys

import pytest
from support import repo_file

sys.path.insert(0, str(repo_file("scripts")))

import loopback_signal as signal


def recording(columns, frames):
    """An interleaved s32 recording with the given 0-based channels set, the rest silent."""
    data = bytearray()
    for i in range(frames):
        row = [0] * signal.CHANNELS
        for index, samples in columns.items():
            row[index] = int(samples[i] * 2 ** 31)
        data += struct.pack("<%di" % signal.CHANNELS, *row)
    return bytes(data)


def noise(count, seed, scale=0.01):
    generator = random.Random(seed)
    return [generator.gauss(0, scale) for _ in range(count)]


@pytest.mark.parametrize("lag", [0, 1, 37, 480])
def test_a_delayed_copy_is_carried_at_its_lag(lag):
    source = noise(24000, 1)
    target = [0.0] * lag + [0.5 * x for x in source[:len(source) - lag]]
    found = signal.analyse(recording({0: source, 4: target}, 24000), 1, 5)
    assert found["verdict"] == "carried"
    assert found["lag_samples"] == lag
    # Normalised over the whole window: only the overlapping part correlates.
    assert found["peak"] > 0.99 * (1 - lag / found["samples"])
    assert abs(found["lag_ms"] - lag * 1000 / signal.RATE) < 0.01


def test_independent_noise_is_not_carried():
    found = signal.analyse(recording({0: noise(24000, 1), 5: noise(24000, 2)}, 24000), 1, 6)
    assert found["verdict"] == "not-carried"
    assert abs(found["peak"]) < signal.ABSENT


def test_digital_silence_is_not_carried_and_has_no_level():
    found = signal.analyse(recording({0: noise(24000, 1)}, 24000), 1, 9)
    assert found["verdict"] == "not-carried"
    assert found["peak"] == 0.0
    assert found["target_dbfs"] is None


def test_a_half_correlated_mix_decides_nothing():
    source, other = noise(24000, 1), noise(24000, 2)
    target = [0.3 * a + b for a, b in zip(source, other, strict=True)]
    found = signal.analyse(recording({0: source, 2: target}, 24000), 1, 3)
    assert found["verdict"] == "undetermined"


def test_an_inverted_copy_still_counts_and_keeps_its_sign():
    source = noise(24000, 3)
    found = signal.analyse(recording({0: source, 1: [-x for x in source]}, 24000), 1, 2)
    assert found["verdict"] == "carried"
    assert found["peak"] < -0.99


def test_a_subsample_lag_is_refined_between_samples():
    # A two-tap average delays white noise by half a sample.
    source = noise(24000, 4)
    target = [0.0] + [(source[i] + source[i - 1]) / 2 for i in range(1, len(source))]
    lag, peak, refined = signal.correlate(signal.middle(source, 8192),
                                          signal.middle(target, 8192), 100)
    assert peak > 0.6
    assert 0.3 < refined < 0.7
    assert lag in (0, 1)


def test_the_level_is_the_rms_in_dbfs():
    assert signal.level_dbfs([0.5, -0.5]) == pytest.approx(20 * math.log10(0.5))
    assert signal.level_dbfs([0.0, 0.0]) == -math.inf
    with pytest.raises(ValueError, match="no samples"):
        signal.level_dbfs([])


def test_a_short_recording_is_refused():
    with pytest.raises(ValueError, match="needed"):
        signal.analyse(recording({0: noise(100, 1)}, 100), 1, 2)


@pytest.mark.parametrize(("source", "target", "lag"), [
    ([1.0], [1.0, 2.0], 0), ([], [], 0), ([1.0, 2.0], [1.0, 2.0], 2)])
def test_correlate_refuses_unequal_or_out_of_range_input(source, target, lag):
    with pytest.raises(ValueError, match=r"length|max_lag"):
        signal.correlate(source, target, lag)


def test_the_fft_needs_a_power_of_two():
    with pytest.raises(ValueError, match="power of two"):
        signal._fft([0j] * 3)
    values = [complex(x) for x in range(8)]
    back = signal._fft(signal._fft(values), invert=True)
    assert all(abs(a - b) < 1e-9 for a, b in zip(values, back, strict=True))


def test_the_delay_slope_fits_the_points():
    slope, intercept = signal.delay_slope([(0.0, 1.5), (0.01, 2.5), (0.05, 6.5)])
    assert slope == pytest.approx(100.0)
    assert intercept == pytest.approx(1.5)
    with pytest.raises(ValueError, match="two points"):
        signal.delay_slope([(0.0, 1.0)])
    with pytest.raises(ValueError, match="differ"):
        signal.delay_slope([(0.01, 1.0), (0.01, 2.0)])


@pytest.mark.parametrize(("peak", "verdict"), [
    (0.5, "carried"), (-0.9, "carried"), (0.1, "not-carried"), (0.0, "not-carried"),
    (0.3, "undetermined"), (-0.2, "undetermined")])
def test_the_verdict_thresholds(peak, verdict):
    assert signal.verdict(peak) == verdict
