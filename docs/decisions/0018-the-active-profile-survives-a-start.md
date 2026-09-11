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
