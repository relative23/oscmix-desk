# 0.7.1 qualification

The corrected runtime and test/tool files match
`abd859486ee8b61e7ed2567545642d202595e842`. All 27 software gates pass:
1,677 tests with two empty-parameter skips, Python 3.9–3.14, five suite
repetitions, a 200-cycle soak and fifteen fault repetitions. A complete
check with the physical UCX II switched off also passes; reconnection
automatically starts the installed service and completes its verifier.

Coverage is 97.58% including branches, displayed as 98%; the gate remains
97%. The fresh mutation run covers **8,248 mutants: 6,571 killed, 1,663 survived,
14 timeouts and 0 uncovered**, score **0.798033**. Timeouts are excluded
from the ratio. Six additional injected-fault checks cover Route properties
that mutmut does not mutate; they are not added to its total.

The [software record](software-qualification.json) contains exact hashes,
gate durations and the historical intermediate candidate's provenance.
An independent C calculation of the unchanged scalar conversions agrees
in 34,930 cases. The [lifecycle record](lifecycle.json) includes the physical
off/on test, installed runtime identity and restored state. The
[release notes](release-notes.md) describe compatibility and scope.

## Fresh hardware measurements

UCX II serial 24216011, USB 3.01 / DSP 36, pinned oscmix
`f2fdd5ec78338848754aad32cc07f3440de63395`, at 48 kHz. Monitoring was
physically off; no external loopback was connected.

| Artifact | Result |
| --- | --- |
| [Corrected write sweep](write-sweep-ucx2.json) | 1,888 confirmed, 14 protected reference-level entries skipped; no error or residual drift. Full type tags and arguments are preserved. |
| [Configured playback routes](hardware-evidence.json) | All five routes measured, complete, at a generated -40 dBFS. Existing PipeWire volume settings were preserved. |
| [Mono input matrix](mono-input.json) | Reproduces the old even-channel neighbour write, then passes all four corrected input 1/2 → output 5/6 combinations from linked pairs. |
| [Playback regressions](playback-cases.json) | Four mono selections plus linked/unlinked -12 dB and mute cases pass. Gain is compared with the simultaneously measured playback signal. |
| [Microphone capture](microphone.json) | BETA 58A on input 1, 40 dB temporarily, 48 V off; 20 seconds of actual ALSA 24-bit/48-kHz capture. Input 1 peaks at -2.9 dBFS, input 2 stays near -102 dBFS. Raw speech was not saved. The operator spoke loudly and close to the mic; normal speech gain is not calibrated. |
| [Input gain and mute](input-gain-mute.json) | Live microphone/ambient signal: linked and unlinked paths measure -31.998/-32.014 dB for an expected -32 dB. Mute is at the digital floor. Only input 1 had a microphone. |
| [Sample-rate matrix](sample-rates.md) | 24 USB/ALSA combinations, 18 passing and six incomplete/failed, all restored; physical digital ports remain unmeasured. |
| [Playback after rate changes](playback-after-rates.json) | All five known routes pass again through their original PipeWire sinks. |

Every before/after comparison in these input/playback experiments equals
the sweep's shared baseline across all 2,252 readable paths. Their JSON
references that full state instead of duplicating it. The playback matrix
remains unreadable: the known main route was re-established and its effect
checked with meters, not recovered from a snapshot.

Meter results do not establish analog connector performance or physical
Room EQ delay. Higher-rate qualification is recorded separately. Historic
0.7.0 sweeps retain their original data and method limits. The tag workflow
requires these completed records and matching files before publication.
Native packages belong to the following milestone.
