# 0027 -- A write that fails part of the way is a state, with both lists

## Status

Accepted, 0.7.0. Amends [0011](0011-a-profile-switch-states-its-outcome.md).

## Context

ADR 0011 gave a switch three outcomes and said of a fourth: "partly
applied, and here is a traceback" is the state the module exists to make
unrepresentable. It rejected a rollback on partial failure, rightly, and
said nothing about what a switch *reports* when the write itself fails.

A third outside review asked. Measured before anything changed: a
backend whose socket gives out after *k* registers -- `ENETUNREACH`, a
firewall's `EPERM`, no descriptors left -- raised `OSError` out of
`switch_profile` and `restore_main`, whose contract is "an outcome, never
an exception". `--profile X` ended in a traceback with *k* registers of
the profile on the device, the marker untouched, and nothing said about
which registers. The unrepresentable state was represented, as the
traceback.

## Decision

* `Backend.send` accounts for its burst: a socket error raises
  `WriteFailed`, an `OSError` that carries the register paths handed to
  the kernel before the failure and the ones that were not. A socket
  that cannot be had at all is a burst of which nothing went out.
  `apply_routing` extends that to the whole plan across its three
  bursts.
* Handed to the kernel is what "written" means. A datagram socket knows
  no more, and it is still the difference between "nothing happened" and
  "the desk is between two configs".
* Nothing written is `REFUSED`, like every other reason the desk was not
  touched, and exit 2.
* Something written is a fourth state, `WRITTEN_IN_PART`, with both
  lists in plan order. The marker is left alone: the desk in effect is
  still the previous one, so a reload or a start writes it back, which
  is the repair -- and the command asks the unit for that reload, where
  an applied switch whose marker could not be written must not (ADR
  0019). Exit 1: the switch did not happen.
* A read-back whose request cannot be sent, after a complete write, is
  `APPLIED_UNVERIFIED` and not read back, with the cause.
* Because `WriteFailed` is an `OSError`, the start, the verifier and the
  reconcile stand down exactly as they did. Only a switch reads the
  lists, because only a switch has to say what it did.

## Alternatives considered

**Roll back.** Rejected in 0011 and still: the rollback is another burst
over the wire that just failed.

**Report it as `APPLIED_UNVERIFIED`.** The marker would move and the
unit would be told to follow a desk that is not on the device.

**Report it as `REFUSED` whatever went out.** `REFUSED` means nothing was
written, and scripts branch on that.

## Consequences

`STATES` has four members and the test that holds it exhaustive says
why. `Outcome` gains `written` and `unwritten`; `applied` stays "whether
anything reached the device", which is true of this state. No transaction
is claimed anywhere: the README and the architecture page said a profile
is "switched as a transaction", and say "under one lock, in a fixed
order, with a stated outcome" now.
