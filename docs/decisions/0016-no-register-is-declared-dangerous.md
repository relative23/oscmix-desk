# 0016 -- No 0.4.0 register is declared dangerous, and the clock source stays pinned

**Status:** accepted (0.4.0)

**Clarification in 0.7.1:** REMEMBER does not mean “never written”. A
declared value is sent on initial apply; selective reconciliation then
leaves the device's value. Only dump rendering comments it out. The
historical wording in decision 3 below refers to that export behavior.
Sweep restoration now shares the probe permission checks, including
the explicit exclusions for phantom power and reference level.

## Decision

Three questions the roadmap carried as *"has to be decided, not
measured"* are settled here. Two of them turned out to be measurable
after all, which is most of what this record is for.

1. **None of 0.4.0's registers is withheld as dangerous.** The bar stays
   what `48v` set: withhold a register when a wrong value damages
   equipment. Disruption is not that bar.
2. **`/clock/source` stays PIN.** The device keeps what it is told, so a
   config that declares a source has no hardware behaviour to fight.
3. **Reverb, echo and the other creative settings stay in the model, as
   REMEMBER.** A dump has to be able to reproduce the desk; REMEMBER
   means the file shows them without setting them.

## 1. Dangerous, and what the word already meant here

There is no `DANGEROUS` flag in this codebase, and looking for one is
how this got answered. `48v` is withheld by having **no value domain**,
so `settable_options` cannot reach it, and the reason is written next to
it: *"an off-by-one in a silent output is a bug; an off-by-one in
phantom power is a damaged ribbon microphone."*

So the criterion is equipment damage, not inconvenience. The candidates
the roadmap listed are all inconvenience:

- `/clock/source` costs downstream devices their lock,
- `/hardware/lockkeys = All` locks somebody out of their own front panel,
- `/hardware/{opticalout,spdifout,standalonearc}` change what the box
  does with no computer attached.

None can damage anything. What actually separates them from an ordinary
setting is whether a write can be taken back, and **that is measurable**
without locking anyone out of anything, because the OSC path is the way
back. Measured on the UCX II, every value of every candidate written,
read back, and restored:

| register | values tried | all landed | original restored |
|---|---|---|---|
| `lockkeys` | Off, Keys, All | yes | yes |
| `opticalout` | ADAT, SPDIF | yes | yes |
| `spdifout` | Consumer, Professional | yes | yes |
| `standalonearc` | Volume, 1s Op, Normal | yes | yes |
| `clock/source` | Internal, Word Clock | yes | yes |

`lockkeys = All` was the one worth being careful about, and it is fully
recoverable: the front panel is locked by a register, and the register
is writable from the same place that locked it. Somebody who locks
themselves out with a config gets back in with a config.

**What this does not cover.** These are recoverable *while oscmix is
running and reachable*. A config that sets `lockkeys = All` and a
machine that then fails to boot leaves a locked front panel and no OSC
path. That is a real scenario and it is an argument for reading a config
before applying it, not for withholding the register: the same is true
of `opticalout`, and of the routing itself.

## 2. Clock: state, measured

The *rate* was settled earlier and is not a decision at all.
`/clock/samplerate` is `{"samplerate", CLOCK_SAMPLERATE, .new=...}`
upstream, a reporter with no `.set`. A config cannot declare what oscmix
cannot write, so the question "should a config pin the sample rate"
never arises.

The *source* is settable, and the open question was whether the device
decides it for itself. It does not. Set to `Word Clock` **with nothing
connected**:

- the device accepts it and reports `Word Clock` on the next refresh,
- it does not fall back to `Internal`, not within eight seconds,
- it pushes nothing unprompted in that time, consistent with the earlier
  finding that only `/output/{ch}/stereo` and `/clock/samplerate` are
  pushed at all,
- `/clock/samplerate` stays 48000 throughout,
- and setting it back to `Internal` restores it exactly.

So the source is state the device holds, including state that is
currently useless to it. A config that pins a source is not arguing with
anything. PIN stays.

**The case not measured** is the one that needs hardware this project
does not have: a source that is present, in use, and then disappears.
Whether the device falls back *then* is unknown, and if it does, a
pinned `Word Clock` would be re-asserted on the next start. That is
worth knowing before anyone runs this in a word-clocked studio, and it
is stated here rather than assumed either way.

## 3. Creative settings in a config file

Not measurable, so this half is a judgement, and the judgement is that
the question answers itself once `--dump-config` exists.

The dump's job is to reproduce a desk as a file. A reverb tail is part
of that desk. Leaving reverb, echo and the EQs out of the model would
mean a dump that silently describes less than the device holds, which is
the failure mode this project has fixed three times already.

What keeps that from becoming "a config file full of creative
decisions" is the policy column, not the model. Everything here defaults
to REMEMBER (ADR 0012): the dump writes them as commented lines carrying
the device's value, and they are only set if somebody puts them in
`[pin]` on purpose. The file *shows* the reverb and does not *impose*
it, which is the distinction the roadmap was reaching for.

## Consequence

The 0.4.0 model exposes every register the device reports and oscmix can
write, with no withheld list beyond `48v`. If a future family can damage
equipment, it gets `48v`'s treatment and an ADR saying so, and the
question to ask is that one, not "does this feel risky".
