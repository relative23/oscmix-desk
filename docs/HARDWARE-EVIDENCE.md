# Hardware evidence and its limits

UCX II serial 24216011 is the physical device used by this project.
Its measured backend pin is
`f2fdd5ec78338848754aad32cc07f3440de63395`. Evidence describes that
combination and the firmware it records, not every Fireface or firmware.

## Current measurements for 0.7.3

The [0.7.3 sweep](evidence/0.7.3/write-sweep-ucx2.json) and
[five-route check](evidence/0.7.3/hardware-evidence.json) use candidate
`af89b03`, USB 3.01 / DSP 36 and the unchanged backend pin. The sweep
confirms 1,888 entries, skips 14 protected entries and restores all 2,252
readable messages exactly. All five routes pass with a -40 dBFS stimulus
through their named stereo PipeWire sinks at the original 48 kHz.
Two complete refreshes after the routing test again match the original
state. The real per-user upgrade from 0.7.2 preserves the desk and passes
startup and SIGHUP. Each artifact retains the source actually measured;
subsequent packaging/test changes leave those runtime files unchanged.

## 0.7.2 measurements

The [0.7.2 records](evidence/0.7.2/) identify the final runtime `e9ad292`
and its USB 3.01 / DSP 36 device. A fresh schema-2 sweep confirms 1,888
entries and skips 14 protected reference-level entries. Every one of the
2,252 readable messages matches the baseline after restoration. All five
declared playback routes pass the quiet device-meter check. The installed
0.7.2 service passes startup and SIGHUP with the same readable state and
preserved user configuration.

Separately identified development recordings measure 13 USB/rate modes:
52 linked/unlinked gain/mute cases, 12 unavailable-source refusals and
65 route checks through the three PipeWire Pro Audio stereo sinks. These
records preserve the original source identity and per-case observations.

## 0.7.1 measurements

The [0.7.1 evidence](evidence/0.7.1/) measures the corrected runtime
`abd8594` on USB 3.01 / DSP 36. The schema-2 sweep confirms 1,888 entries,
skips 14 reference-level entries and restores all 2,252 readable messages
exactly, including every argument and type tag. No phantom-power or
reference-level writes are sent. An initial pass exposed a lost
partner-channel restoration between sweep groups; it was corrected and
the entire sweep repeated before retaining the passing result.

Separate experiments cover even/odd mono input and playback selection
from linked pairs, linked/unlinked gain and mute, and all five configured
playback routes. A BETA 58A on input 1 is captured through actual ALSA at
24-bit/48 kHz with 40 dB temporarily and phantom power off; no speech file
is retained. Loud, close speech peaks at -2.9 dBFS without digital clipping.
This confirms capture, not normal-speech gain calibration or microphone
quality. Original gain and mixer state are restored.

The [sample-rate table](evidence/0.7.1/sample-rates.md) records 24 actual
USB/ALSA combinations, including six incomplete/failed cases, with a
return to the original state after every attempt. It qualifies playback
and output-meter paths.

## Historical evidence

**Historical numeric limits:** sweeps through 0.7.0 used a tolerance that
could accept changed but incorrect scalar reports and omitted raw values
for confirmations. Snapshots rounded floats to one decimal place. The
counts and comparisons below describe those methods; they are not proof
of exact scalar equality or absence of smaller drift. See the corrected
[numeric contract](NUMERIC-CONTRACT.md). Original artifacts are retained.

The historical fixtures retained from 2026-08-27 are:

| Artifact | What it establishes | Provenance limit |
| --- | --- | --- |
| `tests/data/refresh-dump.json` | 2322 reported paths, including 70 streamed paths; register shapes and arrival times | Predates the firmware field. |
| `docs/evidence/write-sweep-ucx2.json` | 1902 entries, 1888 confirmed writes, 14 reference-level entries deliberately skipped | Predates the firmware field. |

These old files are not retroactively given firmware read from a newer
run. New recordings include USB revision and DSP version. Release route
measurements report those fields, serial, backend revision, sinks, channel
layouts and per-route results. Qualification records identify the desk
commit used to collect each artifact. A changed backend pin, firmware or
register model requires the remeasurement specified by the
[release checklist](RELEASE-CHECKLIST.md).

For 0.7.0, [fresh recordings from 2026-09-22](evidence/0.7.0/measurement.json)
name USB 3.01 / DSP 36 and candidate `86fb4e4`. Both linked and unlinked
runs measure all five routes successfully. The fresh dump again contains
2322 paths (70 streamed); the fresh sweep again confirms 1888 and skips
14, with `not_restored: []`. A snapshot comparison before the installation
and after the hardware tests finds no changed values across 2252 stable
registers. The older fixtures above remain intact.

The playback mix matrix is writable but the pinned backend cannot report
it reliably. No dump, sweep or `--diff` result converts it into verified
state. Route evidence uses test tones and the device's own meters to check
the effect; it is not an independent analog measurement at the output
connectors. `48v` is readable but has no config write domain. The sweep
excludes it from probing and deliberately skips reference-level changes.
Before 0.7.1, restoration did not enforce the probe exclusions; the
corrected tool applies the same permission checks to restoration/retries.

Multi-device selection, locking, backend replacement and interrupted
writes are exercised with simulated interfaces and real CLI processes.
The 802 has only a channel map; the pinned backend cannot operate it and no register model
or hardware evidence establishes support.

## Reporting another device

First establish that the backend can operate the model. Report the exact
model, hardware serial (privately if preferred), USB revision, DSP/firmware
version, Linux/ALSA versions, oscmix commit and oscmix-desk commit. Include
the relevant startup log and describe which operations were actually
performed. A similar model name is not evidence of a compatible register
map.

With a matching backend and a quiet setup, `scripts/record-dump.py`
records shapes and timing without mixer values. The write sweep is a
UCX-II-specific writer, not a discovery tool for unknown hardware: do not
run it on a new model until its register domains and dangerous operations
have been reviewed. A supported model needs its own register/capability
data and measured write and route evidence. Tie each support claim to
the corresponding source and recorded measurement.
