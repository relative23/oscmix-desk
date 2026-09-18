# 0018 -- The active profile is remembered beside the config, and a start applies it

## Status

Accepted, 2026-09-11. Measured on a Fireface UCX II, serial 24216011:
the trap this closes was reproduced on the desk on 2026-09-10, when a
`reload` sent by hand undid a profile the same way the resume hook does
after every wake.

## Context

`oscmix-session --profile X` writes X to the device and exits. The
service keeps running on `routing.conf`, and the next backend start --
a replug, a reboot, `systemctl --user restart` -- re-applies
`routing.conf` over X. So does every reload, and the resume hook sends
one after every suspend. A tracking session set up before lunch is
gone after the laptop lid, with nothing in the journal saying why: the
start line names `routing.conf`, which is exactly what was applied.

0.6.2 documented this and carried the fix as roadmap item F with the
design question it needed answered first: what the active profile
means when `routing.conf` changes underneath it.

## Decision

**The active profile's name is state, kept beside the config.** A
successful switch writes it to `<config dir>/active-profile`, one line;
`--no-profile` removes it after applying `routing.conf`. Nothing else
writes the file. The service only reads it, which is what keeps
`ProtectHome=read-only` true for the unit.

**A start and a reload apply the effective config, which is the active
profile when it loads and `routing.conf` otherwise.** `effective_config`
answers that question in one place, and every path that asks "what
desk did you declare" -- the start, `SIGHUP`, `--dry-run`, `--diff`,
`--pipewire-sinks` -- asks it there. The journal says which one is in
effect on every start, and says that `routing.conf`'s routes are not.

**A remembered profile that no longer loads falls back to
`routing.conf` with a warning, and the marker stays.** Falling back is
what keeps the desk up; a refused start over a profile file nobody
edited would be the failure ADR 0006 exists to prevent. Keeping the
marker is what keeps the warning coming until somebody decides --
`--no-profile`, or fixing the file -- rather than silently forgetting a
choice on the first boot that could not honour it.

**`routing.conf` changing underneath an active profile changes the
machine, not the desk.** The profile inherits `[osc]` and `[device]`
from `routing.conf` at every load (ADR 0011), so editing the ports or
the device name takes effect on the next start. Editing the routes or
the channel state does not, and the start line says so. To make a
profile the default, copy it over `routing.conf` and `--no-profile`.

**A switch that could not remember itself is still a switch.** The
outcome states are the three of ADR 0011; a marker that cannot be
written is a warning in the journal, not a fourth state, because the
device already has the profile and pretending otherwise would be the
"applied, but the flag says otherwise" case that ADR forbids.

## Consequences

- The resume hook does what it says: a wake re-applies the desk that
  was chosen, not the default one. Same for a replug.
- `--diff` compares the device against the effective config, so a desk
  on a profile reads as matching; a desk on `routing.conf` with a stale
  marker reads as differing, with the warning above naming why.
- `--list-profiles` marks the active one.
- **An applied switch reloads the running unit.** The switch writes the
  device from a second process while, for up to about 22 s after a
  start, the unit's own verifier is still re-applying the config it
  started with -- and it overwrote the switch (measured: a switch sent
  right after a restart read back at the old fader value fifteen
  seconds later). The reload makes the unit re-read the desk in effect,
  and its reconcile queues behind the verifier (ADR 0013), so the last
  write is the profile's. Without a running unit there is nothing to
  reload and the switch stands on its own.
- The marker is per config directory: `--config` pointing elsewhere
  reads that directory's marker, so a test config cannot pick up the
  user's choice by accident.
- One more file in `~/.config/oscmix` that the tool writes rather than
  the user. It is one line, named for what it is, and `uninstall.sh
  --purge` removes the directory with it.

## Two switches at once, and a marker that is never half written

Added after a third review round (2026-09-11), which asked two things
the first version did not answer.

**Concurrent switches are serialised per config directory.** Two
`--profile` commands at once would interleave their link phases and mix
writes on the wire and race each other to the marker and the reload.
A switch and `--no-profile` take `active-profile.lock` beside the marker
(`flock`) from the first datagram to the last check, *after* the
profile parsed, so a refusal for a bad config still costs nothing. A
second switch waits, says so in the journal, and refuses after
`SWITCH_LOCK_WAIT` (30 s -- one queued switch with margin) with the
reason named: nothing written, the same outcome as any refusal. The
lock file stays beside the config, empty; `--purge` removes it with the
directory. The unit does not take the lock: it cannot write in `$HOME`,
and its own writes are ordered against a switch by the reload the
switch sends (above).

