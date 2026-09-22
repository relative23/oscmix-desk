# UCX II sample-rate measurements

Measured on 2026-09-22, Ubuntu 26.04.1 LTS / x86_64, kernel
7.0.0-31-generic; UCX II serial 24216011, USB 3.01 / DSP 36.
Desk runtime `abd859486ee8b61e7ed2567545642d202595e842`, unchanged
oscmix pin `f2fdd5ec78338848754aad32cc07f3440de63395`.

The [24 recordings](sample-rates.json) establish USB playback channel
mapping and the five configured analog mixer paths through hardware
meters. They do not establish physical digital connector performance,
high-rate microphone capture quality or automatic rate-aware validation.

## Measured combinations

Numbers in the headings are the actual ALSA channel count / USB alternate
setting. **Pass** means every requested playback channel carried its
individual signal, in order, with the expected level and duration, and
all five configured stereo routes passed their meter checks.

| Device rate | 20 / alt 1 | 16 / alt 2 | 14 / alt 3 | 8 / alt 4 |
| --- | --- | --- | --- | --- |
| 44.1 kHz | Pass: 1–20 | Pass: 1–16 | Pass: 1–14 | Pass: 1–8 |
| 48 kHz | Pass: 1–20 | Pass: 1–16 | Pass: 1–14 | Pass: 1–8 |
| 88.2 kHz | Incomplete: only 1–16 carry signal | Pass: 1–16 | Pass: 1–14 | Pass: 1–8 |
| 96 kHz | Incomplete: only 1–16 carry signal | Pass: 1–16 | Pass: 1–14 | Pass: 1–8 |
| 176.4 kHz | Failed timing / timeout | Failed timing / timeout | Pass: 1–14 | Pass: 1–8 |
| 192 kHz | Failed timing / timeout | Failed timing / timeout | Pass: 1–14 | Pass: 1–8 |

ALSA accepted all requested combinations. The descriptors advertise all
six rates for all four alternate settings; neither fact proves successful
audio transfer. The four failed Quad-Speed attempts retain actual ALSA
parameters, USB stream frequencies and meter traces. Their OSC refresh
was not retained before the timeout, so their independent device-clock
field is **unknown**. Successful attempts correlate `/clock/samplerate`
with the active ALSA parameters and USB stream setting.

The 2,252 control-report paths persist across all successful combinations;
only the sample-rate value changes. A responding control address therefore
does not establish an available audio channel. In particular, the 20-channel
Double-Speed stream does not make playback 17–20 usable.

## Method and restoration

The measurement opens the hardware PCM directly with `S24_3LE`, disables
resampling and channel/format conversion, and plays a -40 dBFS / 997 Hz
signal through each USB channel for 0.5 seconds. A concurrent capture
stream selects the same mode; its bytes are discarded, not saved or
claimed as input-channel evidence. KRK monitors were powered off and
headphones disconnected. Phantom power and reference levels were unchanged.

The five existing routes are playback 1/2 → output 1/2, 7/8 → 7/8,
and 5/6 → 1/2, 5/6 and 7/8. Each output is compared with the simultaneously
measured playback signal plus its existing hardware fader. The opposite
side must stay below -110 dBFS. Source/output peak-hold windows can differ
by one 100 ms report interval: judgement excludes the first 100 ms and
last 180 ms of each source window and requires at least two output reports.
Raw traces are retained so that this treatment of transitions is reviewable.

Every attempt, including failures, returns to 48 kHz / 20 USB channels,
reapplies the known desk and restores all 2,252 readable messages exactly.
The JSON references the complete [sweep baseline](write-sweep-ucx2.json)
for identical before/after states and stores the actual differences during
each successful transition. A missing during-state is null, not a claimed
empty hardware state. The [final PipeWire check](playback-after-rates.json)
passes all five routes again after the matrix. Playback mix remains
unreadable; this verifies restoration of the known desk's effect.

## Scope for users

0.7.1 does not select the USB mode, change clocks or reject routes according
to the currently active rate. The table describes these measured modes;
it is not a blanket 192 kHz support claim. A DAW or PipeWire configuration
must select a suitable mode. PipeWire resampling can hide the device rate;
check the actual hardware stream when diagnosing it.

The [RME manual, version 1.6, sections 33.2 and 39.4–39.5](https://rme-audio.de/downloads/fface_ucx2_e.pdf)
distinguishes USB alternate settings and physical ADAT capacity. Its
8/4/2-channel ADAT limits do not halve every register or USB channel count.
The pinned backend labels 1–8 analog, 9/10 S/PDIF, 11/12 AES and 13–20 ADAT.
No external ADAT, AES, S/PDIF or analog return cable was connected. Clock
source remained Internal and optical output mode ADAT. Physical digital mapping, optical S/PDIF mode,
external clock transitions and physical Room EQ delay remain unmeasured.

A future rate-aware planner needs an explicit policy for unknown mode and
clock changes between preview and apply. These recordings supply boundary
fixtures for that work; the existing device lock cannot prevent a DAW or
the interface from changing clocks.
