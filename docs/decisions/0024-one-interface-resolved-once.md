# 0024 -- One interface, resolved once, and a lock only its group can touch

## Status

Accepted, 0.6.9. Extends [0023](0023-one-lock-path-for-every-writer.md)
and makes [0022](0022-the-lock-names-the-device.md) true: 0022 said a
start without the lock fails, and until 0.6.9 it did not.

## Context

0.6.8 gave every writer one lock path. A review of it, followed by a
second reviewer's ordering of the findings, reproduced six ways the
guarantee still did not hold. Each was probed, not reasoned about.

**The interface was worked out four times.** The unit bound the first
sequencer client whose name matched, pinned the first serial of the
card list, a switch keyed its lock on a rule of its own that said
`ambiguous` for two boxes, and the OSC port was taken from whoever held
it. With one interface the four agreed. With two identical ones and no
`[device] serial`, the probe had the unit on `2a39-3fd9-24216011` and a
switch on `2a39-3fd9-ambiguous` -- two lock files over one desk. And
`[device] serial` only renamed the lock: a config naming box B still
bound box A.

**A bound port was taken for the backend.** A plain Python socket on
the OSC port received a whole routing from a switch, which reported
`applied-unverified` and recorded the marker. The start already accepted
the port only from the child it spawned; the switch did not ask.

**A start without the lock reported READY.** `_apply_and_verify` refused
to write and returned None, which also meant "nothing declared" and "a
stop arrived", and `run_session` sent READY=1 for all three. systemd
showed a started desk that had never been written, and nothing retried.

**A FIFO at the lock path hung every writer.** Opening it read-write was
refused by fs.protected_fifos, and the read-only fallback then blocked in
open() until a writer appeared -- still blocked after 25 s in the probe,
the 30 s lock wait never reached. It would have hung the unit's start.

**A symlink at the lock path was followed.** In a directory the kernel
does not protect, the probe's lock open followed a planted link and
chmod'ed its target to 0666. In `/run/oscmix-desk` only
fs.protected_symlinks=1 stood in the way; some containers run without it.

**Anyone could hold the lock.** Mode 1777 and lock files 0666 let every
local user pre-create a lock file only they could open, or hold one for
ever. Both ended in refusals for the real desk.

## Decision

**One resolution.** `discovery.resolve_device` answers, from the config
and the machine, which interface a desk is for: its serial, its
sequencer client and its lock key. `[device] serial` selects the client
whose name carries that number. Without it there must be exactly one
candidate -- in the sequencer clients, or in the card list when no client
is up -- and more than one raises DeviceAmbiguous instead of picking. The
unit exits 2 for it, which `RestartPreventExitStatus=2` keeps from
looping; a switch and a restore refuse with the same words. The unit
pins the serial of the client it bound, from the same resolution, so its
key and a switch's cannot differ.

**The port has to be held by this interface's backend.** A switch or
restore that opens its own socket asks `process.port_holder`: the socket
inode leads to the holding process, which must be an oscmix of this user
(any user's, for root), and the alsaseqio beside it -- its child on a
real desk, where alsaseqio forks and the original process execs oscmix
-- names the client it bridges. When that client or its serial is known
and is not the resolved interface, the write is refused. A holder whose
bridge cannot be followed is accepted on its name alone; that is weaker,
and it is still a different statement from "something is bound".

**No lock, no READY.** `_apply_and_verify` raises DeviceLockUnavailable.
`run_session` sends READY=1 only after an apply returned, in the same
block; on the refusal it stops the backend it spawned and exits 1, and
`Restart=on-failure` tries again after `RestartSec`.

**Only a regular file is a lock.** Lock files are opened with
`O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` and accepted only when `fstat`
says S_ISREG. The mode and group are changed only for a file this
process owns with exactly one link: 0660, and the directory's group.

**The trust circle is `audio`.** `/run/oscmix-desk` is 3770 root:audio.
Members can create, open and hold locks; the setgid bit gives new files
the group, the sticky bit keeps members from removing each other's.
Everyone else cannot reach a lock at all, and a refusal says which group
owns the directory.

## Consequences

- Two identical interfaces need `[device] serial`, in the unit's config
  and in every desk that writes to them. Without it nothing writes, and
  the log says which boxes it found.
- The user running the unit must be in `audio`. The installer warns when
  it is not; the refusal names the group.
- A lock held for longer than the wait makes the unit retry every
  `RestartSec` plus the wait, loudly, instead of reporting a started
  desk. Five failures inside `StartLimitIntervalSec` stop the retries.
- The layering gains two edges, both toward leaves or down the graph:
  `discovery` imports `errors`, `profiles` imports `process`.
- Only one of the six probes still needs a person to run it: a lock file
  owned by *another* user cannot be created by the test suite. It was
  validated on the desk with two throwaway accounts.

## Alternatives considered

- **Keep the `ambiguous` key for two boxes.** It serialises writes, and
  says nothing about which box gets the desk. The review's point was the
  second half. Rejected, and `device_key` with it: once every writer
  resolves the interface, a second way to name the lock is the four-way
  split this record removes.
- **Accept a bound port whose holder is any process named oscmix.**
  That is the stranger case with a better disguise, and a second
  interface's backend is an oscmix too. Rejected in favour of following
  it to the client.
- **Keep 1777 and document the local denial of service.** A desk that
  any local account can stop is not what "one lock for every writer"
  was meant to buy. Rejected.
- **Retry the lock inside the start instead of failing it.** The wait is
  already 30 s; a longer one is a start that hangs, and systemd's
  restart is the retry with a log line per attempt.
