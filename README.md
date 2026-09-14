# oscmix-desk

[![CI](https://github.com/relative23/oscmix-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/relative23/oscmix-desk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Your RME Fireface, described in a text file.** Write down what the desk
should look like -- routing, faders, EQ, dynamics, reverb, the clock -- and
it is applied every time the interface is plugged in or the machine boots.
Then `--diff` tells you whether it still looks that way.

It started as an autostart, and it still is one: plug the interface in, the
backend comes up, the mixer GUI is one click away in the app menu. What it
grew into is a state layer. **2028 registers are declared**, each with its
type, its bounds and its verification class, every one of them measured
against a real UCX II rather than copied from a datasheet.

[oscmix] by Michael Forney does the hard part: it speaks the Fireface's
MIDI SysEx protocol and exposes the hardware mixer over OSC, with a GTK
GUI similar to TotalMix FX. This project makes the desk *declarative*, and
makes the desktop integration disappear.

| Piece | What it does |
|---|---|
| `routing.conf` | the desk as a text file: routes, faders, per-channel state, EQ, room EQ, dynamics, low cut, auto level, crossfeed, reverb, echo, control room, clock |
| `--diff` | what an apply would change, without changing it |
| `--dump-config` | the desk you have, as the file that reproduces it |
| `--snapshot` | every register the device reports, for comparing two moments |
| profiles | named alternatives, switched as a transaction |
| `[pin]` | which settings the file owns and which the device keeps |
| udev rule | starts the backend on hotplug, disables Fireface USB autosuspend, and keeps affected ASM4242 host controllers awake |
| systemd user service | supervises the backend (`Type=notify`: "started" means "audio works") |
| `--pipewire-sinks` | named outputs ("Monitors", "Headphones") in your desktop's sound settings |
| desktop entry + launcher | "RME Fireface Mixer" in the app menu, with sanity checks and notifications |
| `install.sh` | builds oscmix at a pinned revision and installs everything per-user |

[oscmix]: https://github.com/michaelforney/oscmix

![oscmix-gtk showing the Fireface UCX II hardware mixer](docs/img/oscmix-gtk.png)
*The upstream oscmix-gtk mixer on a UCX II. This project keeps that desk in
a file, and keeps the file and the desk agreeing.*

## Measured, not asserted

Every claim here was taken off a real device, and the ones that did not
survive were removed rather than softened. Three defects in 0.1.3 were
invisible at message level and only showed up by playing a tone and reading
the device's own meters; that set the standard the project has been held to
since.

- **Every settable register is proven to accept a write**, not assumed
  to: a sweep writes each of the 1902 a different legal value and
  confirms the device's own report, with per-register verdicts in
  [docs/evidence/write-sweep-ucx2.json](docs/evidence/write-sweep-ucx2.json).
  It found two defects in this project's own register model and one
  upstream before any user could.
- Each release attaches a **hardware evidence artifact**: the routes
  measured, the levels, the device serial, the exact oscmix revision
  and, since 0.6.2, the firmware it was taken against -- because a
  device that changed underneath the evidence would otherwise be
  invisible in it.
- The upstream backend is **pinned to a full commit SHA**, and the pin only
  moves together with a fresh measurement.
- Seventeen [decision records](docs/decisions/) carry the reasoning and the
  measurement behind anything non-obvious, including the ones that say *we
  looked and there was nothing to fix*.
- Five issues and two fixes have gone upstream from this work
  ([documented](docs/upstream-issues.md)); all five are fixed at the
  pinned revision.

## Why you want this

Out of the box, the UCX II works as a class-compliant USB audio device on
Linux (`snd-usb-audio`), but the hardware mixer is a black box: whether you
hear anything depends on whatever routing state the device happens to be
in. PipeWire also maps the 8 analog outputs as "7.1 surround", so stereo
audio only reaches outputs 1/2 -- if your monitors are connected elsewhere,
you get silence.

oscmix-desk makes the state predictable: every time the device is
plugged in or the machine boots, the routing you declared in a small config
file is applied to the hardware mixer. Zero-latency hardware routing,
independent of the audio server.

## Requirements

- Linux with systemd and udev (any mainstream distro)
- Python >= 3.9 (standard library only)
- To build oscmix: `git`, `make`, a C compiler, `pkg-config`,
  ALSA headers, and GTK 3 headers for the GUI

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

```sh
git clone https://github.com/relative23/oscmix-desk
cd oscmix-desk
./install.sh
```

The installer builds oscmix from upstream, installs everything into
`~/.local` / `~/.config`, and asks for sudo once -- only for the udev rule
in `/etc/udev/rules.d/`. Run `./install.sh --no-udev` for a fully rootless
install (you lose hotplug autostart; the launcher still starts the backend
on demand). Existing files are backed up, an existing `routing.conf` is
never overwritten.

Then plug in the Fireface (or reboot) and open **RME Fireface Mixer** from
the app menu.

## Configure your routing

Edit `~/.config/oscmix/routing.conf`:

```ini
[route:main-out]          # headphones on the front panel
playback = 1/2
output = 1/2

[route:monitors]          # speakers on rear outputs 5/6
playback = 1/2
output = 5/6
level = 0.0               # mix gain in dB (0 = unity, -65 = mute)
```

Apply with `systemctl --user restart oscmix.service`. Mono routes
(`playback = 3` / `output = 7`) work too. PipeWire and PulseAudio send
stereo audio to playback channels 1/2, so most setups only route 1/2 to
wherever their speakers are connected.

### The rest of the strip

Since 0.4.0 the file is not limited to routing. Anything the device
exposes and oscmix can write can be declared:

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

`oscmix-session --dump-config` writes the desk you already have as a file
in exactly this shape, which is usually the easiest way to start.

**A route rewrites exactly the registers it declares, and nothing else.**
Everything not named keeps whatever you set in the mixer and survives
every restart. That is the default for *every* setting above, including
the ones you can now express: writing them in the file shows them, a
`[pin]` entry makes the file own them. The one route-level exception is
opt-in: adding `volume = <dB>` to a route pins that output's fader, and
every backend start forces it back to that value. That is what you want for a fixed installation, and
what you do not want if you set your monitor level by hand -- in which
case just leave the line out. Note that `level` is a different thing: it
is the routing itself, the mix-matrix gain, and is always written.

Shortly after startup,
`oscmix-session` also reads the state back from the device in the
background and re-sends once on mismatch -- the journal line `routing
verified against device state` is your proof that everything the device
reports back matches. The playback mix matrix is not in that: oscmix
never reports it, so it is re-established rather than confirmed, and no
read-back can prove it.

## Profiles

Several routings, and a command to switch between them -- TotalMix's
snapshots, but as text files you can diff and keep in version control.

A profile is a whole `routing.conf`, in a `profiles` directory beside
your main one:

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
machine, not the desk, and are taken from your main config unless a
profile states them itself.

A switch reports exactly one of three things, and no partly valid
config is ever half-applied:

```
applied 'tracking' and verified it at the device
applied 'tracking'; 1 register(s) this backend cannot report: /mix/1/playback/1
refused 'tracking', nothing written: [route:x] output: channel 99 out of range 1..64
```

**A refusal costs nothing.** The profile is parsed and validated in
full before the first byte goes out, so a typo costs you an error
message rather than your monitoring. That matters because there is no
undo on a mixer: once a fader value is on the wire, the speakers already
have it.

The middle line is the normal outcome on a desktop, and it is not a
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
a lock named after the interface in `$XDG_RUNTIME_DIR/oscmix-desk/`,
and so does the service for
its own apply, verifier and reconcile: one writer at a time, whichever
it is ([ADR 0019](docs/decisions/0019-one-lock-for-every-writer.md)).
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

That re-reads `routing.conf`, reads the device back, re-applies what is
pinned and leaves what is remembered exactly where you put it. Use
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
```

More in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Other Fireface models

Every number in this repository was measured on one Fireface UCX II,
and that is the method rather than a gap in it: upstream oscmix is
written for the UCX II, with experimental support for the 802, and a
state layer that claimed more than it had measured would be the kind
of documentation this project exists to avoid. The register table is
indexed by device from its first line, so a second model is a data
change with an evidence artifact, not a rewrite.

What an 802 gets today: routes, with channel ranges checked against
upstream's own table; channel, nested and global sections are dropped
with a warning that names the device and says nothing in them could
reach it, rather than parsed into nothing. The device name and USB ID
are configurable in `routing.conf` (`[device]`); for hotplug, adapt the
IDs in `udev/90-rme-fireface.rules`.

What it would take, with the hardware on the desk: record a dump
(`scripts/record-dump.py`), declare the rows, run the write sweep
(`scripts/sweep-writes.py`) and attach one evidence artifact. That is
the bar `Device.supported` states in the data. Reports welcome.

## Development

```sh
pip install -r requirements-dev.txt

make check            # everything CI enforces, fastest failure first
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
byte every time. Every failure mode found in this project so far was a
timing bug, and every one of them survived a green single run.

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
