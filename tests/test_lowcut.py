"""Low cut: 120 registers, a switch and two options on 40 channels.

Fourth nested family, and the first whose bounds did not all come from
upstream. `freq` has `.min=20 .max=500`; `slope` has nothing at all, so
its range was measured at the device instead -- see below.

Everything generic is in `test_sub_families.py`. This file is the two
things only this family knows.
"""

import pytest

from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.registers import NUMBER, register_policy, settable_nested


def test_freq_carries_upstreams_bounds():
    """`{"freq", LOWCUT_FREQ, .set=setint, .new=newint, .min=20, .max=500}`
    -- no `.scale`, so the raw bounds are already the config's."""
    freq = settable_nested(UCX2, "lowcut", "input")["freq"]
    assert (freq.lo, freq.hi) == (20.0, 500.0)
    assert freq.unit == "Hz"
    assert freq.tags == "i"


def test_slope_carries_bounds_the_device_gave_not_upstream():
    """Upstream declares none for `slope`. Written and read back on
    `/output/5/lowcut/slope`: 0, 1, 2, 3 return as written; 4, 7 and -1
    all return **3**. The device clamps, and four positions is the count
    RME's low cut has.

    This is the one pair of bounds in the model that came from the
    hardware rather than from the node table, which is why it is asserted
    with the measurement written next to it.
    """
    slope = settable_nested(UCX2, "lowcut", "output")["slope"]
    assert (slope.lo, slope.hi) == (0.0, 3.0)
    assert slope.tags == "i"
    assert slope.domain == NUMBER


def test_slope_is_an_index_and_says_so():
    """Not "dB/oct". The device holds 0 and 1 where a dB/oct reading
    would hold 6 and 12, and which index means which steepness was never
    measured -- so `slope = 1` must not read as one decibel per octave.

    Not an ENUM either: upstream takes it with `setint` and declares no
    names, so a config writing "12 dB/oct" would send a string that
    `oscgetint` drops without a word.
    """
    slope = settable_nested(UCX2, "lowcut", "output")["slope"]
    assert slope.unit == "index"
    assert slope.choices == ()


def test_the_options_are_the_two_upstream_declares():
    assert set(settable_nested(UCX2, "lowcut", "input")) == {
        "enabled", "freq", "slope"}


def test_lowcut_is_remembered_rather_than_pinned():
    assert register_policy(UCX2, "/input/3/lowcut/freq") == "remember"
    assert register_policy(UCX2, "/input/3/lowcut") == "remember"


@pytest.mark.parametrize(("line", "why"), [
    ("freq = 10", "below upstream's 20"),
    ("freq = 800", "above upstream's 500"),
    ("slope = 4", "above the 3 the device clamps to"),
    ("slope = -1", "below 0, and the device returns 3 for it"),
])
def test_a_value_outside_the_bounds_is_refused(tmp_path, line, why):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n"
                    "[lowcut:output:5]\n%s\n" % line)
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert "out of range" in str(raised.value), why
