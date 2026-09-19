# Roadmap

Where this project is going, and why. Every number in here was measured
against this repository or a Fireface UCX II (24216011), not estimated.

## What this project is

oscmix-desk is the **state layer** for a Fireface on Linux. Upstream
[oscmix] speaks the device's MIDI SysEx protocol and offers a live mixer
GUI; this project makes the resulting state *declarative, reproducible and
verified* -- applied on every boot and hotplug, checked against what the
device actually reports.

A knob to turn belongs in the GUI. A state that has to survive a reboot
belongs in `routing.conf`.

### Where we can be better than TotalMix FX

Not at DSP, metering or breadth of controls -- that is RME's and
upstream's ground. At **state management**, and at being *provably*
correct:

| | TotalMix FX | oscmix-desk |
|---|---|---|
| Configuration | GUI, opaque blob | text file, reviewable, diffable |
| Version control | no | yes |
| Reproducible after reboot | manual snapshot recall | automatic, every start |
| Verified against the device | no | read-back with per-register verdicts |
| Desktop audio integration | none | named PipeWire sinks from the same config |
| Scriptable / headless | no | yes |

### The bar is the stack, not this repository

**Draft, 2026-08-16 -- the matrix is measured, the conclusions are not
yet decided.**

"As complete as TotalMix FX, and better" is a claim about a *stack*. What
a user actually runs is upstream `oscmix` (the protocol), upstream
`oscmix-gtk` (the mixer GUI) and this project (the state). Measuring any
one of the three against a product that is all three at once is how a
feature gap turns into an argument about scope, and this document has an
[explicit non-goals](#explicit-non-goals) section that reads as a refusal
of the goal if the stack is never named.

So the bar is written down once, for the stack. The middle column is
measured: it is what a `/refresh` dump on a UCX II reports, recorded in
`tests/data/refresh-dump.json` (2322 registers, 70 of them streamed
without being asked, against the pinned revision). The third says when
the state becomes *declarable* here.

**There is deliberately no oscmix-gtk column.** Nobody has walked that
GUI against this list, and a column of guesses next to a column of
measurements is the exact failure mode item **L** was about. Filling it
in is a task, not an assumption: for every row below, does upstream's
GUI expose it at all?

| TotalMix FX capability | In the dump | Declarable here |
|---|---|---|
| Submix per output, playback sources | `/mix/<out>/playback/*` **absent** | today (re-established, never verified) |
| Submix per output, input sources | `/mix/<out>/input/*` (100) | 0.3.0 |
| Input strip: gain, `48v`, hi-z, reflevel, mute, phase, stereo | all seven reported | 0.3.0; gain on Analog 5-8 since 0.6.0 |
| Output strip: volume, pan, mute, phase, reflevel, stereo | all six reported | today: volume, stereo. 0.3.0: mute, phase, reflevel -- phase not settable in 0.5.x, again since 0.6.0. `pan` is not declared |
| EQ (3 band) and low cut, in and out | `eq/band1..3{freq,gain,q}`, `type` on bands 1 and 3 only, `lowcut/{freq,slope}` | 0.4.0 |
| Dynamics, auto level | `dynamics/{attack,release,comp*,exp*,gain}`, `autolevel/{headroom,maxgain,risetime}` | 0.4.0 |
| Room EQ (outputs) | `roomeq/band1..9{freq,gain,q}`, `type` on bands 1, 8 and 9, `delay` -- 640 since the pin moved, 320 before it | 0.4.0: modelled and reported; settable since 0.6.0, when the pin moved to the upstream fix for the ignored writes (#33) |
| Reverb and echo FX | `/reverb/*` (14), `/echo/*` (7) | 0.4.0 |
| Control room: main out, dim, mono, recall volume | `/controlroom/*` (6) | 0.4.0 |
| Crossfeed | `/output/<n>/crossfeed` | 0.4.0 |
| Clock source, sample rate, word clock | `/clock/*` (5) | 0.4.0, and see the open decision below |
| Optical/SPDIF mode, standalone, key lock | `/hardware/*` (10) | 0.4.0 |
| Loopback, channel names | **absent** -- write-only | 0.4.0, unverifiable by construction |
| Metering | 70 streamed registers | never: a meter is not state |
| Snapshots (8) | -- | 0.3.0: profiles, plural text files -- done |
| Workspaces, layouts, matrix view | -- | never: GUI, and no device state |
| DURec transport | not in this dump | never: interactive |
| Remote control (MIDI/OSC) | the whole interface is OSC | already better than the original |

Read down the last column and the shape of the answer is: **the device
surface is nearly all reachable and nearly all declarable, and the two
rows that are not** -- the playback matrix and the write-only registers
-- **are exactly the two this project already treats as special cases.**
That is a stronger position than "we win at state management", and it is
worth stating as the goal it is.

What the matrix does *not* answer, and what has to be decided rather than
measured: which rows this project should own at all, and which belong to
a GUI that nobody here maintains. Every row marked 0.4.0 is a row where
the honest answer today is "turn it in the GUI, and hope nothing resets
it" -- which is the same answer TotalMix gives, minus the snapshot.

## Where we are (0.6.11)

*What came before -- the status of every release back to 0.6.0, and the
chapters for 0.2.0 through 0.5.0 as they were planned and closed -- is
in [docs/history/](history/). This file keeps what is still ahead.*

**0.6.11 (2026-09-19)** is what two outside reviews of 0.6.10 named,
measured before anything changed. One finding was a defect: the receive
port's `listen()` reported every failure to bind as "the mixer GUI has
it", so a port that could never be bound -- `recv-port = 80`, `EACCES`
-- left the desk unverified for good under a message that named the
wrong cause. The review's remedy, re-raising, would have torn an apply
between its phases; the barrier waits blind instead and the verifier
fails out loud once the mix is safe (ADR 0025). With it: an outcome
nobody could read back is worded as one, routes on an unmodelled device
warn, the sink search survives `"info": null`, and one table decides
what a re-read desk may not change. Reviewing that warning found the
release's second defect, older than the review: a profile was validated
against the default device rather than the desk's, so on a desk for
another interface its channel sections reached hardware the main config
is not allowed to touch. Two more rounds of the same review stayed on
that seam -- a desk is validated for the device its file names, and
`--device` or a reload under a running session can replace the name
afterwards. `--device` says so now, a switch refuses the overrides it
never honoured, and a dry run shows the desk as the switch loads it;
validating *for* the replacement is what the frozen `Config` of 0.7.0
is for. A second outside review, of 0.6.10, then named the rest of the
seam: a profile may state its own port and serial, the switch wrote it
to that backend, and the reload the switch sent made the unit write the
same desk to its own -- one persisted profile, three targets, and two
device locks over one marker. 0.6.11 stops the wrong writes -- a
running session does not apply a re-read desk that is for somewhere
else -- and decides the question: a profile is the desk, not the
machine (ADR 0026). What "for somewhere else" means took the longest:
a re-read file is resolved the way a restart would resolve it, the
command line's overrides included, and only then held against what the
session runs. Mutation 0.794 on 7485 mutants with the floor at 0.78,
coverage 97%.

### Planned: 0.7.0

Decided, and recorded so that none of it is found twice. No new surface:
nothing is added to what a `routing.conf` can declare.

1. **Files a person can read, first and alone.** *Done, with no change
   in behaviour.* Six source modules were over 600 lines, up to 1038;
   none is now, the largest being `verify` at 576. Split at seams that
   were there: `devices` out of `registers`; `locking`, `marker` and
   `outcome` out of `profiles`, which keeps the order of a switch;
   `model`, `paths`, `sections` and `notices` out of `config`; `dump`
   out of `reconcile`; `reload` out of `session`; `reads` out of `cli`.
   The test files follow their subjects instead of the order the
   reviews arrived in -- the largest was 1869 lines, the largest now is
   the subprocess integration suite at 632 -- and this file and the
   changelog keep what is ahead and what changed lately, with the rest
   in `docs/history/`. Alone, because the suite isolates itself by
   patching module attributes and a moved function silently unhooks a
   patch: `monkeypatch.setattr` on one of this project's modules now
   fails when nothing in that module reads the name, which caught the
   suite's own lock-directory isolation the moment the lock moved.
2. **A profile is the desk** (ADR 0026): a profile that resolves to
   another machine than its `routing.conf` is a `ConfigError`. One that
   restates the same values, as a dumped profile does until its
   `routing.conf` changes them, stays accepted.
3. **Tighter types** (first review): `Phase` as `IntEnum`, `WriteReason`
   and the register domains, policies and verification classes as enums
   where they steer control flow; one named result for "the receive port
   is unavailable" instead of `None` through `listen`,
   `await_link_echo` and `verify_routing`; `RegisterValue` instead of
   `value: object` (most of the 24 `type: ignore`). The floor is Python
   3.9: no `StrEnum`, and `format()` of a `(str, Enum)` changed in 3.12,
   so output uses `.value`.
4. **A frozen `Config` from a builder**: parse, validate, freeze. The
   parser knows the overrides, so `--device` is validated *for* rather
   than warned about, and `_find_client` stops writing the pinned serial
   into its argument.
5. **Outcome as a result type**: `durable`, whether the unit was told,
   whether the target was confirmed -- fields today, states then.
6. **What a third outside review of 0.6.11 named**, checked against the
   tree first. *Done:* a switch whose write gives out part of the way
   says how far it came instead of raising (ADR 0027, a fourth outcome);
   a receive port that cannot be read is an error and not a quiet
   backend, which also ends a busy loop nobody had seen (ADR 0025,
   amended); the oracle builds its own messages and both sides are held
   to literals. One finding was already fixed at the release it
   reviewed (the reconciler's stale module note). *Still ahead, and last
   on purpose:* the package root exports 77 names, most of them
   internals; the supported surface is declared once the types below
   have changed the names it would list, so that it is touched once.
7. **Smaller, each with its reason:** refuse the stale-backend cleanup
   when `pidfd_open` is unavailable instead of falling back to
   `os.kill`; one retry of a reconcile that was skipped because the GUI
   held the port, on the next trigger and never on a timer (ADR 0013);
   a strict/best-effort policy for unmodelled devices and a firmware
   range the evidence vouches for (amends ADR 0006); multi-process tests
   over two targets, the marker, SIGHUP/SIGTERM and fsync faults;
   `--dry-run` printing the planned writes without the device; optional
   `[project]` metadata, with `install.sh` staying the installer (ADR
   0004; `pip install --user` is refused on the target platform, PEP
   668).

Considered and not planned: an apply journal. Every start is a full
apply already, so a dirty flag adds nothing there; the one gap is a
switch killed mid-apply under a running unit, which the next trigger
repairs. A config-state lock beside the device lock: unnecessary once a
profile cannot name a backend. The review's larger half -- enums
for phases and write reasons, a result type for the barrier, a frozen
`Config`, a decision about strict handling of unmodelled devices --
changes public names and is 0.7.0. Its packaging proposal is declined
as a requirement: ADR 0004 stands, and on the target platform `pip
install --user` into the system Python is refused (PEP 668).

## Decisions that are free now and expensive later

**Draft, 2026-08-16. None of these is decided.** They are here for the
reason item **B** turned out to be right: each one costs a paragraph
today and a migration after 0.3.0. Item B was the only one of the twelve
that was a *promise* rather than a task, and it was the one that would
have been unfixable a release later.

### Two writers, one device

*Today:* this project writes the routing at start, and through the
verifier for up to a minute after. `oscmix-gtk` writes whenever the user
turns something. Neither knows the other exists. The device takes the
last write, and nothing anywhere states who is supposed to win.

That stays invisible for one reason: this project writes at start and
then stops. `routing.conf` is not a *desired state* that is maintained,
it is an *initial state* that is applied. The difference does not show at
six registers wide.

*What makes it visible:* the pin/remember model in 0.3.0. "`48v` wants
pinning" is a statement about what happens **after** start, when
something else has changed it. With a start-only writer, `pin` means "set
once, then hope" -- which is not what the word says, and is not more than
TotalMix already offers.

*Position two, taken in 0.3.0.* SIGHUP (`systemctl --user reload`) and a
system-sleep hook for resume -- **measured 2026-08-20**: across a real S3
cycle the interface never leaves the USB bus and all 1932 reported
registers survive, so the hook has nothing to repair on this machine, and
it does fire correctly, reconciling a pinned fader back from -22.0 dB.
ADR 0013 has the numbers, and why `rtcwake -m mem` cannot test it.
Hotplug needed nothing, because udev
already restarts the unit and the cold-plug recording says so. No timer,
asserted by test. [ADR 0013](decisions/0013-reconcile-triggers.md).

*Hit for real, 2026-08-27, from an unexpected side:* the second writer
was not the GUI. Verifying the output-phase fix for upstream PR #36 ran
a second (briefly a third) `alsaseqio`+`oscmix` pair against the live
device around midnight; the service backend died four times in 80
seconds while the clients overlapped (SIGPIPE cascade, the supervisor
restarted it each time) and never once during the later single-client
runs. Diagnostic clients count as writers too: one at a time.

*Three positions, in increasing cost:*

- **Start-only, stated.** Applied at every start, the GUI always wins
  afterwards. Cheapest and perfectly honest -- but then the option is
  not called `pin`.
- **Reconcile on a signal.** Re-apply on hotplug, on resume, on a sample
  rate change, on `SIGHUP`. Never on a timer. Bounded, explainable, and
  it covers the cases where state is actually lost.
- **Continuously reconcile.** The device snaps back within a second of
  any GUI change. Maximally declarative, and the point where the two
  writers genuinely fight: a user watching a knob undo itself files a
  bug, not a compliment.

*Measured, 0.3.0, and the answer is mostly no.* Only
`/output/{ch}/stereo` is pushed when it changes; every other register a
config can set is silent until a `/refresh`. The reason is visible in
upstream: `wfd` in main.c is one socket on a fixed address, and state
comes back only from device echoes over MIDI, which this device sends
for the link flags and not for faders, mutes, reference levels or gains.

That rules out position three. It leaves position two intact -- one
dump per event is cheap -- and it is why 0.3.0's `pin` is defined as
"the config wins while this session is still looking" rather than as
"snaps back", which nothing here could honestly deliver.

*The original phrasing, kept because it is the right question:* does a
GUI-initiated change show up as a report on the receive port? The device
reports on change, so it should -- but this is one measurement, not an
argument.

It cannot be done with `scripts/record-dump.py`, which binds UDP 8222 and
therefore cannot run while the GUI holds it. Sniff the loopback instead,
which is how the three 0.1.3 defects were found in the first place:

    sudo tcpdump -i lo -n -s 0 -U -w gui.pcap 'udp port 8222'
    tshark -r gui.pcap -T fields -e udp.payload -Y udp.dstport==8222

Turn one fader in oscmix-gtk and the answer is in the capture. If the
reports arrive, position two is cheap and position three is possible at
all; if they do not, only position one is honest.

### Sample rate and clock changes destroy state

*Today:* nothing in this repository says what happens to the mixer when
the clock source or the sample rate changes. 0.4.0 lists "clock source"
as a *feature to declare*, which is a different thing from the *event* it
is.

*Why it belongs here:* a sample rate change is the routine way a Fireface
loses mixer state in ordinary use -- more routine than a reboot, which
this project handles, and more routine than a hotplug, which it also
handles. A project whose premise is "the state survives" has an
unexamined hole exactly where the state does not survive.

*Measured, 0.3.0, and the premise above is wrong for this device.* A
48 kHz -> 44.1 kHz change destroys nothing:

- **1931 of 1932 reported registers were byte-identical across the
  change.** The one that differed was `/clock/samplerate` itself.
- **The playback mix matrix survived too**, and that had to be shown by
  signal rather than by dump, because the matrix is never reported: a
  1 kHz tone at -40 dBFS into playback 1/2 came out at outputs 1, 5 and
  7 afterwards, at the levels `routing.conf` routes it to.
- **`/clock/samplerate` is pushed when it changes.** Ten seconds of
  genuinely quiet observation -- no `/refresh` anywhere near the window
  -- then the change, then exactly one datagram: `/clock/samplerate
  (44100,)` at t=11.16 s. So a rate change *can* be a trigger without
  polling, unlike every other register a config sets.

Which leaves the trigger with nothing to do. It is buildable and cheap,
and there is no measured loss for it to repair. **Not built**, on that
basis, and this paragraph is the reason rather than an omission.

Two false starts are worth recording, because both produced confident
wrong answers first. Watching while `pw-metadata` was written saw
nothing: the device only re-opens when a stream starts, so the window
held the *request* and not the change. And a tone that lit no meter at
all looked like a destroyed matrix until the levels turned out to be
reported in dB -- `-inf`, against a comparison seeded at `0.0` -- with
the audio going to the 20-channel `Direct` sink rather than through the
named one. Neither zero meant what it looked like.

*A first step that needs no decision, and now cheaper than it looked:*
notice it and log it. The register is pushed, so a session that already
holds the receive port sees the change arrive -- no poll, no timer. That
turns a future "the routing was gone after I switched to 96k" into a
readable journal entry, and it is the honest amount to build for a loss
nobody has observed.

*Still unmeasured:* rates above 48 kHz. The UCX II halves its channel
count at 88.2/96 kHz and quarters it at 176.4/192 kHz, so the register
*model* changes shape there, which is a different question from whether
state survives -- and one the channel map would have to answer first.

### More than one Fireface

*Today:* the udev rule, the unit and `routing.conf` are singletons. Two
interfaces on one host is not supported, and not refused either. It is
undefined, which is the worse of the two.

*Why the timing matters:* the answer is a template unit
(`oscmix@.service`) with the device instance as `%i` and the config path
derived from it. Today that is a rename and a path change. After profiles
land it is a rename, a path change, a config schema change and a
migration for every existing user.

*The decision is not "implement it".* It is whether the single-device
assumption is **stated as a limit** in the README and the config, or
**designed out** while it is still a rename. Both are defensible.
Silence is not, and silence is what ships today.

### The upgrade path is untested, in the release that moves everything

*Today:* 0.2.0 moves the runtime from `lib/` to `src/` and rewrites that
path in three places. `install.sh` is now smoke-tested end to end -- but
into an *empty* `HOME`. Nobody has run it over an existing 0.1.3 install,
which is what every current user will do exactly once.

*The specific risks, none of them confirmed:* a stale `lib/` tree left
behind and still resolving first; a running unit that keeps the old code
until it is restarted rather than reloaded; and a `routing.conf` written
against 0.1.3 now read under ADR 0006's rules.

*Target:* one more case in the install smoke test -- install `v0.1.3`,
install this revision over it, then assert what the empty-`HOME` case
asserts. It is the cheapest test in the release, and it covers the one
path taken by everybody who already has this installed.

## Upstream is part of the quality goal, not the weather

Three of the constraints below are upstream limits: the playback matrix
cannot be read back, the register cache does not self-synchronise, and a
dump has to be waited out at all. The ceiling on "provably correct" is
therefore set by code this project does not own. Treating that as given
would cap the whole effort.

There is a fourth, and its history is the useful part. The Room EQ
registers were carried as reporting implausible values, then withdrawn
when a measurement showed all 220 gains at 0.0 dB, then filed after all
as [#32](https://github.com/michaelforney/oscmix/issues/32) once the
mechanism turned up: the block is folded onto its own lower half, so a
reader that keeps the first reported value sees zeros and one that keeps
the last sees the +30 dB the original note described. Both observations
were half of a double report.

[docs/upstream-issues.md](upstream-issues.md) keeps the withdrawal and
the resolution side by side, because being wrong in both directions
about one register block is more instructive than either.

So, as work items rather than complaints:

- **Watch for upstream taking on a mirror state.** Not a work item of
  ours and not a promise of his -- but on 2026-08-21, replying on #30,
  the maintainer wrote that oscmix was built to hold as little state as
  possible and use the device as the source of truth, and that "it seems
  this isn't always possible, so maybe oscmix needs keep its own
  complete mirror state."

  If that happens it is the ground under several things here. The
  stereo-link race exists precisely because oscmix does *not* track the
  flag it just wrote; `LINK_ECHO_TIMEOUT`, `LINK_SETTLE` and
  `LINK_SYNC_BLIND_DELAY` are all workarounds for that one fact, and
  `patches/0001` is the narrow version of the same fix. The measurement
  that only `/output/{ch}/stereo` is pushed on change -- which is what
  made "pin" mean "the config wins while this session is looking" rather
  than "snaps back" -- is a statement about a backend that keeps no
  mirror.

  So: nothing to do now, and nothing to plan around. What it changes is
  what a future measurement could show, and ADR 0008 already fixes the
  order for that -- bump the pin, measure on hardware, *then* remove
  what the measurement made unnecessary. Worth watching rather than
  waiting for.

- ~~Offer the cache-synchronisation patch.~~ **Offered as
  [michaelforney/oscmix#31](https://github.com/michaelforney/oscmix/pull/31)**
  on 2026-08-17, 28 lines, nothing changed on the wire. `setbool` not
  updating oscmix's own view is the single root cause of
  `LINK_ECHO_TIMEOUT`, `LINK_SETTLE` and `LINK_SYNC_BLIND_DELAY`, and
  the patch makes the output side consistent with `setinputstereo()`,
  which already updates on write. Measured at the point that reads the
  flag (`patches/README.md`): unpatched `setlevel` sees `stereo=0` on a
  pair that was just linked, patched it sees `1`.

  *If it is accepted, the order is fixed by
  [ADR 0008](decisions/0008-pinned-upstream-revision.md): bump the pin,
  measure on hardware, **then** delete the three constants. Not before —
  that would remove the workaround for a fix this project has not yet
  shipped against.*
- ~~File the `unexpected enum value -1` issue.~~ **Filed as
  [michaelforney/oscmix#30](https://github.com/michaelforney/oscmix/issues/30)**
  on 2026-08-17. Traced to
  `/controlroom/mainout`, which this device reports as `-1` -- outside
  the ten names `CTLROOM_MAINOUT` declares, so `oscsendenum()` takes its
  fallback branch and sends `,i` instead of `,is`. 42 occurrences in 24 h
  of ordinary use. The half that is a bug regardless of what `-1` means:
  the diagnostic does not print the address, so it says only `unexpected
  enum value -1` and cannot be acted on. Drafted in
  [docs/upstream-issues.md](upstream-issues.md).
- ~~The Room EQ issue.~~ **Filed as
  [michaelforney/oscmix#32](https://github.com/michaelforney/oscmix/issues/32)
  on 2026-08-20, with the mechanism and a before/after measurement;
  `patches/0002` carries the one-line fix.** It was
  filed as "all 220 gain registers read 0.0 dB", withdrawn as not
  reproducing, and that was the right call on the evidence at the time.
  Measured properly on 2026-08-20, after the release:

  A single `/refresh` reports **460 registers more than once, and 260 of
  those with conflicting values -- every one of them `roomeq`.** Nothing
  else in the dump does it. `/output/1/roomeq/band1gain` arrives as both
  `0.0` and `30.0`; `band1type` as both `Low Shelf` and `Peak`. Reading
  the same register four times in a row gives 0.0 once and 0.7 three
  times.

  So the family is double-reported and whichever value a reader sees is
  down to arrival order. "All zero" was one of the two answers, not a
  wrong reading -- which is why it did not reproduce.

  *Not urgent for this project:* `roomeq` is not in the register model
  and nothing here sets it. It matters for 0.4.0, which plans to declare
  it, and it matters now as a *method* note -- `--dump-config` keeps the
  first value it sees for a path, which is correct only as long as no
  settable register behaves this way. None does today, and that is
  measured rather than assumed.
- **Ask for a targeted register query.** `/refresh` dumps 2252 registers
  when what this project needs is a handful of `/output/<n>/stereo`.
  That is a feature request, not a benchmark -- see the reframing of
  point 7 above. **Worth less than this document assumed:** the ask was
  sized against a 15-20 s dump, and the measurement says 1.9 s on a warm
  device. Settle the cold-hotplug case first (the finding under Still
  open); if the dump is fast there too, this is a tidiness request rather
  than a fix, and the cache-synchronisation patch above is the one that
  earns its keep.

## Explicit non-goals

- **A mixer GUI.** That is [oscmix-gtk][oscmix].
- **Metering and DSP.** Upstream's ground.
- **DURec transport control.** Interactive by nature; a config file is the
  wrong shape.
- **Matching TotalMix FX feature for feature *in this repository*.** A
  knob to turn is a GUI's job; this project's job is the state behind it.

  That is a division of labour, **not a ceiling on the stack**. The
  goal for oscmix + oscmix-gtk + this project together is to be at least
  as complete as TotalMix FX and better where being declarative and
  verifiable wins -- see [the bar is the
  stack](#the-bar-is-the-stack-not-this-repository), which puts numbers
  on how much of the device surface is already within reach. Every row
  in that matrix has to land somewhere; a non-goal here is a statement
  about *which component owns it*, and it is only honest as long as some
  component does.

## Known constraints and upstream issues

All measured, all things the design has to live with:

- **The playback mix matrix cannot be read back.** A `/mix` write draws no
  reply and the dump omits `/mix/*/playback/*`. It can only be
  re-established from a known link state, never verified. Input routing
  does not share this limitation.
- **oscmix does not sync its register cache on its own.** It learns the
  device's values only from a `/refresh` dump. How long that takes is
  **measured at 1.9 s** for the 2252 registers a dump reports (2322 with
  the streamed meters) on a UCX II whose backend was restarted
  (`tests/data/refresh-dump.json`), against the ~15-20 s this document
  asserted from an earlier, unrecorded observation. The cold device
  after a replug has since been measured too
  (`tests/data/cold-plug-timeline.json`, both OSC ports): the link
  registers come back **0.01 s after the `/refresh`**, the dump is over
  in ~4 s, and nothing follows for the next 272 s.
  `LINK_SYNC_BLIND_DELAY` is **5 s** on that evidence -- see ADR 0010,
  which also fixes the shape of the mistake: a constant whose
  justification is a sentence rather than a file in `tests/data/`.
- **The device reports a register only when it changes.** Writing a value
  it already holds produces no report, so "wait for the echo" cannot be
  the only synchronisation mechanism.
- **A write is never echoed on the path it was written to**, which is
  the stronger form of the line above and was measured while building
  the 0.5.0 sweep. A stereo-linked pair answers on the *partner* path
  carrying the value written (`/output/3/volume` reports
  `/output/4/volume`), and a register with no linked partner draws no
  reply at all (`/input/1/gain`) even though a dump shows the value
  landed. So a write is confirmed by re-reading a `/refresh` dump,
  never by waiting on the path just written.
- **Bursting writes loses them.** oscmix turns each into a MIDI SysEx
  message, and that wire carries roughly a thousand registers a second
  -- the measured rate of a refresh dump. 295 writes in a few
  milliseconds lost 40 of them; the same registers take the value when
  written one at a time. Bulk writes have to be paced
  (`WRITE_PACE` in `scripts/sweep-writes.py`).
- **An out-of-range write is refused, not clamped.** `/reverb/width =
  1.02` on a 0..1 register leaves the value where it was and reports
  nothing, so a config asking for it would be ignored in silence. This
  is why the three registers upstream leaves unbounded were bracketed
  against the hardware rather than left open.
- **`unexpected enum value -1`** on every start (42 times in 24 h),
  from `/controlroom/mainout`: the device reports `-1`, which is outside
  the ten values `CTLROOM_MAINOUT` names, most likely meaning the
  Control Room main output is unassigned. Harmless noise here, but the
  message names no register, which is why it took a full state dump to
  attribute. Upstream; drafted in
  [docs/upstream-issues.md](upstream-issues.md).
- **Only the UCX II is tested.** The 802 path is untested and always has
  been.

[oscmix]: https://github.com/michaelforney/oscmix
