"""Signal analysis for the output-loopback probe: no I/O apart from reading raw recordings.

The probe feeds an input's own noise to an output through a readable mix cell and records all
capture channels. With loopback on, capture channel N carries output N, so it correlates with
the source input's capture channel at a small lag; with loopback off it carries input N, which
does not. The lag of the correlation peak is the output path's latency, so a change of the
output's Room EQ delay shows up as a change of that lag. Pure Python (no numpy): a radix-2 FFT
over a few thousand samples is fast enough for the handful of recordings a probe takes.
"""

from __future__ import annotations

import cmath
import math
import struct
from typing import List, Optional, Sequence, Tuple

RATE = 48000
CHANNELS = 20
#: Signed 32-bit little-endian frames, as `pw-record --format=s32` writes them.
SAMPLE = struct.Struct("<i")
FULL_SCALE = float(2 ** 31)
#: A peak this high says the two channels carry the same signal.
PRESENT = 0.5
#: A peak this low says they do not; between the two the recording decides nothing.
ABSENT = 0.1


def channel(raw: bytes, index: int, *, channels: int = CHANNELS) -> List[float]:
    """One channel (0-based) of an interleaved s32 recording, scaled to -1..1."""
    frame = SAMPLE.size * channels
    count = len(raw) // frame
    return [SAMPLE.unpack_from(raw, i * frame + index * SAMPLE.size)[0] / FULL_SCALE
            for i in range(count)]


def middle(samples: Sequence[float], length: int) -> List[float]:
    """``length`` samples from the middle, away from the start and stop of a recording."""
    if len(samples) < length:
        raise ValueError("recording holds %d samples, %d needed" % (len(samples), length))
    start = (len(samples) - length) // 2
    return list(samples[start:start + length])


def level_dbfs(samples: Sequence[float]) -> float:
    """RMS level in dBFS; -inf for digital silence."""
    if not samples:
        raise ValueError("no samples")
    power = sum(x * x for x in samples) / len(samples)
    return 10 * math.log10(power) if power > 0 else -math.inf


def _fft(values: List[complex], invert: bool = False) -> List[complex]:
    n = len(values)
    if n & (n - 1):
        raise ValueError("FFT length must be a power of two")
    a = list(values)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    sign = 1 if invert else -1
    while size <= n:
        step = cmath.exp(sign * 2j * math.pi / size)
        half = size // 2
        twiddles = [step ** k for k in range(half)]
        for start in range(0, n, size):
            for k in range(half):
                u = a[start + k]
                v = a[start + k + half] * twiddles[k]
                a[start + k] = u + v
                a[start + k + half] = u - v
        size *= 2
    if invert:
        a = [x / n for x in a]
    return a


def correlate(source: Sequence[float], target: Sequence[float],
              max_lag: int) -> Tuple[int, float, float]:
    """(lag, peak, refined lag) of the normalised cross-correlation, lag in samples.

    A positive lag means ``target`` follows ``source``. ``peak`` is the correlation
    coefficient at that lag (-1..1, sign kept); the refined lag adds a parabolic
    sub-sample estimate. Only lags within ``max_lag`` count.
    """
    n = len(source)
    if n != len(target) or n == 0:
        raise ValueError("source and target need the same, nonzero length")
    if not 0 <= max_lag < n:
        raise ValueError("max_lag must be below the segment length")
    energy = math.sqrt(sum(x * x for x in source) * sum(y * y for y in target))
    if energy == 0:
        return 0, 0.0, 0.0
    size = 1
    while size < 2 * n:
        size *= 2
    a = _fft([complex(x) for x in source] + [0j] * (size - n))
    b = _fft([complex(y) for y in target] + [0j] * (size - n))
    product = _fft([x.conjugate() * y for x, y in zip(a, b)], invert=True)
    values = {lag: product[lag % size].real / energy for lag in range(-max_lag, max_lag + 1)}
    lag = max(values, key=lambda k: abs(values[k]))
    refined = float(lag)
    if -max_lag < lag < max_lag:
        left, centre, right = values[lag - 1], values[lag], values[lag + 1]
        denominator = left - 2 * centre + right
        if denominator:
            refined += 0.5 * (left - right) / denominator
    return lag, values[lag], refined


def verdict(peak: float) -> str:
    """What one correlation peak says about loopback: carried, not carried, or nothing."""
    magnitude = abs(peak)
    if magnitude >= PRESENT:
        return "carried"
    if magnitude <= ABSENT:
        return "not-carried"
    return "undetermined"


def analyse(raw: bytes, source: int, target: int, *, length: int = 8192,
            max_lag: int = 2400) -> dict:
    """Correlate capture channel ``target`` against ``source`` (both 1-based) in one recording."""
    left = middle(channel(raw, source - 1), length)
    right = middle(channel(raw, target - 1), length)
    lag, peak, refined = correlate(left, right, max_lag)
    return dict(source=source, target=target, samples=length,
                source_dbfs=_rounded(level_dbfs(left)), target_dbfs=_rounded(level_dbfs(right)),
                lag_samples=lag, lag_ms=round(refined * 1000 / RATE, 4), peak=round(peak, 4),
                verdict=verdict(peak))


def _rounded(value: float) -> Optional[float]:
    return None if math.isinf(value) else round(value, 2)


def delay_slope(points: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """Least-squares (slope, intercept) of lag in ms against the written OSC delay."""
    if len(points) < 2:
        raise ValueError("a slope needs two points")
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread == 0:
        raise ValueError("the delays written must differ")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    return slope, mean_y - slope * mean_x
