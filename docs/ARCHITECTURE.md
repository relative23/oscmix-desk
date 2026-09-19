# Architecture

How oscmix-desk is built, in the present tense. It carries no history:
why a thing is the way it is lives in [the decision
records](decisions/), and what was measured to get there lives in
[the roadmap](ROADMAP.md).

**This page is checked against the code.** `tests/test_architecture.py`
requires every runtime module to be named here and every module named
here to exist. A page that drifts fails the suite, which is the only
reason to trust one.

## The system it sits in

Four layers have to cooperate for audio to work; oscmix-desk owns the
glue between them:

```
┌──────────────────────────────────────────────────────────────┐
│ 1  USB / kernel                                              │
│    snd-usb-audio registers the Fireface as an ALSA card and  │
│    MIDI device (class compliant, no custom driver).          │
├──────────────────────────────────────────────────────────────┤
│ 2  udev (udev/90-rme-fireface.rules)                         │
│    On hotplug: disables USB autosuspend for the device and   │
│    asks the user's systemd instance to start oscmix.service  │
│    (SYSTEMD_USER_WANTS). On removal the backend exits with   │
│    its device and the session exits 0; the tagged remove     │
│    event lets the user manager retire its device unit.       │
├──────────────────────────────────────────────────────────────┤
│ 3  backend (systemd/oscmix.service → bin/oscmix-session)     │
│    Discovers the ALSA sequencer client, runs                 │
│    `alsaseqio <client>:1 oscmix`, applies routing.conf via   │
│    OSC, supervises the process.                              │
├──────────────────────────────────────────────────────────────┤
│ 4  frontend (desktop entry → bin/oscmix-launch → oscmix-gtk) │
│    Checks the device is present, ensures the backend runs,   │
│    then execs the GTK mixer.                                 │
└──────────────────────────────────────────────────────────────┘
```

## The shape of the whole thing

Everything this project does is one pipeline, and every command is a
different place to stop along it:

```
routing.conf ──config──▶ Config
                           │
                    reconcile.desired()
                           ▼
                        Entry[]           what the file asks for
                           │
      device ──backend──▶ reconcile.observed()
                           ▼
                        seen{}            what the device reports
                           │
                    reconcile.plan()
                           ▼
                        Plan              what to write, and why
                           │
              ┌────────────┴────────────┐
        routing.apply()            cli --diff
        (sends it)                 (prints it)
```

`--dump-config` runs the middle of it backwards: device state in,
`routing.conf` out. `--snapshot` stops one step earlier and prints
`seen{}` verbatim, which is the only view that includes registers a
config cannot express.

**The reconciler is pure.** No socket, no clock, no device. That is what
lets it be tested against recorded dumps instead of hardware, and it is
why the register model is data rather than code.

## The modules

Layered: each may import only from those below it, enforced as an
acyclic graph.

| Module | What it owns |
|---|---|
| `constants` | every timing constant and exit code, each with the measurement that produced it |
| `errors` | `ConfigError`, the one exception a user ever sees; the two refusals that are not config text, an ambiguous interface and an unavailable device lock; and `ReceivePortError`, a receive port that cannot be bound for a reason other than a holder (ADR 0025) |
| `log` | journal-shaped logging, no configuration |
| `osc` | encode and decode OSC messages; no I/O |
| `registers` | what a register row and a device are -- path, tags, bounds, verification class, policy -- and the questions the parser, the reconciler and the verifier ask of a device's table |
| `devices` | the tables themselves: the UCX II's rows and channel map, the 802's channel map, and which of them a config names |
| `config` | parse `routing.conf` into a `Config`, refusing what the model declares unsettable and warning about what it does not model at all |
| `discovery` | find the device and resolve which interface a desk is for: serial, sequencer client and lock key from one answer; USB presence; whether a UDP port is bound |
| `notify` | `sd_notify`, so `Type=notify` means "the routing is applied" |
| `reconcile` | `desired` / `observed` / `plan`, and rendering a `Config` back to text |
| `backend` | the one place that opens a socket to the device; its `Traits` name the upstream behaviour the timing constants work around |
| `routing` | send a plan in two phases, with the link barrier between them |
| `verify` | read the device back and say confirmed, mismatched or unverifiable |
| `process` | supervise the backend: start, `SIGTERM`, escalate to `SIGKILL`, reap; and say who holds a port and which interface that backend bridges |
| `pipewire` | generate named virtual sinks from the same config |
| `locking` | the lock every writer of one interface holds: where it lives, how it is opened, how long it is waited for |
| `marker` | which profile is in effect, remembered beside the config: read, written through a rename, removed |
| `outcome` | what a switch did, as a value: applied and verified, applied and unverified, refused |
| `profiles` | switch to `profiles/<name>.conf` under that lock, in one fixed order -- validate, write, remember, check -- reporting an outcome rather than raising |
| `session` | the service lifecycle: wait for the device, start the backend, apply, signal ready, verify, shut down |
| `launcher` | the desktop entry's entry point; deliberately depends on almost nothing |
| `cli` | argument parsing and the exit-code mapping, and nothing else |
| `__init__` | the public surface, and the only module that re-exports |

