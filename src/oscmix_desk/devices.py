"""The devices this project knows: their channels and their registers.

The table that ``registers`` describes the shape of. ``48v`` on inputs
1-2 and ``hi-z`` on 3-4 are UCX II facts, not Fireface facts, so every
row belongs to a device, and the untested 802 is listed with its channel
map and no rows of its own.

**Derived from a recording, not from memory.** Every channel range below
was read out of ``tests/data/refresh-dump.json``, and
``tests/test_registers.py`` checks the table against that recording and
against ``tests/data/cold-plug-timeline.json``. A claim here that the
device does not support is a failing test, not a surprise on someone
else's desk.

Data, and one question: which of these devices a config names.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .constants import LEVEL_MAX, LEVEL_MIN
from .registers import (
    BOOL,
    ENUM,
    GLOBAL,
    NUMBER,
    PIN,
    REESTABLISHED,
    VERIFIABLE,
    WRITE_ONLY,
    Device,
    Register,
)

# --------------------------------------------------------------------------
# The register table itself, exempt from mutation. ADR 0015.
#
# Everything from here to the end of the device literals is the table:
# the channel-map helper, the two loops that expand a row table into
# rows, and the device literals themselves. It is all built at import
# time, which is what mutmut cannot attribute to a covering test -- it
# runs a subset that does not contain the test which would kill the
# mutant, and reports a survivor that the full suite kills. Verified by
# hand for `_eq_registers` (three mutants) and for `_seq` (dropping the
# `+ 1` fails test_an_option_the_channel_does_not_have_is_refused).
#
# What checks it instead is `tests/data/refresh-dump.json`, which fixes
# every path and every type tag against what the device reports -- a
# stricter statement than a surviving mutant. ADR 0015 has the numbers.
# --------------------------------------------------------------------------

# pragma: no mutate start

def _seq(first: int, last: int) -> Tuple[int, ...]:
    return tuple(range(first, last + 1))


# --------------------------------------------------------------------------
# Fireface UCX II.
#
# Every range below was read out of a recorded /refresh dump against the
# pinned oscmix revision, not typed from the manual. Two of them would
# have been wrong if guessed:
#
#   * the level meters run to 22 while every control register stops at
#     20 -- so a single "channel count" for the device is already wrong;
#   * `/mix/<out>/input/<in>` appeared only on odd channels in the
#     recording. That is *link state*, not a capability: linked pairs
#     fold onto the odd channel. It is deliberately not modelled here.
# --------------------------------------------------------------------------

#: The three-band EQ, per channel, as upstream's `eqtree` declares it.
#:
#: Built from a table rather than written out twenty-four times, and the
#: reason is one row: `band1type` offers a **Low** Shelf and `band3type`
#: a **High** Shelf. The rest of each band is identical, which is exactly
#: the condition under which copy-paste gets the odd one out wrong.
#:
#: Bounds are upstream's: freq `20..20000` Hz, gain `.scale=0.1,
#: .min=-200, .max=200` (-20..+20 dB), q `.scale=0.1, .min=4, .max=99`
#: (0.4..9.9).
_EQ_BANDS = (
    (1, ("Peak", "Low Shelf", "High Pass", "Low Pass")),
    (2, None),
    (3, ("Peak", "High Shelf", "Low Pass", "High Pass")),
)


#: Room EQ, outputs only: nine bands where EQ has three, plus a delay.
#: Filter types sit on bands 1, 8 and 9 -- band 1 offers a **Low** shelf
#: and the last two a **High** shelf, the same odd-one-out that made the
#: EQ table worth generating rather than typing.
#:
#: 320 of these were unreachable until the pin moved: `device_ffucxii.c`
#: folded the upper half of each output's block onto its own lower half
#: (michaelforney/oscmix#32, fixed in 55802a6). Declared here only after
#: the fix was measured -- 640 reported where 320 were before.
_HIGH_SHELF = ("Peak", "High Shelf", "Low Pass", "High Pass")

_ROOMEQ_BANDS = (
    (1, ("Peak", "Low Shelf", "High Pass", "Low Pass")),
    (2, None), (3, None), (4, None), (5, None), (6, None), (7, None),
    (8, _HIGH_SHELF),
    (9, _HIGH_SHELF),
)


def _band_registers(family: str, sub: str,
                    bands: Tuple[Tuple[int, Optional[Tuple[str, ...]]], ...],
                    ) -> Tuple["Register", ...]:
    """A parametric-EQ sub-family: its switch, then freq/gain/q per band.

    Shared by `eq` and `roomeq`, which differ only in how many bands
    they have and which of them offer a filter type. The bounds are the
    same in upstream's two trees -- freq `20..20000`, gain
    `.scale=0.1 .min=-200 .max=200`, q `.scale=0.1 .min=4 .max=99` --
    and that is checked against both, not assumed from one.
    """
    prefix = "/%s/{ch}/%s" % (family, sub)
    rows = [Register(prefix, "i", VERIFIABLE, family, BOOL)]
    for band, types in bands:
        rows.append(Register("%s/band%dfreq" % (prefix, band), "i",
                             VERIFIABLE, family, NUMBER,
                             lo=20.0, hi=20000.0, unit="Hz"))
        rows.append(Register("%s/band%dgain" % (prefix, band), "f",
                             VERIFIABLE, family, NUMBER,
                             lo=-20.0, hi=20.0, unit="dB"))
        rows.append(Register("%s/band%dq" % (prefix, band), "f",
                             VERIFIABLE, family, NUMBER, lo=0.4, hi=9.9))
        if types is not None:
            rows.append(Register("%s/band%dtype" % (prefix, band), "is",
                                 VERIFIABLE, family, ENUM, types))
    return tuple(rows)


def _roomeq_registers() -> Tuple["Register", ...]:
    """Room EQ: 640 registers, modelled, readable, **and settable**.

    That last word changed with the pin. Until `f2fdd5e` every row here
    was declared with no value domain, because writes did not reach the
    device: oscmix sent them to the same `0x35D0` block it reads the
    family from, and the UCX II takes Room EQ *writes* at `0x3400` --
    a split range nobody had measured (upstream #33). Fixed upstream on
    2026-08-27 and measured here the same night at `f2fdd5e`:

        /output/1/roomeq/band1gain  -6.0  ->  setreg 3403, reads back -6.0

    where it had always read 0.0 before. So the family now carries the
    same domains as the channel EQ (checked against both upstream
    trees), plus `delay` with upstream's own bounds: `.min=0 .max=425
    .scale=0.001`, i.e. 0 to 0.425 s on the OSC side. `settable_nested`
    answers for it, and `[roomeq:output:N]` sections load.
    """
    rows = list(_band_registers("output", "roomeq", _ROOMEQ_BANDS))
    rows.append(Register("/output/{ch}/roomeq/delay", "f", VERIFIABLE,
                         "output", NUMBER, lo=0.0, hi=0.425, unit="s"))
    return tuple(rows)



#: Sub-family option tables, as (name, tags, lo, hi, unit) in upstream's
#: own order. Bounds are upstream's `.min`/`.max` *after* `.scale`:
#: `setfixed` divides the OSC value by the scale on the way in, so a node
#: with min=-300 max=300 scale=0.1 is -30.0..30.0 to a config. Getting
#: that backwards would declare every range ten times too wide, and
#: upstream enforces none of them -- see `sections._parse_number`.
_DYNAMICS_OPTIONS = (
    ("gain", "f", -30.0, 30.0, "dB"),
    ("attack", "i", 0.0, 200.0, "ms"),
    ("release", "i", 100.0, 999.0, "ms"),
    ("compthres", "f", -60.0, 0.0, "dB"),
    ("compratio", "f", 1.0, 10.0, ":1"),
    ("expthres", "f", -99.0, 20.0, "dB"),
    ("expratio", "f", 1.0, 10.0, ":1"),
)

#: `slope` carries no `.min`/`.max` upstream, so its bounds are the one
#: pair here that came from the device rather than from the node table.
#: Written and read back on `/output/5/lowcut/slope`: 0, 1, 2 and 3 come
#: back as written, and 4, 7 and -1 all come back as **3** -- the device
#: clamps. Four positions, which is the count RME's low cut has
#: (6/12/18/24 dB/oct).
#:
#: The unit is "index", which is what the value is. The device holds 0
#: and 1 where a dB/oct reading would hold 6 and 12, and which index
#: means which steepness was not measured -- declaring "dB/oct" would
#: make `slope = 1` read as one decibel per octave. Not an ENUM either:
#: upstream takes it with `setint` and declares no names, so a config
#: writing a name would send a string `oscgetint` drops.
_LOWCUT_OPTIONS = (
    ("freq", "i", 20.0, 500.0, "Hz"),
    ("slope", "i", 0.0, 3.0, "index"),
)

_AUTOLEVEL_OPTIONS = (
    ("maxgain", "f", 0.0, 18.0, "dB"),
    ("headroom", "f", 3.0, 12.0, "dB"),
    ("risetime", "f", 0.1, 9.9, "s"),
)


def _sub_registers(family: str, sub: str,
                   options: Tuple[Tuple[str, str, Optional[float],
                                        Optional[float], str], ...]
                   ) -> Tuple["Register", ...]:
    """One sub-family's rows: its own switch, then its options.

    The switch carries a value as well as a subtree (`/input/3/dynamics`
    is a bool), which is the shape ADR 0014 spells `enabled`.

    `.../meter` is deliberately absent from every table here. It is
    streamed and has no `.set` upstream, so it is not a setting -- the
    model declares no meters, and the recording shows one arriving only
    for whichever channels happened to be moving.
    """
    prefix = "/%s/{ch}/%s" % (family, sub)
    rows = [Register(prefix, "i", VERIFIABLE, family, BOOL)]
    for name, tags, lo, hi, unit in options:
        rows.append(Register("%s/%s" % (prefix, name), tags, VERIFIABLE,
                             family, NUMBER, lo=lo, hi=hi, unit=unit))
    return tuple(rows)


UCX2 = Device(
    key="ucx2",
    name="Fireface UCX II",
    usb_id="2a39:3fd9",
    channels={
        "input": _seq(1, 20),
        "output": _seq(1, 20),
        "playback": _seq(1, 20),
        # Meters exist for two more than the control registers do.
        "meter": _seq(1, 22),
        "48v": (1, 2),
        "hi-z": (3, 4),
        # Reported on all eight. What each channel *accepts* is
        # narrower, and splits three ways -- see the three gain rows.
        "input-gain": _seq(1, 8),
        "input-gain-mic": (1, 2),
        "input-gain-inst": (3, 4),
        "input-gain-line": _seq(5, 8),
        "input-reflevel": _seq(3, 8),
        "output-reflevel": _seq(1, 8),
    },
    registers=(
        # --- what 0.2.0 already writes ---------------------------------
        Register("/playback/{ch}/stereo", "i", VERIFIABLE, "playback",
                 policy=PIN),
        Register("/output/{ch}/stereo", "i", VERIFIABLE, "output",
                 policy=PIN),
        Register("/output/{ch}/volume", "f", VERIFIABLE, "output", NUMBER,
                 lo=LEVEL_MIN, hi=LEVEL_MAX, unit="dB"),
        # The playback matrix: a /mix write draws no reply and the dump
        # omits it entirely. Re-established from a known link state.
        Register("/mix/{out}/playback/{pb}", "fi", REESTABLISHED, "output",
                 policy=PIN),

        # --- the surface 0.3.0 declares --------------------------------
        # Reported, so almost all of the new surface is verifiable --
        # unlike the playback matrix this project started with.
        Register("/mix/{out}/input/{in_}", "fi", VERIFIABLE, "output",
                 policy=PIN),
        # 48v deliberately has NO domain: it is readable by the code and not
        # settable from a routing.conf. See registers.settable_options and
        # the roadmap's rule -- phantom power is not exposed until a
        # hardware case proves the channel it names is the channel it
        # hits, because an off-by-one is damaged equipment, not silence.
        Register("/input/{ch}/48v", "i", VERIFIABLE, "48v", policy=PIN),
        Register("/input/{ch}/hi-z", "i", VERIFIABLE, "hi-z", BOOL,
                 policy=PIN),
        # Gain is one register to the protocol and three to the device.
        # Upstream's channel table carries the ranges, and `setinputgain`
        # clamps to them silently: `.gain={0, 750}` on the two mic
        # preamps and `{0, 240}` on the two instrument channels. Analog
        # 5-8 had *no range at all* and were clamped to {0, 0} -- found
        # by the 0.5.0 write sweep, filed as upstream #35, and fixed by
        # the maintainer in fdc47f7 ("Pre Gain", 0-24 dB in the device
        # UI). Measured here at f2fdd5e: /input/5/gain takes 12.0 dB
        # and reads it back.
        Register("/input/{ch}/gain", "f", VERIFIABLE, "input-gain-mic",
                 NUMBER, lo=0.0, hi=75.0, unit="dB", policy=PIN),
        Register("/input/{ch}/gain", "f", VERIFIABLE, "input-gain-inst",
                 NUMBER, lo=0.0, hi=24.0, unit="dB", policy=PIN),
        Register("/input/{ch}/gain", "f", VERIFIABLE, "input-gain-line",
                 NUMBER, lo=0.0, hi=24.0, unit="dB", policy=PIN),
        Register("/input/{ch}/reflevel", "is", VERIFIABLE, "input-reflevel", ENUM,
                 ("+13dBu", "+19dBu"), policy=PIN),
        Register("/input/{ch}/mute", "i", VERIFIABLE, "input", BOOL),
        Register("/input/{ch}/phase", "i", VERIFIABLE, "input", BOOL),
        Register("/input/{ch}/stereo", "i", VERIFIABLE, "input", policy=PIN),
        # Verifiable, but see complete_after_cold_plug below: a cold
        # plug delivers these only for some channels.
        Register("/output/{ch}/mute", "i", VERIFIABLE, "output", BOOL),
        # Settable since the pin moved to f2fdd5e: `ctltoreg` gated
        # OUTPUT_PHASE on an *input* flag no output carries, so the
        # write never left oscmix (upstream #34). Fixed by this
        # project's PR #36, merged upstream as 9dba36f, and measured on
        # every one of the 20 outputs: phase reaches the device and
        # reads back, analog and digital alike.
        Register("/output/{ch}/phase", "i", VERIFIABLE, "output", BOOL),
        Register("/output/{ch}/reflevel", "is", VERIFIABLE, "output-reflevel", ENUM,
                 ("+4dBu", "+13dBu", "+19dBu"), policy=PIN),
        # Crossfeed: the last of 0.4.0's per-channel families and the only
        # *flat* one, so it lives here rather than in a sub-family table.
        # Upstream declares no bounds -- `{"crossfeed", OUTPUT_CROSSFEED,
        # .set=setint, .new=newint}` -- so 0..5 came from the device:
        # written and read back on /output/7/crossfeed, 0 through 5 return
        # as written and 6, 10, 99 and -1 all return 5. Six positions,
        # which is Off plus the five TotalMix offers.
        #
        # `index` for the same reason as `lowcut/slope`: 0 is off and the
        # rest are increasing amounts, but what each step does was not
        # measured, so nothing here claims a scale.
        Register("/output/{ch}/crossfeed", "i", VERIFIABLE, "output", NUMBER,
                 lo=0.0, hi=5.0, unit="index"),

        # --- global: no channel dimension (0.4.0) ----------------------
        # The echo send. Bounds and names are upstream's node table
        # verbatim (oscmix.c, the "echo" tree): delay is `.scale=0.001,
        # .min=0, .max=2000`, volume `.scale=0.1, .min=-650, .max=60`
        # -- which is LEVEL_MIN..LEVEL_MAX, the same range a fader has.
        #
        # `feedback` is `setint` with no min or max upstream, so this
        # declares none either. `width` is `.scale=0.01` with no bounds;
        # 0..1 is what the scale implies and what the device reports,
        # but implied is not declared, so it is left open too.
        #
        # All REMEMBER: an echo send is what somebody dials in while
        # working, not what describes the installation (ADR 0012).
        Register("/echo", "i", VERIFIABLE, GLOBAL, BOOL),
        Register("/echo/type", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("Stereo Echo", "Stereo Cross", "Pong Echo")),
        Register("/echo/delay", "f", VERIFIABLE, GLOBAL, NUMBER,
                 lo=0.0, hi=2.0, unit="s"),
        Register("/echo/feedback", "i", VERIFIABLE, GLOBAL, NUMBER),
        Register("/echo/highcut", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("Off", "16kHz", "12kHz", "8kHz", "4kHz", "2kHz")),
        Register("/echo/volume", "f", VERIFIABLE, GLOBAL, NUMBER,
                 lo=LEVEL_MIN, hi=LEVEL_MAX, unit="dB"),
        # Bounds upstream does not declare, measured here because the
        # device *rejects* rather than clamps: 1.02 leaves the register
        # where it was and reports nothing, so a config that asked for
        # it would be silently ignored. That is the failure this project
        # exists to prevent, and it outweighs the standing rule against
        # inventing a range -- these are not invented. Measured
        # 2026-08-25 by the write sweep and then bracketed: 1.0 and 0.0
        # accepted, 1.02 and -0.01 refused, on both width registers.
        Register("/echo/width", "f", VERIFIABLE, GLOBAL, NUMBER,
                 lo=0.0, hi=1.0),

        # The control room section. `dimreduction` and `recallvolume` are
        # `.scale=0.1, .min=-650, .max=0` -- dB down to the same floor a
        # fader has, but never above unity, which is what makes them a
        # reduction rather than a level.
        #
        # PIN on the three that describe the monitoring setup: how far
        # DIM reduces, what RECALL returns to, and which pair the
        # section controls are all set once for a room. REMEMBER on the
        # three that are buttons somebody presses while working.
        #
        # The eleventh name arrived: the pin moved to 55802a6, and
        # e8151cd (upstream #30) gave `mainout` a "None" option. It is
        # the one enum here whose value is not its position -- "None" is
        # -1 -- which is what `values` exists for.
        Register("/controlroom/mainout", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("1/2", "3/4", "5/6", "7/8", "9/10",
                  "11/12", "13/14", "15/16", "17/18", "19/20", "None"),
                 values=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, -1),
                 policy=PIN),
        Register("/controlroom/dimreduction", "f", VERIFIABLE, GLOBAL, NUMBER,
                 lo=LEVEL_MIN, hi=0.0, unit="dB", policy=PIN),
        Register("/controlroom/recallvolume", "f", VERIFIABLE, GLOBAL, NUMBER,
                 lo=LEVEL_MIN, hi=0.0, unit="dB", policy=PIN),
        Register("/controlroom/dim", "i", VERIFIABLE, GLOBAL, BOOL),
        Register("/controlroom/mainmono", "i", VERIFIABLE, GLOBAL, BOOL),
        Register("/controlroom/muteenable", "i", VERIFIABLE, GLOBAL, BOOL),

        # The reverb send. Upstream declares bounds on *none* of these --
        # every number is `setint` or `setfixed` with no min or max, and
        # `/reverb/volume` in particular has no range at all, unlike
        # `/echo/volume`. Copying the echo's -65..+6 onto it would have
        # looked consistent and rejected values the device accepts.
        #
        # All REMEMBER: a reverb tail is dialled in while working.
        Register("/reverb", "i", VERIFIABLE, GLOBAL, BOOL),
        Register("/reverb/type", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("Small Room", "Medium Room", "Large Room", "Walls",
                  "Shorty", "Attack", "Swagger", "Old School",
                  "Echoistic", "8plus9", "Grand Wide", "Thicker",
                  "Envelope", "Gated", "Space")),
        Register("/reverb/predelay", "i", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/lowcut", "i", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/highcut", "i", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/highdamp", "i", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/attack", "i", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/hold", "i", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/release", "i", VERIFIABLE, GLOBAL, NUMBER),
        # 0 and 100 accepted, -1 and 101 refused. Same measurement.
        Register("/reverb/smooth", "i", VERIFIABLE, GLOBAL, NUMBER,
                 lo=0.0, hi=100.0),
        Register("/reverb/roomscale", "f", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/time", "f", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/volume", "f", VERIFIABLE, GLOBAL, NUMBER),
        Register("/reverb/width", "f", VERIFIABLE, GLOBAL, NUMBER,
                 lo=0.0, hi=1.0),

        # The clock. All PIN: which clock a room runs on, and whether the
        # word clock output is terminated, describe the installation.
        #
        # `samplerate` has **no domain**, and the reason is upstream's
        # own: its node is `{"samplerate", CLOCK_SAMPLERATE,
        # .new=newsamplerate}` -- a reporter with no `.set`. oscmix
        # cannot write it, so neither can a config, and the roadmap's
        # open question "is the rate state or an event" is answered by
        # the node table rather than by argument. Measured separately:
        # the device changes it on its own, pushes the change, and loses
        # no mixer state doing so.
        Register("/clock/source", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("Internal", "Word Clock", "SPDIF", "AES", "Optical"),
                 policy=PIN),
        Register("/clock/samplerate", "i", VERIFIABLE, GLOBAL),
        Register("/clock/wckout", "i", VERIFIABLE, GLOBAL, BOOL, policy=PIN),
        Register("/clock/wcksingle", "i", VERIFIABLE, GLOBAL, BOOL, policy=PIN),
        Register("/clock/wckterm", "i", VERIFIABLE, GLOBAL, BOOL, policy=PIN),

        # The box itself: what it does with its optical port, its front
        # panel, and what it does when no computer is attached. All PIN
        # -- none of it is a preference somebody dials during a session.
        #
        # Three have no domain for the same reason as `samplerate`:
        # `ccmode` is `.new=newbool` with no setter, and `dspload` and
        # `dspvers` come from nameless nodes that only report. A config
        # cannot set what oscmix cannot write.
        Register("/hardware/opticalout", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("ADAT", "SPDIF"), policy=PIN),
        Register("/hardware/spdifout", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("Consumer", "Professional"), policy=PIN),
        Register("/hardware/ccmix", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("TotalMix App", "6ch + phones", "8ch", "20ch"), policy=PIN),
        Register("/hardware/standalonearc", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("Volume", "1s Op", "Normal"), policy=PIN),
        Register("/hardware/standalonemidi", "i", VERIFIABLE, GLOBAL, BOOL,
                 policy=PIN),
        Register("/hardware/lockkeys", "is", VERIFIABLE, GLOBAL, ENUM,
                 ("Off", "Keys", "All"), policy=PIN),
        Register("/hardware/remapkeys", "i", VERIFIABLE, GLOBAL, BOOL,
                 policy=PIN),
        Register("/hardware/ccmode", "i", VERIFIABLE, GLOBAL),
        Register("/hardware/dspload", "i", VERIFIABLE, GLOBAL),
        Register("/hardware/dspvers", "i", VERIFIABLE, GLOBAL),

        # --- the three-band EQ, in and out (0.4.0) ---------------------
        # 480 registers, the largest family in the release, and the
        # first written in a nested section (ADR 0014):
        #
        #     [eq:input:3]
        #     band1freq = 80
        #
        # REMEMBER throughout. An EQ curve is dialled in while listening;
        # a session that put one back would be arguing with whoever set
        # it. A config that wants otherwise says so with [pin].
        *_band_registers("input", "eq", _EQ_BANDS),
        *_band_registers("output", "eq", _EQ_BANDS),
        *_roomeq_registers(),
        *_sub_registers("input", "dynamics", _DYNAMICS_OPTIONS),
        *_sub_registers("output", "dynamics", _DYNAMICS_OPTIONS),
        *_sub_registers("input", "autolevel", _AUTOLEVEL_OPTIONS),
        *_sub_registers("output", "autolevel", _AUTOLEVEL_OPTIONS),
        *_sub_registers("input", "lowcut", _LOWCUT_OPTIONS),
        *_sub_registers("output", "lowcut", _LOWCUT_OPTIONS),

        # --- accepted, never reported ----------------------------------
        Register("/input/{ch}/name", "s", WRITE_ONLY, "input"),
        Register("/output/{ch}/name", "s", WRITE_ONLY, "output"),
        Register("/output/{ch}/loopback", "i", WRITE_ONLY, "output"),
    ),
    supported=True,
    # Measured across a real USB replug: the stereo flags arrive for all
    # 20 channels within ~2.3 s, and nothing else does. /output/N/mute
    # came back for channels 1,2,3,8,9,10 and not for 4-7 or 11-20 --
    # a truncated stream rather than a rule, which is exactly why this
    # is a list of what IS complete rather than a flag on what is not.
    complete_after_cold_plug=("/output/{ch}/stereo", "/playback/{ch}/stereo"),
    evidence="hardware-evidence.json attached to v0.2.0",
)


