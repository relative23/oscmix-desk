# 0012 -- Pin and remember are a column in the register table

## Status

Accepted, 0.3.0. Every claim below measured on a Fireface UCX II,
serial 24216011, against the pinned upstream revision.

## Context

[ADR 0003](0003-declared-registers-only.md) settled that a
routing touches only the registers a config names. That left one question
open, and the roadmap has carried it since: *of the registers it does
name, who wins after the initial write?*

The words suggest a strong answer. "Pin" sounds like "the device snaps
back when anything else changes it". Before designing anything, the
roadmap asked for the measurement that decides whether that is even
possible: **does a change made by someone else show up as a report on the
receive port?**

It mostly does not. Of every register a config can set, exactly one is
pushed when it changes:

| register | changed | pushed to listeners |
|---|---|---|
| `/output/{ch}/stereo` | 1 → 0 | **yes** |
| `/playback/{ch}/stereo` | 1 → 0 | no |
| `/output/{ch}/volume` | 0.0 → −12.0 | no |
| `/output/{ch}/mute` | 0 → 1 | no |
| `/output/{ch}/reflevel` | +13dBu → +4dBu | no |
| `/input/{ch}/hi-z` | 0 → 1 | no |
| `/input/{ch}/gain` | 0.0 → 6.0 | no |

Each row read the value first, wrote a different one, and confirmed by a
later dump that it really changed -- a register that did not change
cannot be evidence about echoes. An earlier pass without that check
produced four confident "no"s that meant nothing, and a later pass with
`/refresh` traffic overlapping the observation window produced a
confident "yes" for `volume` that was the tail of the previous dump.

`/output/{ch}/stereo` is pushed because the device echoes it over MIDI --
the echo the two-phase apply already waits for. Everything else changes
silently. Seeing it requires a `/refresh`: 2002 registers, against a
device already streaming ~880 meter datagrams a second.

**So the strong reading of "pin" is not available at any sensible price,
and this ADR does not pretend otherwise.**

What was there instead was an accident. A fader turned 0.5 s after a
restart came back at the config's value; the same turn at 1.5, 3 and 6
seconds survived -- and the 0.5 s case was overwritten by the ordinary
start-up apply, not by the verifier, because the apply finishes around
1.8 s. The cut-off between "the config wins" and "the user wins" was how
long the apply happened to take.

## Decision

Every register in the model declares a **policy**, `PIN` or `REMEMBER`.

**PIN** -- the config wins for as long as this session is still looking.
A device value that disagrees is a mismatch: the read-back re-sends it,
and so will any future reconcile trigger.

**REMEMBER** -- the device wins after the initial write. The value is
applied at start and then let go. A later disagreement is somebody having
turned something, which is information and is logged as such, never a
fault and never re-sent.

The default is REMEMBER, because that is ADR 0003's rule and a register
that forgot to declare a policy should fall on the side that surprises
nobody. PIN belongs to registers that describe the *installation* rather
than a preference:

- **pinned:** the routing itself (`/mix/*`, every `stereo` flag) and what
  has to match the cable that is plugged in -- `reflevel`, `gain`,
  `hi-z`, `48v`. A wrong value there is a signal problem, not a taste.
- **remembered:** what a person reaches for during a session --
  `volume`, `mute`, `phase`.

A `[pin]` section overrides it per option, as `<family>.<option> = pin |
remember`. A **section** and not an option inside `[output:N]`, and that
is not style: ADR 0006 makes an unknown option in a known section an
error, so putting it there would mean every config using it is rejected
whole by 0.2.x -- the routing gone, on a machine that updated its config
before its package. An unknown section only warns, so this degrades to
"the table defaults apply", which is what those versions already do.

The same column answers what `--dump-config` writes down. A dump cannot
tell "I meant this" from "this is where I left it", so pinned options are
emitted as config and remembered ones as comments carrying the value.
Uncommenting is a decision a person makes, which is exactly the decision
a dump cannot make for them.

## Consequences

- **The re-apply had to learn the policy too.** `verify_and_repair`
  repairs by re-sending the *whole* routing, so a single unconfirmed link
  register dragged every remembered fader back. Measured: with the
  policy honoured only in the decision, a fader moved to −20.0 dB came
  back to the config's −6.0 dB in the run that had explicitly remembered
  it. `apply_routing` now takes `leave_alone`. The lesson is the one this
  release keeps relearning -- a policy that is real in the log and absent
  at the device is not a policy.
- **The distinction is visible at the device.** Two configs identical but
  for a `[pin]` section, both writing `/output/1/volume = -6.0`, with the
  register then set to −20.0 before the read-back: *remember* leaves
  −20.0, *pin* returns −6.0.
- **It is honest about its reach.** Pinning holds through the read-back
  window and would hold through any later reconcile trigger. There is no
  such trigger yet, and adding one is bounded work
  (`SIGHUP`, resume, hotplug) rather than the continuous reconciliation
  the measurement rules out.

## Alternatives considered

**Continuous reconciliation.** The device snaps back within a second of
any change. Ruled out by measurement rather than by taste: the registers
a config pins do not report their own changes, so this means polling a
2002-register dump forever, and the roadmap's social objection stands
anyway -- a user watching a knob undo itself files a bug.

**Keep it implicit: declared means pinned.** What the code did, and it is
defensible until you look at the clock. It made the answer depend on how
long the apply took, which is not a decision anybody made and not one a
user can predict.

**Per-channel policies.** `[pin] output.5.volume = pin`. Rejected for
now: the question "should my monitor faders come back after a restart" has
one answer per installation, and the per-channel form is four more lines
of config for a distinction nobody has asked for. If a real case turns
up, the key grows a channel and existing configs keep meaning what they
meant.
