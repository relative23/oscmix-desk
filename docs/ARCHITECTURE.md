# Architecture

How oscmix-desk is built, in the present tense.

`tests/test_architecture.py` checks the runtime module inventory against this
page and enforces the dependency rules.

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
 AutoSet-off → confirmation → links → barrier → matrix → settings → AutoSet-on
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
| `constants` | every exit code and the session's timing constants, each with the measurement that produced it; the ODK1 client's queue limits and keepalive interval live in `backend`, the backend's clocks in `protocol` |
| `arguments` | the session's command-line grammar, shared by the CLI and inspection of the running service's selected config |
| `errors` | configuration, ambiguous-device and lock refusals; `BackendControlError` for failed coordinated backend operations (`ReceivePortError` remains an alias), and `WriteFailed` with submitted/pending paths |
| `log` | journal-shaped logging, no configuration |
| `osc` | encode and decode OSC messages; no I/O |
| `protocol` | the ODK1 wire values (request, event, status, role and source numbers, header layout), mirrored from `control.h` in patch 0003 and checked against it by a test |
| `registers` | what a register row and a device are -- path, tags, bounds, verification class, policy -- and the questions the parser, the reconciler and the verifier ask of a device's table |
| `numeric` | finite values, OSC representation and the pinned backend’s scalar encodings; parameter-specific comparisons and lossless decimal formatting |
| `devices` | the tables themselves: the UCX II's rows and channel map, the 802's channel map, and which of them a config names |
| `model` | a desk as data with validated level/balance claims on linked route aliases: routes, channel and global settings, device identity, `Overrides` (`CommandLine` remains an alias) and two retained legacy OSC ports; only device name, USB ID and serial select the machine |
| `playback` | pure validation of unreadable playback-mute ownership: both adjacent flags must agree; unmute explicitly owns both complete matrix columns, including zero cells; no I/O or inferred DEVICE original |
| `autogain` | pure AutoSet/gain ownership by input and stereo pair, typed dependency values and shared declaration validation; no transport or gain-policy change for other inputs |
| `paths` | where a desk is looked for: the config, `profiles/` beside it, and what a profile name is |
| `sections` | the sections the register table declares -- channels, families, globals -- refusing unsettable values; strict mode rejects unknown or unsupported sections, compatibility mode warns and ignores them |
| `config` | parse `routing.conf` into a `Config`: `[device]`, `[osc]`, routes and pins here, the rest through `sections`; unreadable or invalid files raise `ConfigError` |
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
| `routing` | send explicit mute-on, link, mix, calibration, channel and mute-off phases, with fresh prerequisite barriers before links/mix/unmute; coordinated writes do not imply an atomic or acoustically qualified transition |
| `verify` | read the device back and say confirmed, mismatched or unverifiable |
| `process` | the supervise loop (reload requests, `SIGTERM` then `SIGKILL` on stop); the unit's process and `systemctl` calls; associating the listening control inode with its executable and exclusive ALSA bridge, and ending an orphaned backend |
| `pipewire` | generate named virtual sinks from the same config |
| `locking` | the desk file lock and shared device endpoint path: location, permissions and bounded acquisition; backend leases additionally coordinate GTK |
| `marker` | which profile is in effect for each config file, persisted atomically beside it; `routing.conf` retains the legacy marker name |
| `outcome` | the five `OutcomeState` results of a switch, with immutable paths, separate mismatched/missing/invalid feedback and validated persistence and service reload facts |
| `profiles` | honour maintenance fences, then switch to `profiles/<name>.conf` under that lock: validate, write, check, finish the backend operation and persist the selection; hand over to the matching service, report exact outcomes and preserve the previous marker on incomplete operations |
| `desk` | one coordinated operation: pin the interface, own the device lock and checked connection, take one write lease for apply/verify/repair, acknowledge its end and release resources on every path; readers use the same connection lifetime without a writer lock or lease |
| `reload` | a desk read again by a running session -- under the lock at the start, and on `SIGHUP` -- kept for the machine the session runs on, or refused as a desk for somewhere else |
| `session` | the service lifecycle: wait for the device, start and stop the backend, apply, signal ready, verify, shut down |
| `launcher` | desktop entry point; checks GTK prerequisites and exact backend/desk identity before launch |
| `reads` | the three actions that read the device and write nothing: `--snapshot`, `--diff`, `--dump-config` |
| `cli` | one action per invocation and the exit-code mapping, including a switch's device, persistence and service reload facts |
| `__init__` | the supported surface -- read a config, open a Desk operation, switch profiles, the errors and outcomes, and launch the mixer -- including `Policy` and the optional profile-backend type `Control`; the only module that re-exports |

