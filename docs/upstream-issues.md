# Upstream issues

The record of what went to [michaelforney/oscmix][oscmix] from this
project -- filed, fixed, withdrawn or still open -- and of what was
observed upstream that this project depends on.

The roadmap treats upstream limits as work items rather than weather. It
also means those items are held to the same standard as everything else
here: an issue that wastes a maintainer's time on an unreproducible
report is worse than no issue.

Entries 1 and 2 were observed against
`2411b12d8a13b82829caf3b0b628078980c3d3a4` on a Fireface UCX II
(serial 24216011), Linux 7.0. **Both are fixed upstream.** The pin moved
to `55802a6ab865e551540ee9ad5081b8ae3276f8ca` to carry them -- measured
on the same device: the dump goes from 2002 registers to 2322, Room EQ
from 320 to its real 640, and `/controlroom/mainout` now arrives as
`('is', (-1, 'None'))` instead of unnamed. Entries 3 to 5 were observed
against that pin, which 0.4.0 and 0.5.0 carried; in 0.6.0 it moved on
to `f2fdd5ec78338848754aad32cc07f3440de63395`, which carries their
fixes as well.

---

## 1. Fixed upstream: `unexpected enum value -1` from `/controlroom/mainout`

**Status:** the maintainer added optional value lists for enums and
pushed to master on 2026-08-21, after a branch test from here. The
warning also names the OSC address now.

Two things came out of it that outlast the fix.

**On the report itself:** *"The AI text contains a lot of fluff, and
it's hard to tell what's important and what's irrelevant on the first
read through."* The measurements were right and every number held, but
the write-up was some forty lines for a result that needed five, and the
one paragraph he engaged with was the short observation at the end. The
style this repository uses -- counter-checks, taxonomies, methodology
stated so a claim can be re-derived -- is for readers auditing the work.
A maintainer is deciding whether to apply a patch. Short version first,
and in a human's own words.

**On the design:** he replied that oscmix was built to hold as little
state as possible and use the device as the source of truth, "however,
it seems this isn't always possible, so maybe oscmix needs keep its own
complete mirror state." That is the ground under the stereo-link race,
the three timing constants and `patches/0001`. Recorded as a thing to
watch in the roadmap, not as a plan.

---



