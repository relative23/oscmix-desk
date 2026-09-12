# 0020 -- The desk is read under the lock that writes it

## Status

Accepted, 0.6.6. Extends
[ADR 0019](0019-one-lock-for-every-writer.md).

## Context

0.6.5 gave every writer of this project the same lock: a switch,
`--no-profile`, and the unit's start-up apply, its verifier and every
reconcile. That closed the interleave on the wire, and an outside review
of the release pointed out what it did not close.

A lock serialises *writing*. It says nothing about *when the thing to
be written was decided*. Both writers in the unit decided first and
locked second:

- the start parsed the config in `cli.main`, waited for the device, the
  backend and its port, and only then took the lock in
  `_apply_and_verify`;
- the reconcile read `effective_config` and took the lock after it.

So a switch that committed in that window was overwritten by a snapshot
older than it, with no interleaving at all. Two writers, correctly
serialised, wrong end state.

## Decision

**The desired state is read inside the lock that writes it.**

- `_apply_and_verify` takes the lock, checks that a stop has not
  arrived while it waited, and then re-reads the desk with
  `_desk_under_the_lock`.
- `_reconcile` takes the lock first and reads `effective_config` after.
- Machine settings do not come from that read. The OSC ports and the
  device name belong to the process that is running, not to the desk,
  so they are transplanted from the running config. That rule already
  existed in the reconcile and is now the same in both.
- A config that stopped parsing between the two reads is not fatal:
  the start applies the desk the process came up with, the reconcile
  keeps the running one. Both say so.

## Consequences

- A profile switch that lands while the unit is starting or reconciling
  is the desk that ends up on the device, whichever of the two gets the
  lock first.
- The start now touches the config file a second time. It is one parse
  of a file the process already read, inside a lock it already holds.
- `_apply_and_verify` grew a second reason to return without writing:
  a stop that arrived during the wait. A process on its way out must
  not write a full routing, and until 0.6.6 it could.

## Alternatives considered

- **Compare a generation number before and after the lock**, and re-read
  only when it changed. One more piece of state to keep truthful, for a
  read that costs a parse of a small file. Rejected.
- **Hold the lock from the first parse.** The start parses before it
  knows whether there is a device at all, and would then hold the lock
  across the ALSA wait, the backend spawn and the port wait. A switch
  would queue behind all of it. Rejected.
