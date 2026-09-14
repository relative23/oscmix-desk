# 0023 -- One lock path for every writer, and no writing to an absent device

## Status

Accepted, 0.6.8. Extends
[0022](0022-the-lock-names-the-device.md), which keyed the lock on the
interface but left the *path* to it depending on the caller.

## Context

0.6.7 keyed the device lock on `<usb id>-<serial>` and put it in
`$XDG_RUNTIME_DIR/oscmix-desk/`. An adversarial review of that release
measured six ways the guarantee still came apart, four of them on a
live UCX II.

**The path depended on the environment.** `$XDG_RUNTIME_DIR` is set by
the user's systemd instance and absent from `sudo`, `cron` and a bare
`ssh host oscmix-session ...`. Measured: with a holder on
`/run/user/1000/oscmix-desk/2a39-3fd9-24216011.lock`, the same switch
run with the variable cleared computed a path beside the config,
took it in 2 s and wrote the whole routing while the holder held.

**The path depended on the user.** `/run/user/<uid>` is per user, so
two people driving one interface held two locks over one piece of
hardware.

**The path could vanish.** `/run/user/<uid>` goes with the last logout
of a user without lingering. A holder's lock file then no longer
exists, and the next writer creates a fresh inode and takes it.

**A writer with no config directory took no lock at all.**
`take_device_lock(None, key)` returned a lock object with no descriptor,
because the path search needed a config directory once the runtime
directory was gone.

**The key moved under a running writer.** It is recomputed on every
write from `/proc/asound/cards`, which empties when the interface is
unplugged. Measured across a real unplug: the key went from
`2a39-3fd9-24216011` to `2a39-3fd9-unknown`, and a second lock file
appeared beside the first. The window is not theoretical -- after a
resume the device re-enumerates, udev restarts the unit and the resume
hook sends its reload into exactly that gap.

**Two identical interfaces shared the first one's name.** The serial
came from the first matching line of the card list, whichever box the
process was driving. Unplugging that box moved the survivor's key.

**A switch to an unreachable device reported success.** No write path
checked whether anything would receive the datagrams. Measured: with
the device unplugged, a switch wrote into a port nobody bound, printed
`applied`, exited 0 and recorded the marker -- a desired state that had
never been at the device, which the next start then applied. The unit
had already been stopped by udev and said so in the same output.

**The loudest writer took no lock.** `scripts/sweep-writes.py` walks
every settable register and writes each one, and it is in the release
checklist. It could interleave with the unit's reconcile.

## Decision

**One path, the same for every writer on the machine.**
`/run/oscmix-desk/<key>.lock`, a directory the installer creates through
`tmpfiles.d` with mode 1777. It depends on neither the environment nor
the user nor the config directory. `$XDG_RUNTIME_DIR/oscmix-desk/` is
searched second and the config directory third, for a machine whose
installer never ran its root steps -- but once `/run/oscmix-desk`
exists, every writer uses it and none falls back, because falling back
from a directory other writers are using is the hole itself.

Mode 1777 is `/tmp`'s: any user may create their interface's lock file,
and the sticky bit stops one user removing another's. `flock` lives on
the open file description, so a second user opening the file read-only
holds it exactly as well.

**The key is pinned at discovery.** `run_session` records the serial the
device showed when it was found, and every later writer in that process
keys on it. The card list emptying mid-session can no longer move the
key out from under a holder.

**The key never guesses between two boxes.** One interface gives its
serial; several give the shared key `ambiguous` and a warning naming
the remedy; none gives the model alone. `[device] serial` names a box
outright and always wins. A shared key over-serialises two boxes, which
costs a wait; a guessed key writes to the wrong lock, which costs a
desk.

**A writer refuses when the write would go nowhere.** Not "when the
interface is absent": presence in sysfs is not reachability. Measured
on the desk while writing this -- `authorized=0` emptied the ALSA card
list and the sequencer clients while `/sys/bus/usb/devices/5-2` stayed
in place with `idVendor` readable, so a check on sysfs alone still said
the device was there and the switch still reported `applied`. The
question that covers both the missing interface and the stopped backend
is whether anything is bound to the OSC port. Checked before the lock,
like a bad config (ADR 0011): a refusal that costs nothing should wait
for nothing. Nothing is written, nothing is remembered, and the exit
code is the same 2 a bad profile gives.

**Every writer takes the lock, the sweep included.** It needs no config
directory now that the shared path does not.

## Consequences

- The installer has a third root step, and the uninstaller removes the
  file it wrote. The directory itself lives on tmpfs and goes with the
  next boot, so it is never removed under a running holder.
- A machine installed with `--no-udev` keeps the 0.6.7 behaviour and its
  limits: the runtime directory, per user, absent under `sudo`.
- Two interfaces without `[device] serial` now contend with each other.
  That is a wait where 0.6.7 had two independent locks, and it is the
  direction that cannot corrupt a desk.
- A switch can now fail for a reason that is not the desk: the interface
  is unplugged. It says so, and writes nothing.
- The test suite needs `OSCMIX_LOCK_DIR`, because `/run/oscmix-desk` is
  real on any machine where the installer ran.

## Alternatives considered

- **Keep `$XDG_RUNTIME_DIR` and document that it is per user.** The
  documentation would have been correct and the lock still wrong: a
  `sudo oscmix-session --profile` is a normal thing to type, and it went
  straight past the holder. Rejected.
- **`/var/lock` or `/run/lock`.** Root-owned and not group-writable on
  this distribution, so an unprivileged writer could not create its
  file at all. Rejected.
- **A lock server, or one lock for all interfaces.** Both serialise
  writes to *different* devices that have no reason to wait for each
  other. Rejected.
- **Name the first box when two are present.** It is what 0.6.7 did. It
  keys a writer of the second box on the first box's name, and
  unplugging the first moves the survivor's key. Rejected.
- **Let a switch to an absent device keep reporting success.** It is
  the availability argument again, and here it has no case at all:
  nothing arrives, so there is nothing to be available.