## The register model is data

`devices.py` declares every register as a row of the shape `registers.py`
defines: path template, OSC type
tags, which channels have it on which device, how it verifies, what a
config may set it to, its bounds and unit, and who wins after the first
write.

Two consequences run through everything else:

- **A config can set exactly what declares a value domain.** Phantom
  power has none, so no `routing.conf` can reach it. That is one rule in
  one place instead of a list of exceptions in the parser, and it is why
  this page names no families: what a config can reach is a property of
  the table, and the table moves when the pin does.
- **The wire type comes from the declared tag**, never from the Python
  value. A `,f` written to a register that reads integers is accepted,
  dropped, and changes nothing.

The table itself is exempt from mutation testing and checked against
recorded device dumps instead ([ADR 0015](decisions/0015-the-register-table-is-not-mutated.md)).

## Who wins: pin and remember

Every settable register carries a policy. `REMEMBER` is the default: the
file describes the value, a dump shows it as a comment, and the device
keeps whatever it has. `PIN` means the file owns it and every start
writes it back.

The device does not announce most of its own changes, so "pinned" means
*the config wins while this session is looking*
([ADR 0012](decisions/0012-pin-and-remember.md),
[ADR 0013](decisions/0013-reconcile-triggers.md)).

## The two-phase apply

Channel links are written before the mix matrix, with a barrier between
them. Sending both in one burst silences every even output, because
oscmix only learns a pair is linked when the device echoes the change
back over MIDI, and a `/mix` write that overtakes that echo is evaluated
against the stale flag ([ADR 0001](decisions/0001-two-phase-routing-apply.md)).

The barrier waits for the echo, or for a fixed settle when the receive
port is held by the mixer GUI and the echo cannot be observed.

## The two seams

**`backend`** is the only module that opens a socket. Everything above it
takes a `Backend` argument, which is what lets the whole apply and verify
path be driven by a fake in tests.

**`devices`** is the only place that knows a device exists. The model is
indexed by device from the first line (`registers.Device`), so a second
interface is a table rather than a rewrite. Only the UCX II has one; the 802 has its
channel map and no registers, because oscmix cannot drive it.

### Exit codes

| Code | Meaning | systemd reaction |
|---|---|---|
| 0 | device absent, clean shutdown, or clean backend exit | none |
| 1 | runtime failure | restart after 3 s (max 5 per 2 min) |
| 2 | a configuration the unit cannot run: a routing.conf error, two interfaces and no `[device] serial`, a session already running on the port; from the command line also a usage error or a refused switch | **no** restart (`RestartPreventExitStatus=2`) |
| 3 | `--diff` only: the device and the config disagree | never seen; the service runs no flag |
| 4 | a switch reached the device but could not be recorded | never seen; flags only |
| 5 | the unit is running and refused the reload | never seen; flags only |
| 130 | interrupted (Ctrl-C) before the backend ran | never seen; systemd stops with SIGTERM |