**A switch does not write while the unit does.** Measured while
checking the lock: a reload sent while the receive port was held had
its reconcile skipped, as ADR 0013 requires when nothing can be read
back. With the mixer GUI open that is every reload, and then the reload
after a switch does not settle a switch that overlapped the start-up
verifier's blind re-apply. So the switch does not overlap it: the unit
reports its phase through `STATUS=` (0.6.3), and `--profile` and
`--no-profile` wait while it says applying, verifying or reconciling,
bounded by `SWITCH_LOCK_WAIT` and logged. Past the bound the switch
proceeds and says so; an unresponsive unit must not hold a person's
desk hostage. The reload after the switch stays, for the unit's own
state.

**What the wait does not cover.** The 0.6.4 release's live test, two
shells switching at once, logged the ordering: the reload a switch
sends goes out after its lock is released, and `ExecReload` is a plain
`kill -HUP`, so `systemctl reload` returns before the unit has begun to
reconcile, and the second switch takes the lock at once. In that run
the unit's reconcile found the receive port held by the second
switch's read-back and was skipped, as ADR 0013 requires. Had it
started in a gap where the second switch did not hold the port, it
would have re-applied the first switch's desk -- the marker still named
it -- while the second was writing. The second switch's own reload then
re-applies its desk, so the end state is right; what is lost is the
guarantee in between, and that switch's read-back can report registers
the unit was still writing. The fix is one lock for every writer: the
unit taking the same lock around its apply, verifier and reconcile,
which also makes the phase wait unnecessary. That changes the unit and
the installer -- the unit cannot create the lock file under a read-only
home -- so it is roadmap item G rather than part of this release.

**The marker is written beside and renamed over.** `write_text` in
place could leave an empty file between open and close, and an empty
marker reads as "no profile" -- the choice gone on the next start with
nothing saying so. The name goes to `active-profile.tmp`, is fsynced,
and is renamed over the marker; the directory is fsynced best-effort.
A write that fails leaves the previous marker whole and the temporary
file removed.

## Alternatives considered

- **Copy the profile over `routing.conf` on switch.** Destroys the
  user's main config, which is the one file this project promises never
  to touch. Rejected.
- **A state directory (`~/.local/state/oscmix`).** The right place for
  state by the XDG letter, but the profiles it names live beside
  `routing.conf`, and a marker that can point at profiles of a
  different config directory is a marker that can be wrong. Beside the
  config, where `--config` selects both together.
- **Documenting the trap and stopping there.** 0.6.2 did. A trap that
  the resume hook springs on every wake is not one a README paragraph
  closes.

## Amended in 0.6.10

The lock a switch takes is no longer `active-profile.lock` beside the
marker but the interface's lock in `/run/oscmix-desk` (ADR 0023, 0024);
the name beside the marker is the last fallback. The unit takes the same
lock for its apply, verifier and reconcile (ADR 0019), and a switch does
not poll the unit's STATUS line -- it reloads the unit and the unit's
reconcile is serialised behind its verifier (ADR 0019, 0013).

## Amended in 0.6.11

The inheritance happens *before* the profile is validated. Until 0.6.11
a profile was parsed onto the defaults and given `[osc]` and `[device]`
afterwards, so it was checked against the UCX II whatever interface the
desk was for: on a desk naming another box, a profile's `output = 25/26`
was refused for the UCX II's twenty channels, and its `[output:1]`
section was accepted through the UCX II's table while the same section
in `routing.conf` is ignored with a warning. A profile is now read onto
the main config's machine settings; one that states a setting itself
still wins, because the parser falls back to what it is read onto only
for what the file does not say.

"Takes effect on the next start" has a consequence that is now said out
loud. A running session keeps the interface it was started for, so a
reload that finds `routing.conf`, or an active profile, naming another
one uses a desk that was checked for the wrong interface. It warns, and
a restart moves it. `--device`, which also arrives after the
validation, warns the same way, and is refused together with a switch
or a restore, which never saw it.
