# Upstream patches and the coordinated 0.8.0 development series

`backend-series.json` is the reproducible 0.8.0 candidate: full base commit,
protocol and SHA-256 for each patch applied by `scripts/prepare-backend.py`.
Patch 0003 extends the existing backend, ALSA bridge and GTK mixer together.
It is carried locally; no upstream acceptance is claimed. Its software
contract is [documented here](../docs/BACKEND-CONTROL.md). The development
installer now builds and verifies this exact series. Final hardware,
packaging and release qualification remain pending; the installed/published
0.7.3 release is a separate build.

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

The base pin is `f2fdd5ec78338848754aad32cc07f3440de63395`.
The earlier pin `55802a6ab865e551540ee9ad5081b8ae3276f8ca` is the base of
the historical patches below. Patch 0001 is not in the applied series;
the output-link barrier is retained. **Patch 0002 was merged upstream as
`55802a6` and is kept only as a record**; applying it now would conflict
with the fix it asked for.

---

## 0001 -- update our view of output stereo on write

**Status:** offered as [michaelforney/oscmix#31][pr31] on 2026-08-17.

[pr31]: https://github.com/michaelforney/oscmix/pull/31

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
with the two-phase apply and the barrier between the phases
([ADR 0001](../docs/decisions/0001-two-phase-routing-apply.md)).
The 0.8.0 path retains the output-link barrier and bounded verification;
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

Not a deletion, and not immediately. [ADR 0008](../docs/decisions/0008-pinned-upstream-revision.md)
fixes the order: **bump the pin, measure on hardware, then** remove
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
50 Hz. Both readings are half of a double report, and
`docs/upstream-issues.md` records the earlier withdrawal that came of
reading only one half.

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
