# Upstream patches and the coordinated backend series

`backend-series.json` is the reproducible backend of this release: full base
commit, protocol and SHA-256 for each patch applied by
`scripts/prepare-backend.py`. The installer and the package builds prepare and
verify this exact series. Patch 0003 extends the existing backend, ALSA bridge
and GTK mixer together; 0004-0016 refine it. The series is carried locally; no
upstream acceptance is claimed. Its software contract is
[documented here](../docs/BACKEND-CONTROL.md).

Changes to [michaelforney/oscmix][oscmix] that this project would like to
see, kept here so the reasoning survives whether or not they are
accepted, and so the measurement that justifies each one is in the
repository rather than in a pull request thread.

**These files carry oscmix's code, not this project's.** A diff quotes
the lines it changes, so the surrounding context here is Michael
Forney's work under [ISC][isc], the licence oscmix ships. This
repository is MIT and that does not extend to what is quoted in this
directory. The two licences are compatible and no relicensing is needed
or implied; the point is only that the attribution belongs where the
code came from.

[isc]: https://github.com/michaelforney/oscmix/blob/master/LICENSE

The base pin is `89ed835ecf8118a21d1d451be7d71944fa5ec384` ("oscmix: Use output index for
loopback", [#38][i38], [#39][pr39], merged 2026-11-04). It differs from the previous pin
`f2fdd5ec78338848754aad32cc07f3440de63395` in one line of `setoutputloopback`, which no patch of
this series touches: patches 0003-0014 apply to it unchanged, and the changed line is measured
([evidence](../tests/data/loopback-output-index-ucx2.json)). The refresh dump was recorded
again at the new pin. The playback-mute evidence of 2026-10-08 was carried to this series
instead: the prepared sources differ in `setoutputloopback`, the WRITE branch of `control.c`
(0016), one GTK binding (0015) and the build identity, none on the path that writes playback
mute, and `tests/evidence_carry_forward.py` refuses any further change to the series.
The earlier pin `55802a6ab865e551540ee9ad5081b8ae3276f8ca` is the base of
the historical patches below. Patch 0001 is not in the applied series;
the output-link barrier is retained. **Patch 0002 was merged upstream as
`55802a6` and is kept only as a record**; applying it now would conflict
with the fix it asked for.

---

## 0001 -- update our view of output stereo on write

**Status:** offered as [michaelforney/oscmix#31][pr31] on 2026-08-17.

[pr31]: https://github.com/michaelforney/oscmix/pull/31
[i38]: https://github.com/michaelforney/oscmix/issues/38
[pr39]: https://github.com/michaelforney/oscmix/pull/39

### What it changes

`{"stereo", OUTPUT_STEREO, .set=setbool, ...}` becomes
`.set=setoutputstereo`, a 28-line function that updates `outputs[].stereo`
before forwarding the register -- exactly what `setinputstereo()` already
does for inputs.

**Nothing changes on the wire.** `setval()` still forwards the register
unchanged; only oscmix's own view is corrected at the point of the write.
That is what makes this a consistency fix rather than a design change.

### Why it matters here

`setlevel()` reads `out->stereo` to decide whether a `/mix` write
addresses one channel or the pair. Unpatched, that flag only changes when
the *device* echoes the register back over MIDI. A `/mix` arriving in
between takes the unlinked branch, writes `mix[0]` and never touches
`mix[1]` -- the pair's right channel is left alone.

That was the original **every even output silent** defect. The historical
workaround used three constants --
`LINK_ECHO_TIMEOUT`, `LINK_SETTLE` and `LINK_SYNC_BLIND_DELAY` -- along
with the two-phase apply and the barrier between the phases.
The series retains the output-link barrier and bounded verification;
the old GUI-held-receiver blind delay no longer applies.

### The measurement

Two attempts at demonstrating this through audio failed, and both
failures are worth recording because they are why the third approach was
chosen:

1. Played a tone into `oscmix.krk-monitors`, which feeds **playback 5/6**,
   while writing a mix from playback 1. The signal path carried nothing
   and the -56 dB of crosstalk read as a pass.
2. Fixed the sink, but the race was masked: writing `mix[0]` only does
   not *clear* `mix[1]`, so the value left by the preceding control
   measurement kept the right channel audible.

Audio is a consequence of the bug, and consequences can be masked. The
third approach observes the mechanism itself -- `fprintf` of
`out->stereo` inside `setlevel()`, then `/output/5/stereo=1` immediately
followed by `/mix/5/playback/1`, on a Fireface UCX II:

```
unpatched:  PROBE setlevel out=5 stereo=0     <- pair just linked, view stale
patched:    PROBE setlevel out=5 stereo=1
```

The instrumentation is not part of the patch.

### What acceptance would allow here

Not a deletion, and not immediately. The order is fixed: **bump the pin, measure on hardware, then** remove
`LINK_ECHO_TIMEOUT`, `LINK_SETTLE` and `LINK_SYNC_BLIND_DELAY`. Doing it
the other way round would delete the workaround for a fix this project
has not yet shipped against.

[oscmix]: https://github.com/michaelforney/oscmix

## 0002 -- Room EQ register folding (**merged upstream, do not apply**)

**Status:** fixed by michaelforney as `55802a6 ffucxii: Fix regtoctl for
room EQ`, and [#32][32] is closed. The pin moved to that commit; this
section stays because the measurement behind it is the reason the fix
exists, and because a patch file with no story is a patch nobody can
review later.


`device_ffucxii.c` recombines the per-output Room EQ offset with `|`
against a base (`0x35D0`) whose low five bits are already `0x10`. For
offsets 16..31 the bit collides and the address lands 16 registers
lower, folding the upper half of each output's block onto its own lower
half.

Reported as [michaelforney/oscmix#32][32]. Filed with a measurement
rather than a description, because this symptom has already been
mistaken once: a client that keeps the *first* reported value sees every
Room EQ gain at 0.0 dB, one that keeps the *last* sees +30 and +40 dB at
50 Hz. Both readings are half of a double report.

Measured on a UCX II (serial 24216011) against the pinned revision, one
`/refresh` before and after:

| | unpatched | patched |
|---|---|---|
| registers in the dump | 1932 | 2252 |
| paths reported twice with conflicting values | 260 | 0 |
| band 6-9 registers visible | 0 | 140 |

`2252 - 1932 = 320` -- exactly the 16 aliased offsets on each of the 20
outputs.

**Not applied as a separate patch:** the base pin includes the upstream fix.
The text and measurement above record the historical investigation. The
current register model does include supported Room EQ settings; see the
[feature inventory](../docs/FEATURE-SURFACE.md).

[32]: https://github.com/michaelforney/oscmix/issues/32

## 0006 -- terminate after known MIDI input loss

The inherited ALSA bridge continued after `snd_seq_event_input()` reported
`-ENOSPC`, despite the lost events and cleared input FIFO. A fault-injection
probe compiling the actual prepared read loop reproduces a subsequent read
after that error, without opening ALSA or USB devices. Patch 0006 ends the
bridge on the error. The backend handles MIDI hangup/error before buffered
input or waiting writes: a second real-process regression reproduces a write
when `POLLIN` and `POLLHUP` arrive together. Both boundaries invalidate connected
observations and leases. There is no new transport, retry queue or device
command. The exact series identity changes; earlier hardware measurements do
not qualify this build.

## 0007 -- keep GTK connection notifications out of the observation stream

The real GTK mixer could fall behind continuous device and meter reports and
be disconnected as a slow consumer during refresh. Every observation notified
the global `writable` and `status` properties, which are bound to the window's
sensitivity and header. Patch 0007 confines those notifications to control
replies and events. Value and meter bindings still receive every observation;
malformed input and disconnects still invalidate the connection.

A display-free test runs the actual GTK mixer object against the actual C
backend with simulated MIDI. All 101 requested value updates arrive; the old
code also emits 202 unnecessary global notifications. Real GTK replay of the
captured continuous device traffic reproduces the slow-consumer failure and
passes with this correction. Queue limits, refresh windows, protocol and device
commands are unchanged. Fresh hardware and platform checks must identify this
new series; earlier results remain development evidence for their own builds.

## 0008 -- drain writable queues before declaring overflow

The coordinator checked an already full userspace queue before attempting to
send its contents again. A consumer that had resumed reading could therefore
be disconnected on the next delivery despite available kernel send capacity.
Patch 0008 drains the old queue before applying the existing overflow checks.
The usual post-enqueue drain remains; the packet and byte bounds do not change.

A socket-pair regression compiles the actual coordinator, fills its kernel
and userspace queues, resumes the reader and publishes the next packet. The
prior code disconnects the writable reader; the correction preserves every
packet in order. The same test then stops reading again and verifies that the
unchanged bound still disconnects the stalled peer. No hardware is opened by
this test; combined GTK and hardware load qualification remains separate.

## 0009 -- size the bounded queue for measured device bursts

The real UCX continued to overflow the original 32-packet queue while GTK
briefly changed its window sensitivity. A recorded refresh has 541 complete
MIDI frames in 150 ms and 814 in one second. A system-call trace of the real
GTK found a roughly 132 ms reception pause after its busy event; untraced
hardware runs also fail. These timings describe those probes, including the
trace overhead, rather than a guaranteed upper bound on GUI latency.

The queue now accepts up to 1024 packets, still capped at 256 KiB of complete
wire data per consumer. Variable-length packet allocations replace the fixed
8192-byte slots, keeping retained data and bookkeeping bounded. Every packet
is freed on successful delivery or disconnect. A failed allocation terminates
that consumer. No report is coalesced, discarded or turned into cached read-back.

The actual coordinator's socket-pair test retains and delivers an 814-packet
burst in order, then separately verifies packet overflow, byte overflow and
allocation-failure cleanup. The former code fails the burst case. The real
backend's stalled-client test now sends 4096 reports, beyond the new bound,
and still requires that another writer can proceed. Combined GTK, hardware
and platform qualification must identify this new series.

## 0010 -- give the sequencer input the largest pool

The bridge left its sequencer client at ALSA's default input pool of 200
events. A refresh delivers up to 541 MIDI frames in 150 ms; on a UCX II,
refreshes with every CPU busy used up to 138 cells, and the overflow that
patch 0006 turns into a stopped backend happened twice within two minutes. Patch 0010
asks the kernel for 2000 cells, its maximum, before the port exists. A
refused request is reported and the default stays in effect; lost input
still ends the bridge.

A probe compiles the actual prepared bridge with the ALSA calls before the
first port substituted and records the requested pool size. The former
code does not request one. No hardware is opened by the test.


## 0011 -- reconstruct stereo pan at silence and both endpoints

The previous reconstruction divided by a silent stereo leg at +100 pan and
could derive an invalid volume/width. Use the stronger leg for reconstruction,
define silence explicitly, and use exact zeros at the mono endpoints. A mono
output observation does not replace the input's stereo width.

## 0012 -- re-establish playback mute from complete columns

Playback mute has no physical register reporter. ON/OFF writes all cells in
the affected cached playback column(s), including zeros and repeated requests.
Validate mappings, bounds and finite coefficients before submission and change
flags only after the synchronous MIDI write. Encode each register word
separately in the RME seven-bit format. Setter errors propagate as INVALID.
The MIDI writer applies one absolute two-second deadline across partial writes,
EINTR and EAGAIN. A transport error may still leave hardware partially written.

## 0013 -- publish mix coefficients and metadata together

A single mix command prepares at most eight register words: coefficients and
the physical input DB/PAN metadata. Submit them in one bounded MIDI frame
while retaining word order, values and successful cache behavior. The desired
cache is never DEVICE readback; one host frame does not prove an atomic audio
transition.

`tests/backend_mixer.py` compiles full prepared translation units with only
sinks or system calls replaced. It checks actual mixer callbacks, both pan
aliases and all feedback orders, complete playback columns, invalid mappings,
partial submission and absolute write deadlines. The normal CI backend job
runs these beside the process-level and GTK integration tests.

## 0014 -- observe canonical stereo silence from fresh dB floors

A complete fresh stereo mix is still reconstructed from the fresh dB and pan
metadata of both input partners and the observed links. One narrow derivation
is added for two actively linked stereo pairs: when both partners' last fresh dB
values are at or below -650, all four link flags are fresh since the last
command or REFRESH, and all four physical coefficients are finite and exactly
zero, the level calculation reports canonical silence (volume `-inf`, width 100)
whatever the two pan registers hold.

A new `mix_observed` bit (value 4) witnesses the latest fresh dB floor. The dB
bit (1) and the pan bit (2) stay separate, pan never creates the floor witness,
and a new non-silent dB value clears it. The existing command, REFRESH and
link-change resets clear all bits. A cached zero, a missing partner dB, mono or
mixed forms, stale links, non-finite coefficients and pan-only reports get no
extra publication. The native cache invariant (finite, between 0 and 2) is
checked before the arithmetic, and a refusal invalidates that compound's
observations without repairing any coefficient.

The canonical report keeps the DEVICE origin of the register path that produced
it and proves no actual pan register value. The decoder may still attempt
physical MIDI output, so no claim of zero physical writes is made.

## 0015 -- bind GTK's High Damp control to the backend's node

GTK bound its High Damp slider to `/reverb/damp`, a path the backend does not declare; the
backend's node is `/reverb/highdamp` (register 0x300A, in Hz). The slider therefore neither showed
nor set the value. The patch changes that one binding. The slider's range, 2000 to 20000 Hz,
already matches what the device reports (18000 Hz in the write sweep). `tests/backend_writes.py`
checks that every reverb path GTK binds is a node the backend declares.

**Status:** an upstream GTK fix; not offered upstream yet.

## 0016 -- let the lease owner's WRITE renew the idle time

Only BEGIN and KEEPALIVE renewed the lease's five-second idle time. A WRITE can block in the MIDI
writer for up to two seconds per call (0012), and the desk sends its next KEEPALIVE only after
the WRITE is acknowledged, so a healthy owner could lose its lease to MIDI backpressure. After the
command, a WRITE from the lease owner now sets the idle deadline as a KEEPALIVE does. The
90-second hard limit is unchanged, and a refused WRITE (another client, a stale token) renews
nothing. The idle check still runs before queued requests are read, so MIDI work ahead of a
request (the meter timer) still counts against the idle time; the desk's one-second heartbeat
stays. A WRITE whose acknowledgement takes longer than the desk's two-second wait still ends the
connection: that takes a MIDI bridge stalled for about two seconds, which the backend itself
treats as fatal within one call. `tests/backend_writes.py` covers both cases; against the
previous build the renewal case fails.

**Status:** part of the coordinated backend (patch 0003's `control.c`); nothing to offer upstream.
