"""Numeric contracts of the pinned backend, without transport or I/O.

An OSC float is a representation, not a unit. Scalar setfixed nodes
round after float32 division; input gain truncates float32 multiplication.
Their signed 16-bit reports are distinct from the logarithmic mix gain.
"""

from __future__ import annotations

import math
import struct
from typing import Optional

from .constants import LEVEL_MIN
from .registers import BOOL, ENUM, NUMBER, Register


def finite(value: object) -> float:
    """A real numeric argument, never a coerced OSC string or NaN."""
    if not isinstance(value, (int, float)):
        raise TypeError("expected a numeric value")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("number is not finite") from None
    if not math.isfinite(number):
        raise ValueError("number must be finite")
    return number


def integer(value: object) -> int:
    """An exact OSC integer; no truncation of malformed float reports."""
    number = finite(value)
    if not number.is_integer() or not -(2 ** 31) <= number < 2 ** 31:
        raise ValueError("expected an integral OSC Int32 value")
    return int(number)


def float32(value: object) -> float:
    """An encodable finite OSC float, returned in its wire precision."""
    number = finite(value)
    try:
        result: float = struct.unpack("!f", struct.pack("!f", number))[0]
    except (OverflowError, struct.error):
        raise ValueError("number is outside OSC Float32") from None
    if not math.isfinite(result):
        raise ValueError("number is outside OSC Float32")
    return result


def raw_value(value: object, register: Register) -> int:
    """The signed scalar register after the backend's numeric conversion.

    The backend masks to 16 bits and sign-extends on read. Reject overflow
    instead of permitting a wraparound that changes the requested value.
    """
    if register.scale is None:
        raw = integer(value)
    else:
        wire = float32(value)
        if register.truncates:
            raw = math.trunc(float32(wire * 10.0))
        else:
            quotient = float32(wire / float32(register.scale))
            # C lroundf: ties away from zero, not Python's ties to even.
            raw = (math.floor(quotient + .5) if quotient >= 0
                   else math.ceil(quotient - .5))
    if not -32768 <= raw <= 32767:
        raise ValueError("number is outside the backend's signed 16-bit register")
    return raw


def expected_report(value: object, register: Register) -> float:
    """The Float32 value newfixed/newinputgain would report."""
    raw = raw_value(value, register)
    if register.truncates:
        return float32(raw / 10.0)
    return float32(raw * float32(register.scale or 1.0))


def number_value(value: object, register: Register, *,
                 reported: bool = False) -> float:
    """Validate domain bounds separately from wire/backend representation."""
    number = finite(value)
    if register.tags.startswith("i") or register.scale is not None:
        raw_value(number, register)
    else:
        float32(number)
    for bound, lower in ((register.lo, True), (register.hi, False)):
        if bound is None:
            continue
        limit = bound
        if reported and register.tags.startswith("f"):
            limit = float32(bound)
            if register.scale is not None:
                # newfixed multiplies by a Float32 scale. At e.g. Q=9.9
                # that lands one ULP beyond float32(9.9), still the same
                # legal raw step. A domain check must admit its own report.
                encoded_limit = expected_report(bound, register)
                limit = (min(limit, encoded_limit) if lower
                         else max(limit, encoded_limit))
        if (number < limit if lower else number > limit):
            raise ValueError("%s out of range %s..%s%s" % (
                format(number, ".9g"), register.lo, register.hi,
                (" " + register.unit) if register.unit else ""))
    return number


def equal_float(want: object, got: object, register: Optional[Register],
                tolerance: Optional[float] = None) -> bool:
    """Compare a float argument according to its actual parameter contract."""
    wanted = finite(want)
    if register is not None and register.mix_level:
        if wanted <= LEVEL_MIN:
            return got == -math.inf
        # Measured logarithmic mix-gain tolerance. It applies to this
        # argument only, never to pan, a threshold, a ratio or a delay.
        return abs(wanted - finite(got)) <= .5
    reported = finite(got)
    if register is not None and register.domain == NUMBER:
        number_value(wanted, register)
        number_value(reported, register, reported=True)
        if register.scale is not None:
            expected = expected_report(wanted, register)
            return math.isclose(reported, expected, rel_tol=2e-7,
                                abs_tol=register.scale * 1e-4)
    if tolerance is not None:
        return abs(wanted - reported) <= tolerance
    # Unknown parameters get Float32 equality, not a guessed physical unit.
    return float32(wanted) == float32(reported)


def report_value(value: object, register: Register) -> float:
    """A valid scalar observation; it need not have the desired value."""
    if register.domain == NUMBER:
        return number_value(value, register, reported=True)
    result = integer(value)
    if register.domain == BOOL and result not in (0, 1):
        raise ValueError("invalid boolean report")
    if register.domain == ENUM:
        wire = register.values or tuple(range(len(register.choices)))
        if result not in wire:
            raise ValueError("unknown enum report")
    return result


def render_number(value: object, register: Optional[Register]) -> str:
    """Preserve a reported quantum without displaying Float32 noise."""
    number = finite(value)
    if register is not None:
        number_value(number, register, reported=True)
        if register.tags.startswith("i"):
            return str(integer(number))
        if register.scale is not None:
            places = max(0, -math.floor(math.log10(register.scale)))
            return format(number, ".%df" % places)
    return format(number, ".9g")
