"""Auto level: 160 registers, three options and a switch on 40 channels.

Third nested family, and the cheapest so far -- the generic checks live
in `test_sub_families.py` and this file is only what upstream alone
knows: the bounds, and what they mean after `.scale`.

Small enough to state plainly what it is. Auto level is a per-channel
automatic gain control: it lifts a quiet signal toward `headroom` below
full scale, by at most `maxgain`, over `risetime`. That makes `maxgain`
the one worth measuring at the device, because a scale applied the wrong
way would declare 0..180 dB of automatic gain.
"""

import pytest

from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.registers import NUMBER, register_policy, settable_nested

#: upstream `autoleveltree`, oscmix.c at 55802a6. Raw `.min`/`.max`;
#: `setfixed` divides the OSC value by `.scale` on the way in, so a
#: config sees min*scale .. max*scale.
UPSTREAM = {
    #  option:      (scale, raw min, raw max, unit)
    "maxgain":      (0.1,     0,     180,    "dB"),
    "headroom":     (0.1,    30,     120,    "dB"),
    "risetime":     (0.1,     1,      99,    "s"),
}


@pytest.mark.parametrize("option", sorted(UPSTREAM))
def test_the_bounds_are_upstreams_after_the_scale(option):
    scale, raw_lo, raw_hi, unit = UPSTREAM[option]
    register = settable_nested(UCX2, "autolevel", "input")[option]
    assert register.lo == pytest.approx(raw_lo * scale)
    assert register.hi == pytest.approx(raw_hi * scale)
    assert register.unit == unit
    assert register.domain == NUMBER


def test_the_options_are_the_three_upstream_declares():
    """No more and no less. `/X/{ch}/autolevel/meter` is reported and has
    no `.set`, so it is not one of them."""
    options = settable_nested(UCX2, "autolevel", "output")
    assert set(options) == set(UPSTREAM) | {"enabled"}


def test_maxgain_is_eighteen_decibels_not_a_hundred_and_eighty():
    """The failure a factor of ten hides. Measured at the device rather
    than argued -- see the roadmap entry for auto level."""
    maxgain = settable_nested(UCX2, "autolevel", "input")["maxgain"]
    assert (maxgain.lo, maxgain.hi) == (0.0, 18.0)


def test_autolevel_is_remembered_rather_than_pinned():
    """ADR 0003's default: something a person reaches for during a
    session, not installation state."""
    assert register_policy(UCX2, "/input/3/autolevel/maxgain") == "remember"
    assert register_policy(UCX2, "/input/3/autolevel") == "remember"


@pytest.mark.parametrize(("line", "why"), [
    ("maxgain = 20.0", "above upstream's 18.0"),
    ("headroom = 1.0", "below upstream's 3.0"),
    ("risetime = 12.0", "above upstream's 9.9"),
])
def test_a_value_outside_the_bounds_is_refused(tmp_path, line, why):
    """Which is the only refusal there is: oscmix reads .min and .max
    nowhere at the pinned revision."""
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n"
                    "[autolevel:input:3]\n%s\n" % line)
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert "out of range" in str(raised.value), why
