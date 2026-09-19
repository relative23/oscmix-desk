"""The address arithmetic, checked against what was measured.

`docs/register-addresses.md` records eleven addresses read off the MIDI
pipe on a UCX II. This asserts that the arithmetic documented there
reproduces them from the model's own paths, so the two cannot drift
apart silently: the table is evidence, and this is what keeps the code
agreeing with it.

Why it matters is in the doc. Every defect of the last two releases
lived between the OSC path and the register address, and the paths are
oscmix's invention while the addresses are the device's.
"""

import re

import pytest
from support import repo_file

#: The offsets upstream's `ctltoreg` assigns, for the controls measured.
#: Transcribed from `device_ffucxii.c`, confirmed on the wire, and
#: re-checked at f2fdd5e (only Room EQ's write base changed); the doc
#: names the runs.
CONTROL_OFFSET = {
    "phase": 0x07,          # input
    "gain": 0x08,           # input
    "volume": 0x00,         # output
    "stereo": 0x04,         # output
    "crossfeed": 0x0A,      # output
    "lowcut/freq": 0x0D,
    "eq/band1gain": 0x11,
    "dynamics/gain": 0x1C,
    "autolevel/maxgain": 0x24,
}


def channel_address(family, channel, option):
    """`idx << 6 | reg`, with outputs starting at index 20."""
    idx = (channel - 1) if family == "input" else 20 + (channel - 1)
    return idx << 6 | CONTROL_OFFSET[option]


def matrix_address(out, in_):
    return 0x2000 | (out - 1) << 6 | (in_ - 1)


MEASURED = {
    ("input", 3, "phase"): 0x0087,
    ("input", 3, "gain"): 0x0088,
    ("output", 5, "volume"): 0x0600,
    ("output", 5, "stereo"): 0x0604,
    ("output", 5, "crossfeed"): 0x060A,
    ("output", 5, "lowcut/freq"): 0x060D,
    ("output", 5, "eq/band1gain"): 0x0611,
    ("output", 5, "dynamics/gain"): 0x061C,
    ("output", 5, "autolevel/maxgain"): 0x0624,
    # From the runs that produced upstream #33 and #34.
    ("input", 1, "phase"): 0x0007,
    ("output", 1, "eq/band1gain"): 0x0511,
}

#: Room EQ has its own block and its own arithmetic, so it is not in the
#: table above; `test_the_room_eq_address_follows_its_own_arithmetic`
#: covers it.
ROOMEQ_BAND1GAIN_OUTPUT_5 = 0x3653


@pytest.mark.parametrize(("key", "address"), sorted(MEASURED.items()),
                         ids=lambda k: k if isinstance(k, int) else
                         "%s%d/%s" % k if isinstance(k, tuple) else str(k))
def test_the_arithmetic_reproduces_a_measured_address(key, address):
    family, channel, option = key
    assert channel_address(family, channel, option) == address


def test_the_matrix_address_is_reproduced():
    assert matrix_address(5, 1) == 0x2100


def test_the_document_lists_every_address_this_asserts():
    """The doc is the evidence and this is the check; a claim in one and
    not the other is how a measured table turns back into folklore."""
    text = repo_file("docs", "register-addresses.md").read_text()
    listed = {int(m, 16) for m in re.findall(r"`0x([0-9A-F]{4})`", text)}
    assert ROOMEQ_BAND1GAIN_OUTPUT_5 in listed
    for address in MEASURED.values():
        assert address in listed, "0x%04X asserted here, absent from the doc" % address


def test_the_room_eq_address_follows_its_own_arithmetic():
    """`0x35D0 + reg + (out << 5)`, confirmed on the wire once the trace
    decoder stopped mangling `\\v` and stopped taking the parity bit for
    part of the address."""
    assert 0x35D3 + (4 << 5) == 0x3653


def test_the_matrix_level_address_is_a_different_block_from_pan():
    """`MIX` is pan at 0x2000, `MIX_LEVEL` is level at 0x4000, and a
    single `/mix/<out>/input/<in>` write emits both. Reading them as one
    register is how a matrix write would look half-applied."""
    assert (0x2000 | (5 - 1) << 6 | (1 - 1)) == 0x2100
    assert (0x4000 | (5 - 1) << 6 | (1 - 1)) == 0x4100


