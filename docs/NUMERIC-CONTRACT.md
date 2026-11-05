# Numeric values and what verification establishes

The config parser, planner, verifier and write-sweep use the parameter's
numeric contract. An OSC `f` identifies a representation, not a unit or
an error tolerance. Unknown parameters use Float32 equality.

At the pinned oscmix revision
[`89ed835ecf8118a21d1d451be7d71944fa5ec384`](https://github.com/michaelforney/oscmix/blob/89ed835ecf8118a21d1d451be7d71944fa5ec384/oscmix.c),
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
| Echo delay, Room EQ delay | 0.001 (Room EQ: 0.01 ms, measured) | Expected backend report; see the measured unit below |
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

## Physical units and RME limits

The [UCX II manual, version 1.6, February 2026](https://rme-audio.de/downloads/fface_ucx2_e.pdf),
page 62, specifies Room EQ delay up to 42 ms, with 0.01 ms increments.
The pinned backend declares raw limits 0–425 and OSC scale 0.001, so the
backend value runs from 0 to 0.425.

Measured on a UCX II through output loopback (2026-11-05,
[evidence](../tests/data/output-loopback-probes-ucx2.json)): one OSC unit is
10.005 ms of delay at the output, with a 0.008 ms residual over five settings
from 0 to 0.425. One backend step (0.001) is therefore 0.01 ms, the step RME
documents, and the backend's upper limit, 425 steps, is **4.25 ms** -- a tenth
of the 42 ms the device offers. A config states the delay in milliseconds,
`delay = 2.5 ms`, from 0 to 4.25 ms; exports write it that way. A number
without a unit keeps its old meaning, OSC units, so an existing config sets
what it set before. The output path itself adds 8 samples (0.17 ms at
48 kHz) whatever the delay.

Since `89ed835` the backend's output-loopback setter sends `on << 7 | output
index` (0-based); the setter before it sent `0xffffffff` for every output and
both states ([#38](https://github.com/michaelforney/oscmix/issues/38),
[#39](https://github.com/michaelforney/oscmix/pull/39),
[evidence](../tests/data/loopback-output-index-ucx2.json)). The delay was
measured with `scripts/probe-loopback.py`, which needs no cable: an input
pair's own noise reaches the output through a readable mix cell, and with
loopback on, the output's capture channel carries it at the output's delay.

The same manual says Room EQ's enable button controls EQ; nonzero delay
and volume calibration remain active independently. Its UCX II chapter
limits Room EQ to 16 mono/8 stereo channels. Register-address coverage
does not prove that every DSP block can run simultaneously. At higher
rates, physical ADAT capacity and USB alternate settings also differ; see
[the per-mode table](FEATURE-SURFACE.md#hardware-boundary).

## Export and write sweeps

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

Sweep schema 3 records requested, encoded and reported values, the
comparison rule, running binary identity and any failure. Since 0.8.0 each
chunk of up to 32 registers is one backend operation (lease) and one entry
in `transactions`, with its own baseline (`before`), final read (`after`) and
anything it could not restore; schema 2 had one original and one final
state for the whole run, which a released lease no longer allows. Original/final messages retain all arguments and their OSC type
tags, including mix pan and enum labels. Source provenance includes every
runtime module used to judge the reports; a change during measurement
invalidates the pass. SIGINT/SIGTERM requests end probing and allow bounded
restoration to finish. SIGKILL, power loss and device loss cannot be made
restorable by this process.

Restoration shares the probe permission checks: protected,
read-only, invalid or no-longer-identifiable state is left explicitly
unrestored.
