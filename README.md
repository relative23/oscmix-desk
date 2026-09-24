# oscmix-desk

[![CI](https://github.com/relative23/oscmix-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/relative23/oscmix-desk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Your RME Fireface UCX II, described in a text file.** Write down what the desk
should look like -- routing, faders, EQ, dynamics, reverb, the clock -- and
enable automatic operation to apply it whenever the interface is plugged
in or the machine boots.
Use profiles for different setups and `--diff` to compare the config with
the state the device reports. Settings that cannot be read back are shown
as unverifiable.

It started as an autostart, and it still is one: plug the interface in, the
backend comes up, the mixer GUI is one click away in the app menu. What it
grew into is a state layer. **2028 registers are declared**, each with its
type, its bounds and its verification class, checked against recorded
UCX II dumps and a write sweep. The measurement limits are stated below.

[oscmix] by Michael Forney does the hard part: it speaks the Fireface's
MIDI SysEx protocol and exposes the hardware mixer over OSC, with a GTK
GUI similar to TotalMix FX. This project makes the desk *declarative*, and
makes the desktop integration disappear.

| Piece | What it does |
|---|---|
| `routing.conf` | the desk as a text file: routes, faders, per-channel state, EQ, room EQ, dynamics, low cut, auto level, crossfeed, reverb, echo, control room, clock |
| `--diff` | what an apply would change, without changing it |
| `--dump-config` | reported settings exported as config, with unrepresentable or unknown state identified |
| `--snapshot` | every register the device reports, for comparing two moments |
| `--status [--json]` | read-only installation, device, backend, playback-mode and mixer diagnosis |
| profiles | named alternatives, switched under one lock with a stated outcome |
| `--dry-run --profile NAME` | planned writes and previously declared routes the target leaves undeclared |
| `[pin]` | ownership overrides for flat input/output settings; other families use their register defaults |
| udev rule | starts the backend on hotplug, disables Fireface USB autosuspend, and keeps affected ASM4242 host controllers awake |
| systemd user service | supervises the backend; reports initial apply and subsequent verification separately |
| `--pipewire-sinks` | named outputs ("Monitors", "Headphones") in your desktop's sound settings |
| desktop entry + launcher | "RME Fireface Mixer" in the app menu, with sanity checks and notifications |
| `install.sh` | builds oscmix at a pinned revision and installs everything per-user |

[oscmix]: https://github.com/michaelforney/oscmix

The [feature inventory](docs/FEATURE-SURFACE.md) distinguishes desk config,
the pinned backend and its existing GTK controls, including their limits.

![oscmix-gtk showing the Fireface UCX II hardware mixer](docs/img/oscmix-gtk.png)
*The upstream oscmix-gtk mixer on a UCX II. oscmix-desk applies declared
settings and checks reported state under the PIN/REMEMBER rules.*

## Measured, not asserted

Hardware claims are tied to recordings from a real UCX II. Multi-device
identity and concurrency are tested with simulated devices. Three defects in 0.1.3 were
invisible at message level and only showed up by playing a tone and reading
the device's own meters; that set the standard the project has been held to
since.

- **1888 sweep entries confirmed; 14 deliberately skipped.** The sweep
  covers 1902 settable entries, skips reference-level changes (ADR 0016),
  and records per-register verdicts in
  [the 0.7.3 sweep](docs/evidence/0.7.3/write-sweep-ucx2.json).
  It preserves requested and reported values and restores all 2252
  readable messages exactly, including type tags and additional arguments.
  Playback matrix writes cannot be verified by this backend. The recorded
  refresh contains 2322 paths, including 70 streamed paths. See the
  [evidence and provenance limits](docs/HARDWARE-EVIDENCE.md).
- Each release attaches a **hardware evidence artifact**: the routes
  measured, the levels, the device serial, the exact oscmix revision
  and, since 0.6.2, the firmware it was taken against -- because a
  device that changed underneath the evidence would otherwise be
  invisible in it.
- [Sample-rate measurements](docs/evidence/0.7.1/sample-rates.md) cover
  24 USB/ALSA combinations. At 88.2/96 kHz, the 20-channel stream has
  only 16 active playback channels; at 176.4/192 kHz, the 8/14-channel
  modes pass while 16/20 fail. The 0.7.2 runtime checks the active stream
  before playback writes; see
  [upgrade behavior](docs/UPGRADING.md#upgrading-to-072).
  The desk never selects a rate automatically. The
  [higher-rate routing checks](docs/evidence/0.7.2/) record 52 direct-ALSA
  gain/mute cases and 65 route checks through PipeWire Pro Audio.
- The upstream backend is **pinned to a full commit SHA**, and the pin only
  moves together with a fresh measurement.
- Twenty-eight [decision records](docs/decisions/) carry the reasoning and the
  measurement behind anything non-obvious, including the ones that say *we
  looked and there was nothing to fix*.
- Five issues and two fixes have gone upstream from this work
  ([documented](docs/upstream-issues.md)); all five are fixed at the
  pinned revision.

## Why you want this

Out of the box, the UCX II works as a class-compliant USB audio device on
Linux (`snd-usb-audio`), but the hardware mixer is a black box: whether you
hear anything depends on whatever routing state the device happens to be
in. PipeWire profiles can expose the interface as surround audio or one
multichannel device. A stereo application may therefore miss the playback
channels routed to your monitors. Named stereo sinks make that choice explicit.

oscmix-desk makes the state predictable: every time the device is
plugged in or the machine boots, the routing you declared in a small config
file is applied to the hardware mixer. Hardware direct monitoring works
independently of the host audio server.

## Requirements

- Linux with ALSA; systemd and udev for automatic hotplug/resume operation
- Python >= 3.9 (standard library only)
- To build oscmix: `git`, `make`, a C compiler, `pkg-config`,
  ALSA headers, and optionally GTK 3 headers for the existing upstream mixer

  ```sh
  # Debian/Ubuntu
  sudo apt install build-essential git pkg-config libasound2-dev \
                   libgtk-3-dev libglib2.0-dev-bin
  # Fedora
  sudo dnf install gcc make git pkgconf-pkg-config alsa-lib-devel gtk3-devel
  # Arch
  sudo pacman -S --needed base-devel git alsa-lib gtk3
  ```

## Install

**Source installation and native packages support explicit activation.**
Obtain a [verified source release](docs/RELEASE-ARTIFACTS.md), unpack it,
and run:

```sh
./install.sh --check
./install.sh
~/.local/bin/oscmix-session --dry-run --timeout 0
```

Preflight names missing dependencies, permissions, the backend pin and
service availability. A fresh installation copies files without starting
the mixer. Review `~/.config/oscmix/routing.conf`, then explicitly activate
automatic operation with `./install.sh --no-build --enable`. An upgrade
preserves the old service's active/enabled state and existing configuration.

Use `--manual` for foreground operation without a systemd user manager;
a missing manager selects that mode automatically. Use `--no-udev` to skip
root integration, including the shared lock directory. The
[installation and recovery guide](docs/INSTALLATION.md) describes these
choices, prerequisites and limits.

Native packages are built for Ubuntu 24.04, Fedora 44, openSUSE Leap 16
and Arch on x86_64. They include the pinned backend and use `oscmix-setup`
for explicit per-user setup and migration. Use the
[package installation guide](docs/INSTALLATION.md) for the matching target.
`pip` alone does not install the required host integration.

From 0.7.3, the optional `oscmix-desk-gtk` companion adds upstream's mixer,
schema and desktop entry to the matching core package. The headless package
keeps its own dependencies. [Runtime status and mixer use](docs/STATUS.md)
explain connection checks and the close/read-back/reopen workflow.

Prefer a [verified source release](docs/RELEASE-ARTIFACTS.md) or a package
qualified for your distribution. Read the
[upgrade notes](docs/UPGRADING.md) before moving an existing desk. The
existing upstream GTK mixer remains optional; the separate desk desktop
application is deferred.

## Configure your routing

Edit `~/.config/oscmix/routing.conf`:

```ini
[route:main-out]          # rear line outputs 1/2
playback = 1/2
output = 1/2

[route:monitors]          # speakers on rear outputs 5/6
playback = 1/2
output = 5/6
level = 0.0               # mix gain in dB (0 = unity, -65 = mute)
```

For a pair with `stereo = false`, the level range is -65 to 0 dB:
compensation uses the available positive gain headroom. `level = -65`
writes digital mute on both sides.

Apply with `systemctl --user restart oscmix.service`. Mono routes
(`playback = 3` / `output = 7`) work too. They explicitly unlink both
source and output pairs so only the named channels are addressed. Routes
requiring contradictory link states for the same pair are rejected before
anything is written. PipeWire and PulseAudio send
stereo audio to playback channels 1/2, so most setups only route 1/2 to
wherever their speakers are connected.

### The rest of the strip

The file supports the options with a validated config domain in the
device model, in addition to routing:

```ini
[input:3]                 # per-channel state
gain = 12.0
hi-z = true

[eq:input:3]              # three-band EQ, per channel
enabled = true
band1freq = 80
band1gain = -3.0
band1type = Low Shelf

[dynamics:output:5]       # compressor and expander
compthres = -18.0
compratio = 4.0

[clock]                   # settings with no channel at all
source = Internal

[pin]                     # who wins after the first write
output.volume = pin
```

Bounds come from the device: `compratio = 12.0` is refused because the
register stops at 10, and the message says so. Values are checked before
anything reaches the hardware.

`oscmix-session --dump-config` exports the representable reported subset
in this shape, naming omissions. Playback routes, channel names and
loopback cannot be recovered this way. Phantom power has no config
domain; sample rate and CC mode are read-only. Clock source is a
configurable enum. The UCX II's front
headphones are outputs **7/8**, not 1/2.

**A config describes a partial desired state.** Omitting an old route
does not mute its crosspoint. Declare its mute explicitly when required;
linked-channel writes can also affect the partner channel.

Initial applies write declared settings. Later selective reconciliation
enforces PIN values and leaves REMEMBER values with the device. `[pin]`
overrides apply only to flat input/output options; uncommenting a dump
comment does not turn a setting into a pin. There is no continuous
polling or automatic saving of GUI changes. Adding `volume = <dB>` to a
route sets that output's fader on each initial apply, including backend
starts and explicit profile switches. Its default policy is REMEMBER;
`output.volume = pin` in `[pin]` also enforces it during later selective
reconciliation. Leave the fader undeclared when the initial apply should
not set its value. The route's `level` controls its mix-matrix gain and
is always written.

Shortly after startup,
`oscmix-session` also reads the state back from the device in the
background and re-sends once on mismatch -- the journal line `routing
verified against device state` means that the read-back found no remaining
problem requiring repair under the PIN/REMEMBER and reportability rules.
It does not mean that every declared value equals the hardware: a differing
REMEMBER value is deliberately kept and logged separately. The summary
separates matching reports, retained REMEMBER values, PIN differences,
missing reports and backend-unreportable settings. The playback
mix matrix is never reported by oscmix, so it is re-established rather than
confirmed; read-back cannot prove it.

## Profiles

Profiles are named configuration variants you can diff and keep in version
control. Switching applies the settings declared in the selected profile;
omitted settings and existing routes are not automatically reset or muted.
They describe partial desired states, not complete device snapshots.

A profile uses the same format as `routing.conf`, in a `profiles` directory
beside your main one:

```
~/.config/oscmix/routing.conf
~/.config/oscmix/profiles/tracking.conf
~/.config/oscmix/profiles/mixdown.conf
```

```sh
oscmix-session --list-profiles
oscmix-session --profile tracking
```

Leave `[osc]` and `[device]` out of a profile. Those describe the
machine, not the desk, and are taken from your main config. A dumped
profile restates them, which is harmless until `routing.conf`'s own
change -- it then names the old ones. A profile that names *another*
port or interface is refused: a switch to it writes nothing, and one
that was still active from 0.6.x falls back to `routing.conf` with a
warning
([ADR 0026](docs/decisions/0026-a-profile-is-the-desk-not-the-machine.md)).
One main config per directory: the profiles and the record of the
active one belong to the directory.

A switch validates the full config before its first write. It reports
four outcomes; a transport failure can still interrupt valid writes:

```
applied 'tracking' and verified it at the device
applied 'tracking'; 1 register(s) this backend cannot report: /mix/1/playback/1
refused 'tracking', nothing written: [route:x] output: channel 99 out of range 1..64
written-in-part: lists written/unwritten paths; active marker unchanged
```

**A refusal costs nothing.** The profile is parsed and validated in
full before the first byte goes out, so a typo costs you an error
message rather than your monitoring. That matters because there is no
undo on a mixer: once a fader value is on the wire, the speakers already
have it.

The second line is the normal outcome on a desktop, and it is not a
problem. The playback mix matrix is one of the few things oscmix never
reports back, so a perfectly good switch still cannot confirm it -- and
if you have the mixer GUI open it holds the port the read-back needs, so
nothing at all can be confirmed. Both cases say so instead of claiming
success. The reasoning is in
[ADR 0011](docs/decisions/0011-a-profile-switch-states-its-outcome.md).

**A profile survives a start.** The switch remembers the profile's
name in `active-profile` beside `routing.conf`, and every backend start
and every reload -- a replug, a reboot, the resume hook's reconcile
after a suspend -- applies the profile rather than `routing.conf`. The
journal says which one is in effect. `oscmix-session --no-profile`
applies `routing.conf` again and forgets the profile;
`--list-profiles` marks the active one. A remembered profile that no
longer loads falls back to `routing.conf` with a warning, and keeps
warning until you decide. While the switch writes, it holds
a lock named after the interface in `/run/oscmix-desk/`,
the one path every writer on the machine computes the same way,
and so does the service for
its own apply, verifier and reconcile: one writer at a time, whichever
desk operation it is
([ADR 0019](docs/decisions/0019-one-lock-for-every-writer.md)).
The upstream mixer GUI and direct OSC clients do not take this lock.
That directory belongs to the group `audio`, so the user running the
desk has to be in it; the installer warns when it is not. A switch
writes only to the backend that drives this desk's interface, and with
two identical interfaces on one machine `[device] serial` has to say
which one a desk is for -- without it nothing writes, rather than
configuring whichever box the kernel found first
([ADR 0024](docs/decisions/0024-one-interface-resolved-once.md)).
If the marker cannot be written -- a read-only config directory, a full
disk -- the switch says so and does not reload the service, because
that reload would re-read `routing.conf` and undo it. While a profile is active, edits to
`routing.conf`'s routes are not in effect; edits to `[osc]` and
`[device]` are, because a profile inherits those. The reasoning is in
[ADR 0018](docs/decisions/0018-the-active-profile-survives-a-start.md).

## What comes back after a restart, and what does not

Every setting this project writes is either **pinned** -- the config wins
and is re-sent if the device disagrees -- or **remembered** -- the config
sets it once and then the mixer wins.

The defaults follow what a setting *is*:

| pinned | remembered |
|---|---|
| the routing itself, channel links | `volume` |
| `reflevel`, `gain`, `hi-z` | `mute`, `phase` |

Pinned settings describe your installation: a reference level or a hi-Z
switch has to match the cable that is plugged in, and a wrong value there
is a signal problem. Remembered settings are the ones you reach for
during a session -- turn a monitor fader and it stays turned. `48v` is
modelled as pinned as well, but no config can set it yet: phantom power
stays out of the file until a hardware case proves the channel it names
is the channel it powers.

Override it per option when your setup disagrees:

```ini
[pin]
output.volume = pin      # a fixed install: levels are set once
input.gain = remember    # a studio that rides gain by hand
```

**What pinning does not mean.** It does not snap back the moment you
change something in the mixer GUI. It cannot: of everything a config
sets, the device announces only channel links when they change --
measured. Everything else is silent until something asks for a full state
dump. So pinning means *this session insists*, through the read-back
after start; it does not mean a background process fighting you all day.
[ADR 0012](docs/decisions/0012-pin-and-remember.md) has the measurements.

`--dump-config` uses the same rule: it writes pinned values as config and
remembered ones as comments, because a dump cannot tell "I meant this"
from "this is where I left it".

**When does it insist?** At startup, and whenever you ask:

```sh
systemctl --user reload oscmix.service
```

That re-reads the desk in effect -- the active profile, else
`routing.conf` -- reads the device back, re-applies what is pinned and
leaves what is remembered exactly where you put it. Use
`reload`, not `kill --signal=SIGHUP`: the latter signals every process in
the unit, and the backend does not handle SIGHUP, so it dies.

A replug already does the full thing -- udev restarts the service. And
after suspend, an installed hook asks for the same reconcile you would
ask for by hand. Nothing runs on a timer.

## Named outputs in your sound settings (PipeWire)

PipeWire presents the Fireface's analog outputs as a single "7.1
surround" device. If you would rather pick "Monitors" or "Headphones" by
name in GNOME/KDE sound settings, generate one virtual sink per stereo
route:

```sh
mkdir -p ~/.config/pipewire/pipewire.conf.d
oscmix-session --pipewire-sinks > ~/.config/pipewire/pipewire.conf.d/oscmix-sinks.conf
systemctl --user restart pipewire wireplumber
```

Each sink feeds the device playback channels that match the route's
output pair, so those pairs need an identity route (`playback = output`)
in routing.conf -- the generated file contains a ready-to-paste note if
one is missing. The Fireface sink node and its real channel layout are
auto-detected via `pw-dump`, so the mapping is correct in both the
surround and the pro-audio/Direct profile; pass
`--pipewire-target <node.name>` to override the detection.

## How it works

```
USB hotplug ── udev rule ── systemd user service ── oscmix-session
                                                        │
                                     ┌──────────────────┼─────────────────┐
                                 finds MIDI       starts alsaseqio    applies routing
                                 client via       + oscmix (OSC ⇆    from routing.conf
                                 /proc/asound     MIDI SysEx)         via OSC/UDP
```

Details, including the failure model and exit-code semantics, are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Notes on the OSC interface
oscmix exposes are in [docs/OSC-PROTOCOL.md](docs/OSC-PROTOCOL.md), and
the choices that are not obvious from the code are recorded in
[docs/decisions/](docs/decisions/). What the service is trusted with --
including the fact that the control port is unauthenticated -- is in
[docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md). Where this is heading
is in [docs/ROADMAP.md](docs/ROADMAP.md).

## Troubleshooting

```sh
systemctl --user status oscmix.service      # is the backend running?
journalctl --user -u oscmix.service -e      # backend logs
oscmix-session --dry-run                    # what would be started/sent?
oscmix-session --status                     # read-only device/backend/GTK diagnosis
oscmix-session --status --json              # versioned report for scripts
```

More in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Other Fireface models

Hardware measurements come from one Fireface UCX II. **The pinned
backend cannot operate a Fireface 802.** This project's 802 entry is a
channel map used for validation, without a register model or a support
claim. Other models require a working backend and measured evidence.

What an 802 config gets today: routes, with channel ranges checked against
upstream's own table; channel, nested and global sections are dropped
with a warning that names the device and says nothing in them could
reach it, rather than parsed into nothing. The device name and USB ID
are configurable in `routing.conf` (`[device]`); for hotplug, adapt the
IDs in `udev/90-rme-fireface.rules`.

What it would take, with the hardware on the desk: record a dump
(`scripts/record-dump.py`), declare the rows, run the write sweep
(`scripts/sweep-writes.py`) and attach one evidence artifact. That is
the bar `Device.supported` states in the data. Start with the
[hardware-report recipe](docs/HARDWARE-EVIDENCE.md#reporting-another-device).

## Development

```sh
pip install -r requirements-dev.txt

make check            # lint, type checks, dead-code checks and tests
make test             # pytest, no hardware needed
make lint             # ruff + shellcheck + syntax check
make typecheck        # mypy --strict over the runtime package
make deadcode         # vulture
make coverage         # with the ratchet from pyproject.toml
make flake            # the suite five times over, to surface races
make soak             # restart cycles; the gate is the scheduled workflow
make mutation         # do the assertions actually catch a wrong value?
make verify-hardware  # measure the audio itself (needs a Fireface)
```

Coverage, repeated-suite, soak and mutation checks are separate gates;
`make check` does not run them. The [release checklist](docs/RELEASE-CHECKLIST.md)
also requires the Python-version matrix, backend build and hardware checks.

Install the dev requirements before trusting a green run. Without
`hypothesis`, `tests/test_contracts.py` skips itself -- the suite says so
loudly at the end, and `OSCMIX_REQUIRE_CONTRACTS=1` turns that skip into
an error, which is how CI runs it.

The integration tests run `oscmix-session` against a stub backend with a
fake `/proc` and sysfs, so the full startup/routing/shutdown path is tested
without a Fireface attached. The device stand-ins in
`tests/test_apply_routing.py` go one step further and model oscmix's
stereo-link state machine, which is what pins down the ordering the mixer
matrix depends on.

Two gates exist because this project got burned by exactly what they
catch. The Python matrix runs 3.9 through 3.14: a test helper that shadowed
a private `threading.Thread` attribute failed on 3.13 alone -- the
colliding name exists only there -- and passed on 3.11 and 3.14, so no
local run on one interpreter could have caught it. And `make flake` repeats the suite, because the tests
bind real UDP sockets and drive background threads, where a teardown race
survived several consecutive green runs.

A third runs nightly rather than per commit: `.github/workflows/soak.yml`
restarts the session 200 times and checks the routing datagrams byte for
byte every time. Timing defects have survived individual green runs.
Numeric, export and validation defects also need independent semantic
tests; repeating the suite alone does not establish correctness.

The runtime itself has no Python dependencies -- `oscmix-session` uses only
the standard library, so it runs before any package manager is involved.

## Uninstall

```sh
./uninstall.sh          # keeps ~/.config/oscmix
./uninstall.sh --purge  # removes the config too
```

## Credits and license

All the actual protocol work happens in [oscmix] (ISC license) -- this
project is just the glue that makes it feel native on a Linux desktop.
oscmix-desk is MIT licensed, see [LICENSE](LICENSE).
