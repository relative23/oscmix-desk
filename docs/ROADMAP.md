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

**0.6.11 (unreleased)** is what an outside review of 0.6.10 named,
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
afterwards. Both say so now, a switch refuses the overrides it never
honoured, and a dry run shows the desk as the switch loads it;
validating *for* the replacement is what the frozen `Config` of 0.7.0
is for. The review's larger half -- enums
for phases and write reasons, a result type for the barrier, a frozen
`Config`, a decision about strict handling of unmodelled devices --
changes public names and is 0.7.0. Its packaging proposal is declined
as a requirement: ADR 0004 stands, and on the target platform `pip
install --user` into the system Python is refused (PEP 668).

**0.6.10 (2026-09-17)** is what a full check of the released 0.6.9
found -- gates, the live desk, and independent reviews of the whole
tree rather than of a diff. Two behaviours were wrong: a session started
by hand terminated the running unit's backend, and a switch of another
desk -- named by `--config`, or by the shell's own environment -- asked
the unit to reconcile its own desk over it; the unit's desk is now
worked out the way the unit worked it out, from its own command line
and environment. Behind those, a layer of smaller defects that had never
been exercised: only three of twenty-one action pairs were refused, a
config read differently depending on the order of its sections, Ctrl-C
before the backend ran was a traceback, a profile marker that was not
UTF-8 raised on every start, the launcher polled the wrong port while a
profile with its own `[osc]` was active, a relative `XDG_CONFIG_HOME`
sent the installer's config into the working directory, and the test
suite read the developer's own desk and, run as root, reached the
machine's lock directory. The rest is documentation that described an
earlier release -- the lock's location and ownership in the unit and
the installer, a `StopWhenUnneeded` that never fires on an enabled
unit, an upstream record that still called a fix the pin has carried
since 0.6.0 open, ten decision records now carrying an amendment for
what later releases changed. Nothing in the routing moved.

**0.6.9 (2026-09-16)** is what a review of the 0.6.8 lock found, in the
order a second reviewer gave it. The interface was worked out four
times -- the service bound the first matching client and pinned the first
serial of the card list, a switch keyed on a rule of its own, and the OSC
port was taken from whoever held it -- and with two identical boxes the
four disagreed. One resolution answers all of it now, `[device] serial`
selects the box, and more than one candidate without it is refused
rather than guessed (ADR 0024). A switch writes only to the backend that
bridges that interface; a start without the lock fails instead of
reporting ready; the lock file is opened without following links or
blocking on a FIFO; and `/run/oscmix-desk` belongs to the group `audio`
instead of every local user. The review's architecture test -- two
identical UCX II, the desk for the second, every path on its lock and
its backend -- is in the suite. A last sweep before release found the
rest on the same path: the unit could not have written the lock
directory wherever its sandbox actually applies (Ubuntu's user manager
silently skips it, which is why the desk here never showed it), the start
budget left out the lock wait, a reload could change the interface under
a running process, and a scratch-home uninstall reached for the
machine's system files. The security model now says which hardening
applies on which system. An independent review of that result, run
before release rather than after it, found nine more -- among them a
backend check made before a 30 s lock wait and never after it, a process
name that crashed every switch, and a Fireface of another model that
made a UCX II ambiguous -- and each is a test that fails on the commit
before the fix.

**0.6.8 (2026-09-14)** is what an adversarial review of the 0.6.7 lock
found: the key named the hardware, but the *path* to the lock still
depended on the caller. `$XDG_RUNTIME_DIR` is per user and absent from
`sudo`, `cron` and a bare `ssh` command, so a writer without it computed
a different file and walked past the holder -- measured on the desk, two
seconds to write a full routing while a holder held. It lives in
`/run/oscmix-desk/` now, created by the installer through `tmpfiles.d`,
the one path that depends on neither the environment nor the user nor a
config directory (ADR 0023). Three more followed from the same review:
the key was recomputed on every write and moved when the interface was
unplugged, so a reconcile in that window took a second lock file; two
identical boxes shared the first one's name, and `[device] serial` now
names one outright; and a switch to an absent interface reported
`applied`, exited 0 and recorded a marker for a desk that had never been
written -- it refuses now. The write sweep, the loudest writer here,
finally takes the lock too.

**0.6.7 (2026-09-14)** is what a sixth outside review of 0.6.6 led to,
and it is mostly about what the lock actually promised. It is keyed by
the interface now, `<usb id>-<serial>` in `$XDG_RUNTIME_DIR/oscmix-desk/`
rather than one file per config directory, so two desks over one device
contend as they must; and a writer that cannot hold it no longer writes,
where the start used to apply anyway and make the whole guarantee
conditional on nothing going wrong (ADR 0022). Two defects in the start
path went with it: a bound port counted as readiness even when its owner
could not be resolved, and a backend that exited before binding still
collected READY through the exit mapping. The pin does not move; the
register table has no new row.

**0.6.6 (2026-09-13)** is what a fifth outside review of 0.6.5 led to.
The lock serialised the writes but not the decision of what to write,
so a switch committed while the unit was starting or reconciling could
be overwritten by an older snapshot; both writers read the desk inside
the lock now (ADR 0020). Three paths reported success for work that did
not happen: a backend that never bound its port still reached `READY=1`,
a reconcile that stood down still said `reconciled`, and a refused
reload was reported as a stopped unit. The stale cleanup signalled by
process name rather than by socket ownership (ADR 0021). The pin does
not move; the register table has no new row.
**0.6.5 (2026-09-12)** closes roadmap item G: every writer of the
device takes one lock, the unit included, so a switch and the unit's
own apply, verifier and reconcile can no longer overlap. The phase
polling it replaces is deleted. A switch whose marker could not be
written no longer reloads the unit -- measured before the fix as a
profile undone two seconds after it was reported as applied. ADR 0019.
**0.6.4 (2026-09-11)** is what the third outside review of the profile
work led to: two switches at once are serialised by a lock beside the
marker, the marker is written atomically, and a switch waits while the
unit reports that it is writing the device -- the third found next to
the first two, because with the mixer GUI holding the receive port the
0.6.3 reload cannot reconcile. Measured on the desk: two shells
switching at once, and a switch during the start-up verifier with the
port held. One overlap is left and is item G: a second switch started
at the same moment can still write while the unit reconciles for the
first -- the end state is right, the guarantee in between is not. The
pin does not move; the register table has no new row.
**0.6.3 (2026-09-11)** closes roadmap item F: a profile is remembered
beside `routing.conf` and applied on every start and reload (ADR 0018),
and a switch sent right after a start is no longer reverted by the
start-up verifier -- measured on the desk before and after, the second
member of the race family 0.6.2 found the first of. The unit reports
its phase to systemd, the register table alone decides what the device
ever reports, and the README says why one measured device is the
method. **0.6.2 (2026-09-10)** is what an outside review of 0.6.1 led to: the
stale claim in `reconcile.py` it named, and what an audit found next to
it -- channel sections silently dropped on a device without a register
table, a reload that could write routing while the start-up verifier
still was (measured on the desk, before and after), three functions
nothing called, a backend trait nothing read, and evidence artifacts
that never said which firmware they were taken against. The pin does
not move; the register table has no new row. **0.6.1 (2026-09-05)** fixes two defects an audit of the tree found:
a config declaring channel or global state and no route was never
applied at start -- a `config.routes` guard from 0.1.0 that outlived
the surface it was written for, while `--dry-run` printed the writes
and a SIGHUP reload sent them -- and one integration test reached the
developer's real `systemctl` and started their user service. Neither
moves the pin. **0.6.0 was tagged on 2026-08-28**: the pin moved to `f2fdd5e` and
three families became settable -- Room EQ, output phase, gain on
Analog 5-8 -- each fixed upstream and measured here, with the write
sweep grown to 1902 proven registers. **0.5.2 was tagged on 2026-08-27**: the resolve-order fix, after a boot
ran a February build from a pre-rename install for six hours -- closed
for the backend pair and the GUI alike, with the corrections its second
review demanded. 0.5.1 (2026-08-26) was the cleanup the 0.5.0 audit
left behind. **0.5.0 was tagged and published on 2026-08-25.** The surface did not
grow; the release proved the half of it nobody had asked the device
about. Every settable register was written a different legal value and
confirmed against the device's own report: **1224 of 1238 confirmed, 14
skipped as dangerous, none deaf**, per-register verdicts in
[docs/evidence/write-sweep-ucx2.json](evidence/write-sweep-ucx2.json).

0.4.0 (2026-08-24) had finished the surface itself, and renamed the
project from `oscmix-autostart` to `oscmix-desk`: the old name described
0.1.0 exactly and named one of two halves by the end.

