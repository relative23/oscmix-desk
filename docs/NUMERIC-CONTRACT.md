# Numeric values and what verification establishes

The config parser, planner, verifier and write-sweep use the parameter's
numeric contract. An OSC `f` identifies a representation, not a unit or
an error tolerance. Unknown parameters use Float32 equality.

At the pinned oscmix revision
[`f2fdd5ec78338848754aad32cc07f3440de63395`](https://github.com/michaelforney/oscmix/blob/f2fdd5ec78338848754aad32cc07f3440de63395/oscmix.c),
`setfixed` divides the OSC Float32 value by the node's Float32 scale and
uses C `lroundf` (ties away from zero). `newfixed` reports the signed raw
value multiplied by that scale. Input gain instead truncates `value * 10`
on write and divides the integer by ten on read. Scalar storage is signed
16-bit; sending a larger value would wrap it. These conversions are
modelled explicitly, including intermediate Float32 rounding.

| Parameter | OSC scale per scalar step | Comparison |
| --- | --- | --- |
| Output volume, EQ gain/Q, dynamics floats, input gain | 0.1 | Expected backend report; gain uses truncation |
| Echo/reverb width, reverb room scale | 0.01 | Expected backend report |
| Echo delay, Room EQ delay | 0.001 | Expected backend report; see duration limit below |
| Integer settings, bools, enums | Integral wire values | Exact; invalid bool/enum reports are rejected |
| Input-mix level | Logarithmic gain | Existing measured 0.5 dB tolerance; pan remains an exact integer |

Finite values and representable wire/storage values are required even
where upstream has no physical bounds. Fractional integer settings,
NaN, infinity and overflow are config errors before a write. The `-inf`
mute report is accepted only for a mix-level argument at the mute floor,
never for a delay, ratio, threshold or scalar output fader.

For an unlinked stereo pair, -65 dB bypasses compensation and writes
digital zero. A silent input pair reports balance zero; that balance is
irrelevant once digital mute is established. Nonzero signals still need
the requested pan. Unlinked-pair levels above 0 dB are refused because
compensation has already consumed the available boost.

A matching register report proves the requested backend state was
reported. It does not prove physical gain, delay, frequency response or
delivery through a particular connector. Those need signal measurements.
The unchanged backend pin and firmware remain part of that evidence.

## Physical units and RME limits

The [UCX II manual, version 1.6, February 2026](https://rme-audio.de/downloads/fface_ucx2_e.pdf),
page 62, specifies Room EQ delay up to 42 ms, with 0.01 ms increments.
The pinned backend instead declares raw limits 0–425 and OSC scale 0.001.
Those declarations do **not** establish seconds at the physical output.
Existing config numbers retain their backend meaning and range 0–0.425;
they are labelled **OSC units**. Do not convert a desired physical delay
using the old seconds label. An independently measured mapping is still
required before physical units or a wider range can be offered.

The same manual says Room EQ's enable button controls EQ; nonzero delay
and volume calibration remain active independently. Its UCX II chapter
limits Room EQ to 16 mono/8 stereo channels. Register-address coverage
does not prove that every DSP block can run simultaneously. At higher
rates, physical ADAT capacity and USB alternate settings also differ;
see the [rate qualification plan](plans/high-sample-rates.md).

## Export and evidence

`--dump-config` keeps the precision of each scalar step, including
remembered comments. It omits malformed scalar reports and matrix states
that the route syntax cannot reproduce, naming those omissions. Stereo
routes require known, consistent input/output links and matching pan and
level conditions. No recovered routes is not proof of no monitoring.

Uncommenting a remembered value declares an initial setting. It does not
change its reconciliation policy. `[pin]` overrides cover flat input and
output options only. Playback routing remains unreadable: merge an
export into the existing desk rather than replacing it wholesale.

`--snapshot` preserves the reported float digits. In 0.7.0 and earlier,
its one-decimal formatting could hide smaller differences. Historical
snapshot equality therefore cannot establish absence of sub-tenth drift.

Sweep schema 2 records requested, encoded and reported values, the
comparison rule, original/final state, running binary identity and any
failure. Original/final messages retain all arguments and their OSC type
tags, including mix pan and enum labels. Source provenance includes every
runtime module used to judge the reports; a change during measurement
invalidates the pass. SIGINT/SIGTERM requests end probing and allow bounded
restoration to finish. SIGKILL, power loss and device loss cannot be made
restorable by this process.

Restoration shares the probe permission checks: protected,
read-only, invalid or no-longer-identifiable state is left explicitly
unrestored. Historical sweep counts used the old tolerance and lack raw
reports for confirmations; keep those artifacts as historical results,
not as evidence under the corrected comparison.
