# 0026 -- A profile is the desk, not the machine

## Status

Accepted. 0.6.11 warns and stops the wrong writes; 0.7.0 refuses
`[osc]` and `[device]` in a profile. Amends
[0011](0011-a-profile-switch-states-its-outcome.md) and
[0018](0018-the-active-profile-survives-a-start.md), which let a profile
state machine settings "and then it wins", and
[0024](0024-one-interface-resolved-once.md), which pinned a re-read desk
to the running interface and applied it.

## Context

Two rules had grown side by side, each reasonable alone:

* **A stated machine setting in a profile wins** (0011, 0018) -- port,
  receive port, device name, usb id, serial. The reason given was a
  machine with a second backend, whose profiles would be per-backend.
* **Machine settings belong to the running process** (0024). A reload
  keeps the ports and the interface the backend is bound to.

A second outside review of 0.6.10 put them together. With
`routing.conf` on UDP 9001 and serial A, and a profile stating 9500 and
serial B:

1. `--profile second` resolves B, takes B's lock, writes B, records the
   marker, and reloads the unit, because the switch was for the unit's
   config file.
2. The unit re-reads the desk in effect -- `second` -- pins A's settings
   over it, and writes the same desk to A.
3. After a restart the unit loads `second` with B's settings and moves.

One persisted profile, three targets. And because the device lock is
per interface (0022), two such profiles hold two locks over one
`active-profile` marker -- and shared one `active-profile.tmp`.

Nothing in this was an untested corner. `test_a_profile_that_states_a_
port_keeps_its_own` pinned the first rule; the 0.6.9 re-read test pinned
the second *with a profile that names another box*.

## Decision

**A profile describes what is routed where and at what level. It does
not say where the desk is plugged in.** `[osc]` and `[device]` belong to
`routing.conf`; the README has said "leave them out of a profile" since
profiles exist. A machine with two backends has two config directories,
each with its own `routing.conf`, profiles, marker and unit -- which is
also the only arrangement in which "the active profile" has one answer.

In 0.6.11, without changing what an accepted file means (ADR 0006):

* A profile that states `[osc]` or `[device]` still wins, and is told
  where its desk is written that 0.7.0 refuses it.
* A re-read desk that resolves to another device name, usb id, serial or
  port than the session was started with is **not applied**: the
  reconcile is skipped, the error names what differs, a restart follows
  it. What the file resolved to is compared (`Config.loaded`), not the
  live attributes, so `--device`, `--osc-port` and the pinned serial do
  not read as a change.
* A switch to such a profile does not reload the unit.
* The marker's temporary file has a unique name.
* "One main config per directory" is stated: the marker and `profiles/`
  belong to the directory, while a reload matches the unit by file.

In 0.7.0 the sections are a `ConfigError` in a profile, naming the file
and the remedy, and the inheritance code goes: a profile is read onto
its main config's machine settings and cannot change them. The device
lock then covers the marker, because every profile of a directory has
the directory's interface.

## Rejected

**Keep per-backend profiles and add a config-state lock.** It would
serialise the marker, and leave the question it serialises unanswered:
which of two backends is "the" active profile for? It also puts a second
lock, with an ordering rule, in front of every switch for the sake of a
layout a second directory expresses without any code.

**Restart the unit when a switch changes the target.** A switch would
then stop the backend of interface A to start one for B, leaving A
unmanaged, from a command whose name says "profile".

## Consequences

A setup that relied on a profile's own `[osc]`/`[device]` has to move
them by 0.7.0. It is told on every switch and start until then, and the
changelog will say that a file which loaded now refuses.
