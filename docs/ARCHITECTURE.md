# Architecture

How oscmix-desk is built, in the present tense. It carries no history:
why a thing is the way it is lives in [the decision
records](decisions/), measurements live in [the evidence guide](HARDWARE-EVIDENCE.md)
and [history](history/), and future work lives in [the roadmap](ROADMAP.md).

`tests/test_architecture.py` checks the runtime module inventory against this
page and enforces the dependency rules. These checks do not establish that
every behavioral description is accurate; those still require source review.

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
│    `alsaseqio -x <client>:1 oscmix -c <socket>`, applies     │
│    routing.conf through the coordinated owner.              │
├──────────────────────────────────────────────────────────────┤
│ 4  frontend (desktop entry → bin/oscmix-launch → oscmix-gtk) │
│    Checks exact device/backend identity and GTK protocol,   │
│    then connects GTK to that same owner.                     │
└──────────────────────────────────────────────────────────────┘
```

## The shape of the whole thing

The same declarations feed two pure decisions: observation comparison and
write selection for the chosen operation intent.

```
routing.conf ──config──▶ Config
                           │
                    reconcile.desired()
                           ▼
                        Entry[]           what the file asks for
                           │
         ┌─────────────────┴───────────────────┐
         ▼                                     ▼
 application_plan(intent)            reconcile.plan(entries, seen)
         │                                     ▲             │
         ▼                                     │             ▼
 routing.apply_routing()              backend observations   --diff
 links → barrier → matrix → settings
         │
         ▼
 verification → permitted PIN repair
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
| `errors` | configuration, ambiguous-device and lock refusals; `ReceivePortError` for failed coordinated observations (retained API name), and `WriteFailed` with submitted/pending paths |
| `log` | journal-shaped logging, no configuration |
| `osc` | encode and decode OSC messages; no I/O |
| `registers` | what a register row and a device are -- path, tags, bounds, verification class, policy -- and the questions the parser, the reconciler and the verifier ask of a device's table |
| `numeric` | finite values, OSC representation and the pinned backend’s scalar encodings; parameter-specific comparisons and lossless decimal formatting |
| `devices` | the tables themselves: the UCX II's rows and channel map, the 802's channel map, and which of them a config names |
| `model` | a desk as data: routes, channel and global settings, and the five settings that say where it goes |
| `paths` | where a desk is looked for: the config, `profiles/` beside it, and what a profile name is |
| `sections` | the sections the register table declares -- channels, families, globals -- refusing what it declares unsettable and warning about what it does not model at all |
| `config` | parse `routing.conf` into a `Config`: `[device]`, `[osc]`, routes and pins here, the rest through `sections`; total, so every input is a `Config` or a `ConfigError` |
| `notices` | what there is to say about a desk before it is written or shown |
| `discovery` | find the device and resolve serial, sequencer client and lock key from one answer; USB presence and executable selection |
| `notify` | `sd_notify`, so `Type=notify` means "the routing is applied" |
| `reconcile` | `desired` / `observed` / `plan`: what should be written, in what order, and why |
| `observation` | latest decoded classification for a fixed expectation set and one observation window; matches are revocable, and no observation performs a write |
| `dump` | the other direction: what the device reports, recovered as routes and settings and rendered as a `routing.conf` |
| `backend` | checked ODK1 connections, bounded leases/queues and provenance; shared read-only identity from `diagnostics` before connecting |
| `streams` | observe the exact interface's active ALSA/USB playback mode and enforce measured playback limits before write phases; no PCM or clock changes |
| `diagnostics` | read-only socket, exact backend/bridge/device identity and service inspection, shared by commands, status and launcher; never opens the control protocol |
| `hostservice` | root-owned OpenRC/runit registration, exact native-supervisor child identity, persistent maintenance fences and pidfd-scoped reload; no device transport or alternate supervision loop |
| `desktop` | inspect the matching upstream GTK executable through its display-free protocol-version command |
| `status` | versioned read-only runtime report for source and native installations; no OSC requests or service activation |
| `preview` | compare two partial desk declarations for omitted matrix paths and changed link requirements |
| `routing` | send a plan in two phases, with the link barrier between them |
| `verify` | read the device back and say confirmed, mismatched or unverifiable |
| `process` | supervise the backend: start, `SIGTERM`, escalate to `SIGKILL`, reap; and associate the listening control inode with its executable and exclusive ALSA bridge |
| `pipewire` | generate named virtual sinks from the same config |
| `locking` | the desk file lock and shared device endpoint path: location, permissions and bounded acquisition; backend leases additionally coordinate GTK |
| `marker` | which profile is in effect, remembered beside the config: read, written through a rename, removed |
| `outcome` | what a switch did, as a value: applied and verified, applied and unverified, refused, or written in part with both lists |
| `profiles` | switch to `profiles/<name>.conf` under that lock: validate, write, check, finish the backend operation, then persist the selection; report exact outcomes and preserve the previous marker on incomplete operations |
| `reload` | a desk read again by a running session -- under the lock at the start, and on `SIGHUP` -- kept for the machine the session runs on, or refused as a desk for somewhere else |
| `session` | the service lifecycle: wait for the device, start the backend, apply, signal ready, verify, shut down |
| `launcher` | desktop entry point; checks GTK prerequisites and exact backend/desk identity before launch |
| `reads` | the three actions that read the device and write nothing: `--snapshot`, `--diff`, `--dump-config` |
| `cli` | argument parsing, one action per invocation, and the exit-code mapping -- a switch's outcome and the unit's reload included |
| `__init__` | the supported surface -- read a config, apply and verify it, switch profiles, the errors and outcomes, the two entry points -- and the only module that re-exports; every other module is implementation |

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

