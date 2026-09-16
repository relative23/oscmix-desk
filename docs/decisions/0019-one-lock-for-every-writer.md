# 0019 -- One lock for every writer of the device

## Status

Accepted, 0.6.5. Replaces the phase wait of
[ADR 0018](0018-the-active-profile-survives-a-start.md), and closes
roadmap item G.

## Context

0.6.4 gave the CLI a lock: `--profile` and `--no-profile` hold
`active-profile.lock` beside the marker from their first datagram to
their last read-back, so two switches cannot interleave their link
phases and mix writes.

Two things were measured in that release's own live test, and neither
is covered by a lock the CLI alone holds.

**The unit is a third writer.** Its start-up apply, its start-up
verifier and every SIGHUP reconcile write the whole routing. 0.6.4 kept
them apart from a switch by reading the unit's `STATUS=` line and
waiting while it said applying, verifying or reconciling. That wait
runs *before* the switch takes its lock, and the switch's own reload is
sent *after* it releases it. `ExecReload` is `kill -HUP`, so
`systemctl reload` returns before the unit has begun to reconcile. A
second switch waiting on the lock therefore takes it and writes while
the unit reconciles for the first. The end state was still right, since
the second switch's own reload re-applies its desk, but the guarantee
in between was not.

**A wait on a reported phase is not a lock.** It is a poll of a string,
it cannot be held across a transaction, and it has no way to make the
unit wait for the CLI. The mechanism was doing half of what a lock
does, in a second way, which is the shape of the three defects 0.3.0
fixed.

## Decision

**Every writer of this desk takes the same lock.**

- The file stays `active-profile.lock` beside the config, one per
  config directory: `--config` selects a desk, and two desks do not
  contend.
- The unit holds it from the start-up apply until its verifier
  finishes, which is one transaction and about 22 s, and again around
  every reconcile.
- A switch and `--no-profile` hold it as before, and now wait for the
  unit as they wait for each other. Past `SWITCH_LOCK_WAIT` a switch
  refuses and writes nothing, which it already did for a switch that
  would not let go.
- The unit's start proceeds with a warning if the lock does not come
  free: a desk with no routing at all is worse than a re-apply of what
  a switch just wrote, and the marker makes both the same desk anyway.
- A reconcile that cannot take the lock is skipped with a warning
  naming the reload to send again, the same answer it already gives
  when the verifier outlives its wait.
- **The installer creates the lock file**, because the unit cannot:
  `ProtectHome=read-only` refuses both the creation and the write open.
  `flock` needs neither, so the unit opens the existing file read-only
  and locks that. A missing file is a warning and an unlocked write,
  not a refusal to run: an install that predates this ADR must still
  drive the device.
- `STATUS=` stays. It is what `systemctl --user status` shows and it
  was worth having on its own; what goes is the CLI polling it
  (`_wait_for_the_unit_to_settle`, `process.service_phase`).

**A switch the marker did not record is not handed to the unit.**
`remember_active_profile` already returned whether the marker was
written, and `switch_profile` dropped that on the floor. Measured on
the desk with the config directory made read-only: the switch applied
the profile, printed `applied`, exited 0, and reloaded the unit -- whose
reconcile re-read `routing.conf` and undid it two seconds later, with
the main output measured at -1.0 dB and back at 0.0 dB. The outcome
carries `persisted` now, `--no-profile` carries the same fact about
removing the marker, and the CLI sends no reload when it is false. The
desk keeps what was written until something else reloads the unit, and
the line says so.

## Consequences

- The transient interleave is gone, in both directions: the unit waits
  for a switch, and a switch waits for the unit.
- One mechanism instead of two. The release that adds a lock to the
  service deletes more lines than it adds to the CLI.
- A switch sent during a start now waits up to 30 s and then refuses,
  where 0.6.4 warned and wrote anyway. A refusal writes nothing and
  says why, which is the same contract a bad config gets.
- A failed marker write is visible in the outcome line rather than only
  in the log, and it costs the reload rather than the switch.
- The lock is advisory and names one file. Anything that writes the
  device without taking it is still unserialised, and the mixer GUI is
  exactly that: it knows nothing about this project. The lock covers
  every writer *this project* has, which is what it can promise.

## Alternatives considered

- **Move the reload inside the switch's lock.** Necessary but not
  sufficient: `kill -HUP` returns before the reconcile starts, so the
  next switch can still take the lock first. Only the unit taking the
  lock makes the order true.
- **A runtime-directory lock** (`RuntimeDirectory=oscmix`), which the
  unit could create itself. It would be one lock for every config
  directory on the machine, and a `--config` elsewhere would contend
  with the running desk for no reason. Rejected.
- **`Type=notify-reload`**, so `systemctl reload` waits for the
  reconcile. It fixes the ordering only for the process that sends the
  reload, does nothing for the start-up verifier, and puts the
  guarantee in systemd's hands rather than in one file both sides can
  see. Kept as a possible addition, not as the mechanism.

## Amended in 0.6.10

The reload after an applied switch went to the unit whatever config the
switch was for, and the unit's reconcile then re-applied its own
`routing.conf` over a switch made with `--config` for another file. The
reload is sent only when the switch is for the config the unit runs, the
one `discover_config_path` finds.

## Amended in 0.6.10

The installer no longer creates a lock file: the lock lives in
`/run/oscmix-desk`, created by tmpfiles.d, or in the unit's runtime
directory (ADR 0023). A lock that cannot be taken is a refusal for every
writer, never an unlocked write (ADR 0022); the runtime directory that
this record rejected became the fallback location.
