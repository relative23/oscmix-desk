# 0011 -- A profile switch states its outcome; it never raises

## Status

Accepted, 0.3.0. Measured on a Fireface UCX II, serial 24216011.

## Context

A profile switch writes registers to a device somebody is listening to.
There is no rollback for a mixer: once `/output/1/volume` is on the wire
the monitors are already loud, and "undo" is another write that is just
as likely to be the half that lands.

That makes the interesting question not "did it work" but **how far did
it get**, and the caller has to be able to tell three situations apart:

- nothing was written, so the old state is intact and a retry is free;
- everything was written and the device confirmed it;
- everything was written and the confirmation is unavailable.

An exception collapses the first into the same shape as a partial
failure, and a boolean collapses the second into the third. Both were
tried on paper and neither survives the question "may I re-run this?".

## Decision

`profiles.switch_profile()` returns an `Outcome` with exactly one of
three states, and never raises for a bad config.

| state | written? | means |
|---|---|---|
| `REFUSED` | no | the config did not parse, the profile is missing, or the name was not a name |
| `APPLIED_VERIFIED` | yes | every expected register reported back at its value |
| `APPLIED_UNVERIFIED` | yes | written; `unverified` lists what was not confirmed |

`Outcome.applied` is derived from the state, not stored beside it. Two
sources for one fact is how "applied, but the flag says otherwise"
happens.

`STATES` is asserted exhaustive by a test, so a fourth state is a
deliberate design change rather than something that arrives by
accretion. The fourth state this forbids has a name: *"partly, and here
is a traceback"*.

**Validation happens before the first datagram, not around it.** The
refusal path never constructs a backend. That is the property that makes
switching safe on a live desk, and it is measured rather than asserted:
a counter bound to the send port records 0 datagrams for a refused
profile and 5 for a good one, through the same instrument, seconds
apart.

**`unverifiable` is separate from `unverified`.** On any profile with a
route, `/mix/<out>/playback/<pb>` is never reported back -- measured,
and declared as `backend.Traits.dumps_playback_matrix`. So the common
outcome of a perfectly good switch is `APPLIED_UNVERIFIED`, and
reporting that as "could not confirm 1 register" in the same words used
for a genuine miss would train people to ignore the message that
matters. `APPLIED_VERIFIED` is reachable for profiles that pin channel
state without touching the matrix, which is a real thing to want.

## Consequences

- A shell script branches on the exit code: `EXIT_CONFIG` for a refusal
  (nothing happened, same code a bad `routing.conf` gives at startup),
  `EXIT_OK` otherwise. Applied-but-unverifiable is not a failure; on a
  desktop with the mixer GUI open it is the *normal* case, because the
  GUI holds UDP 8222.
- A profile inherits `[osc]` and `[device]` from the main config unless
  it states them. Machine settings are the machine's; a profile
  describes the desk. This is not tidiness: a profile without `[osc]`
  fell back to the compiled-in default 7222 during development, which on
  the development machine is the live backend, and a unit test moved a
  fader on real hardware.
- The switch does not implement its own apply or its own read-back. It
  calls `routing.apply_routing` and `verify.verify_routing` with its own
  backend. The first version did both itself and dropped the link
  barrier: 48 ms for a switch whose barrier alone is 1.5 s, which is the
  0.2.0 stereo-link race on a new write path.

## Alternatives considered

**Raise `ConfigError`, return `None` on success.** Cheapest, and it is
what the rest of the codebase does at startup. Rejected because the
caller then cannot distinguish "nothing was written" from "written,
unconfirmable" without catching a type and inspecting a log.

**A two-phase commit: dry-run, then apply.** Appealing, and wrong for
this device. There is no transaction on the wire; a dry run proves the
config parses, which is exactly what `REFUSED` already guarantees, and
it proves nothing about the second half.

**Roll back on partial failure.** Rejected: the rollback is another
burst of writes with the same failure modes, applied to a state nobody
observed. A torn mix that at least matches a config the user wrote is
better than one matching neither.

## Amended in 0.6.10

The outcome maps to more than two codes since 0.6.6: 4 when the switch
was applied but its marker could not be written, 5 when the unit refused
the reload; a `--diff` that differs exits 3 (ADR 0019).

## Amended in 0.6.11

"A profile inherits `[osc]` and `[device]` from the main config unless
it states them" stays true in 0.6.x, and ends for a profile that states
*another* machine: [ADR 0026](0026-a-profile-is-the-desk-not-the-machine.md).
Restating the main config's own values, as a dumped profile does, stays
accepted.

## Amended in 0.7.0

Three states became four. "Partly applied" was to be unrepresentable,
and a wire that gives out half-way represented it anyway, as a traceback
out of a function that promises an outcome:
[ADR 0027](0027-a-write-that-fails-part-of-the-way-is-a-state.md) names
it `WRITTEN_IN_PART`, with the registers that went out and the ones that
did not. The rejection of a rollback above stands.

