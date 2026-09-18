# 0026 -- A profile is the desk, not the machine

## Status

Accepted. 0.6.11 stops the wrong writes and warns; 0.7.0 refuses a
profile that names another machine than its `routing.conf`. Amends
[0011](0011-a-profile-switch-states-its-outcome.md) and
[0018](0018-the-active-profile-survives-a-start.md), under which a
profile that states machine settings keeps them, and
[0024](0024-one-interface-resolved-once.md), which pinned a re-read desk
to the running interface and applied it.

## Context

Two rules had grown side by side, each reasonable alone:

* **A machine setting stated in a profile wins** (0011: "inherits ...
  unless it states them") -- port, receive port, device name, usb id,
  serial. `load_profile`'s docstring gave the reason: a machine with a
  second backend, whose profiles would be per-backend.
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
not say where the desk is plugged in.** Every profile of a config
directory is for that directory's machine.

What is ended is a profile that resolves to *another* machine, not the
sections themselves: `--dump-config > profiles/x.conf` is the documented
way to make a profile, and it writes `[device]` and `[osc]` into every
one. A profile that restates its `routing.conf`'s values says nothing,
and stays accepted -- for as long as they are its `routing.conf`'s
values. Change the port there and a dumped profile names the old one:
it is then a profile for another machine, is told so, and the remedy it
is given, taking the sections out, is the right one.

In 0.6.11, without changing what an accepted file means (ADR 0006):

* Such a profile still wins for the switch, and is told where it is
  written that 0.7.0 refuses it.
* A running session does not apply a re-read desk that resolves to
  another device name, usb id, serial or port than its own: the
  reconcile is skipped and the error names what differs. The re-read
  file is resolved the way a restart would resolve it -- `--device` and
  `--osc-port` over it, which a `Config` now remembers
  (`Config.overrides`) -- and then held against what the session runs;
  no serial means "the only one", which is the box the start pinned
  (`Machine.elsewhere`). So a reload applies what a restart would apply
  here, and refuses what a restart would take somewhere else. Comparing
  the bare file refused a session its own desk under `--osc-port`, with
  advice to restart that a restart would not have followed.
* The advice fits the cause, not the presence of a profile. The profile
  is the cause when `routing.conf` alone says something else about
  where. A restart would follow such a profile -- off this interface,
  which nothing would then manage, or into exit 2 beside the session
  that already holds that port -- so it is sent to the profile: take the
  sections out, or `--no-profile`. When `routing.conf` has moved as
  well, `--no-profile` is not offered, since it writes where
  `routing.conf` says and nothing listens there yet: the sections out,
  then a restart. Everything else is a `routing.conf` that moved, and a
  restart follows it, with or without a profile active.
* A reload names a desk that was checked for another interface than
  the `--device` one, as the start does: the start's notice covers the
  desk of that moment, and a file given its sections later was applied
  unannounced.
* The switch still reloads the unit and leaves the decision to it. A
  first cut decided in the CLI by comparing the profile with
  `routing.conf`; but the unit may itself have been started under such a
  profile, and then that comparison is inverted (found by review).
* The marker's temporary file has a unique name.
* "One main config per directory" is stated: the marker and `profiles/`
  belong to the directory, while a reload matches the unit by file.

In 0.7.0 a profile that resolves to another machine is a `ConfigError`
naming the file, what differs and the remedy. The device lock then
covers the marker, because every profile of a directory has the
directory's interface.

## Rejected

**Keep per-backend profiles and add a config-state lock.** It would
serialise the marker, and leave the question it serialises unanswered:
which of two backends is "the" active profile for? It also puts a second
lock, with an ordering rule, in front of every switch.

**Restart the unit when a switch changes the target.** A switch would
then stop the backend of interface A to start one for B, leaving A
unmanaged, from a command whose name says "profile".

**Refuse `[osc]` and `[device]` in a profile outright.** The first
wording of this record. It would have refused every dumped profile for
restating what it inherits anyway.

## Not claimed

Two backends on one machine are not a supported deployment, before or
after this. A second config directory with a session started by hand
(`--config`) is what works; this project ships one unit, and a switch
reloads only that one (`SERVICE_UNIT`), so a hand-started session is not
told about a switch and its verifier can re-apply the old desk for the
first seconds after its own start (the race of 0.6.3).

## Consequences

A setup whose profile really names another backend has to give that
backend its own directory by 0.7.0. It is told on every switch and start
until then, and the changelog will say that a file which loaded now
refuses.
