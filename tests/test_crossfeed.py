"""Crossfeed: 20 registers, one option, outputs only.

The last per-channel family of 0.4.0 and the only **flat** one -- it is
`/output/{ch}/crossfeed`, so it belongs in `[output:5]` and not in a
section of its own. That makes it the first flat channel option added
since 0.3.0, and the reason three existing tests had to move: the
per-option policy table, the list of valid options in an error message,
and a test that had been using "crossfeed" as an example of a name this
version does *not* know.

Upstream declares no bounds for it, so like `lowcut/slope` they came
from the device.

Audible, and measured that way. A left-only tone into the phones pair,
crossfeed 0 to 5 on outputs 7 and 8: output 8 goes from silent (-144.0
dBFS) to -8.0 dB below the direct channel, monotonically, while output 7
gives up 3.4 dB along the way. The baseline is the part that matters --
with the setting off, the tone reads -58.7 dBFS on 7 and -144.0 on 8,
three times running, and the mirror case is exact. An earlier attempt
read signal on output 8 with crossfeed off and wandered 2 dB between
identical runs; the mixer GUI was holding the receive port, so the level
reader saw half the stream.
"""

import pytest

from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.registers import (
    NUMBER,
    REMEMBER,
    register_policy,
    settable_options,
)
from oscmix_desk.verify import register_promptly_reported


def test_it_is_a_flat_output_option_not_a_sub_family():
    """`[output:5]\\ncrossfeed = 3`, never `[crossfeed:output:5]`."""
    assert "crossfeed" in settable_options(UCX2, "output")
    assert "crossfeed" not in settable_options(UCX2, "input")


def test_the_bounds_came_from_the_device_not_upstream():
    """`{"crossfeed", OUTPUT_CROSSFEED, .set=setint, .new=newint}` -- no
    `.min`, no `.max`, no `.scale`. Written and read back on
    `/output/7/crossfeed`: 0 through 5 return as written, and 6, 10, 99
    and -1 all return **5**. Six positions, which is Off plus the five
    TotalMix offers.
    """
    crossfeed = settable_options(UCX2, "output")["crossfeed"]
    assert (crossfeed.lo, crossfeed.hi) == (0.0, 5.0)
    assert crossfeed.tags == "i"
    assert crossfeed.domain == NUMBER


def test_it_is_an_index_and_says_so():
    """0 is off and the rest are increasing amounts, but what each step
    does was not measured -- so the unit says `index` and claims no
    scale, the same call as `lowcut/slope`."""
    assert settable_options(UCX2, "output")["crossfeed"].unit == "index"
    assert settable_options(UCX2, "output")["crossfeed"].choices == ()


def test_it_is_remembered_rather_than_pinned():
    """A headphone listening preference, not installation state."""
    assert register_policy(UCX2, "/output/7/crossfeed") == REMEMBER


def test_a_cold_plug_does_not_report_it_for_every_channel():
    """6 of 20 in `tests/data/cold-plug-timeline.json`, so absence after
    a hotplug is expected rather than a lost datagram. Being flat, it
    reaches this rule through `settable_options` -- the path that was
    right all along for flat options and wrong for nested ones."""
    assert not register_promptly_reported("/output/7/crossfeed", UCX2)


@pytest.mark.parametrize(("value", "why"), [
    ("6", "above the 5 the device clamps to"),
    ("-1", "below 0, and the device returns 5 for it"),
    ("99", "well outside, returns 5"),
])
def test_a_value_outside_the_bounds_is_refused(tmp_path, value, why):
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n"
                    "[output:7]\ncrossfeed = %s\n" % value)
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert "out of range" in str(raised.value), why


def test_a_value_inside_the_bounds_is_written(tmp_path):
    from oscmix_desk import reconcile

    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n"
                    "[output:7]\ncrossfeed = 3\n")
    entries = {e.path: e for e in reconcile.desired(load_config(path))}
    assert entries["/output/7/crossfeed"].tags == "i"
    assert entries["/output/7/crossfeed"].args == (3,)
