# Security model

What this project trusts, and what it does not. Short, because the
surface is small -- but one item on it deserves to be stated plainly
rather than discovered.

## The control port is unauthenticated

oscmix listens on **UDP 127.0.0.1:7222** and acts on every datagram it
receives. There is no authentication, no authorisation and no origin
check beyond the loopback bind. **Any process running as your user can
write any mixer register**, including this project's own routing.

That is upstream's design, and on a single-user desktop it is a
reasonable one -- it is the same trust level as your audio server. It is
worth naming anyway, because the consequences grow with what the mixer
can do:

- today: routing and output faders. A hostile local process can silence
  your monitors, or make them very loud.
- from 0.3.0, when `[input:N]` sections land: **phantom power**. `48v`
  is a register like any other. Sending 48 V into a ribbon microphone
  damages it.

If that matters for your setup, the port is the boundary to defend --
either by not running untrusted code as your audio user, or by moving
oscmix into a namespace where 7222 is not reachable. This project cannot
fix it from the outside; it can only avoid making it worse.

## What the service is allowed to do

`systemd/oscmix.service` is sandboxed as far as an unprivileged **user**
unit can be. The hardening that a user manager cannot apply is documented
in `tests/test_unit_file.py` along with why -- capability-dropping and
cgroup-based directives fail with `218/CAPABILITIES`, which stops the
audio rather than securing it.

Declared: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateTmp`, `LockPersonality`, `MemoryDenyWriteExecute`,
`SystemCallArchitectures=native`, `SystemCallFilter=@system-service`, and
`RestrictAddressFamilies` limited to UNIX and IP sockets.

### Declared is not the same as applied

On Ubuntu with `kernel.apparmor_restrict_unprivileged_userns=1` -- the
default since 24.04 -- the user manager may create a user namespace but
not a mount namespace inside it, and it drops `ProtectSystem`,
`ProtectHome` and `PrivateTmp` **without a word in the journal**. Measured
on the desk in 0.6.9: the running unit shares the host's mount namespace,
`/` is mounted read-write inside it, and it can create files in the home
directory and `/var/tmp`. What does apply there is everything that does
not need a mount: `NoNewPrivileges` and the seccomp filter (the unit's
`/proc/<pid>/status` shows `NoNewPrivs: 1` and `Seccomp: 2`), and with
them `MemoryDenyWriteExecute`, `RestrictAddressFamilies` and
`LockPersonality`.

Where a manager can apply the mount sandbox -- measured by running the
same directives under the system manager -- `/` is read-only and the
home directory refuses writes. Check a given machine with:

```sh
pid=$(systemctl --user show -p MainPID --value oscmix.service)
grep ' / / ' /proc/$pid/mountinfo      # "ro," means the sandbox applies
grep -E 'NoNewPrivs|Seccomp:' /proc/$pid/status
```

Nothing in this project can make the user manager apply it; that is the
distribution's AppArmor policy. The directives stay in the unit because
they do apply wherever the policy allows, and the unit is written to work
under them.

### What the session writes

One file: the device lock, `/run/oscmix-desk/<usb id>-<serial>.lock`, or
the same name under `$XDG_RUNTIME_DIR/oscmix-desk/` on a machine whose
installer never ran its root steps (ADR 0023, ADR 0024). It holds no data;
the lock lives on the open file description. `ReadWritePaths` names that
directory and nothing else.

That line is load-bearing. Under a sandbox that is actually applied, `/run`
is read-only without it and no lock file can be created, so every start
would be refused -- measured under the system manager in 0.6.9, where the
shipped 0.6.8 unit got "Read-only file system" and the one with
`ReadWritePaths=-/run/oscmix-desk` took the lock. The dash lets the unit
start on a machine without the directory.

The routing config stays read-only to the service. The profile marker and
`--dump-config` output are written by the CLI a user runs, outside the
service, which is the outcome the questions below were written to aim
for:

- **Which directory, and does the session need to write it, or only the
  CLI?** If only the interactive path writes, the service stays at the
  narrowest `ReadWritePaths` and the write happens outside it.
- **Does the routing config itself become writable?** It should not. A
  config the service can rewrite is no longer a reviewable source of
  truth, which is the project's premise.
- **What happens on a partial write?** A truncated file is an unbootable
  state. Write to a temporary file and rename.

`tests/test_unit_file.py` asserts `ReadWritePaths` exactly, so widening
it cannot happen quietly.

### Who can reach the lock

`/run/oscmix-desk` is 3770 root:audio. Members of `audio` can create,
open and hold a lock there; nobody else can reach one. Inside that group a
member can still hold a lock for ever or plant a file at another member's
lock path -- the second is refused with its reason rather than followed or
blocked on, the first is a wait. That is the trust the group grants, and
it is the same group that may drive the interface at all.

The lock serialises the writers of this project -- the service, a switch,
a restore, a reconcile, the write sweep. It does not reach a process that
writes to the backend's port without asking, the mixer GUI first among
them: a fader moved by hand is the user's own write, and the pin/remember
policy decides what a later reconcile does with it (ADR 0012, ADR 0019).

## Signalling other processes

`_cleanup_stale_backend` terminates a leftover `oscmix` that is holding
the OSC port. It only considers processes owned by the calling user whose
`comm` or argv0 is `oscmix`, and it signals through `os.pidfd_open` so a
PID recycled between the `/proc` scan and the signal cannot be hit by
mistake. A process it cannot verify is reported, never signalled.

## The supply chain

`install.sh` clones and compiles upstream oscmix -- the only place this
project executes code from the network. It builds a **pinned commit**
(`OSCMIX_REF`, default a full SHA), verifies that the checkout landed on
exactly that commit, and records the built revision in the hardware
evidence artifact. Tracking upstream is an explicit opt-in:

```sh
OSCMIX_REF=master ./install.sh
```

There is no signature verification: upstream publishes no signed tags.
That is a real gap, and it is the reason the default is a specific commit
that has been measured against real hardware rather than a moving branch.

## Not in scope

Multi-user separation, remote access, and anything about the audio data
itself. This project configures a mixer; it does not carry audio.
