# 0021 -- A start that cannot be heard is not a start

## Status

Accepted, 0.6.6.

## Context

Two failures shared one shape: the session reported success for work
that never reached the device.

**The port wait warned and carried on.** `_await_backend_port` waited
for oscmix to bind its OSC port and, on timeout, logged a warning and
returned. If the child was still alive, the session applied the whole
routing and sent `READY=1`. OSC here is UDP: every datagram sent to a
port nobody bound is accepted by the kernel and dropped. The apply
looked like it worked, the verifier confirmed nothing, and systemd was
told the desk is set -- which under `Type=notify` is exactly the claim
this project makes about its own start.

**The stale cleanup signalled by name.** When the port was already
taken, `find_stale_backends` listed every process of this user whose
`comm` or `argv0` is `oscmix`, and all of them got SIGTERM. Nothing
connected a name to the socket. A stranger holding the port and a
legitimate oscmix driving a second interface on another port is enough:
the legitimate one is killed, the holder is not, and the start fails
anyway.

## Decision

- **`_await_backend_port` returns whether the port came up.** A backend
  that is alive but has not bound after `PORT_READY_TIMEOUT` fails the
  start: the child is stopped, `EXIT_FAILURE` is returned, and `READY=1`
  is never sent. A child that has already exited keeps the old path, so
  "the device was unplugged during start" still reports what it always
  did.
- **The cleanup resolves ownership before it signals.** The socket inode
  for the port comes from `/proc/net/udp{,6}`; `/proc/<pid>/fd/*` says
  which process holds that inode. Only that process is terminated, and
  only when it is also an `oscmix` of this user. An unidentifiable
  holder, or one that is not ours, is left alone and the start fails on
  the port wait.

## Consequences

- A wedged backend now produces a failed start and a restart by systemd
  rather than a service that is "running" with a desk nobody wrote.
- The cleanup can no longer make room for itself by killing a process
  that never had the port. It can also no longer clear a stale backend
  whose `/proc` entry is unreadable, which is the trade this ADR makes
  deliberately: failing to start is recoverable, killing a stranger's
  process is not.
- `find_stale_backends` stays, as the identity half of the test.

## Alternatives considered

- **Keep warning and continue.** It is what produced a service reported
  as ready while every datagram went nowhere. Rejected.
- **`ss -lunp` instead of parsing `/proc`.** An external binary, and
  `/proc` is already how this project finds the ALSA client, the USB
  device and the port itself. Rejected.