`diff(1)` returns 1 for "differing" and that is not available here,
because 1 already means a failure. A caller has to be able to tell *the
desk drifted* from *the backend never answered*: conflating them makes a
monitoring check report healthy silence while the backend is down.

## Design decisions

- **Python, standard library only.** The original implementation was shell
  + inline Python. A single Python process gives testable pure functions,
  real signal handling and process supervision, and error messages that
  name the section/option at fault -- without adding a single dependency
  beyond what the shell version already needed.

- **Per-user installation.** Everything lives in `~/.local` and
  `~/.config`; root is needed for three files: the udev rule, the resume
  hook and the tmpfiles.d entry for the shared lock directory. `--no-udev`
  gives a rootless install that loses hotplug autostart, the reconcile
  after suspend, and the machine-wide lock (it falls back to the per-user
  runtime directory, ADR 0023).

- **`oscmix-launch` depends on almost nothing.** It imports only
  `constants` and `discovery` from the package, so a backend refactor
  cannot break the desktop entry. The package itself is installed as a
  plain file copy under `~/.local/lib/oscmix-desk`, no Python packaging.

- **Routing lives in the config, not in code.** The backend re-applies it
  on every start, so the device state is reproducible regardless of what
  the hardware remembered or what was changed interactively in the GUI.

- **udev remove matches `ENV{PRODUCT}`.** At remove time the sysfs
  attributes are already gone, so an `ATTR{idVendor}` match never fires.
  What ends the service on unplug is not this rule but the backend
  exiting with its device (ADR 0013); the remove match is what lets the
  user manager retire the device unit it tagged on add.

- **Stale cleanup signals the holder, not the namesake.** If the OSC
  port is already taken at startup, the socket inode in `/proc/net/udp`
  is resolved to the process that holds it through `/proc/<pid>/fd`, and
  that process is terminated only when it is also an `oscmix` of this
  user whose parent is not a live `oscmix-session`. A backend a running
  session supervises is somebody's desk: the start exits 2 at once and
  names that session (0.6.10). Anything else keeps the port and the start
  fails on the port wait. Until 0.6.6 every `oscmix` of the user was
  terminated as soon as *anything* held the port, which could stop a
  second interface's backend and leave the actual holder running
  (ADR 0021).

- **One interface, resolved once.** `discovery.resolve_device` answers
  which interface a desk is for -- serial, sequencer client and lock key
  together -- and the service, a switch, a restore and a reconcile all
  use that one answer. `[device] serial` selects among identical
  interfaces; without it more than one candidate is refused, never
  guessed. A switch writes only when the OSC port is held by an `oscmix`
  of this user whose `alsaseqio` bridges the resolved client, and the
  lock directory belongs to the group `audio`, whose members alone can
  create or hold a lock in it (ADR 0024).

- **Named PipeWire sinks are generated, not hardcoded.**
  `oscmix-session --pipewire-sinks` derives one loopback sink per stereo
  route from routing.conf and auto-detects the Fireface sink node via
  `pw-dump`, so the desktop integration follows the same single source of
  truth as the hardware mixer.

## Installed files

```
~/.local/bin/oscmix                  backend (built from upstream)
~/.local/bin/oscmix-gtk              GTK mixer (built from upstream)
~/.local/bin/alsaseqio               ALSA sequencer bridge (built from upstream)
~/.local/bin/oscmix-session          backend supervisor (this project)
~/.local/bin/oscmix-launch           desktop launcher (this project)
~/.local/lib/oscmix-desk/            the package (this project)
~/.config/oscmix/routing.conf        your routing (never overwritten)
~/.config/systemd/user/oscmix.service
~/.local/share/applications/oscmix-gtk.desktop
~/.local/share/icons/hicolor/scalable/apps/oscmix.svg
~/.local/share/glib-2.0/schemas/oscmix.gschema.xml   (needed by oscmix-gtk)
/etc/udev/rules.d/90-rme-fireface.rules              (root)
/usr/lib/systemd/system-sleep/oscmix                 (root; reconcile after resume)
/usr/lib/tmpfiles.d/oscmix-desk.conf                 (root; /run/oscmix-desk, group audio)
```
