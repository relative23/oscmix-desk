# 0022 -- The lock names the device, and a writer without it does not write

## Status

Accepted, 0.6.7. Extends
[ADR 0019](0019-one-lock-for-every-writer.md), which introduced the
lock, and [ADR 0020](0020-the-desk-is-read-under-the-lock.md), which
decided what is read inside it.

## Context

0.6.5 and 0.6.6 built one lock for every writer of the desk. A sixth
outside review of 0.6.6 pointed at the two holes left in it, and both
are in the same sentence of the old design: *one file per config
directory, so two desks selected by `--config` do not contend*.

**The key was the wrong thing.** A config directory is a description of
a desk, not the desk. Two directories can name one interface on one
machine, and then two writers held two different locks over the same
hardware. The lock promised something about the device while it was
keyed on a file path.

**A lock that could not be taken was not a refusal.** A missing lock
file, a filesystem that cannot `flock`, and a wait that ran out all
ended with the start writing the routing anyway, on the grounds that a
desk with no routing is worse than a re-apply. That reasoning is
defensible on its own and fatal to the guarantee: "every writer holds
one lock" became "every writer holds one lock unless something went
wrong", which is not a guarantee anybody can rely on.

## Decision

**The lock is keyed by the interface**, as `<usb id>-<serial>`, sanitised
into a filename. The serial is the one printed on the box, read from
`/proc/asound/cards`, which is the same string the evidence artifacts
carry. Without a serial the model alone is the key, which
over-serialises two identical interfaces and is the safe direction: the
cost is a wait, not a half-written desk.

**It lives in `$XDG_RUNTIME_DIR/oscmix-desk/`**, created by the unit's
own `RuntimeDirectory=`. That is the one place both the unit and the
CLI can write: the unit's home is `ProtectHome=read-only`, which is why
the installer had to create the old file for it. Nothing has to be
installed now, and a per-boot directory suits a lock whose meaning ends
with the machine's uptime. Without a runtime directory the lock falls
back beside the config, where it lived until 0.6.7.

**A writer that cannot hold it does not write.** `take_device_lock`
returns None for contention that outlasted the wait, for a lock file it
cannot open and for a filesystem that cannot lock. A switch refuses, a
reconcile stands down, and the start fails so systemd can try again.

## Consequences

- Two config directories over one interface now contend, which is what
  the lock always claimed.
- A start can fail for a reason that has nothing to do with the device:
  no runtime directory, no config directory, an unwritable lock. It
  fails loudly and systemd retries; it does not write a desk nobody
  serialised.
- `install.sh` no longer creates a lock file. One left over from an
  older install is harmless and stays where it is.
- The old fallback path beside the config keeps working for a session
  with no `$XDG_RUNTIME_DIR`, such as a bare `oscmix-session` in a
  minimal environment.

## Alternatives considered

- **Keep the config-directory key and document the limit.** 0.6.6 did,
  and the limit outlived two releases of people reading "one lock for
  every writer" as a device-wide promise. Rejected.
- **Key on the USB `iSerial` instead of the printed serial.** A second
  number for one box, and not the one the evidence artifacts or the
  hardware label carry. Rejected for the same reason `device_serial`
  reads the product string.
- **Keep writing after a lock timeout at start.** It is the availability
  argument, and it is real: a desk with no routing is audible. But the
  timeout is 30 s, systemd restarts the unit, and a write that raced
  another writer can leave a mix nobody asked for. Consistency wins here.

## Amended in 0.6.10

`$XDG_RUNTIME_DIR/oscmix-desk/` is the fallback since 0.6.8; the first
location is `/run/oscmix-desk`, the same for every user (ADR 0023). The
serial is read by `discovery.resolve_device`, which replaced
`device_serial` in 0.6.9 (ADR 0024).