# The 802, and why it declares no registers.
#
# Not "never tested" any more -- read. `device_ff802.c` exists upstream
# and is compiled in, so the channel map below is derived from it rather
# than guessed: 30 in, 30 out, gain and reference level on the eight
# analog inputs, 48V and hi-Z on the four Mic/Inst channels, reference
# level on the twelve analog and phones outputs. Note what the 802 does
# *not* have where the UCX II does: its Mic/Inst channels carry no gain
# register at all, and its analog inputs carry reference level from
# channel 1 rather than from 3.
#
# The registers stay empty because **oscmix cannot drive this device at
# the pinned revision**, for two independent reasons:
#
#   * `init()` in oscmix.c holds a device list of exactly one entry,
#     `&ffucxii`. An 802 never matches, so it exits with "unsupported
#     device" before anything else happens.
#   * `ff802` declares no `.refresh`, no `.regtoctl` and no `.ctltoreg`.
#     Those are called unguarded in seven places -- `setval` alone has
#     three -- so a device that got past the list would take a NULL call
#     on the first write.
#
# So the upstream table is a stub: channel names and counts, no register
# mapping. Declaring a register model against it would describe writes
# that cannot happen. This is the same shape of blocker as Room EQ, one
# level deeper.
FF802 = Device(
    key="ff802",
    name="Fireface 802",
    usb_id="2a39:3fc0",
    channels={
        "input": _seq(1, 30),
        "output": _seq(1, 30),
        "playback": _seq(1, 30),
        # No meter row: the UCX II's runs two past its control registers
        # and nothing says whether the 802 does the same. An unmeasured
        # guess here would be indistinguishable from a measurement.
        "48v": _seq(9, 12),
        "hi-z": _seq(9, 12),
        "input-gain": _seq(1, 8),
        "input-reflevel": _seq(1, 8),
        "output-reflevel": _seq(1, 12),
    },
    registers=(),
    supported=False,
)

DEVICES: Tuple[Device, ...] = (UCX2, FF802)

# pragma: no mutate end

# --------------------------------------------------------------------------
# Back under mutation from here: everything below queries the table, and
# a wrong answer there is behaviour rather than data.
# --------------------------------------------------------------------------


def device_for_name(name: str) -> Optional[Device]:
    """The device a ``routing.conf`` names, or None if it is not modelled.

    Matching is on the configured device name, which is what the config
    already uses to find the ALSA client. None is a normal answer: an
    unmodelled device must keep working exactly as it did, which is why
    every caller treats it as "no opinion" rather than as an error.
    """
    for device in DEVICES:
        if device.name.lower() == name.strip().lower():
            return device
    return None


def modelled_names() -> str:
    """The devices whose register table has rows, for the warnings."""
    return ", ".join(d.name for d in DEVICES if d.registers)