## One profile switch, end to end

`oscmix-session --profile tracking`, module by module. The start and a
reload run the same middle part (steps 3 to 8) with their own intent.

1. `cli.main` parses the command line, finds the config
   (`paths.discover_config_path`) and refuses during an unfinished package
   update (`hostservice.maintenance_problem`).
2. `profiles.switch_profile` parses and validates the whole profile onto the
   main config's machine settings (`config`, `sections`, `devices`) and
   checks the maintenance fence for Python callers too. A refusal here has
   written nothing and takes no lock. With `strict=True`, unknown or
   unsupported sections in the main config and target are refusals.
3. `desk.Desk` resolves the interface (`discovery.resolve_device`): its serial,
   sequencer client and lock key. A session retains its already pinned identity.
4. The operation takes `locking.device_lock`, the file lock every writer of
   that interface takes. Start and SIGHUP re-read their selected desk inside it,
   retaining the validation mode selected by the session's startup arguments.
5. The operation calls `backend.connect_backend`, identifying the backend through
   `diagnostics.backend_status` (`process`, `discovery`), connects, checks the
   kernel peer credentials and says HELLO as the desk (`protocol`).
   Entering connects; the first apply, verify or repair takes the lease
   (`Control.begin`). All methods share it.
6. `Desk.apply` delegates to `routing.apply_routing`, which asks
   `reconcile.application_plan` what to write
   for the intent (`reconcile.ApplyIntent`: explicit, repair or reconcile,
   which decides PIN and REMEMBER), refuses a plan that would lose a value it
   must keep (`retention_problem`) or a changed playback mode
   (`streams.PlaybackGuard`), then sends links, waits at the link barrier
   for device reports (`await_link_echo`), and sends the mix and channel
   state.
7. `Desk.verify` delegates to `verify.verify_routing`, which requests a refresh
   and compares the device-origin
   reports (`observation`, `numeric`) with the expected registers, giving a
   `VerifyResult`.
8. The operation acknowledges `Control.finish` and closes the connection;
   only then does `marker.remember_active_profile` record the switch, still
   holding the file lock. Every exit releases the connection and lock.
   A failed write disconnects without replaying or committing a marker.
9. `profiles` asks `process.reload_service` to hand the selection to the
   running service after releasing the lock, if it runs the matching config.
   A partial write retains the previous selection and requests its PIN
   reconciliation; an uncommitted complete write requests no reload.
   `Outcome.reload_failed` retains any reload failure beside the write facts.
   The result becomes the exit code in `cli`, which sends no second reload.

At start, `session` runs steps 3 to 6 before `READY=1` and step 7 in a
background thread with a repair (`verify.verify_and_repair`); a SIGHUP runs
them in `reload` with the reconcile intent, which keeps remembered values.
An unusable desk found by a strict startup re-read refuses with exit 2 before
any writes or readiness. On SIGHUP it skips the reconcile and keeps the
session running. Default compatibility still permits startup fallback.
Read commands use the same operation as a subscriber with no writer lock or
write lease, so they can observe while another operation holds ownership.

`--check` stops at parsing: it strictly validates the main file, every
profile and the stored selection without a device lock, connection, marker
change or service reload. It returns 0 after the complete check, or 2 for the
first failure, including an unreadable profile directory. Explicit profile
selection/restore validates its target directly so it can recover a broken
active selection; its preview uses the same target validation and a
compatibility reference for the previous declared desk.

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
recorded device dumps instead.

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
reconciliation loop.

## The two-phase apply