# --------------------------------------------------------------------------
# The stored offset table, which is what "independent of oscmix" means.
# --------------------------------------------------------------------------

def offsets():
    import json
    return json.loads(repo_file("docs", "register-offsets.json").read_text())


#: Which control each measured address belongs to. The addresses are not
#: 2028 independent facts: they are three rules over 82 offsets, so this
#: is what has to be storable rather than a list of two thousand numbers.
CONTROL_OF = {
    ("input", 3, "phase"): "INPUT_PHASE",
    ("input", 3, "gain"): "INPUT_GAIN",
    ("output", 5, "volume"): "OUTPUT_VOLUME",
    ("output", 5, "stereo"): "OUTPUT_STEREO",
    ("output", 5, "crossfeed"): "OUTPUT_CROSSFEED",
    ("output", 5, "lowcut/freq"): "LOWCUT_FREQ",
    ("output", 5, "eq/band1gain"): "EQ_BAND1GAIN",
    ("output", 5, "dynamics/gain"): "DYNAMICS_GAIN",
    ("output", 5, "autolevel/maxgain"): "AUTOLEVEL_MAXGAIN",
    ("input", 1, "phase"): "INPUT_PHASE",
    ("output", 1, "eq/band1gain"): "EQ_BAND1GAIN",
}


def address_from_table(family, channel, control):
    """Apply the stored rule for a control to a channel."""
    entry = offsets()["offsets"][control]
    if entry["rule"] == "channel":
        idx = (channel - 1) if family == "input" else 20 + (channel - 1)
        return idx << 6 | entry["offset"]
    if entry["rule"] == "roomeq":
        return entry["offset"] + ((channel - 1) << 5)
    raise AssertionError("unknown rule %r" % entry["rule"])


@pytest.mark.parametrize(("key", "address"), sorted(MEASURED.items()),
                         ids=lambda k: k if isinstance(k, int) else
                         "%s%d/%s" % k if isinstance(k, tuple) else str(k))
def test_the_stored_table_reproduces_every_measured_address(key, address):
    """The point of storing it, and the only way to know it is usable.

    If oscmix stopped being maintained, the OSC paths would be worth
    nothing and this table plus the three rules would still address the
    hardware. That claim is only worth making if the table in this
    repository -- not the one extracted from upstream on demand --
    produces the addresses that were measured on the wire.
    """
    family, channel, _option = key
    assert address_from_table(family, channel, CONTROL_OF[key]) == address


def test_the_room_eq_rule_reproduces_its_measured_address():
    assert address_from_table("output", 5, "ROOMEQ_BAND1GAIN") == 0x3653


def test_the_room_eq_write_range_sits_0x1d0_below_the_read_range():
    """f2fdd5e (upstream #33): the UCX II reports Room EQ from 0x35D0
    and takes writes at 0x3400. Measured on the wire at the new pin:
    /output/1/roomeq/band1gain emits 0x3403 and /output/5 emits 0x3483,
    both exactly 0x1D0 below the addresses the device reports from."""
    read5 = address_from_table("output", 5, "ROOMEQ_BAND1GAIN")
    read1 = address_from_table("output", 1, "ROOMEQ_BAND1GAIN")
    assert read5 - 0x1D0 == 0x3483
    assert read1 - 0x1D0 == 0x3403


def test_the_table_says_where_it_came_from():
    """A table of numbers with no provenance is folklore. It also carries
    upstream's attribution: the numbers are Michael Forney's under ISC,
    the same as the code quoted under patches/."""
    data = offsets()
    assert data["revision"] == "f2fdd5ec78338848754aad32cc07f3440de63395"
    assert "device_ffucxii.c" in data["source"]
    assert any("ISC" in line for line in data["_"])


def test_the_table_covers_the_families_that_have_cost_something():
    """Every family that has produced a defect must be addressable from
    here: EQ, Room EQ, dynamics, low cut, auto level, crossfeed, and the
    phase that is never written."""
    stored = offsets()["offsets"]
    for control in ("EQ_BAND1GAIN", "ROOMEQ_BAND1GAIN", "DYNAMICS_GAIN",
                    "LOWCUT_FREQ", "AUTOLEVEL_MAXGAIN", "OUTPUT_CROSSFEED",
                    "OUTPUT_PHASE", "INPUT_48V"):
        assert control in stored, control
