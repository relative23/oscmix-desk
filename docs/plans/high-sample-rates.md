# UCX II qualification above 48 kHz

**Status: 24 USB/ALSA modes measured on 2026-09-22.** See the
[results and limits](../evidence/0.7.1/sample-rates.md): 18 combinations
pass, six are incomplete or fail. All return to the exact readable
baseline and the known desk. These measurements establish the capacity table used by the playback
checks in H1 of the [roadmap](../ROADMAP.md). The protocol below records
the method; the backend pin is unchanged.

**0.7.2 runtime:** `streams.py` enforces the recorded USB playback
limits at the I/O boundary. It reads the exact ALSA card/serial and
correlates two `stream0` observations with the active PCM's `hw_params`.
Each nonempty write phase and verifier mix reapply is checked. Unknown
idle mode is explicitly unvalidated; inconsistent observations are refused.
A detected change between phases reports partial writes without advancing
the profile marker. No PCM is opened or rate selected by this check, and
the pure register planner is unchanged. The lock does not prevent an
external clock/DAW change between observations or after the apply.
See [upgrade behavior](../UPGRADING.md#upgrading-to-072).

The [new link/gain/mute evidence](../evidence/0.7.2/) adds 13 actual modes,
52 signal cases and 12 refused unavailable sources with no register writes.
It retains one startup xrun and both successful repetitions rather than
claiming every attempt passed. Every completed attempt returns to the
baseline. A separate [PipeWire recording](../evidence/0.7.2/pipewire-rates.json)
checks the same 13 modes in the Pro Audio profile: all 65 known-route
checks pass, with the expected AUX links and actual hardware rate. Each
run restores the readable device state and all saved PipeWire parameters.
The recording identifies the Pro Audio profile, explicit AUX maps and
actual hardware parameters used for each measurement.

## Question to answer

At 88.2, 96, 176.4 and 192 kHz, which configurations can this measured
backend apply correctly, which channels really exist, and what survives
a transition back to the user's original rate?

The historical 48 → 44.1 kHz experiment did not show a general loss of
the routing matrix. It does not qualify Double/Quad Speed. Preserve that
experiment in [history](../history/roadmap-through-0.7.0.md#sample-rate-and-clock-changes-destroy-state)
instead of treating its old heading as an established defect.

## Avoid the channel-count shortcut

The [RME UCX II manual](https://rme-audio.de/downloads/fface_ucx2_e.pdf)
describes ADAT capacity of eight, four and two channels across Single,
Double and Quad Speed. This is not a rule that every input/output count
halves. Section 33.2 also distinguishes Class Compliant alternate settings
with 8, 14, 16 and 20 channels. Linux's selected stream setting, physical
digital channels and oscmix addresses must be measured independently.

| Rate group | Rates to record | Physical ADAT capacity expected from the manual | USB/ALSA/register map |
| --- | --- | --- | --- |
| Single Speed baseline | 44.1 / 48 kHz | 8 | All four USB modes pass their playback/mixer checks |
| Double Speed | 88.2 / 96 kHz | 4 | 8/14/16 pass; 20 exposes four silent playback channels |
| Quad Speed | 176.4 / 192 kHz | 2 | 8/14 pass; 16/20 fail timing or time out |

## Record before touching the rate

Use the [hardware evidence contract](../HARDWARE-EVIDENCE.md) and record:

- Exact desk commit, backend SHA, firmware (USB and DSP), device serial,
  host distribution/kernel, architecture and ALSA/PipeWire versions.
- Baseline config, profiles, active marker, service state, readable stable
  register snapshot and current routing. Preserve the known playback
  configuration: a snapshot cannot recover a matrix the backend omits.
- Actual device sample rate and clock source/synchronisation, optical
  mode, USB descriptors/alternate settings, ALSA stream channel positions
  and PipeWire node/profile/position map. Keep timestamps and raw records.
- The external connections and output attenuation used. Stop unrelated
  audio clients, avoid an active recording, and arrange a quiet measurement
  window before a rate transition or any signal is attempted.

The rate requested by a PipeWire client alone is not proof of the device's
rate. Correlate `/clock/samplerate`, the ALSA stream and, when needed, the
front-panel indication. Record an unsuccessful transition as such.

## Execute in bounded stages

1. **Read-only inventory.** Record descriptors and backend reports at the
   baseline. Identify an explicit, reproducible way to select a supported
   rate; do not assume a backend write alone controls the USB stream.
2. **One rate at a time.** Capture before/after state, actual rate and
   active stream setting. Start with 88.2/96, then 176.4/192. Return to the
   baseline after each case so a previous failure does not contaminate
   the next measurement. Separate rate changes from clock-source changes.
3. **Map before writing.** Enumerate input/output/playback addresses and
   physical channel correspondence; distinguish unavailable channels from
   retained register slots. Check stereo link partners, ADAT holes and
   refresh completion/latency. Do not infer a smaller contiguous register
   range from a smaller USB stream.
4. **Minimal write cases.** After the map is understood, exercise selected
   analog and available digital routes, linked/unlinked cases and selected
   readable settings. Test invalid-channel refusal against recorded
   fixtures before permitting any such plan near hardware. No blind full
   register sweep at each rate.
5. **Signal-path evidence where needed.** For playback, use a known digital
   or loopback measurement when available. Acoustic checks are optional,
   separately announced and kept quiet. A low dBFS signal alone does not
   bound speaker SPL; confirm physical attenuation/routing first. Do not
   turn on phantom power or change reference levels merely to test rates.
6. **Restore and compare.** Restore original rate/clock, stream mapping,
   config, profile selection and service state. Reapply the known desk
   through its normal path, compare stable readable values and explicitly
   list expected differences and unobservable playback state. On a lost
   device, failed clock lock or unexplained mismatch, stop the sequence
   and resolve recovery before any further writes.

Keep raw evidence for each stage, including failures and restoration;
"returned to 48 kHz" alone does not prove that the desk was restored.

## Model and validation after measurement

Keep rate capability separate from the register address model. If some
addresses remain writable while their physical channels disappear, the
planner must not present an available route merely because a register
exists. Validate the effective device, optical mode, stream mapping and
observed rate before writes that depend on them.

Define the handling of an unknown rate or a rate changing between preview
and apply before implementing it. An offline preview states its assumed
mode; an online apply rechecks that assumption under the existing writer
coordination. A cooperative lock cannot prevent a DAW or the device from
changing clocks. Detect what can be detected, report partial/unknown
outcomes honestly and do not add automatic clock changes to resolve it.

Tests come from observed boundaries: valid/invalid routes at each measured
mode, exact playback/output mapping, link order, stale-preview refusal,
transition failure and restoration. Existing 44.1/48 behaviour and offline
planning must retain their contracts.

## Acceptance and release claim

Publish one evidence row per rate and optical/USB mode tested, with
firmware, pin, channel map, route/read-back results, measurement method
and restoration result. A rate is qualified only for the combinations actually
exercised. Derive model fixes and regression fixtures from these records,
then run the existing affected checks and release gates for that change.

H1 may finish with explicit unsupported combinations. It must not finish
with guessed channel counts or a blanket "192 kHz supported" label based
only on the device manual.