Channel links are written before the mix matrix, with a barrier between
them. Sending both in one burst silences every even output, because
oscmix only learns a pair is linked when the device echoes the change
back over MIDI, and a `/mix` write that overtakes that echo is evaluated
against the stale flag.

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
completion leaves that marker unchanged.

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
| 3 | `--diff`: the device and config disagree; profile selection: writes completed but read-back reported differing or invalid values | never seen; the service runs no flag |
| 4 | a switch reached the device but could not be recorded | never seen; flags only |
| 5 | the unit is running and refused the reload | never seen; flags only |
| 130 | interrupted (Ctrl-C) before the backend ran | never seen; systemd stops with SIGTERM |

`diff(1)` returns 1 for "differing" and that is not available here,
because 1 already means a failure. A caller has to be able to tell *the
desk drifted* from *the backend never answered*: conflating them makes a
monitoring check report healthy silence while the backend is down.

## Design constraints

- **Python, standard library only.** The original implementation was shell
  + inline Python. A single Python process gives testable pure functions,
  real signal handling and process supervision, and error messages that
  name the section/option at fault -- without adding a single dependency
  beyond what the shell version already needed.

- **User-owned state and explicit host integration.** The source installer
  puts its runtime in `~/.local`; native packages and NixOS use their managed
  system locations. Configuration, profiles and the active marker remain
  with the selected user. Root installs host activation/resume adapters and
  the shared runtime directory. A source install with `--no-udev` omits those
  host changes and uses the per-user runtime fallback. Installation
  and explicit hardware activation remain separate operations on every target.

- **`oscmix-launch` shares the read-only identity checks.** It uses the
  ordinary config lookup, backend/manager inspection, companion inspection
  and maintenance fence. It may start an explicitly enabled matching desk,
  then passes the checked backend and device identity to GTK. It does not
  import the routing/apply path or issue mixer writes.

- **Routing lives in the config, not in code.** A new desk session applies
  its declared starting state through the backend. Later repair and reconcile
  preserve REMEMBER controls; explicit profile/main-desk application may
  deliberately restore the declared values.

- **udev remove matches `ENV{PRODUCT}`.** At remove time the sysfs
  attributes are already gone, so an `ATTR{idVendor}` match never fires.
  What ends the service on unplug is not this rule but the backend
  exiting with its device; the remove match is what lets the
  user manager retire the device unit it tagged on add.

- **Stale cleanup identifies the control owner.** The listening SEQPACKET
  inode in `/proc/net/unix` is associated with `/proc/<pid>/fd`, the executable
  and its exclusive ALSA bridge. Only this user's orphaned backend for the
  selected sequencer client and serial may be terminated. A pidfd pins the
  process and the association is checked again before signalling. A live desk,
  another device or uncertain ownership is a refusal. The backend handles
  abandoned socket files under its own owner lock; desk does not kill an old
  UDP session to make room.

- **One interface, resolved once.** `discovery.resolve_device` answers
  which interface a desk is for -- serial, sequencer client and lock key
  together -- and the service, a switch, a restore and a reconcile all
  use that one answer. `[device] serial` selects among identical
  interfaces; without it more than one candidate is refused, never
  guessed. A switch checks the control owner and exclusive ALSA bridge,
  connects to the matching kernel peer, and acquires the whole-operation
  lease while holding the device file lock. The shared runtime directory
  belongs to `root:audio`; filesystem access, process identity and operation
  ownership are distinct checks.

- **Named PipeWire sinks are generated, not hardcoded.**
  `oscmix-session --pipewire-sinks` derives one loopback sink per stereo
  output pair from playback routes, named after the first route targeting
  that pair. Input-only monitoring routes do not create desktop playback
  sinks. It auto-detects the Fireface sink node via `pw-dump`, so the
  desktop integration follows the declared desk.

## Installed files

NixOS uses the same runtime and upstream patch series in one immutable store
output, built through the common preparation and payload functions. Its opt-in
module adapts the existing systemd user unit, udev rules and shared lock directory.
Deployment identity lives in a generation-owned file; the ordinary persistent
package fence protects changes of that identity. Configuration, profiles and
marker stay in the user's writable configuration directory. No additional
runtime manager or protocol implementation is introduced.

Silverblue layers the ordinary Fedora RPM pair into its immutable deployment.
The common systemd user service and setup opt-in remain the runtime interface.
Its RPM composition sandbox defers package-maintenance hooks; the operator uses
the common guard on the booted host before deployment changes and finishes the
persistent fence only after checking the new booted payload.

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