**Status:** filed as [michaelforney/oscmix#30][issue30] on 2026-08-17.
Reproducible on every start.

[issue30]: https://github.com/michaelforney/oscmix/issues/30

### Title

`unexpected enum value -1` on every start; diagnostic does not say which register

### Body

Running `oscmix` against a Fireface UCX II prints this to stderr on every
start, and repeatedly during `/refresh` dumps:

```
unexpected enum value -1
```

Measured here: **42 occurrences in 24 hours** of ordinary desktop use.

#### Which register

The message does not say, so I found it by asking the device for
everything and looking for the value:

```
/controlroom/mainout    ,i   -1
```

Every other enum register comes back as `,is` with its name, e.g.
`/reverb/type ,is 2 "Large Room"`. This one takes the fallback branch in
`oscsendenum()` (oscmix.c:1440):

```c
if (val >= 0 && val < nameslen) {
        oscsend(addr, ",is", val, names[val]);
} else {
        fprintf(stderr, "unexpected enum value %d\n", val);
        oscsend(addr, ",i", val);
}
```

`CTLROOM_MAINOUT` is declared with ten names, `"1/2"` through `"19/20"`
(oscmix.c:1269). The device reports `-1`, which is outside that range.

#### What -1 probably means

I do not know for certain, and I would rather ask than assert: on this
interface the Control Room main output appears to be **unassigned**, and
`-1` (raw `0xFFFF`) looks like the device's way of saying "none" rather
than a decoding error — every other value in the same dump is plausible,
and the Control Room section is optional on this model.

If that is right, the fix is to give the unassigned state a name rather
than to treat it as unexpected.

#### The part that is a bug regardless

**`oscsendenum()` does not print the address.** Whatever the value turns
out to mean, a message that says only `unexpected enum value -1` cannot
be acted on: there are dozens of enum registers, and nothing in the
output narrows it down. Finding this took a full state dump and a
comparison of type tags.

One line:

```c
fprintf(stderr, "unexpected enum value %d for %s\n", val, addr);
```

That change is useful on its own, independently of what `-1` means here.

#### Reproducing

Any UCX II with no Control Room main output assigned. `oscmix` prints it
at startup without any client interaction.

---

## 2. Fixed upstream: Room EQ registers reporting implausible values

**Status:** filed as [michaelforney/oscmix#32][32] on 2026-08-20, with a
mechanism and a before/after measurement; **fixed upstream in
`55802a6`** (2026-08-21) and measured here when the pin moved to it --
640 Room EQ registers reported where 320 were before.

**It was withdrawn first, and the withdrawal was right on the evidence
at the time.** What follows is the original reasoning, kept intact,
because the way it was wrong is the useful part -- see the resolution at
the end of this section.

[32]: https://github.com/michaelforney/oscmix/issues/32

**Original status:** withdrawn -- does not reproduce.

`docs/ROADMAP.md` carried this as a known constraint and as an upstream
work item:

> **Room EQ registers report implausible values** (+30 dB and +40 dB at
> 50 Hz, Q=80, on every output). This looks like a register offset in
> upstream's decoding rather than real device state.

It was going to be filed. It should not be, because the current
measurement contradicts it. Every EQ and Room EQ register read back from
the device against the pinned revision:

| | count | min | max |
|---|---|---|---|
| `band*gain` | 220 | **0.0 dB** | **0.0 dB** |
| `band*q` | 200 | 0.7 | 5.0 |
| `band*freq` | 200 | 50 Hz | 5000 Hz |
| `roomeq/delay` | 20 | 0.0 | 0.0 |

Zero values outside a plausible range. Not one gain at +30 or +40 dB, no
Q anywhere near 80.

### Why the original observation might still have been real

Three possibilities, and I cannot distinguish them from here:

1. It predates the pin, and the revision it was seen on decoded these
   registers differently.
2. The device held different state at the time -- a loaded TotalMix
   snapshot with real Room EQ settings, misread as implausible.
3. The observation was mistaken.

What settles it is a recording, not an argument. If it reappears,
capture the dump (`scripts/record-dump.py` records shape; values would
need a variant that keeps them) and file it with numbers attached.

### Resolution (2026-08-20): both readings were half of a double report

`device_ffucxii.c:124` recombines the per-output Room EQ offset with `|`
against the base `0x35D0`, whose low five bits are already `0x10`. For
offsets 16..31 the bit collides, so the upper half of each output's
block decodes as its own lower half: `0x35E3` (`BAND6FREQ`) arrives as
`BAND1GAIN`, `0x35E6` (`BAND7FREQ`) as `BAND2GAIN`.

So one `/refresh` carries **two values for the same OSC path**, and
which one a client keeps is arrival order. That is why the table above
reads *every gain at 0.0 dB* -- the reader kept the first -- and why the
roadmap's original note read *+30 dB and +40 dB at 50 Hz*: those are
band 6 and 7 **frequencies** decoded through the gain scale.

Neither observation was mistaken. The roadmap's original hypothesis --
"a register offset in upstream's decoding rather than real device
state" -- was correct, and the withdrawal disproved a claim nobody had
made ("the device holds implausible values") rather than the one that
mattered.

Measured on the UCX II against the pinned revision, one `/refresh`
before and after the one-line fix: 1932 registers with 260 conflicting
paths becomes 2252 with none, and bands 6 to 9 appear with plausible
values. `patches/0002` carries the change.

### The lesson, which is the point of writing this down

**Revised 2026-08-20, because the first version of this lesson was drawn
on the same incomplete evidence as the withdrawal.** It is kept rather
than replaced, because being wrong twice in opposite directions about
one register block is the instructive part.

It used to read: *this claim sat in the roadmap for two releases and
reached the point of being filed before anybody checked it against the
device; a single query would have caught it at any time.*

A single query **did** check it. That is what produced the table above,
and the table is what withdrew the report -- confidently, with numbers,
and wrong. One query reads one half of a double report, and nothing
about the answer says a second value exists.

So the rule is not "query before you claim". It is: **a measurement that
could have more than one answer has to be shown to have one.** The
`/refresh` was read once and the value taken. Reading the same register
four times in a row -- 0.0 once, 0.7 three times -- is what actually
settled it, and that took seconds.

This project has the same rule for its own zeros elsewhere: a
measurement reporting *nothing happened* must prove its instrument was
alive in the same run. The withdrawal above is that rule applied to
somebody else's bug and skipped.

That is the same failure the `15-20 s` dump figure had, and the same one
`LINK_SYNC_BLIND_DELAY = 20` had -- see
[ADR 0010](decisions/0010-timing-constants-need-a-recording.md). The rule
that ADR states for timing constants applies to claims about upstream
too: **name the recording, or do not make the claim.**

[oscmix]: https://github.com/michaelforney/oscmix

## 3. Fixed upstream: Room EQ writes are sent but the device ignores them

**Status:** filed as [michaelforney/oscmix#33][33] on 2026-08-24.

**Closed 2026-08-27** by the maintainer with `f2fdd5e`: the UCX II
takes Room EQ *writes* at `0x3400` while *reporting* the family from
`0x35D0` -- a split range, which is why writes to the reported block
changed nothing. Confirmed on this device the same night: at
`f2fdd5e`, `/output/1/roomeq/band1gain` takes -6.0 dB
(`setreg 3403 FFC4`) and reads it back, where it had always read 0.0.

Distinct from #32, which was about *reading* the block: with the fold
fixed and all 640 Room EQ registers readable, writing any of them is
accepted by oscmix, put on the wire, and ignored by the device -- the
value reads back unchanged. Found while measuring for the 0.4.0
release. This project declared the family **reported and not settable**
until it moved, and it has been settable since 0.6.0.

[33]: https://github.com/michaelforney/oscmix/issues/33

## 4. Fixed upstream: `/output/N/phase` writes never leave oscmix

**Status:** filed as [michaelforney/oscmix#34][34] on 2026-08-24. Fixed
by this project's [oscmix#36][36pr], merged on 2026-08-27 as `9dba36f`
(entry 8); the pin carries it since 0.6.0, and output phase is settable
here since then.

`ctltoreg` gates `OUTPUT_PHASE` on `INPUT_HAS_REFLEVEL` (bit 2 of the
input flags), but an output only ever carries `OUTPUT_HAS_REFLEVEL`
(bit 0), so the guard always breaks, `ctltoreg` returns -1, and the
write is dropped on every output. Input phase, which is ungated, works.
Found by reading the node table while declaring the register model;
confirmed on the device by tracing that nothing reached the MIDI pipe.

(An earlier draft of this entry blamed a missing `.set` on the output
`phase` node. That was wrong -- the node has `.set=setbool` and has
since early 2025; the filed issue named the `ctltoreg` gate, which is
the real cause, and this entry now matches it.)

Declared **reported and not settable** here, same as Room EQ, until the
fix landed upstream and the pin moved to carry it (ADR 0008).

[34]: https://github.com/michaelforney/oscmix/issues/34
[36pr]: https://github.com/michaelforney/oscmix/pull/36

## 5. Fixed upstream: `/input/5..8/gain` accepts writes but can never change

**Status:** filed as [michaelforney/oscmix#35][35] on 2026-08-26. Found
by the 0.5.0 write sweep on 2026-08-25. **Closed 2026-08-27** by the
maintainer with `fdc47f7`: Analog 5-8 do have a gain stage ("Pre Gain",
0.0 to 24.0 dB in the device UI), so the row gains `gain={0, 240}`.
Confirmed on this device the same evening -- built at `fdc47f7`,
`/input/5/gain` takes 12.0 dB and reads it back -- and said so in a
comment on the issue. The pin moved to `f2fdd5e`, which carries the
fix, in 0.6.0, and the row became settable with it (ADR 0008).

[35]: https://github.com/michaelforney/oscmix/issues/35

### Title

`/input/5..8/gain` accepts writes but can never change on a UCX II

### Body

On a Fireface UCX II, writing `/input/5/gain` through `/input/8/gain`
has no effect. The register stays at 0 whatever value is sent, whether
written alone or with others.

`device_ffucxii.c` gives Analog 5-8 `INPUT_HAS_GAIN` but no `.gain`
range, so it defaults to `{0, 0}`:

```c
{"Analog 5",    INPUT_HAS_GAIN | INPUT_HAS_REFLEVEL,
        .reflevel={reflevel_input, LEN(reflevel_input)},
},
```

`setinputgain` then clamps every value to that range, so `setval`
always writes 0 and the device reports nothing because nothing changed.
Inputs 1-4 have ranges (`{0, 750}` and `{0, 240}`) and work.

I don't know which way it should go: if Analog 5-8 have no gain stage,
the flag looks wrong; if they do, the range is missing. Either way the
node is in the OSC tree and in the dump today, so it reads as a control
that exists.

`device_ff802.c` has the same shape on all eight Analog inputs. I have
no 802 to test.

Tested at 55802a6.


## 6. Observed: an 802 rework exists in a fork, with hardware behind it

**Status:** observation, not an issue. Recorded 2026-08-26.

Replying [on #35][35c1], `huddx01` -- who has an 802 -- pointed at the
`dev` branch of [huddx01/oscmix][hudd] and announced a complete rework
of `device_ff802.c`, written but not yet pushed ("will come soon").

What the branch already holds (state of 2026-05-07, ten commits ahead
of upstream, none pushed since 2026-08-03):

- **A full 802 register mapping**, and it is a different scheme from
  the UCX II's: channel stride `idx << 8` rather than `idx << 6`,
  outputs based at `0x1E00`, the mix matrix inside the output block
  from offset `0xE0`, globals from `0x3C00`, refresh at `0x0812`, and
  a `DEVICE_MIX_VOLONLY` flag. If this is right, none of the UCX II
  address arithmetic in [register-addresses.md](register-addresses.md)
  transfers -- the rules are per device, which the offsets file's
  design already assumes.
- **Line inputs 1-8 carry `.gain = {0, 120}`** -- the exact hole #35
  describes on the UCX II, plugged on the 802 side.
- Some values are marked as guesses by their own commit messages
  ("add durec regs (guess)"). The fork also reworks `oscmix.c` itself
  and carries a UFX+ table.

One thing looked like #35's mirror image: **Mic/Inst 9-12 carry no
`INPUT_HAS_GAIN`**. Answered by `huddx01` on 2026-08-28: it is not --
the 802 (legacy and, to his knowledge, the FS models) has *physical*
gain knobs on those channels, no digital gain and no reflevel, same
for the phones outs. The missing flag is correct, and the question
prompted him to fix his wiki. Closed without a patch, which is the
right outcome for a question. Asked [in the same thread][35c2], together with the three
measured write-path behaviours (no echo on the written path, bursts
drop writes, out-of-range is refused not clamped) that would otherwise
produce false results when he verifies his table.

**What this changes here: nothing yet, possibly everything for the
802.** The pin stays on upstream (ADR 0008), so this project can use
the work only once it is merged. Until then it is the best available
map of the 802's registers, and the first sign of the missing piece --
somebody with the hardware -- for the one supported-device criterion
this project cannot meet alone.

[hudd]: https://github.com/huddx01/oscmix/tree/dev
[35c1]: https://github.com/michaelforney/oscmix/issues/35#issuecomment-5421134992
[35c2]: https://github.com/michaelforney/oscmix/issues/35#issuecomment-5427409903


## 7. Rewritten: #29 and PR #31, after reading them as the maintainer would

**Status:** both edited in place on 2026-08-26. Neither is a new report;
this entry records why two existing texts changed and what the test was.

The user read the open items against the known signs of model-written
text -- Wikipedia's [Signs of AI writing][wp-signs] and the Claude
trope list at [tropes.fyi][tropes] -- and two of ours failed it. Not on
facts: every number in both was measured. On voice and length, which
is what the maintainer complained about on #30 (entry 1 above) and
what makes a reader skim.

**[PR #31][31]** (`cache-sync-output-stereo`): the body was 339 words
under three headings, with a "How I observed it" narrative of two failed
audio measurements, a "Why I care" section advertising this project,
and two rounds of "happy to adjust". The code carried a nine-line block
comment in a file that has three comments in 1698 lines, none of them
longer than one. Now: 80 words, three paragraphs, the comment gone,
the commit retitled `Update output stereo state on write` to match the
upstream form (capitalised imperative, no `oscmix:` prefix, which
upstream uses only for subcomponents), rebased onto `55802a6`. Diff
+18/-1 instead of +28/-1. Commit `56641bc`.

**[#29][29]** (the project announcement): thanks in the opener and the
closer, "in case it is useful", "feel free to link or ignore", "happy
to look into a patch", one sixty-word sentence listing every feature
with a parenthesis after each, "first-class citizen". It also still
described oscmix-autostart, a name and a scope this project has
outgrown (renamed in 0.5.0, commit `9a276ad`).
Replaced, not commented, so nobody is notified over a rename. The one
technical point in it -- `/refresh` dumps the input mix matrix but not
`/mix/*/playback/*`, and a `/mix` write draws no reply -- is unchanged
and still open; the roadmap carries it as a design constraint.

New body of #29:

> (Edited: the project was called oscmix-autostart when I opened this.
> The old link redirects.)
>
> I've been running oscmix on a UCX II under Linux for a while and built
> the glue around it that I was missing:
> https://github.com/relative23/oscmix-desk
>
> udev rule and a systemd user unit so the backend comes up on hotplug,
> plus a routing.conf that is applied on every start. The config part
> grew more than planned. It now covers the mix matrix, faders, EQ,
> dynamics, reverb and the clock, `--diff` shows what an apply would
> change, and every register in it was checked against the device rather
> than taken from the manual. None of it touches oscmix itself, the
> backend is built from your repo at a pinned commit.
>
> One thing I ran into while verifying the applied state: the dump after
> /refresh contains the input mix matrix but not /mix/*/playback/*, and a
> /mix write gets no reply, so the playback matrix can't be read back at
> all. Is that the device, or is it just not requested?

**#33, #34, #35 were checked the same way and left alone**: 70 to 144
words each, observation first, one code block, one question, within
the 500-1200 characters the repository's other bug reports run to.

**The rule this leaves behind** (also in the project memory): thanks
once or not at all, no offers, no method narrative, no headed sections
in an issue, no bold lead-ins, no "not X but Y", no dashes for pivots,
plain "is" and "does", one direct question at most, no sign-off. And
no fake typos as a disguise; that is a watermark too.

[29]: https://github.com/michaelforney/oscmix/issues/29
[31]: https://github.com/michaelforney/oscmix/pull/31
[wp-signs]: https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing
[tropes]: https://gist.github.com/ossa-ma/f3baa9d25154c33095e22272c631f5a1


## 8. Fix merged: output phase, measured on every output

**Status:** [oscmix#36][36pr] opened 2026-08-27, **merged the same day
as `9dba36f`** after one review round. Fixes the defect in entry 4.
**The review round:** the maintainer read the broken guard as
misplaced rather than wrong -- meant for `OUTPUT_REFLEVEL` with the
`OUTPUT_HAS_REFLEVEL` flag -- and asked for exactly that. Done as
`9a545b4`, rebased onto `fdc47f7`, and re-measured before pushing:
phase reaches the device and reads back on analog and digital outputs;
reflevel still writes on outputs 1-8 (a same-value write of `+13dBu`
went out) and is dropped on the digital ones now, which carry no
reflevel. The reply on the PR is the user's own words.

The gate on `OUTPUT_PHASE` is **removed**, not corrected to
`OUTPUT_HAS_REFLEVEL`, and the reason is measured rather than assumed:
on a UCX II every one of the 20 outputs accepts phase and reports it
back -- the 12 digital ones (ADAT/SPDIF, outputs 9-20) included, and
those carry no reflevel flag, so gating on `OUTPUT_HAS_REFLEVEL` would
leave them broken. Removing it also makes phase consistent with every
other output control, none of which is gated; `OUTPUT_REFLEVEL` itself
is not.

Verified before and after the patch, both directions, on the device:

- stock oscmix: writing `/output/1/phase` produces no MIDI write at all.
- patched: `setreg 0508 0001` on the wire, and the device reports
  `/output/1/phase = 1`; a write of `0` sends `setreg 0508 0000` and
  reads back `0`.
- swept across all 20 outputs: 20/20 read back `1` after a write of `1`,
  20/20 read back `0` after a write of `0`, the device left clean.

Register `0x0508` is what `regtoctl` already decodes as `OUTPUT_PHASE`,
so the write direction now meets the read direction the device was
already sending. The change is confined to `device_ffucxii.c`; other
devices have their own `ctltoreg` and are untouched.
