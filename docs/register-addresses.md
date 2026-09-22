# Register addresses, measured

The register table in `devices.py` carries oscmix's OSC **paths**. The
device knows only **register addresses**, and every defect this project
has found in the last two releases lived in the gap between the two:
Room EQ folded onto itself, `/output/N/phase` never written at all, a
`,f` sent to a register that reads integers.

This file closes that gap for the families where it has cost something.
It is not a copy of upstream's source: the arithmetic is read from
`device_ffucxii.c`, and then **each address is confirmed on the wire**
by writing the register its own current value and reading the SysEx off
the MIDI pipe. Writing back what is already there emits the write and
changes nothing, so the desk is untouched by the measurement.

## The arithmetic

    channel families   idx << 6 | reg
                       idx = input index, or 20 + output index
    matrix pan         0x2000 | out << 6 | in
    matrix level       0x4000 | out << 6 | idx
                       idx = input index, or 20 + playback index
    room EQ, reported  0x35D0 + reg + (out << 5)
    room EQ, written   0x3400 + reg + (out << 5)   (since f2fdd5e; #33)

## The wire format

`setreg()` in oscmix.c:

    regval = (reg & 0x7fff) << 16 | val
    bit 31 = even parity over the whole word

then `base128enc` packs the four little-endian bytes into five septets.

## Confirmed on a UCX II at 55802a6

Written on 2026-08-24, traced on the pipe `alsaseqio` forwards (fd 7).

| path | address | |
|---|---|---|
| `/input/3/phase` | `0x0087` | confirmed |
| `/input/3/gain` | `0x0088` | confirmed |
| `/output/5/volume` | `0x0600` | confirmed |
| `/output/5/stereo` | `0x0604` | confirmed |
| `/output/5/crossfeed` | `0x060A` | confirmed |
| `/output/5/lowcut/freq` | `0x060D` | confirmed |
| `/output/5/eq/band1gain` | `0x0611` | confirmed |
| `/output/5/dynamics/gain` | `0x061C` | confirmed |
| `/output/5/autolevel/maxgain` | `0x0624` | confirmed |
| `/mix/5/input/1` | `0x2100` | confirmed |
| `/output/5/roomeq/band1gain` | `0x3653` | confirmed |

All eleven appeared verbatim. `/output/1/eq/band1gain` at `0x0511` and
`/input/1/phase` at `0x0007` were confirmed separately, in the runs that
produced upstream issues #33 and #34.

## Confirmed at f2fdd5e: the Room EQ write range

Upstream #33's resolution split the family's arithmetic: the device
*reports* Room EQ from the `0x35D0` block and *takes writes* at
`0x3400` -- the same per-band offsets, `0x1D0` lower. Traced on the
pipe on 2026-08-28, at the pin this repository now builds:

| path | write address | |
|---|---|---|
| `/output/1/roomeq/band1gain` | `0x3403` | confirmed, and the value reads back |
| `/output/5/roomeq/band1gain` | `0x3483` | confirmed |

Nothing else in `ctltoreg` moved between `55802a6` and `f2fdd5e`; the
gain-range fix (#35) and the phase-guard fix (this project's #36) change
flags and bounds, not addresses.

## The reading that was wrong first, and why it is worth keeping

Room EQ first decoded as `0x6653` against an arithmetic of `0x3653`, and
the polling register appeared as both `0x3F00` and `0x6F00`. A constant
`0x3000` offset on two unrelated high addresses and on none of the nine
below `0x3000` was the clue that the decoder was at fault rather than
the device, and it was, twice over:

- `\v` in the trace is the byte `0x0B`. The unpacker did not know that
  escape and fell through to the letter, `0x76`, which is why the
  five septets reconstructed to 35 bits where four bytes can only make
  32.
- the register was taken as `word >> 16` without `& 0x7fff`, so bit 31
  leaked in. Bit 31 is `setreg`'s **parity** bit, not part of the
  address.

Both fixed, all eleven match. The lesson is the one this project keeps
relearning: an instrument that has not been checked against something
known is not a measurement. The tell was that the error was *constant*,
which noise never is.

## How many addresses are enough

The roadmap asked this and did not answer it. The answer is that
**2028 addresses are not 2028 facts**: they are the three rules above
over a table of 82 control offsets. Verifying two thousand registers was
never the task.

- **The rules are the part that could be misunderstood, and all three
  are confirmed** by the eleven addresses above: nine exercise the
  channel rule, one the matrix, one Room EQ.
- **The offsets are a table**, and the risk in a table is not
  misunderstanding but availability. Extracted from upstream on demand,
  they are not independent of upstream, which was the entire point.

So "enough" meant: store the table. It is
[`register-offsets.json`](register-offsets.json), 82 entries with the
revision they came from, and `tests/test_register_addresses.py` requires
it to reproduce every address measured on the wire -- from the file in
this repository, not from upstream's source.

The numbers are Michael Forney's under ISC, like the code quoted under
`patches/`, and the file says so.

## What this is for

If oscmix stopped being maintained tomorrow, the OSC paths would be
worth nothing and this table would still address the hardware. That is
the point of writing it down.

**What it still does not give you.** oscmix also carries the SysEx
transport, the ALSA discovery and the register-to-control mapping in the
*read* direction. The transport is documented above; the read direction
is not. A table of addresses is the notes, not the instrument.
