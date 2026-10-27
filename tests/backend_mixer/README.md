# Mixer regression probes

`tests/backend_mixer.py` compiles these against the source directory of the
prepared `OSCMIX_CONTROL_BINARY`, alongside the existing process-level
`backend_control.py` qualification. No hardware is opened.

| Probe | Contract |
| --- | --- |
| playback_mute | Every playback column and linked alias, repeated ON/OFF, zero cells, invalid mappings, unchanged cache and flags after a failed sink. Wire words have an independent parity/scalar decoder. |
| mix_bulk | 67,200 actual callback combinations across input/playback, mono/stereo, mute, width, level and pan; one bounded frame with the complete word sequence. |
| mix_invalid | Invalid coefficient/metadata mappings, non-finite values and capacity are rejected before MIDI/cache mutation. |
| mix_partial | A transport failure may leave hardware partially written. The desired cache never proves device readback or rollback; no retry is implied. |
| pan_restore | Both aliases and every feedback order for the five vectors used by the hardware pilot (acceptance remains separate); tiny-level silence restoration retains the raw -100/+100 pair and returns the complete matrix to zero. |
| mix_geometry | Full prepared `oscmix.c`: independent double-precision matrix/gain/pan/width oracle, silence and exact endpoints, reversed width, DB/PAN floor and quantization, and preservation of global input width at mono outputs. Feedback uses the actual `newmix` callback in every register order. |
| midi_deadline | Actual complete `main.c` with deterministic clock/write/poll hooks. EINTR, partial progress, EAGAIN, stop and clock failures share an absolute two-second write deadline. |

The hooks replace sinks and system calls, not the mixer callbacks or codecs.
Hardware, DSP-meter and acoustic evidence remain separate. Compiler or prepared
source failures fail qualification rather than skipping these tests.

`readback_freshness.c` exercises the full OSC entry point for all ten linked
playback pairs, including repeated ON and partner OFF. It requires every
native link and input-mix observation to be invalidated while preserving
all desired coefficients, playback observations and unrelated channel bytes.
This checks software observation authority; it does not read a device.
