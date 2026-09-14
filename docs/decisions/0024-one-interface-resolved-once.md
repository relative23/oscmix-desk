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

A last sweep before release, asked for so that nothing would surface after
it, found four more on the same path, and an independent review of the
result found the rest (below the four):

**The unit could not write the lock directory where its sandbox applies.**
`ReadWritePaths=` was empty. Ubuntu's user manager silently skips the
mount sandbox (AppArmor denies it the mount namespace), so the desk here
worked. Under the system manager, which applies it, `/run` was read-only
and the lock file could not be created -- every start would have been
refused, and the refusal said "No such file or directory".

**The start budget left out the lock wait.** `startup_budget()` and the
test that holds it against `TimeoutStartSec` had no term for the 30 s the
start has waited for the device lock since 0.6.5.

**A reload could change the interface under a running process.** The desk
re-read under the lock kept the running ports and device name but took
`usb-id` and `serial` from the file, so a profile naming another box
changed what the reload described while the lock and the backend stayed
with the first.

**A scratch-home uninstall reached for the machine's system files.** It
removed the udev rule, resume hook and tmpfiles.d entry that the session's
own installation depends on; only a sudo that could not prompt kept them.

**From the independent review.** A switch checked the port holder before
a lock wait of up to 30 s and never after it, so a backend that changed in
between still received the desk. Any process on the machine whose
kernel-truncated name was not valid UTF-8 made every switch raise. With
the card list and the clients gone but a backend alive, a switch without
`[device] serial` keyed on `unknown` beside the unit's lock. A Fireface of
another model beside a UCX II made both desks ambiguous, because the card
list counted every Fireface. A configured serial that was not plugged in,
beside another box of the model, turned the start into a restart loop. A
user-space sequencer client could name itself into the selection. A
serial with letters passed validation and could never match. And the
start found its client in one read of the machine and its serial in a
second.

## Decision

**One resolution.** `discovery.resolve_device` answers, from the config
and the machine, which interface a desk is for: its serial, its
sequencer client and its lock key. `[device] serial` selects the client
whose name carries that number. Without it neither the kernel's
sequencer clients nor the card list may show more than one interface of
the configured model -- the card list catches a second box whose client
is not up yet -- and more than one raises DeviceAmbiguous instead of
picking. Only clients the kernel created count, so a user-space program
cannot name itself into the choice, and only the configured model, so a
Fireface of another model is not a second candidate. The unit exits 2
for ambiguity, which `RestartPreventExitStatus=2` keeps from looping; a
switch and a restore refuse with the same words. The start waits for the
resolved interface itself and binds its client and pins its serial from
that one answer, so its key and a switch's cannot differ. A configured
serial the machine does not show is "not connected", the clean no-op
start, even while another box of the model is plugged in.

**The port has to be held by this interface's backend, before and after
the lock.** A switch or
restore that opens its own socket asks `process.port_holder`: the socket
inode leads to the holding process, which must be an oscmix of this user
(any user's, for root), and the alsaseqio beside it -- its child on a
real desk, where alsaseqio forks and the original process execs oscmix
-- names the client it bridges. When that client or its serial is known
and is not the resolved interface, the write is refused. A holder whose
bridge cannot be followed is accepted on its name alone; that is weaker,
and it is still a different statement from "something is bound". An interface
without a visible sequencer client is refused outright: no client, no
backend for it, whatever holds the port. And because the lock wait can
take 30 s, the whole check runs again once the lock is held, and a
resolution that changed meanwhile is a refusal too. What remains is the
milliseconds between that check and the first datagram, which a UDP
write cannot close.

**No lock, no READY.** `_apply_and_verify` raises DeviceLockUnavailable.
`run_session` sends READY=1 only after an apply returned, in the same
block; on the refusal it stops the backend it spawned and exits 1, and
`Restart=on-failure` tries again after `RestartSec`.

**Only a regular file is a lock.** Lock files are opened with
`O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` and accepted only when `fstat`
says S_ISREG. The mode and group are changed only for a file this
process owns with exactly one link: 0660, and the directory's group.

**The unit declares the one directory it writes.**
`ReadWritePaths=-/run/oscmix-desk`, and a refusal in a read-only directory
names the directive. `startup_budget()` includes `SWITCH_LOCK_WAIT`, and
`TimeoutStartSec` is 100. A re-read keeps `usb_id` and `serial` from the
running process as it keeps the ports. `uninstall.sh` leaves the system
files alone when systemd's session serves another home, as it already did
for the service.

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
  `RestartSec` plus the wait -- about 33 s -- loudly, instead of
  reporting a started desk. Five of those never fit into
  `StartLimitIntervalSec=120`, so the retries go on for as long as the
  lock is held; each one is a line in the journal.
- `--snapshot`, `--diff` and `--dump-config` only read, and they read
  whatever backend holds the OSC port. The snapshot's header names the
  interface that backend bridges, not the one the config resolves to.
- Without `[device] serial`, a second identical interface that
  enumerates after the start has bound is not seen by that start. The
  next switch sees it and refuses; set the serial on a machine that may
  have two.
- A switch by one user against another user's running desk is refused:
  `/proc/<pid>/fd` of another user's process cannot be read, so the port
  holder cannot be identified. That is correct -- the config, the marker
  and the unit are per user -- and it is stated here so it is not taken
  for a defect.
- One unit per machine. With two identical interfaces `[device] serial`
  picks the one the desk is for; the other has no desk until the roadmap
  item for several interfaces lands.
- The mount sandbox protects only where the distribution lets the user
  manager apply it; docs/SECURITY-MODEL.md says how to check.
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