Every settable register carries a policy. `REMEMBER` is the default: initial
session application and explicit desk selection write its declared starting
value. Repair and later reconcile preserve it regardless of feedback. `PIN`
authorizes restoration subject to device, playback and link-dependency checks.
The pure `application_plan` selects writes from the operation intent;
observation classification remains separate. Retained values are not counted
as confirmations. Indirect link/partner changes are checked before writing.

PIN is enforced only at the enumerated startup, repair and reload/resume
operations. Device reports outside those windows do not trigger a continuous
reconciliation loop
([ADR 0012](decisions/0012-pin-and-remember.md),
[ADR 0013](decisions/0013-reconcile-triggers.md)).

## The two-phase apply

Channel links are written before the mix matrix, with a barrier between
them. Sending both in one burst silences every even output, because
oscmix only learns a pair is linked when the device echoes the change
back over MIDI, and a `/mix` write that overtakes that echo is evaluated
against the stale flag ([ADR 0001](decisions/0001-two-phase-routing-apply.md)).

The barrier waits for device-origin reports on the operation connection.
GTK has its own subscription and cannot take away the desk receive path.
A quiet device can time out without confirmation; connection failure cannot
be translated into silence or a blind write.
A contradicted link, cancellation or receive error stops dependent writes
with exact sent/pending paths. Background link sync uses the completed verifier
result; no internal observation callback writes inside an OSC delivery.
The pure OSC decoder validates a whole delivery before exposing a report.
Truncated bundles or undecodable messages invalidate the connection rather
than allowing a valid prefix to authorize writes.

## The two seams

**`backend`** is the only module that opens a device-control socket.
`notify` separately sends systemd readiness datagrams. Everything above the
control boundary
borrows the operation owner's `Control` connection. The owner takes the file
lock, connects to the checked kernel peer and acquires a backend lease, then
keeps both across every write, observation and repair. Routing and verification
never select a transport, reconnect or acquire another lease. Profile switch
and main-desk restore use one activation path; the acknowledged operation end
precedes marker persistence under the still-held file lock. Disconnect/failed
completion leaves that marker unchanged. See [ADR 0030](decisions/0030-backend-owned-control.md).

The connection filters backend-derived cache reports out of hardware
observations and preserves each decoded delivery until its consumer finishes
it. Read actions carry the connected serial and epoch into their result;
they do not resolve snapshot identity again after the read. Their observed
values replace earlier reports, rather than retaining the first report.
Status remains file/procfs/service inspection and opens no control connection.
The dependency graph is acyclic: `backend` may call read-only `diagnostics`,
which uses `process`, `discovery` and pure runtime-path selection from `locking`.
Neither diagnosis nor planning imports a command that writes hardware.

**`devices`** owns the device register and channel declarations. The
`registers.Device` model keeps them out of transport and planning code;
`streams` separately owns the measured UCX-II playback-capacity checks.
Only the UCX II has a register table. The 802 has a channel map but no
register table or hardware qualification; adding declarations alone would
not establish backend support for another interface.

### Exit codes

| Code | Meaning | systemd reaction |
|---|---|---|
| 0 | device absent, clean shutdown, or clean backend exit | none |
| 1 | runtime failure; from the command line also a switch whose write gave out part of the way | restart after 3 s (max 5 per 2 min) |
| 2 | a configuration the unit cannot run: a routing.conf error, two interfaces and no `[device] serial`, or a conflicting device owner; from the command line also a usage error or a refused switch | **no** restart (`RestartPreventExitStatus=2`) |
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
  `~/.config`; root is needed for the udev rule, the resume hook and
  ordered system service, and the tmpfiles.d entry for the shared lock directory. `--no-udev`
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

NixOS uses the same runtime and upstream patch series in one immutable store
output, built through the common preparation and payload functions. Its opt-in
module adapts the existing systemd user unit, udev rules and shared lock directory.
Deployment identity lives in a generation-owned file; the ordinary persistent
package fence protects changes of that identity. Configuration, profiles and
marker stay in the user's writable configuration directory. No additional
runtime manager or protocol implementation is introduced (ADR 0033).

Silverblue layers the ordinary Fedora RPM pair into its immutable deployment.
The common systemd user service and setup opt-in remain the runtime interface.
Its RPM composition sandbox defers package-maintenance hooks; the operator uses
the common guard on the booted host before deployment changes and finishes the
persistent fence only after checking the new booted payload (ADR 0034).

The source-installation layout is:

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
/usr/lib/systemd/system-sleep/oscmix                 (root; queue resume work)
/usr/lib/systemd/system/oscmix-resume.service        (root; reload after user.slice thaws)
/usr/lib/tmpfiles.d/oscmix-desk.conf                 (root; /run/oscmix-desk, group audio)
```