All eleven register families were declared and measured at the device.
Room EQ was declared **reported and not settable**, because the writes
were ignored (upstream #33 -- **fixed upstream 2026-08-27** in
`f2fdd5e`, a split write range at `0x3400`, confirmed here at the
device), and from 0.5.0 on `/output/{ch}/phase` the same, because the
writes never left oscmix (upstream #34 -- **fixed by our PR #36, merged 2026-08-27**
as `9dba36f`). The write sweep added
`/input/5..8/gain` to that class: upstream's channel table gave those
inputs a gain flag and no range, so every write was clamped to zero --
**fixed upstream on 2026-08-27** (`fdc47f7`, `gain={0, 240}`, measured
here: 12.0 dB written and read back). The pin predated all three fixes,
so the classes waited for the pin to move (ADR 0008,
upstream [#35](https://github.com/michaelforney/oscmix/issues/35)); 0.6.0
moved it to `f2fdd5e`, and all three have been settable since.

The five structural threats in [After
0.4.0](#after-040-what-actually-threatens-this) are all addressed, and
the write direction is proven. **There is no planned next release.** The ALSA-pool question that held
this spot is answered, and the answer is no. Measured 2026-08-28: under
a 10-dump `/refresh` flood -- idle, under full-core spin load, under
the suite, and under four parallel suites (the mutation gate's shape)
-- both the service's client and a fresh one peak at 96 of 200 cells
with zero allocation failures, on ~49,000 events per window. The
historical `Alloc failures` are rare scheduling events inside
~90-minute full-load windows; every register-completeness check ever
taken here came back complete. A pool-sizing patch upstream would be a
fix without a reproducible defect, which is the kind of report this
project does not file. A patched `alsaseqio` (input pool 2000) exists
on a local branch, unneeded until a measurement says otherwise.
What remains upstream is only PR #31 (output stereo) and an 802 this
project cannot test: in one day, 2026-08-27, the maintainer fixed the
gain ranges (#35) and the Room EQ write range (#33), and merged our
output-phase fix (#36) -- every one confirmed at this device, and
carried by the pin since 0.6.0.

Working and verified: playback→output and **hardware input** routing for
mono and stereo pairs, stereo linking with the ordering that requires,
output faders, per-channel state (`gain`, `reflevel`, `hi-z`, `mute`,
`phase`, `volume`), EQ on every input and output, the five channel-less
families (`[clock]`, `[controlroom]`, `[echo]`, `[hardware]`,
`[reverb]`), profiles, PipeWire named sinks, hotplug autostart,
readiness signalling, routing read-back, `--dump-config` over every
declared register the device reports (the playback matrix is not one of
them -- ADR 0002), and reconcile on SIGHUP and resume.

Three release-blocking defects in 0.1.3 were found by measuring output
levels off the wire, not by reading code. Two of them were invisible at
message level. That set the standard every release since has been held
to, and it is why the table below is measurements rather than claims.

What 0.3.0 moved (2026-08-20):

| | 0.2.0 | 0.3.0 |
|---|---|---|
| runtime | 15 modules, 2119 lines | 19 modules, 4480 lines |
| longest function | 66 lines | 68, under the 70 ceiling |
| tests | 392 cases from 281 functions | 663 cases from 470 functions |
| coverage | 94%, gate at 94 | 95%, gate at 94 |
| mutation score | 0.643, floor 0.63 | 0.687, floor 0.67 (`not_covered` 82, unchanged) |
| config surface | routes and faders | + `[input:N]`, `[output:N]`, `[pin]`, `profiles/` |
| register model | none | 18 families, 9 settable, per-device channel maps |
| decisions recorded | ADR 0001-0010 | ADR 0001-0013 |
| upstream pin | 2411b12d | unchanged -- no bump, so no new evidence owed |

As of 0.5.1, on `main`: 5780 runtime lines, 990 test cases, 149
register rows of which 102 are settable (1238 concrete settable paths),
mutation 0.710 with the floor at 0.70, coverage 95% gated at 95, and
ADR 0001-0017. The table above is the released 0.3.0 record and stays
as it was measured.

**What the release is, in one line:** 0.2.0 made the existing behaviour
provable; 0.3.0 spends that on surface, and every piece of it was
decided by a measurement rather than by a plan.

Four of those measurements changed what got built:

- Of every register a config can set, only `/output/{ch}/stereo` is
  pushed when it changes. That killed continuous reconciliation and
  turned "pin" into a promise this project can actually keep.
- A cold plug delivers 1234 of 1932 registers and then stops. That
  shaped which absences count as faults.
- A sample rate change destroys nothing here -- 1931 of 1932 registers
  identical, matrix intact by signal. A planned trigger was **dropped**
  on the strength of it.
- Hotplug was already covered by udev restarting the unit, so the
  second mechanism for it was never written.

And three defects were found in the path every boot already ran, none of
them by a failing gate: channel state written and then left out of the
read-back, `/playback/*` misclassified as never-reported, and an
observation window that closed before channel state could arrive.

## 0.2.0 -- maturity

No new device features. This release is about making the existing
behaviour **provably** correct and cheap to change, because the feature
work in 0.3.0 multiplies the surface by roughly ten and the current
structure will not carry it.

Each item states what is true today, what has to become true, and how
that is proven rather than asserted.

### 1. Architecture and structure -- **done**

*Was:* one 1386-line script holding the OSC codec, config parsing,
routing translation, verification, process supervision, PipeWire
generation and the CLI. `run_session` is 106 lines and does device
discovery, process launch, signal handling, readiness and supervision.

*Target:* a package -- `osc`, `config`, `routing`, `verify`, `supervise`,
`pipewire`, `cli` -- with `bin/oscmix-session` as a thin entry point.

*The constraint this must not break:* the runtime uses nothing outside
the standard library and runs from a checkout without an install step.
That property is why the file is monolithic today, and it is worth more
than tidiness -- it is what makes the thing work on a bare system. So the
package ships alongside the executable rather than replacing it, and an
architecture test enforces the guarantee instead of a comment claiming it.

*Proven by:* an architecture test asserting no third-party import in the
runtime package, no import cycles, a declared layering (config must not
import supervise, routing must not import cli), and a function-length
ceiling. Mechanical, so it cannot rot.

### 2. Contracts -- **done**

*Was:* the invariants exist in comments and in a few tests that happen
to cover them. Nothing states them as contracts.

*Target:* written and machine-checked:

- `route_messages == link_messages + mix_messages` *(already tested)*
- a route writes **exactly** the registers it declares -- the rule the
  `volume` bug taught us -- expressed as: written paths ⊆ declared paths
- exit codes: 0 = device absent or clean stop, 1 = runtime failure
  (restart), 2 = config error (no restart)
- readiness: `READY=1` is sent exactly once on every exit that returns 0
- OSC codec: `decode(encode(x)) == x` for every representable message
- config: parsing is total -- every input either yields a `Config` or a
  `ConfigError` naming the section and option, never a traceback

*Proven by:* property-based tests (hypothesis) for the codec and the
config parser, and a dedicated contract test module. Fuzzing the OSC
decoder against malformed datagrams belongs here too: it parses data off
a socket and currently trusts its own encoder.

### 3. Testability -- **done**

*Was:* 118 tests, but `run_session`, `supervise` and
`wait_for_seq_client` are exercised only through a subprocess, which
coverage does not follow. The 71% figure for the session is therefore
both understated and unearned in places. `_cleanup_stale_backend` -- which
sends `SIGTERM` to PIDs it selected -- has no direct test at all.

*Target:* subprocess coverage via `COVERAGE_PROCESS_START` so the number
is honest; direct tests for every function that makes a decision;
`_cleanup_stale_backend` and `resolve_binary` covered including their
refusal paths.

*Proven by:* a raised coverage ratchet on real numbers, and a policy test
that every public function in the runtime package is named by at least
one test.

*Closed late:* the ratchet sat at 84 while the suite measured 91, and
`bin/oscmix-launch` sat outside the package, the architecture test and
the mutation scope. Both are fixed: the launcher is
`src/oscmix_desk/launcher.py` at 100%, and the gate is 94 against a
measured 94 -- no margin left to erode unnoticed.

### 4. Provability -- **done**

*Was:* the only proof that audio actually reaches both channels is that
I measured it by hand with `tcpdump` and a test tone. Nobody else can
reproduce that, and nothing stops it regressing.

*Target:* `make verify-hardware` -- a committed tool that plays a known
signal, reads `/output/<n>/level` back off the wire and asserts the
audible result per output, skipped when no device is attached so CI stays
hardware-free. It emits an evidence artifact (machine-readable plus a
human summary) that can be attached to a release.

The three defects fixed in 0.1.3 become its first regression cases:
even outputs silent, unlinked pair half-dead, unlinked route 6 dB low.

*Proven by:* the artifact itself. A routing change is not done until the
measurement is in the release.

### 5. Stability -- **done**

*Was:* a flakiness gate runs the suite five times. There is no fault
injection and no soak. Every failure mode found so far -- the link race,
two teardown races, the stub signal race -- was a timing bug.

*Target:* deliberate fault injection (drop, duplicate and reorder UDP
datagrams; kill the backend mid-apply; unplug the device mid-verify;
occupy the receive port halfway through) and a restart soak that applies
the routing N times and asserts the result every time.

*Done, transport:* `tests/test_faults.py` -- drop, duplicate, reorder, a
flood of unrelated registers, a device that never answers, a dead backend
port.

*Done, state:* the three cases that tear a transaction open -- the
backend killed between the link phase and the mix write, the device
vanishing while `/refresh` is still streaming, and the receive port taken
*between* attempts rather than before the first one. Those are the shape
every defect this project has shipped had, and they are what gave the
verifier a stop contract (ADR 0009) rather than the other way round.

*Done, soak:* `.github/workflows/soak.yml`, daily at 04:17 UTC -- 200
restart cycles, each one a real `bin/oscmix-session` against the stub
backend checked datagram for datagram, plus the fault suite repeated 15
times. A scheduled workflow, not a Makefile target: `Proven by: soak on
main` was in this document long before anything ran one.

### 6. Code quality -- **done**

*Was:* ruff on a curated rule set, `mypy` non-strict, no mutation
testing. `mypy --strict` reports 12 errors -- a small, closable gap.

*Target:* `--strict` clean, an expanded ruff selection, and **mutation
testing** with a ratcheted score on the core modules (routing, config,
osc). Mutation testing was skipped in 0.1.2 because mutmut could not find
code to mutate in an extension-less script; the package from item 1
removes that blocker, which is why these two belong in the same release.

*Proven by:* a mutation baseline policy in CI, in the shape
payload-live-preview uses.

### 7. Performance -- **reframed and done**

The original plan here measured the wrong thing. How fast the Python runs
is not the question: time-to-`READY=1` is dominated by the device wait,
the 1.5 s link barrier and the dump, none of which a benchmark against a
stub would touch. A hard wall-clock budget on a shared runner would
mostly measure the runner -- a new flake source in a project whose bugs
are already timing bugs.

*One premise of that reasoning is now in doubt.* This said "the 15-20 s
dump", and the dump measures 1.9 s (see the finding under [Still
open](#still-open-in-020)). The conclusion survives -- the device wait
and the barrier still dominate, and neither is benchmarkable against a
stub -- but the sentence was carrying a number nobody had checked, in the
one item of the nine that exists because a number was being measured for
its own sake.

*Done instead:* the tests assert **growth order**, not milliseconds.
Doubling the input must not quadruple the time; an absurd absolute bound
catches a hang. That survives a busy machine and still rejects an
accidental quadratic in the dump parser.

*The question with actual value* is not benchmarkable here at all: can
the handful of `/output/<n>/stereo` registers be queried directly instead
of waiting out a full `/refresh`? If upstream cannot, that is a feature
request (see below), not a number.

### 8. Supply chain and blast radius -- **done**

*Today (before this release):* `install.sh` built `OSCMIX_REF=master` --
whatever upstream happened to be that day. The component that actually
talks to the hardware was unpinned, which makes the word "verified"
hollow: a measurement is only evidence about the binary it was taken
against. It is also the only path here that fetches and compiles code
from the network.

*Done:* the ref defaults to a full commit SHA, the checkout is verified
to have landed on exactly it, tracking upstream is an explicit
`OSCMIX_REF=master`, and the built revision is recorded in the hardware
evidence artifact. No signature verification -- upstream publishes no
signed tags, which is why the default is a commit that has been measured
rather than a branch.

*Also done:* `_cleanup_stale_backend` signals through `os.pidfd_open`
instead of a bare PID, so a number recycled between the `/proc` scan and
the signal cannot be hit; the unit gained the sandboxing a user manager
can actually apply; and `docs/SECURITY-MODEL.md` states the thing nobody
had written down -- **UDP 7222 is unauthenticated, and any local process
can write any mixer register.** That is upstream's design and acceptable
on a single-user desktop, but from 0.3.0 it means a local process can put
48 V on a ribbon microphone. It deserved a sentence.

### 9. Maintainability -- **done**

*Was:* the prose docs are good, but the expensive knowledge is
scattered through commit messages -- why the apply is two-phase, why
verification and re-apply share one `/refresh`, why `volume` is opt-in.

*Target:* an ADR trail (`docs/decisions/`) for the non-obvious choices.
Each of those took a measurement session to derive; none of them should
have to be rediscovered.

## What 0.2.0 closed

Twelve items, in the order I would close them. **A** and **C** are
leftovers from the nine above; **B** and **D** to **F** are gaps the
release itself opened; **G** to **L** only became visible once there was
a standard strict enough to measure against.

The first three were the ones that would have embarrassed this release if
someone had looked: a stability item claimed as done, a CI check that
guarded the project's most expensive bug by inspecting the wrong
artifact, and a coverage gate seven points below what the suite earned.

**All twelve are closed.** Each item below keeps its original diagnosis
-- that is the reason it existed, and worth more than a tick -- followed
by a *Closed:* line naming what proves it.

Nothing in this section is work any more. The heading read *Still open*
until the tag existed, because a "Still open" section that empties
itself before the release is how a claim outruns its evidence -- which
is the failure mode this entire release was about. `v0.2.0` is tagged
and `hardware-evidence.json` is attached to it, so the section can say
what it did.

The release run itself found one more, which is the argument for having
a checklist at all: `make verify-hardware` reported three convincing
FAILs that were an unplayable tone, not a broken routing. A USB replug
had left the default PipeWire sink as the interface's raw 20-channel
`Direct` sink. The tool now skips rather than failing in that case, and
records the sink in the artifact -- a measurement whose decisive
variable is unrecorded is not evidence.

Three things were found while closing these. The first was settled by
going and measuring the condition that was missing; the other two are
recorded rather than acted on.

- **The dump is 1.9 s, not 15-20 s** -- and on a genuine cold plug the
  link registers come back **0.01 s after the `/refresh`** that asks for
  them, with the whole dump over in ~4 s and nothing following for the
  next 272 s. Measured three times: warm backend, cold backend, and a
  real USB replug captured on **both** OSC ports so a request can be told
  apart from a device push (`tests/data/cold-plug-timeline.json`).
  `/playback/*/stereo` arrives at **0.0 s** -- before the session has
  sent a single message -- not "near the end of a dump that streams for
  many seconds" as `register_promptly_reported` still says.

  `LINK_SYNC_BLIND_DELAY` is **5 s** on that evidence (was 20).
  [ADR 0010](decisions/0010-timing-constants-need-a-recording.md) states
  the general rule this exposed: a device wait names the recording it
  came from, and a test asserts the margin -- **at most 10x**, not just
  at least 2x, because a wait an order of magnitude past its evidence is
  an unmeasured number wearing caution's clothes.
- **The cold dump is incomplete, and 0.3.0 should know.** 1234 of 1932
  non-meter registers arrive; the missing 676 are almost all
  `/output/*`. Everything this release verifies is in the fast part,
  which is why nothing noticed -- but `/output/5/reflevel`,
  `/output/5/mute` and `/output/5/phase` were never reported at all, and
  those are what `[output:N]` plans to verify. A verification class
  derived from the *warm* dump would call them verifiable and then report
  them unconfirmed on every cold boot.
- **The discrepancy went unseen because the verify loop hides it.** It
  exits as soon as the *prompt* set matches, so `/playback/*` was never
  looked at -- the classification made itself true.

### A. The fault cases that tear state, not just packets

*Today:* `tests/test_faults.py` disturbs the **transport** -- datagrams
dropped, duplicated, reordered, drowned in noise, and a peer that never
answers. Every one of those leaves the process intact and the state
machine untouched.

*Missing:* the three cases from item 5 that break state while a
transaction is open, which is the shape every defect this project has
actually shipped had:

- the backend killed **between** the link phase and the mix write, i.e.
  during the one window where the routing is knowingly half-applied
- the device unplugged **during** the read-back, while `/refresh` is
  still streaming
- the receive port taken **halfway through** the dump rather than before
  it -- `tests/test_verify.py` only covers "taken from the start", which
  takes the clean early-return path

*Also missing:* the restart soak. Apply the routing N times, assert the
result every time. Nothing in the repository runs one, and no scheduled workflow
exists, although *Proven by* has said "soak on `main`" since the first
draft.

*Proven by:* the three cases in the normal suite; the soak as a scheduled
workflow, not a Makefile target nobody invokes.

*Closed:* the three state-tearing cases are in `tests/test_faults.py`.
The soak is `tests/test_soak.py` driving the real entry point through
start -> `READY=1` -> verify -> SIGTERM -> exit 0, asserting the routing
datagrams byte for byte on *every* cycle, and
`.github/workflows/soak.yml` runs 200 of them nightly plus 15 repeats of
the fault, apply and verify suites. Measured locally: 50 cycles in 61 s,
green. `make soak` exists to reproduce a scheduled failure, not to be
the gate.

### B. What a `routing.conf` promises across versions

*Today:* `config.py` rejects every unknown option and every unknown
section with a `ConfigError` -- exit 2, and `RestartPreventExitStatus=2`
means systemd will not retry. That is correct while the schema is small
and written by hand.

*Why it stops being correct:* 0.3.0 adds `[input:N]`, `[output:N]`,
profiles and `--dump-config`. From then on the file is machine-generated
and travels between machines and versions. A config written by 0.3.0 and
read by a 0.2.0 install produces no routing at all, no restart, and one
line in the journal. For a project whose whole premise is that the text
file is the source of truth, reviewable and diffable, that is a promise
which has never been stated.

*Target:* a compatibility rule, decided in 0.2.0 because this is the last
release in which it is free -- whether the file carries a schema version;
whether an unknown *section* becomes a warning while an unknown *option in
a known section* stays an error; what is promised in each direction.

*Proven by:* a test per direction (a future-shaped config on today's
parser, today's config on a parser that knows more) and an ADR, because
this is a promise rather than an implementation detail.

*Closed:* [ADR 0006](decisions/0006-routing-conf-compatibility.md). An
unknown **section** is a warning and the rest of the file is applied; an
unknown **option in a known section** stays a `ConfigError`. No schema
version field -- the ADR records why, and when to revisit (the first
*incompatible* change to an existing option). A test per direction, plus
one pinning the known option surface so removing a name is a visible
edit. The cost is stated: `[routes:x]`, a typo, is now a dropped route
and a warning rather than an error.

### C. The gate, and the code outside the package

*Today:* three measurement gaps, all small, all pointing the same way:

- `make coverage` measures 91%; `fail_under` in `pyproject.toml` says 84.
  Seven points of erosion the gate would not notice.
- `bin/oscmix-launch` is the least covered file in the repository (63%,
  124 statements). It duplicates ~25 lines of sysfs/procfs helpers on
  purpose, and that decision leaves it outside the package, outside
  `tests/test_architecture.py` and outside the mutation scope. It is now
  the one quiet exception to everything item 1 established.
- `tests/test_contracts.py` calls `pytest.importorskip("hypothesis")`. A
  checkout without the dev requirements runs 271 tests, reports green, and
  has checked no contract at all. CI installs them, so the gate holds
  there -- but a local "all passed" means less than it looks.

*Target:* the ratchet raised to what the suite earns; the launcher either
covered like the rest or declared out of scope *in writing*; a skipped
contract suite that says so loudly.

*Closed:* the ratchet is 94 against a measured 94% (`fail_under` in
`pyproject.toml`, with the reason for the jump written next to it). The
launcher moved into the package -- so it is inside the architecture
test, the mutation scope and the coverage -- and
`tests/test_launcher.py` took it from 61% to **100%**, including every
refusal path a user meets after a desktop double-click. The hypothesis
skip is loud: without it the terminal summary prints what did not run
and why, and `OSCMIX_REQUIRE_CONTRACTS=1` turns the skip into a
collection error. CI sets it on the test and coverage jobs.

### D. Releasing 0.2.0

*Today:* the version and the changelog are in place. What is not: the
three artifacts that this document's *proven by* clauses name -- the
hardware evidence, the built oscmix revision, the mutation score -- have
no defined place in a release. `make verify-hardware` exists and its
verdict arithmetic is under test, but no measured artifact has ever been
attached to anything.

*Target:* a release checklist stating what must exist before a tag,
including the rule from item 4 that a routing change is not done until its
measurement is in the release.

*Also:* two of the nine items above were *decisions*, not tasks, and
neither has a record -- why performance gates measure growth order rather
than wall-clock time, and why the upstream revision is pinned. Both belong
in `docs/decisions/`, for exactly the reason item 9 gives.

*Closed, except the artifact itself:* `docs/RELEASE-CHECKLIST.md` states
what must exist before a tag, in six stages, and names what is
deliberately *not* on it. The two missing decisions are
[ADR 0007](decisions/0007-growth-order-not-wall-clock.md) (growth order,
not wall-clock) and
[ADR 0008](decisions/0008-pinned-upstream-revision.md) (the pin, and the
bump rule).

*Measured (2026-08-16), against pinned revision 2411b12 on a UCX II
(24216011).* **All three routes pass**, each output responding
**119.2 dB** to its own side of the tone and nothing to the other:
`main-out` (1/2), `krk-monitors` (5/6), `phones` (7/8). Exit 0, which is
what the checklist requires.

It took two runs, and the first one earned its keep. `main-out` failed:
`/output/1/volume` and `/output/2/volume` sat at **-65.0 dB**, the fader
shut on a rear output. The routing was applied and produced nothing
audible -- exactly the class of defect this tool exists for, and exactly
the class that reading OSC messages cannot see. Resolved by declaring
`volume = 0.0` on that route, which makes the fader part of what the
route pins (ADR 0003) instead of state the config has an opinion about
and no control over. Verification went from 3 confirmed registers to 5.

The first run also exposed a defect in the tool itself: it blamed "other
audio on the bus" for a silence it had the information to explain.
`LevelReader.output_state()` now reads volume and mute for every
measured output, and the verdict names the cause -- fader shut, muted,
or turned down, with the number.

*Shipped:* `hardware-evidence.json` is attached to `v0.2.0` -- all three
routes, 98.3 dB response per output, measured into `oscmix.main-out`
against pinned revision `2411b12`. The release run needed two attempts,
and the first one earned its keep twice over: it failed on a shut fader
(user state, now named by the verdict) and then, on the re-measurement,
on an unplayable tone into a 20-channel sink (now a skip, and the sink
is recorded in the artifact). Neither was a routing fault, and neither
would have been visible at message level.

### E. When the upstream pin moves

*Today:* the pin is set, verified at checkout, and recorded in the
evidence artifact. What is nowhere written is when it may move.

*Target:* a bump requires a fresh hardware measurement. Otherwise the
artifact describes a binary that is no longer shipped, and three bumps
quietly restore the situation the pin was introduced to end. The same rule
governs the upstream work below: if the cache-synchronisation patch is
accepted, the order is bump, measure, *then* delete `LINK_ECHO_TIMEOUT`,
`LINK_SETTLE` and `LINK_SYNC_BLIND_DELAY`.

*Small, same item:* the pin forces `install.sh` into a full clone, because
`--depth 1 --branch` accepts a branch or a tag but not a commit. `git
init` plus `git fetch --depth 1 origin <sha>` gets the shallow clone back.

*Closed:* the bump rule is [ADR 0008](decisions/0008-pinned-upstream-revision.md),
including the ordering for the upstream patch (bump, measure, *then*
delete the constants). `install.sh` has its shallow clone back via `git
init` + `git fetch --depth 1 origin <sha>`, with a fallback to a full
clone for servers that refuse a bare SHA. Measured against upstream: one
commit and 480K of `.git` instead of the full history at 632K.

### F. "The session writes nothing" has an expiry date

*Today:* the unit runs with `ProtectHome=read-only` and an empty
`ReadWritePaths`, and `docs/SECURITY-MODEL.md` states that the session
writes nothing. Both are true of what the session does today.

*Target:* profiles, `--dump-config` writing to a file and any cached state
all need a writable path. That is a 0.3.0 conversation, but the security
model should record *now* that this is a property of the current feature
set, and where it would have to be renegotiated -- otherwise the first
feature that needs a file will quietly widen the sandbox instead of
arguing for it.

*Closed:* `docs/SECURITY-MODEL.md` now says that "writes nothing" is a
property of today's feature set, names the four questions the first
writable path has to answer (does the *service* need it or only the CLI;
`StateDirectory=` rather than a hand-written path; the config itself
stays read-only; partial writes), and points at the assertion that keeps
the sandbox from widening quietly -- which did not exist and now does
(`test_the_service_declares_no_writable_path`).

### G. The dry run is not the path it claims to check

*Today:* the cheapest end-to-end assertion in CI greps `--dry-run` output
to prove that links go out before the mix matrix -- the guard against the
defect that silenced every even output. It checks the wrong artifact.
`_print_dry_run` walks `route_messages(route)` route by route and prints
links, mix, links, mix. `apply_routing` sends *every* link of *every*
route, waits for the barrier, then sends every mix. With the one-route
example config the two agree by accident. With two routes, measured on
this revision:

```
dry run:    /playback/1/stereo  /output/1/stereo  /mix/1/playback/1
            /playback/3/stereo  /output/7/stereo  /mix/7/playback/3

real order: /playback/1/stereo  /output/1/stereo
            /playback/3/stereo  /output/7/stereo   <- barrier ->
            /mix/1/playback/1   /mix/7/playback/3
```

*Target:* one function produces the order and both the dry run and the
apply consume it, so the printed sequence *is* the sent sequence.

*Proven by:* a contract test asserting that the dry-run lines are exactly
the datagrams `apply_routing` sends, in the same order -- plus a second
route in the config CI uses, because a single route cannot exhibit the bug
class this check exists for.

*Closed:* `routing_plan()` produces the order and both the dry run and
the apply consume it. A contract test asserts the printed lines are
exactly the datagrams `apply_routing` puts on the wire, in the same
order, over three routes; reverting `_print_dry_run` to the old walk
fails it at index 2. CI's dry run moved to `tests/data/two-routes.conf`
and asserts *last* link < *first* mix rather than one link < one mix.

### H. The timing budget has to compose

*Today:* eight waits (`DEFAULT_DEVICE_TIMEOUT` 30 s, `PORT_READY_TIMEOUT`
10 s, `LINK_ECHO_TIMEOUT` and `LINK_SETTLE` 1.5 s, `VERIFY_SETTLE`,
`VERIFY_TIMEOUT` 10 s, `LINK_SYNC_BLIND_DELAY` 20 s (5 s since ADR
0010), `CHILD_STOP_GRACE`
5 s) and two systemd deadlines (`TimeoutStartSec=75`, `TimeoutStopSec=10`).
The relationship between them lives in a comment in the unit file. The
defaults fit -- device wait plus port wait plus barrier is ~41.5 s of the
75 -- but `--timeout` is a command-line argument, so an edited `ExecStart`
can push the start past the deadline and have the unit killed *during* the
apply. That is item **A**'s torn state, reached by editing a number.

*Target:* the budget as an assertion in the shape `tests/test_unit_file.py`
already uses: parse the unit, read the constants, assert that the
worst-case path to `READY=1` fits inside `TimeoutStartSec` with margin and
that `CHILD_STOP_GRACE` fits inside `TimeoutStopSec`.

*Closed:* `constants.startup_budget()` sums the waits on the path to
`READY=1` -- **42.0 s** against `TimeoutStartSec=75`.
`tests/test_unit_file.py` parses the unit and asserts the budget fits
with at least 10 s of margin, that `ExecStart`'s own `--timeout` fits
(so raising it past ~53 s fails the suite rather than the service), that
`CHILD_STOP_GRACE` plus one poll step fits inside `TimeoutStopSec`, and
that the sum still names every term. Verification's exclusion is
asserted *structurally* rather than arithmetically -- `READY=1` follows
the apply in the same block and `_apply_and_verify` still defers to a
thread -- because with 33 s of margin the arithmetic would pass either
way.

### I. The background verifier has no stated contract

*Today:* `_apply_and_verify` starts `verify_and_repair` on a daemon
thread. It reads `stop_requested` and `child.poll()` exactly once, before
starting. What follows can run for two verification windows plus a blind
delay and issues writes at three points: `send_mix` from the observer,
`send_mix` when the dump never reported the links, and a full
`apply_routing` retry. A `systemctl --user stop` in the first half minute
after a hotplug therefore terminates the backend while the verifier is
still writing routing at it, and the process exits when `supervise`
returns -- cutting the daemon thread wherever it happens to be.

Nothing here is known to break; the writes go to loopback UDP and a dead
port is silent. But *"the mix is never left half-applied"* is the property
the whole two-phase design exists for, and on this path nobody states it.

*Target:* one written rule -- the verifier checks `stop_requested` between
phases and before every write, and the session does not exit until the
verifier has stopped or its deadline passed. Item **A**'s kill-mid-apply
case then has something to assert against.

*Closed:* [ADR 0009](decisions/0009-verifier-stop-contract.md), and the
code to match. The verifier asks `should_stop()` between every phase and
before each of the three writes, and every wait wakes early
(`wait_unless_stopped` replaced the sleeps -- the blind delay was 20 s
against a `TimeoutStopSec` of 10, and it is the path a *user* hits,
because it is taken when the mixer GUI holds the port). `run_session`
joins the verifier for `VERIFIER_STOP_GRACE` (2 s) before exiting; 2 + 5
fits inside 10, asserted against the unit file. A dead backend counts as
a stop.

### J. Nothing proves the unit starts, or that an install works

*Today:* `tests/test_unit_file.py` reads the unit as text. That catches
the directives known to break a user unit; it cannot catch a typo in a
directive name and it starts nothing. `install.sh` is likewise tested for
its shape but never run end to end -- in a release that moved the runtime
from `lib/` to `src/` and rewrote that path in three places.

*Target:* `systemd-analyze verify` on the unit in CI, and an install smoke
test: run `install.sh --no-build --no-udev` into a temporary `HOME`, then
assert the installed file set and that `oscmix-session --dry-run` runs
from it.

*Closed:* `scripts/verify-unit.sh` runs `systemd-analyze verify --user`
and fails on anything it says about this unit -- necessary because the
tool reports an unknown key on stderr and still exits 0, so the exit
status is not a gate. Wired into the quality job, where a missing
`systemd-analyze` is a failure rather than a skip. Three install tests
now run what `install.sh` produced, from a directory that is not the
checkout: the session's `--dry-run`, the launcher's device-absent path
(which would die with `ImportError` if `launcher.py` had not been
installed), and the module set matching `src/` exactly.

### K. The mutation baseline predates half the suite

*Today:* 677 mutants count as `not_covered` because `cli`, `session` and
`process` were reachable only through a subprocess. That was measured
before `tests/test_lifecycle.py` and `tests/test_process.py` existed, and
both drive those modules in process. The floor of 0.72 is set against a
run that no longer describes this suite.

*Target:* re-run and re-baseline. The expectation is that `not_covered`
shrinks rather than that the score moves; if it does not shrink, the
reason is worth knowing before the number is trusted again.

*Closed, and the expectation was half right.* Re-run against this
revision: **2501 mutants, 1551 killed, 861 survived, 81 not covered, 8
timeout -- score 0.643.** `not_covered` fell 677 → 81, as predicted. The
score fell too, 0.728 → 0.643, and the second is caused by the first:
every mutant that stops being "not covered" starts being *judged*. Four
modules that were excluded from the denominator entirely are now in it,
carrying survivors that were always there and simply were not counted.

So 0.728 was never a measurement of this runtime -- it described the
third of it that in-process tests reached at the time. 0.643 is the
first number measured against the whole thing. The floor is 0.63, and
raising it now means killing survivors in code that was structurally out
of reach of this gate until this release, which is the useful direction.

### L. "Promptly reported" is folklore that could be a fixture

*Today:* `register_promptly_reported` encodes which register families the
device dump reports in time, and that decides whether a missing register
is a warning or a note. It is a hand-maintained list, measured once
against a UCX II, checked against nothing.

*Target:* now that the backend revision is pinned, a recorded `/refresh`
dump from exactly that revision belongs in the repository as a fixture,
and the classification becomes a test against it rather than a memory. It
is also what the verification classes in 0.3.0 should be derived from,
instead of being written out a second time by hand.

*Closed:* `scripts/record-dump.py` and `tests/data/refresh-dump.json` --
a real dump from the pinned revision, recorded as register *shape* and
arrival times, never values. It separates the 70 continuously streaming
meter registers from the dump itself, which a first attempt did not:
54970 messages in 60 s, and the dump never went quiet because the meters
never stop.

Confirmed against 2411b12 on a UCX II (24216011), 2002 registers:
`/mix/*/playback/*` absent as the two-phase design assumes;
`/mix/*/input/*` present (400 registers) as 0.3.0 assumes; `48v`,
`hi-z`, `reflevel`, `gain`, `mute`, `phase` all present; `name` and
`loopback` absent, i.e. still write-only. `tests/test_recorded_dump.py`
binds the classification to that measurement in the direction that
matters -- a register the dump never reports may never be called prompt,
or the verifier warns and re-sends on every single run -- and holds the
1.9 s finding above in place with its conditions.

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

## Structural work before the surface grows (0.3.0) -- **done**

Three refactors that were cheap before the feature work and expensive
after. They landed in the same window, in the order their dependencies
forced: the register model first (the other two read it), the reconciler
second, the seam third, and the write path moved onto the plan last with
a hardware measurement on each side.

### A backend seam -- **done**

The OSC calls should sit behind a narrow interface, so the dependency on
oscmix's behaviour is visible and replaceable rather than spread through
the control flow. Two reasons beyond tidiness: the timing constants above
exist purely because of an upstream implementation detail, and a seam is
what makes the option below cheap to keep open.

**The option worth keeping open** is not a competing mixer. It is an own
*state path*: writing and reading the two dozen registers this project
actually pins directly over SysEx, while oscmix keeps the GUI and
metering. That removes the dual-writer problem, makes read-back possible
where upstream does not dump, and kills the cache race at the root. The
cost is owning register decoding for devices that cannot be tested here,
which is why it is not worth doing today -- and exactly why the seam and
the register model go in while the surface is still small.

*Done:* `src/oscmix_desk/backend.py`. Six places used to open their
own socket and know the address; a test asserts none remain outside the
seam, because the seventh is what makes the option above expensive again.

The part worth more than the tidying is `Traits`. The dependency on
oscmix's *behaviour* used to be invisible -- spread through the control
flow as timing constants with nothing naming what they worked around. It
is now three declared, checked properties:
`reports_link_state_on_write` (False, and the sole reason
`LINK_ECHO_TIMEOUT`, `LINK_SETTLE` and `LINK_SYNC_BLIND_DELAY` exist),
`dumps_playback_matrix` (False, asserted against the recorded dump), and
`reports_unchanged_registers` (False).

That ties the upstream work to the code: when
[oscmix#31](https://github.com/michaelforney/oscmix/pull/31) lands and
the pin moves, flipping the first flag is the change, and ADR 0008 fixes
the order -- bump, measure, *then* delete the constants.

### The register model as data -- **done**

The channel-state work multiplies the register surface by roughly ten.
Which channels have `48v` (1-2), `hi-z` (3-4), `reflevel` (3-8), which
registers the device reports back and which are write-only -- all of that
is currently knowledge in prose, spread across format strings in
`routing.py`, ranges in `constants.py` and the known-not-prompt families
in `verify.py`. It belongs in one data structure that validation,
`--dump-config` and verification all read, introduced *before* ten times
as much of it exists.

**Indexed by device from the first line.** `48v` on 1-2 and `hi-z` on 3-4
are UCX II facts, not Fireface facts. A model without a device dimension
casts this one interface into the data structure and puts the untested 802
permanently out of reach -- in the very refactor that could have brought
it closer.

*Done:* `src/oscmix_desk/registers.py`. Every channel range was read
out of `tests/data/refresh-dump.json` rather than typed from the manual,
and `tests/test_registers.py` holds each claim against that recording and
against the cold-plug timeline. The 802 is listed and declares nothing --
"may work" as a property of the data instead of a sentence in a README.

Two ranges would have been wrong if guessed, which is the argument for
deriving them: **the meters run to 22 while every settable register stops
at 20**, so a single channel count per device is already wrong; and
`/mix/<out>/input/<in>` appeared only on odd channels, which is *link
state* (linked pairs fold onto the odd channel) and deliberately not
modelled.

*First consumer:* `config.py` validates channels against the device
instead of against `CHANNEL_MIN..CHANNEL_MAX` (1..64, which describes no
interface). `output = 40/41` on a UCX II used to parse, apply, and do
nothing.

### Desired state, observed state, plan -- **done**

What this project *is*, is a reconciler: desired state from the config,
observed state from the dump, the difference applied. What the code does
is four partly overlapping paths over the same data -- `apply_routing`,
`send_mix`, `blind_reapply_mix` and `verify_and_repair`.

Stated as `desired(config)`, `observed(dump)` and `plan(desired,
observed)`, apply and verify become one path -- and two planned features
fall out of it instead of becoming paths five and six: `--dump-config`
(0.3.0) is `observed()` rendered as config, and `--diff` (0.4.0) is
`plan()` printed instead of sent.

This is also what makes the register model pay for itself: a plan is a set
of registers, and everything above -- validation, verification, the pin
and remember rule -- becomes a property of an entry in that table rather
than a branch in the routing code.

*Landed:* `src/oscmix_desk/reconcile.py`, pure -- no socket, no
clock, asserted. `desired(config)`, `observed(reports)`,
`plan(desired, observed)`, with the verification class read from the
register model rather than branched on in the routing code.

*Switched over, with a measurement on each side.* The write path is
where all three shipped defects lived and where a mistake is inaudible
until somebody is listening, so it was landed first and moved second.
What was proven before the move:
`plan(desired(config))` against an empty observation is the datagram
sequence `routing_plan()` produces today, ordering included, **minus
repeats** -- a register two routes share goes out once instead of twice.
That difference is a change on the wire, so a second test asserts every
dropped repeat carried the value already in the plan. Both hold across
seven config shapes.

*Load-bearing on the reading side too:* `expected_registers` delegates
to `desired()`, so verification and the plan cannot drift apart.

*Measured on the move itself,* against the shipped five-route config:

| | before | after |
|---|---|---|
| datagrams | 17 | **13** |
| diff | — | deletions only, no line added or reordered |
| main-out 1/2 | 98.3 dB | **98.3 dB** |
| krk-monitors 5/6 | 98.3 dB | **98.3 dB** |
| phones 7/8 | 98.3 dB | **98.3 dB** |

The four datagrams that went away were `/playback/1/stereo` twice and
`/output/5|7/stereo` once each -- repeats, machine-checked against the
before-sequence. Identical to 0.1 dB on every output, plus 50 soak
cycles.

## 0.3.0 -- the whole signal path, declared

The feature depth, on top of a base that can carry it. **The base is
built** -- register model, reconciler and seam are done, and the write
path runs through the plan. What is left here is the surface itself.

Two findings shape it, and both are now recordings rather than
recollections (`tests/data/refresh-dump.json`,
`tests/data/cold-plug-timeline.json`), checked by
`tests/test_registers.py`:

**`/mix/<out>/input/<in>` appears in the device dump;
`/mix/<out>/playback/<pb>` does not.** Almost all of the new surface is
therefore verifiable, unlike the playback matrix this project started
with. Confirmed present: input `48v` (ch 1-2), `hi-z` (3-4), `reflevel`
(3-8), `gain`, `mute`, `phase`, `stereo`; output `mute`, `reflevel`,
`phase`, `volume`, `stereo`. Confirmed absent and therefore write-only:
`/input/*/name`, `/output/*/name`, `/output/*/loopback`.

**A cold plug delivers an incomplete dump, and this is the one that
lands on 0.3.0.** After a real USB replug, 1234 of 1932 non-meter
registers arrived within seconds and nothing followed for the next
272 s. Only the stereo flags came back for *every* channel -- which is
why 0.2.0 works and why nothing noticed. `/output/N/mute` came back for
channels 1, 2, 3, 8, 9 and 10 and not for 4-7 or 11-20: ragged, so a
truncated stream rather than a rule.

That is exactly the surface `[output:N]` proposes to verify. A
verification class derived from a *warm* dump would call those registers
verifiable and then report them unconfirmed on every cold boot. The
model records only what is known to arrive whole and refuses to answer
for the rest; the feature work has to respect that rather than discover
it in the field.

- **Hardware input routing -- done.** `input = 1/2` as a route source,
  exclusive with `playback`. Zero-latency direct monitoring, the reason
  TotalMix exists on a tracking session.

  It went first because `/mix/<out>/input/<in>` **is** reported by the
  dump: a monitoring path is the first thing this project routes that it
  can *verify* rather than only re-establish.

  Two things it turned up before shipping:

  - **A muted gain reads back as `-inf`, not as the dB written.**
    Upstream stores anything `<= -65` as zero and reports zero as
    negative infinity (`setmix`/`newmix`). `routing.conf` documents
    `level = -65` as mute, so a muted monitoring route would have been
    reported mismatched on every start and the whole routing re-sent --
    invisible until now only because the playback matrix, the only mix
    family before this, is never reported. The read-back and the plan
    now share one comparison that knows the floor.
  - **Adding `input` to `[route:...]` makes a 0.3.0 config fail whole on
    0.2.0**, playback routes included (ADR 0006: unknown option in a
    known section is an error). Intended: a monitoring route dropped
    with a warning leaves a tracking session silent. A new *section*
    would only have warned, which is why it was not used.

  *Still unmeasured:* the 6 dB compensation on the unlinked-pair path was
  measured for a playback source. oscmix runs both through one
  `setlevel()`, so the same halving is expected for an input -- expected,
  not measured; it needs a signal on a hardware input.
- **Channel state -- done, except phantom power.** `[input:N]` and
  `[output:N]`: gain, reference level, mute, phase, hi-z, output volume.
  Validated against the channels that actually have each option --
  per *channel*, not per device: the mic preamps have gain and no
  reflevel, inputs 3-8 have reflevel, hi-z is on 3/4.

  None of that is listed in the parser. The settable surface is derived
  from the register model: a register is settable exactly when it
  declares a value domain. The model's ranges, read from a recorded
  dump, turned out to agree exactly with upstream's own device table --
  two independent sources, same answer.

  **`48v` is deliberately not settable.** It is modelled, verifiable and
  readable, and it has no value domain, so no config can reach it. The
  rule below says a hardware case must prove the channel a config names
  is the channel the device powers, and that case needs a microphone
  nobody should risk. Asking for it is an error that says so.

  *The measurement that shaped it:* after a cold plug the device does
  not report channel state for every channel, so verifying it naively
  would warn and re-send the whole routing on every hotplug.
  `register_promptly_reported` now asks the model whether a family is
  known to arrive whole, and treats absence as a note for the ones that
  are not. Everything this project verified before 0.3.0 sits in the
  fast, complete part -- which is exactly why nothing noticed until
  channel state arrived.
- **`--dump-config` -- done.** Read the device and emit a `routing.conf`
  that reproduces what it reports. Build it in the GUI, freeze it with
  one command.

  The round trip is a fixed point, proven twice: 16 tests over synthetic
  states built from the same message shapes the apply sends, and once
  against the device -- `input 1/2 -> output 1/2` at -6 dB written
  through the normal apply path, read back as exactly that, then
  restored to `-inf`.

  **What it cannot do, and says so at the top of every file it writes:**
  the playback matrix is not reported (ADR 0002), so software routing
  cannot be read back. A dump reproduces monitoring paths and nothing
  else -- *merge, do not replace*. It refuses outright when UDP 8222 is
  held, because two readers split the device's replies and half an
  answer rendered as a config looks authoritative.

  It also declines to emit `volume`. A route that declares it pins the
  fader on every start (ADR 0003), and a dump cannot tell "I meant this"
  from "this is where I left it". Which registers a dump should pin is
  the pin/remember question below, and the answer belongs in the
  register table rather than in the writer.
- **Profiles** -- *done.* Several configs and a way to switch between
  them; TotalMix's eight snapshots, but as text. A profile is a whole
  `routing.conf` in `profiles/` beside the main one -- not a new section
  type, so it is parsed by the same code, inherits ADR 0006, and
  `--dump-config > profiles/tracking.conf` composes. `--profile NAME`
  switches, `--list-profiles` lists.

  Machine settings are *not* the profile's: `[osc]` ports and the device
  name are inherited from the main config unless the profile states them
  itself. That rule came from an accident -- a profile fixture with no
  `[osc]` section fell back to the compiled-in 7222, which on this
  machine is the live backend, so a unit test moved a fader on the
  UCX II. It passed, because everything it asserted was true.

  The three outcomes are held by `tests/test_profiles.py`, written
  before the module existed, and measured at the device:

  | outcome | measured |
  |---|---|
  | refused, nothing written | 0 datagrams; 5 for a good profile through the same counter, seconds apart |
  | applied | `/output/1/volume` 0.0 -> -6.0, read back through a second `/refresh` |
  | applied, with the list | `/mix/1/playback/1` only -- the one family this backend never reports |

  `APPLIED_VERIFIED` is reachable only for a profile that pins channel
  state without touching the matrix, because `/mix/<out>/playback/<pb>`
  is never reported back. That is stated in the outcome rather than
  papered over: `unverifiable` is a separate list from `unverified`, so
  "I could not check it" and "it cannot be checked" do not read the same.
  **What building it from the contract first turned up.** The tests were
  written before `profiles.py` existed, and four of the five defects
  they found were not in the profile code at all -- three were in the
  path every boot already runs:

  1. `expected_registers()` took `config.routes` and rebuilt a `Config`
     from them, dropping `config.channels`. Every `[input:N]` and
     `[output:N]` register was written to the device and then left out
     of the read-back, so a run logged "routing verified" without having
     looked at one of them. Exact mirror of the write-path defect fixed
     one commit earlier; both now covered by a structural test that
     fails on *any* function rebuilding a Config from a subset.
  2. `register_promptly_reported()` excluded everything under
     `/playback/` as never-reported. The recorded dump carries 42
     registers there, every `/playback/<n>/stereo` among them, and the
     cold-plug timeline has all 20 coming back at t=0.00 s -- earlier
     and more completely than `/output/<n>/stereo` at 2.26 s. A lost
     input-side link write was therefore never a problem and never
     re-sent, on the one register family the two-phase apply exists for.
     The old test asserted the wrong rule, which is why it survived; the
     new one asserts against the recording, which cannot hold a belief.
  3. The observation window closed as soon as the *promptly* reported
     set matched. The stereo flags always arrive first and always match,
     so channel state was structurally unconfirmable -- measured on the
     UCX II as `/output/1/volume` correct at the device and reported
     unverified. One flag was answering two different questions; they
     are now two functions.
  4. In the profile code itself: `_write` sent links, mix and channel
     state with nothing between them, dropping the link barrier. The
     whole switch took 48 ms on live hardware, where the barrier alone
     is 1.5 s. That is the stereo-link race of 0.2.0, reintroduced on a
     new write path, and it is why the switch now calls `apply_routing`
     rather than sending its own three bursts.

  Three of those four are the same mistake: a second implementation of
  something that already existed, correct in everything it did and
  missing something the original had. The seam made each fix a
  delegation rather than a repair.

  A fifth was in the test double, not the code: `confirming_backend`
  echoed back everything it was sent, including the playback matrix that
  upstream never reports. A double more capable than the thing it stands
  in for hides exactly the outcomes that exist because of the limit.

- **The pin/remember model** -- *done.* A `policy` column in the register
  table, PIN or REMEMBER, overridable per option by a `[pin]` section.
  [ADR 0012](decisions/0012-pin-and-remember.md).

  **The measurement this section asked for was taken, and it settles the
  three positions.** Of every register a config can set, exactly one is
  pushed to listeners when it changes: `/output/{ch}/stereo`, which the
  device echoes over MIDI. `volume`, `mute`, `hi-z`, `gain`, `reflevel`
  and `/playback/{ch}/stereo` all change silently -- each verified by
  reading the value, writing a different one, and confirming from a
  later dump that it really moved.

  So **position three is dead**: continuous reconciliation would mean
  polling a 2002-register dump forever, against a device already
  streaming ~880 meter datagrams a second. Position two remains open and
  bounded, and pinning is written so that a trigger can be added without
  changing what the word means.

  What the model replaced was an accident, and that is measured too. A
  fader turned 0.5 s after a restart came back at the config's value;
  the same turn at 1.5, 3 and 6 seconds survived -- and the 0.5 s case
  was overwritten by the ordinary start-up apply, which finishes around
  1.8 s, not by the verifier. The line between "the config wins" and
  "the user wins" was how long the apply took.

  It also settles what `--dump-config` pins, which this section said it
  would: pinned options are emitted as config, remembered ones as
  comments carrying the value.

**Prerequisite, and it is met.** `--dump-config` makes the file
machine-generated and profiles make it plural, so neither was shippable
until `routing.conf` had a compatibility rule. It has one:
[ADR 0006](decisions/0006-routing-conf-compatibility.md) -- an unknown
section warns and the rest of the file is applied, an unknown option in
a known section stays an error, no schema version.

What that leaves for the feature work: every new section
(`[input:N]`, `[output:N]`, `[profile:...]`) is *by construction* safe
to read on an older install, and every new option inside an existing
section is not. That asymmetry is a constraint on where new settings may
go, not just a parser detail.

### What a new register family costs

Every item above multiplies the surface, so the bar for adding one is
fixed in advance rather than argued per feature. A register family is done
when:

1. it has a row in the register model -- path template, type tags,
   valid channels **per device**, verification class, and whether a cold
   plug reports it for every channel. `registers.py` exists now, so this
   is filling in a row rather than inventing a structure; the row is
   checked against the recordings, so a claim the device does not
   support is a failing test rather than a surprise on a user's desk;
2. a contract test states what its config section writes, as written paths
   ⊆ declared paths -- the rule the `volume` bug produced;
3. the round trip covers it: `--dump-config` → apply → dump is a fixed
   point;
4. if it is audible, a case in `verify-hardware` and a line in the
   release's evidence artifact -- which now measures *every* route from
   the sink that feeds it, and names any it could not measure rather
   than omitting them (`complete` in the artifact);
5. if a decision had to be made, an ADR.

None of that is new machinery invented for 0.3.0. It is the 0.2.0
apparatus, applied by default instead of on request -- and after the
structural work above, points 1 and 3 are cheap: a row in a table, and a
round trip through `desired`/`observed`/`plan` rather than a fifth path
over the same data.

### Verification classes, declared per register

The dump splits the surface into three classes, and each register says
which one it is:

- **verifiable** -- reported by the dump, so a value is confirmed,
  mismatched or missing. Most of the new surface, `/mix/<out>/input/<in>`
  included.
- **write-only** -- accepted, never reported (`/input/*/name`,
  `/output/*/name`, `/output/*/loopback`). The verifier reports
  *unverifiable*, never *confirmed*.
- **re-established** -- the playback matrix: unverifiable *and* dependent
  on link state, so it is rewritten from a known-good state rather than
  checked.

Naming the class is what keeps verification from over-claiming as the
surface grows. It used to live in `register_promptly_reported` and in
prose, which was fine for six registers and not for sixty.

*It is data now:* `registers.verify_class` answers it per register, and
`reconcile.plan` reads that answer instead of branching -- so
"re-established" is a row in a table rather than a special case in the
routing code. The classes were checked against the recording, not
asserted: everything declared verifiable is in the dump, everything
declared write-only is absent, and the playback matrix is the only
re-established family.

The fourth dimension the recordings added is *when*, not *whether*: a
register can be verifiable in a warm dump and absent after a cold plug.
`registers.cold_plug_complete` answers that separately, and it answers
False for anything nobody measured whole.

### Dangerous registers get their own rule

`48v` is a register like any other to the protocol and nothing like any
other to a ribbon microphone. Before phantom power is settable from a text
file:

- **it is never implied.** No default, no profile fallback, no
  pin-everything mode turns it on; only an explicit `48v = true` on that
  channel does.
- **it is applied last**, after the routing verified, and logged at
  warning level naming the channel.
- **turning it off is never deferred or retried away.** An unapplied
  disable is a louder failure than an unapplied enable.
- **a hardware case proves the channel it names is the channel it hits.**
  An off-by-one here is not a silent output, it is damaged equipment.

Anything else that can hurt equipment or ears belongs in the same class:
`reflevel` changes and volume jumps on an active monitor path, muted
across the change wherever the device clicks.

### Profiles are transactions

A profile switch writes a set of registers to a device somebody is
listening to. A half-applied switch is item **A**'s torn state with a user
holding the trigger, so the switch has to state its outcome: applied and
verified, applied but unverifiable (with the list), or refused before
anything was written because the config did not parse. Never "partly, and
here is a traceback".

That also settles what `--dump-config` does with the registers a config
does not mention -- it is the same question as the pin/remember model, and
the answer belongs in the register table, not in the writer.

### What "supported" means for a device

Today the 802 is "untested", which is honest and unbounded. A
device-indexed register model turns that into a bar: a device is supported
when its register table is declared, its channel capabilities are
recorded, and one hardware evidence artifact exists for it. Below that
line it is "may work", stated in the README -- and no register table is
merged that silently assumes UCX II channel counts.

## 0.4.0 -- the rest of the strip, and what it costs

**The families still marked 0.4.0 in the table above are 1466
registers.** Raw counts flatter this release and mislead about the work:
0.3.0's model already declares 1046 concrete registers, but 800 of those
are the two mix matrices -- two rows in the table, expanded
mechanically. The numbers that predict effort are the other two:

| | 0.3.0 | 0.4.0 adds |
|---|---|---|
| register families (rows in the model) | 18 | 11 |
| per-channel option names a config can set | 6 | 50 |
| global registers with no channel | 0 | 42 |

**Six per-channel option names to fifty-six**, and a whole dimension --
global settings -- that the config has no section for at all. That is the
release, and it is the config format rather than the register table that
has never carried it.

Counted from `tests/data/refresh-dump.json`, not estimated:

| family | registers | shape |
|---|---|---|
| EQ (in and out) | 480 | 24 options × 20 channels |
| Room EQ (outputs) | 320 reported, **640 real** | 32 × 20 -- **blocked, see below** |
| Dynamics | 322 | 16 × 20 |
| Auto level | 162 | 8 × 20 |
| Low cut | 120 | 6 × 20 |
| Crossfeed | 20 | 1 × 20 |
| Reverb | 14 | global |
| Hardware | 10 | global |
| Echo | 7 | global |
| Control room | 6 | global |
| Clock | 5 | global |

`/reverb/lowcut` is counted under Reverb rather than Low cut; it is the
one register the two filters both match, and 1466 is the count with it
attributed once.

Room EQ is the exception in that table and the reason the total is soft:
the recording shows 320 because upstream folds the block, and the real
surface is 640 -- confirmed by building the fix, where the dump went from
1932 registers to 2252. **So 0.4.0 is 1466 registers as the device is
readable today, and 1786 once the pin moves.**

That table is the plan, because it splits itself in two.

### The 42 global registers are the cheap half

**All 42 are done** -- `[echo]` (7), `[controlroom]` (6), `[reverb]`
(14), `[clock]` (5) and `[hardware]` (10), each measured at the device.
Four of the 42 are declared read-only, and that line was drawn by
upstream rather than by judgement: `/clock/samplerate` is
`{"samplerate", CLOCK_SAMPLERATE, .new=newsamplerate}`, a reporter with
no `.set`, and `/hardware/{ccmode,dspload,dspvers}` are the same shape.
A config cannot set what oscmix cannot write.

**That answers the sample-rate question below without an argument.** It
is not "state or event": oscmix has no setter for it, so it is not
declarable at all.

The half turned out cheap as predicted, and the two surprises were both
in the *data* rather than the structure: `/reverb/volume` has no bounds
upstream while `/echo/volume` has the fader's, so copying one onto the
other would have rejected values the device accepts; and
`/controlroom/{dimreduction,recallvolume}` are `.max=0` -- reductions,
not levels, and -65..0 rather than the fader's -65..+6.

Reverb, echo, control room, clock and hardware are 42 registers with no
channel dimension. They need a new section each and nothing else: no
format change, no new shape in the register model, and `[reverb]` or
`[controlroom]` is a section, which ADR 0006 makes safe to add.

**They should go first**, and not because they are easy. Two of them
answer questions this project has already had to work around --
`/controlroom/mainout` is the register that produced upstream #30, and
`/clock/*` is the one the sample-rate measurement kept running into. A
family this project has already measured is a family whose row can be
written from a recording rather than from a datasheet.

### The 1424 per-channel registers -- 1084 declared, 320 left and blocked

Every current channel option is one flat word: `volume`, `reflevel`,
`hi-z`. Everything left is nested -- `/input/3/eq/band1freq`,
`/output/5/dynamics/compthres` -- and there are **50 distinct option
names** below `/<family>/<channel>/`.

Settled by [ADR 0014](decisions/0014-nested-config-sections.md):

```ini
[eq:input:3]
band1freq = 80
band1gain = -3.0
```

**Family first, and the reason is a measurement rather than taste.**
This section said the dotted form would break 0.3.x while sub-sections
would degrade. Half of that was wrong. Fed to the released parser:

| written as | 0.3.0 does |
|---|---|
| `[input:3]` + `eq.band1freq` | refuses the file |
| `[input:3.eq]`, `[input:3:eq]`, `[input:3/eq]` | **refuses the file** |
| `[eq:input:3]` | warns, skips, applies the rest |

`config.py` dispatches on `section.startswith(("input:", "output:"))`
before it looks at the rest, so anything beginning `input:` reaches the
channel parser and dies on `int("3.eq")`. That is a released version's
behaviour and cannot be fixed retroactively -- so the format moved
instead. `tests/test_config.py` keeps both halves of the table
executable, so the day the constraint changes, the ADR is told.

The cost is that a channel strip is spread over several sections rather
than gathered in one. A config that will not load is worse than one that
reads awkwardly.

**EQ is done** -- 480 registers, in and out, three bands each. Two
things came out of it that were not about EQ:

- **A sub-family's own switch is flat by path shape.** `/input/{ch}/eq`
  has no slash after the prefix, so it landed in `[input:3]` as
  `eq = true` -- the one shape ADR 0014 exists to prevent. Excluded now
  by having children.
- **The wire type has to come from the declared tag, not the value.**
  Every config value parses to a Python float, so encoding by type sent
  `,f` to `band1freq`, which upstream reads with `setint`. `oscgetint`
  rejects a float with "incorrect argument type", `setint` returns
  without writing, and a write draws no reply. Parsed, validated, on the
  wire, device unchanged. `/echo/feedback` had the same latent bug.

**Dynamics is done** -- 320 registers, eight options on 40 channels, and
the first family added *after* the nested shape existed. It cost a
table of eight rows and no new machinery: `[dynamics:input:3]` parses,
dumps and round-trips because ADR 0014's sections are generic. The dump
of the development machine went from 840 lines to 1240.

Three things came out of it that were not in the plan:

- **Upstream declares bounds and does not enforce them.** `.min` and
  `.max` are read nowhere at the pinned revision -- `setfixed` and
  `setint` both end in `setval`, which converts the control to a
  register and writes, with no comparison in between. So the config
  parser's range check is not a second opinion agreeing with oscmix; it
  is the only thing between a file and the register. `_parse_number`
  said the opposite and now says this.
- **The bounds are scaled, and getting that backwards is invisible.**
  `setfixed` divides the OSC value by `.scale`, so `min=-300 max=300
  scale=0.1` is -30.0..30.0 to a config, not -300..300. Declared the
  raw way, every range would be ten times too wide and every
  out-of-range value would reach the device. Measured rather than
  argued: `gain = -10.0` on output 5 moved the device's own level meter
  from -49.4 to -59.5 dBFS, exactly 10 dB. The raw reading would have
  moved it by one.
- **Nested options were escaping the cold-plug rule.** Below.

**Auto level is done**, and it is the number that says whether the
nested shape paid off: 160 registers for one option table of three rows
and a bounds test. Everything else came from the sweep in
`tests/test_sub_families.py`, which now checks all six sub-families --
declared paths equal reported paths in both directions, declared tags
equal reported tags, no meter is a setting, and each section
round-trips. Low cut and crossfeed inherit that for free.

Measured at the device, two points rather than one: with the signal
~49 dB below the headroom target the limit binds, so the rise should
equal `maxgain` exactly. `maxgain = 6.0` lifted output 5 from -49.4 to
-43.4 dBFS, and `maxgain = 12.0` to -37.4 -- **+6.0 and +12.0 dB.** One
point could be a coincidence; two cannot be the wrong scale. Restored
afterwards, and the dump before and after is identical over all 1480
lines.

**Low cut is done**, and it is the family that found the limits of the
sweep and of the "bounds come from upstream" rule.

`freq` is upstream's: `.min=20 .max=500`, no scale. `slope` has **no
bounds upstream at all**, so they came from the device instead --
written and read back on `/output/5/lowcut/slope`, 0, 1, 2 and 3 return
as written and 4, 7 and -1 all return **3**. The device clamps, at four
positions, which is the count RME's low cut has.

That is the one pair of bounds in the model taken from the hardware
rather than from the node table, and it settles a question
`_parse_number` had left open: *the hardware does clamp*, at least here.
One register at one revision is not a rule and nothing relies on it, but
it is why declaring 0..3 is a measurement rather than the invention the
rule warns against.

**`slope` is an index, and saying so was the finding.** The device holds
0 and 1 where a dB/oct reading would hold 6 and 12. Declared "dB/oct" --
which is what it looks like -- `slope = 1` would read as one decibel per
octave. It carries the unit `index` instead, and which index means which
steepness was not measured, so nothing claims it. Nor is it an ENUM:
upstream takes it with `setint` and declares no names, so a config
writing "12 dB/oct" would send a string `oscgetint` drops without a word.

Measured audibly: a 60 Hz tone into output 5 sits at -49.4 dBFS, and the
filter at 500 Hz with slope 3 takes it to **-76.3 dBFS, -26.9 dB**.
Restored afterwards; the dump before and after is identical over all
1680 lines, including after writing 4, 7 and -1 to probe the clamp.

The sweep in `test_sub_families.py` earned itself here twice. It caught
that its own value generator reached for `register.lo` on an option that
has none -- writing the string "None" into a config -- and it caught
`slope` carrying bounds with no unit. The second one was fixed by saying
what the number is rather than by adding an exemption, which is the
outcome that test exists to force.

**Crossfeed is declared, and it is the one family in this release whose
point 4 is not met.** Everything else about it is done: 20 registers,
`/output/{ch}/crossfeed`, the only *flat* per-channel option of 0.4.0 --
it belongs in `[output:5]`, not a section of its own. Upstream declares
no bounds, so 0..5 came from the device: 0 through 5 return as written
and 6, 10, 99 and -1 all return **5**. Six positions, Off plus the five
TotalMix offers. `index` again, for the same reason as `lowcut/slope`.

Being flat rather than nested made it the first new option in
`settable_options` since 0.3.0, which moved three existing tests: the
per-option policy table (it forces a REMEMBER/PIN decision rather than
letting one be inherited -- exactly what it is for), the list of valid
options in an error message, and a test that had been using the word
"crossfeed" as an example of a name this version does *not* know. That
is the third time a test has named a family that later landed.

**Measured, with the baseline established first.** Crossfeed is a
controlled L/R blend for headphones, so the measurement is its
definition. A left-only tone into the phones pair, six steps:

| crossfeed | out 7 | out 8 | bleed |
|---|---|---|---|
| 0 | -58.7 | **silent** | none |
| 1 | -60.0 | -77.8 | -17.8 dB |
| 2 | -60.4 | -74.7 | -14.3 dB |
| 3 | -61.2 | -71.9 | -10.7 dB |
| 4 | -61.6 | -70.9 | -9.4 dB |
| 5 | -62.1 | -70.0 | **-8.0 dB** |

Monotonic in both directions: the cross channel rises and the direct one
gives up a little, which is what a blend does. Restored afterwards, and
the dump before and after is identical over all 1700 lines.

**The first attempt was wrong, and why is worth keeping.** It read
-40.7 dBFS on output 8 at crossfeed 0, and the same setting gave -39.5
and then -41.7 dBFS: about 2 dB of wander against an effect of the same
size, and a right channel that should have been silent. The cause was
the mixer GUI holding UDP 8222, so the level reader saw a split stream.
`LevelReader` refuses to start when the port is held for exactly this
reason, and it was open when the first run started.

The fix was not a better statistic but a baseline: with the GUI closed,
a left-only tone reads -58.7 dBFS on output 7 three times running and
**-144.0** on output 8, and the mirror case is exact. A measurement
whose baseline is not silent cannot say anything about bleed, and
checking that first is what turned an unusable run into a clean one.

Left: nothing. Room EQ is declared as reported and not settable, for the reason below.

### Nested options were classified as promptly reported, and are not

`register_promptly_reported` decides whether an *absent* register is
re-sent, and its own docstring gives the reason it exists: without it
"an `[output:N]` section would be reported unconfirmed on every hotplug
and the whole routing re-sent, every time".

It asked `settable_options`, which knows only a family's **flat**
options. Every nested one fell through to "yes, promptly reported" --
240 paths, measured against the cold-plug recording, of which 480 EQ
registers arrive as 332. Declaring dynamics would have added 320 more.

It now asks the register model directly, through a new
`registers.register_at`, which also replaces a private copy of the same
lookup in the reconciler. The test is a sweep over the model against
`tests/data/cold-plug-timeline.json` rather than a list of paths, so the
next nested family cannot reintroduce it quietly.

Worth naming plainly: this shipped with EQ, in this release, and no gate
caught it. The measurement that would have -- the cold-plug timeline --
was already in the repository.

### The round trip now covers the new sections

Point 3 of the bar, closed for both halves. Before this the dumper knew
only about `[input:N]` and `[output:N]`: a dump of a fully configured
device reconstructed 124 settings of the 604 it reports, and dropped
every global and all 480 EQ registers without a word. Measured against
the device now: **604 channel settings and 38 global settings, 840 lines
against 232.**

The fixed point that holds is *from the second render*, not the first,
and the distinction is real rather than a weakened claim. A remembered
value is written as a comment carrying the device's state (ADR 0012), so
a family that is entirely remembered -- `[echo]` -- keeps its header
while everything under it is a comment the parser is right to drop. The
settings survive from the first render; the file is byte-stable from the
second.

Writing the round trip found two defects that the dumper's own output
looked fine with:

- **`mainout = -1` produced a config that would not load.**
  `/controlroom/mainout` reports -1 for "no main out", and at the pinned
  revision it arrives unnamed, so the dump wrote the raw index and the
  parser refused the file whole (ADR 0006). A dump of a working device
  that could not be read back. It is now a comment naming
  michaelforney/oscmix#30 -- the state is real and worth seeing, it just
  cannot be spelled as a setting yet.
- **`wckout = true` came back as `wckout = 1`.** The renderer decided
  true/false from the Python type, and a bool arrives as `True` from the
  device but as `1` -- the wire form -- from the parser. Both parse, so
  nothing ever failed; the dump of a dump simply stopped matching the
  dump. Fixed the same way `_encode` was: ask the declared domain, not
  the value.

Neither is exotic, and neither would have been found by reading the
output. That is the argument for point 3 being a fixed point rather than
"the dump looks right".

### The pin moved to 55802a6, and what that cost and bought

ADR 0008: the pin may only be bumped together with a fresh hardware
measurement. Here it is, on the UCX II (serial 24216011).

| | 2411b12 | 55802a6 |
|---|---|---|
| registers in a warm `/refresh` | 2002 | **2322** |
| Room EQ registers | 320, folded | **640, real** |
| `/controlroom/mainout` | `('i', (-1,))`, unnamed | `('is', (-1, 'None'))` |

Three upstream commits, two of them ours: `e8151cd` allows
discontinuous enum values and closes #30, `55802a6` fixes the Room EQ
`regtoctl` folding and closes #32. `tests/data/refresh-dump.json` is
re-recorded against the new revision, and the binary it was recorded
from is byte-identical to the installed one -- `record-dump.py` now
refuses to record when they differ, because it mislabelled a recording
exactly once and a fixture that lies about its revision is worse than
no fixture.

**`--diff` reads 2322 registers and reports 0 differences** against the
config on this desk, so nothing regressed in what the session already
did.

**What it cost.** `mainout` grows an eleventh name, and with it the one
enum in the model whose value is not its position: "None" sits at
position 10 and means -1. Upstream's `setenum` reads a `,i` argument as
the **raw value**, so a positional encoder would have written 10 -- not
a main out, not "none", accepted, on the wire, device wrong. Registers
may now declare `values` alongside `choices`, mirroring upstream's own
`.enumvals`, and every other enum leaves it empty because position and
value agree.

The test that asserted `mainout` has ten names now asserts eleven. Its
old docstring named the trap ahead of time -- *"index 10 is not -1 and
not a main out either"* -- which is the argument for writing down why a
thing is absent rather than only that it is.

**Patch 0002 is merged and must not be applied.** PR #31 (patch 0001,
output stereo on write) is still open, so that one stays.

### Room EQ: readable now, still not settable

The pin move made all 640 registers **reportable** -- 320 were folded
onto their own lower half before `55802a6`. It did not make them
writable, and that is a second, separate limit found by measuring
rather than by assuming the fix covered both directions.

Measured at 55802a6 on outputs 1 and 5, with the channel EQ as a
control in the same run:

| write | reads back |
|---|---|
| `/output/N/eq/band1gain = -6.0` | **-6.0** |
| `/output/N/roomeq/band1gain = -6.0` | **0.0** |

The switch and `delay` behave the same way. Output 1 fails too, where
the channel offset is zero and the address is the base exactly, so it is
not the offset arithmetic. The commit that landed is *`Fix regtoctl for
room EQ`* -- `regtoctl` is register→control, the **read** path.
`ctltoreg` maps the same addresses and always did; the writes still do
nothing.

So the family is declared for what it is: 640 registers, modelled,
verifiable, **no value domain**. That is the same line
`/clock/samplerate` and `/hardware/ccmode` sit on -- a config cannot set
what oscmix cannot write -- and it is the honest end of the family
rather than a config section that accepts a shelf and delivers nothing.

**And it found one.** `[roomeq:output:5]` was *accepted* and produced no
settings at all. `_parse_nested_section` returned early on an empty
option set, a guard written for unmodelled devices, which now also
caught a modelled family that is read-only. Two situations, one empty
dict, opposite right answers: an unmodelled device still gets no
opinion, and a family this model knows and declares unsettable is now
refused by name. The audible measurement went the same way -- a Low
Shelf at 120 Hz with 20 dB of cut moved a 60 Hz tone by 0.0 dB, which is
what sent me looking.

**oscmix does send the write, which took a trace to establish.** The
first version of this finding stopped at "writes change nothing", which
would have sent a maintainer into his own code to find nothing wrong.
`aseqdump` cannot subscribe to oscmix's ALSA port, but the bridge is a
pipe -- `alsaseqio` hands the child fd 6 and fd 7 -- so tracing writes
to fd 7 shows the wire. During those two writes, both SysEx messages go
out: register `0x0511` for the channel EQ and `0x35D3` for Room EQ,
exactly what `ctltoreg` maps them to.

The device applies the first and ignores the second, from the same
address block it reports Room EQ values from. That is a device-side
fact, and the open question is whether RME writes Room EQ some other
way -- which a maintainer who has the protocol notes can answer and
this project cannot.

**What is owed upstream.** A short report saying that much. Not filed
yet.

### What had to be decided: settled in ADR 0016

All three are answered, and two of them turned out to be *measurable*,
which was the finding. The section that held them said they had to be
decided rather than measured; looking for the measurement first is what
this project does everywhere else, and it worked here too.

- **The nested-option format.** ADR 0014, first of the release.
- **Which of these are dangerous: none of them.** There is no
  `DANGEROUS` flag in this codebase. `48v` is withheld by having no
  value domain, and the reason is written beside it -- *an off-by-one in
  phantom power is a damaged ribbon microphone*. So the bar is equipment
  damage, and every candidate here is inconvenience instead.

  What separates them from an ordinary setting is whether a write can be
  taken back, and that is measurable without locking anyone out of
  anything, because the OSC path is the way back. Every value of
  `lockkeys`, `opticalout`, `spdifout`, `standalonearc` and
  `clock/source` was written, read back and restored: all landed, all
  restored. `lockkeys = All` included, which was the one worth being
  careful about. Somebody who locks themselves out with a config gets
  back in with a config.

  Stated in the ADR and not glossed over: that holds *while oscmix is
  reachable*. A config that locks the panel and a machine that then
  fails to boot leaves no way back. That is an argument for reading a
  config before applying it, not for withholding the register.
- **Clock is state, measured.** The rate was never a decision:
  `/clock/samplerate` has no `.set` upstream, so a config cannot declare
  it. For the source, set to `Word Clock` **with nothing connected**,
  the device accepts it, reports it, does **not** fall back within eight
  seconds, pushes nothing unprompted, holds 48000 kHz throughout, and
  restores exactly. It keeps what it is told, including state that is
  useless to it, so a pinned source argues with nothing. **PIN stays.**

  The case not measured needs hardware this project does not have: a
  source that is present, in use, and then disappears. Named rather than
  assumed either way.
- **Creative settings stay, as REMEMBER.** The question answers itself
  once `--dump-config` exists: the dump's job is to reproduce a desk as
  a file, and a reverb tail is part of that desk. Leaving it out means a
  dump that silently describes less than the device holds, which is the
  failure this release has now fixed three times.

  What keeps it from becoming a file full of creative decisions is the
  policy column, not the model. REMEMBER means the dump writes them
  commented, carrying the device's value, and nothing sets them unless
  somebody puts them in `[pin]`. The file shows the reverb without
  imposing it.

### What does not change

The bar in *What a new register family costs* applies per family, not
per release: a row in the model checked against a recording, a contract
test that written ⊆ declared, a `--dump-config` round trip, a
`verify-hardware` case if it is audible, and an ADR where a decision was
made. Five families of 42 registers can carry that easily. **EQ alone,
at 480, is the first thing in this project's history where the bar is a
real cost rather than a formality** -- and that is the argument for
doing the global five first and learning what a family costs before
committing to the big ones.

### Also in the release

- `--diff`: **done.** `plan()` printed instead of sent -- it does fall
  out of the reconciler, and the command is thirty lines. Two decisions
  in it were not obvious:

  A **rewrite is not a difference.** `/mix/<out>/playback/<pb>` is never
  reported (ADR 0002) and is written on every apply whatever the device
  holds, so listing it beside a real mismatch would answer "has the desk
  drifted?" with a number that is never zero. They are counted apart.

  **Exit stays 0** when the read succeeds, differences or not. A
  `diff(1)`-style "1 means differing" is more scriptable, but 1 is
  already `EXIT_FAILURE` and a caller could not tell "the desk drifted"
  from "the backend never answered".

  It found a defect the moment it ran on real hardware. Two consecutive
  invocations disagreed -- 1982 registers against 2002 -- and the
  missing twenty were every `/playback/<n>/stereo`. `--dump-config` has
  had that hole since it existed: it never took `DUMP_LISTEN_SETTLE`,
  the wait the verifier takes before asking for a dump, while that
  constant's own test says the cost is paid by "every verification,
  every profile switch and every --dump-config". Measured: without it 4
  of 8 reads lost the whole burst, with it 11 of 11 were complete. So a
  dumped config could not reproduce playback link state, and nothing
  noticed, because the only command that would have shown it is the one
  being added here.
- **The 802: one of the three, and the other two are blocked upstream.**
  A device is supported here when its register table is declared, its
  channel capabilities are recorded, and one evidence artifact exists.

  **Capabilities: recorded.** `device_ff802.c` exists upstream and is
  compiled in, so the map is read rather than guessed -- 30 in, 30 out,
  gain and reference level on the eight analog inputs, 48V and hi-Z on
  the four Mic/Inst channels, reference level on the twelve analog and
  phones outputs. The same derivation was checked against the UCX II
  first: `OUTPUT_HAS_REFLEVEL` gives outputs 1-8 and
  `INPUT_HAS_REFLEVEL` gives inputs 3-8, which is exactly what the
  UCX II already declares. The rule reproduces the known answer before
  it is used on the unknown one.

  Three differences are worth naming, because a capability map that
  quietly matched the UCX II would look declared and mean nothing: 30
  channels against 20, 48V on 9-12 against 1-2, and the 802's Mic/Inst
  channels carrying **no gain register at all**.

  **Register table: blocked, and not for lack of trying.** oscmix cannot
  drive an 802 at the pinned revision, for two independent reasons:
  `init()` holds a device list of exactly one entry, `&ffucxii`, so an
  802 exits with "unsupported device" before anything else runs; and
  `ff802` declares no `.refresh`, no `.regtoctl` and no `.ctltoreg`,
  which are called unguarded in seven places -- `setval` alone has
  three. The upstream table is a stub: channel names and counts, no
  register mapping. A register model against it would describe writes
  that cannot happen.

  **Evidence artifact: impossible without hardware.** So the 802 stays
  `supported=False`, now for a read reason rather than "never tested".

  **What it cost elsewhere.** Declaring capabilities turned a stated
  promise into a narrower one. `[device] name = Fireface 802` with
  `output = 30/31` used to be accepted -- "being listed must not become
  a constraint". It is now refused, with *channel 31 does not exist on a
  Fireface 802 (it has output 1..30)*. The replacement promise is truer:
  being listed constrains a config by exactly what upstream's own table
  says, and never by a guess. A device with no table at all still gets
  no opinion, and that half is asserted separately.

  No ADR: the roadmap already asked for capabilities to be recorded, and
  this is that being carried out rather than a decision taken against
  it. The consequence is written down here because it changed a promise
  a test used to state.

## After 0.4.0: what actually threatens this

0.4.0 finished the surface. Every register the device reports is
declared, measured and either settable or explicitly not. There is no
obvious next feature, and that is the point at which a project either
finds out what it is for or accumulates ornament.

So this section is not a wish list. It is the five things that would
make this unmaintainable, unusable or untrue in a few years, in the
order they are likely to bite.

### A. Everything rests on one maintainer's project

oscmix is the reason this works and the reason it can stop working. One
person, no signed releases, and four of our findings are sitting in his
issue tracker. Room EQ writes (#33), output phase (#34) and the 802 (#4)
are all blocked there, and nothing here can unblock them.

**Started, and the shape is right.** `docs/register-addresses.md` holds
the arithmetic, the wire format and eleven addresses confirmed on the
wire rather than copied out of upstream's source. If oscmix stopped
being maintained tomorrow, the OSC paths would be worth nothing and that
table would still be true.

**Answered, and the answer was that the question was the wrong shape.**
2028 addresses are not 2028 facts: they are three rules over a table of
82 control offsets. The rules are the part that can be misunderstood and
all three are confirmed on the wire. The offsets are a table, and the
risk in a table is availability rather than understanding -- extracted
from upstream on demand, they were not independent of upstream, which
was the entire point.

So "enough" meant *store the table*, not *measure more registers*.
`docs/register-offsets.json` holds all 82 with the revision they came
from, and a test requires it to reproduce every measured address from
the file in this repository rather than from upstream's source. One
commit, not a measurement campaign.

*What this does not do:* it does not make this project independent.
oscmix also carries the SysEx transport, the device discovery and the
register-to-control mapping in both directions. A table of addresses is
the notes, not the instrument.

### B. Nothing notices when the desk drifts

The session applies once, verifies for a minute, and then stops looking.
Turn a knob afterwards and the config and the hardware disagree with
nobody the wiser. `--diff` answers the question, but only when asked.

**The premise this was decided on was wrong**, and it took until 0.4.x
to measure it. ADR 0013 says only `/output/{ch}/stereo` is pushed, so
there is nothing to react to. Measured: the device reports the **partner
channel of a linked pair** for volume, mute, crossfeed and every block of
EQ, dynamics, low cut and auto level. A drift signal needs no clock and
no polling.

*The reason it is still not built:* the registers that stay silent are
`reflevel`, `input/gain` and `input/phase` -- exactly the installation
state ADR 0012 says PIN exists for. A signal built on this would
announce the settings a config mostly leaves alone and say nothing about
the ones it pins.

**Decided, once the measurement was finished: do not build it.** The
half that had never been measured turned out to settle it. Fourteen of
the eighteen settable PIN registers have no channel -- clock, control
room, hardware -- and **all fourteen are silent**. Together with
`input/gain` and both `reflevel`s, that is **one of eighteen** pinned
registers that announces itself: `input/hi-z`.

Meanwhile everything that *does* announce itself -- volume, mute,
crossfeed, EQ, dynamics, low cut, auto level -- is REMEMBER by default,
which is to say precisely what a config leaves to the device.

So a signal built on pushes would report what the file does not own and
stay silent for seventeen of eighteen things it does. That is worse than
no signal, because it is believed: somebody who knows it exists and
hears nothing concludes their pinned reference level still stands, and
it may not. Not a close call; at ten of eighteen it would have been.

**What was built instead:** `--diff` exits 3 when the device and the
config disagree, so the question can be asked by a script rather than
guessed at by a listener. 0 still means they match and 1 still means the
read failed, and keeping those apart is the whole value -- a check that
cannot tell drift from a dead backend reports healthy silence at exactly
the wrong moment.

### C. Security is as closed as it can be here

Done in 0.4.x, and the useful part was finding out how little was left.
`systemd-analyze security --user` went from 8.3 to 5.4, three directives
had been listed as impossible for two releases and were not, and ADR
0017 states the trust boundary instead of implying one.

*What remains is not closable here.* The OSC port authenticates nothing:
any local process can set any register, including the phantom power this
project withholds from config files. That guard is in the config parser
and cannot be moved to the port, which is oscmix's. Stated, not fixed,
and a fix would have to happen upstream.

*The one thing worth revisiting:* `PrivateUsers=yes` is refused here as
untested against ALSA device access. Testing it is an afternoon, and it
is the largest single item left on the exposure score.

### D. The documents cannot answer a question quickly

This file is 2000 lines and is a chronicle: it records what was measured
and when, which is exactly right for auditing a claim and exactly wrong
for finding out how something works. Most facts about this project live
here and nowhere else.

**Done.** `docs/ARCHITECTURE.md` is rewritten in the present tense: the
system it sits in, the pipeline every command is a stopping point along,
the module table, the register model as data, pin and remember, the
two-phase apply and the two seams. History stays here, decisions stay in
the ADRs.

The page had not "failed to keep pace". It described the 0.2.0 system
and mentioned none of `reconcile`, `desired`, `plan`, `snapshot` or
`profiles` -- because nothing checked it, so there was nothing to
notice. **Three tests now do**: every runtime module must appear in its
table, every module in the table must exist, and version numbers in the
body fail because a sentence about how we got here is the kind nobody
updates. All three were verified against a deliberately broken page.

*Why it matters more than it sounds:* this project's whole method is
that reasoning is written down. A chronicle nobody can navigate is a
place where reasoning goes to be lost, which is the failure it was
built to avoid.

### E. The mutation score is expensive and says less each release

An hour per run, and ADR 0015 had to exempt the register table before
the number meant anything at all. Since then it has moved by a
thousandth across four register families, which is the exemption
working -- and also the number telling us very little.

**Answered, and the answer was in the history rather than in a
measurement at the device.**

The workflow's own comment said *"re-measure when a run passes 60 -- and
check the slowest tests before touching this number"*. Three runs in a
row had passed it, at 69, 74 and 73 minutes, so the instruction applied.
Checked, and **there is no hot spot**:

| | 0.3.0 | now | |
|---|---|---|---|
| tests | 665 | 945 | +42% |
| suite | 73 s | 106 s | **+45%, proportional** |
| mutants | 4180 | 4866 | +16% |
| mutation | 44 min | 72 min | +64% |

Cost is mutants × suite time, and `4180×73 → 4866×106` predicts +69%
against +64% measured. The growth is entirely explained. Shortening the
link barrier was tried: it saves 1.6 s of 106 and breaks two tests.

*So the cost is not a defect, which is the problem.* It grows
multiplicatively with the project and would be past two hours by the
next release, for a number that **has never failed its gate in any
run**.

*What the value actually was:* reading survivors found three real
defects in 0.3.0, including pinning silently not working while every
test stayed green because they all used a `[pin]` override. The commit
says it plainly: *"the policy was green either way"*. The score did not
catch them; a score **movement** prompted somebody to look.

**Moved to nightly.** A nightly change still prompts that reading, one
day later, and the release checklist keeps its own run where the number
has to be current rather than recent. Per-push feedback drops from 72
minutes to about five.

Three tests hold the arrangement: the mutation job must be conditioned
on `schedule`, the workflow must actually have one -- a job conditioned
on a trigger the workflow lacks never runs again and nothing would say
so -- and no other job may quietly follow it off the push path.

### F. A profile is undone by the next start -- **closed, ADR 0018**

`--profile X` wrote X to the device and exited; the service kept
`routing.conf`, and the next start or reload re-applied it over X. The
resume hook made that a routine event: suspend, wake, profile gone,
nothing in the journal saying why. 0.6.2 documented it; the release
after remembers the profile beside `routing.conf`, applies it on every
start and reload, falls back to `routing.conf` with a standing warning
when it no longer loads, and adds `--no-profile`. What an active
profile means when `routing.conf` changes underneath it is decided in
[ADR 0018](decisions/0018-the-active-profile-survives-a-start.md):
the machine settings follow `routing.conf`, the desk follows the
profile, and the start line says so.

### G. A second switch can still overlap the unit's reconcile -- **closed, ADR 0019**

Found in the 0.6.4 release's live test and recorded in ADR 0018. A
switch holds the lock while it writes and reads back, then releases it
and reloads the unit; `ExecReload` is a plain `kill -HUP`, so the
reload returns before the unit reconciles. A second switch waiting on
the lock starts at once, and if the unit's reconcile begins in a gap
where that switch does not hold the receive port, the unit re-applies
the first switch's desk while the second writes its own. The second
switch's reload re-applies its desk afterwards, so the end state is
right; the guarantee in between is not.

**The fix is one lock for every writer.** The unit takes the same
`flock` around its start-up apply, its verifier and every reconcile, so
a switch waits for the unit exactly as it waits for another switch, and
the `Status:` polling becomes unnecessary. What that needs: the
installer creates `active-profile.lock`, because the unit cannot create
it under `ProtectHome=read-only` (`flock` itself works on a read-only
descriptor); the verifier's hold of about 22 s has to stay inside
`SWITCH_LOCK_WAIT`; and a switch's reload moves inside its lock.
**Closed in 0.6.5.** The unit holds `active-profile.lock` across its
start-up apply and verifier, and around every reconcile; the installer
creates the file because `ProtectHome=read-only` stops the unit from
creating it, and `flock` holds on the read-only descriptor it can open.
The phase polling is deleted. *Proven by:* tests that hold a reconcile
against a lock taken elsewhere and a start against the same, plus the
live run on the desk -- a switch right after a restart waited for the
unit's transaction, and two switches at once still serialised.

### The limit under all of this

**Every measurement in this repository was taken on one Fireface UCX II,
serial 24216011, on one machine.** Every bound, every timing constant,
every "the device does X" is that device's behaviour. The 802 has never
been tested and its register table cannot be written. There is no second
unit, no second Linux, and no user other than the author.

That does not make the measurements wrong. It makes them narrower than
their wording, and the roadmap should say so once, plainly, rather than
qualify every sentence.

## 0.5.0 -- the write direction, proven

**Every settable register is declared `verifiable`**, and that word is a
promise: write it, and the device reports the new value back. The read
half of that promise was measured -- the recordings prove the model
matches the dump. The write half never had been, and the verification
apparatus this project is built on runs entirely in the read direction.
**What nobody had ever asked the device is whether a register it reports
is a register it accepts.**

That was the release. Not new surface -- the surface is finished -- but
the other half of a claim already made about it.

**Result: 1224 of 1238 confirmed, 14 skipped as dangerous, none
ignored, nothing left unrestored, in 101 seconds.** The per-register
verdict is in `docs/evidence/write-sweep-ucx2.json` with the device
serial and the oscmix pin, and two tests check that file against the
model rather than trusting it.

### The design the plan called for could not work

The plan said: write a register, wait for the device's report, compare.
That is impossible here, and the first trial run said so by reporting
`/output/1/volume` as deaf -- a register this project sets on every
start.

**A write is never echoed on the path it was written to.** Measured on
the UCX II:

| written | reported |
|---|---|
| `/output/3/volume` | `/output/4/volume`, carrying the value written |
| `/output/4/volume` | `/output/3/volume` |
| `/input/1/mute` | `/input/2/mute` |
| `/input/1/gain` | nothing at all |

A dump afterwards shows both channels of a linked pair moved, and that
`/input/1/gain` took its value. So the writes land and only the
*reporting* is partial -- most likely oscmix suppressing what matches
its own cache, which it updates optimistically on the write. Combined
with the device reporting only on change, the path just written is the
one message systematically missing.

So the sweep writes a pass and reads the result back from a refresh
dump. That turned out to be far cheaper than the plan assumed, because
the cost is per *pass* and not per register: 1238 registers cost the
same twelve dumps as twenty would.

### Three false verdicts, and what each one taught

Every one of them said "deaf" about a healthy register, and every one
was caught the same way -- by testing the unbelievable result alone and
unhurried instead of reporting it. Delivering the first run would have
filed 44 device defects that do not exist.

**Bursting a pass loses writes.** oscmix turns each write into a MIDI
SysEx message and that wire carries about a thousand registers a
second; 295 writes in a few milliseconds lost 40. The lost ones read as
`ignored` -- including every `lowcut` and `autolevel` switch, whose
upstream setter and register mapping both check out perfectly. Writing
`/input/1/lowcut` alone takes the value. Writes are now paced, and
every register gets three attempts rather than a bool being condemned
by its single dropped datagram.

**A linked pair written against itself looks clamped.** Writing channel
3 drags channel 4 with it, so a batch containing both leaves the first
reading a value it was never asked for. Passes are split by channel
parity; no pair is ever written against itself.

**Absolute steps on an unbounded register probe outside it.**
`/reverb/width` sits at 0.6 on a scale that stops at 1.0, and with no
declared range the probe fell back to absolute steps and asked for 1.6,
10.6 and 50.6. The device *rejects* rather than clamps, so it refused
all three without a word. The sweep no longer calls that `ignored`:
where no range is declared, a deaf register and a probe that never
landed inside one are indistinguishable, and saying so is
`undetermined`.

### What it found, once it was right

Not deaf registers. **Two defects in this repository's own model**, both
of the kind that makes a config silently ineffective while behaving
perfectly on the read side.

**Input gain is three registers, not one.** Upstream's channel table
gives `.gain={0, 750}` to the two mic preamps, `{0, 240}` to the two
instrument channels, and *no range at all* to Analog 5-8, which leaves
`setinputgain` clamping them to `{0, 0}` -- a control present in the OSC
tree that can never do anything. The model declared one row, 0..75 dB
on channels 1-8, promising a config 75 dB where the device silently
gives 24, and gain on four channels that have none. Now three rows: mic
0..75, instrument 0..24, and line readable with no value domain, the
same shape as `48v` and the output phase.

Two lookups had to become channel-aware to carry it, and one of them
was already wrong: `register_at` answered `/input/9/gain` with a
register whose capability stops at 8.

**Three global registers had no bounds while the device enforces
some.** `/echo/width`, `/reverb/width` and `/reverb/smooth` are
unbounded in upstream's node table, so they were unbounded here under
the standing rule that an invented range would reject values the device
accepts. But the device refuses an out-of-range write outright -- no
clamp, no report -- so a config asking for `width = 1.02` would have
been ignored in silence. The bounds were bracketed against the hardware
(0.0 and 1.0 accepted, -0.01 and 1.02 refused; 0 and 100 on smooth, -1
and 101 refused) and are declared now.

That narrows the rule rather than repealing it: ten reverb numbers stay
unbounded because nobody measured them, and `/reverb/volume` still
carries no ceiling copied from `/echo/volume`.

### The step size is the discriminator, not a parameter

A register that does not move after a write is ambiguous. Either the new
value quantised onto the old one, in which case stillness is correct and
the device is behaving exactly as documented, or the write was ignored.

**A second, larger step separates them.** Of the 1224 confirmed, 1164
answered the first step and 60 needed the third -- so the escalation was
not decoration. Starting small also keeps the sweep quiet on families
that sit in the signal path, instead of stepping every fader through ten
percent of its range.

### reflevel is named, not measured

Fourteen `reflevel` registers are the only members of ADR 0016's
dangerous class that are settable at all -- `48v` correctly has no value
domain, so a config cannot reach it and neither can the sweep. Changing
a reference level on a live output is audible and potentially loud.

**They are skipped, and the artifact says so per register.** "Skipped,
dangerous" is a result. Quietly omitting them and reporting 1224 of 1224
would not be.

### What it does not prove

That a register does what its name says. A confirmed write proves the
value round-trips; it does not prove `/output/5/crossfeed = 3` changes
anything anyone can hear. That is `verify-hardware`'s ground, and it
covers routing only.

**So this release closed a class of defect, not the gap between declared
and audible.** The declared surface is 1238 settable registers; the
audible evidence is a handful of routes. Naming that ratio is more
useful than narrowing it, because narrowing it means an audio
measurement per register family and this project has one room, one
device and one pair of ears.

Nor does the artifact stay true by itself -- but it cannot rot unseen:
`test_the_artifact_covers_every_settable_register` compares it against
the model, so a new settable register fails the suite until the sweep
has run again.


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
