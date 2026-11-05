# oscmix-desk

[![CI](https://github.com/relative23/oscmix-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/relative23/oscmix-desk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Your RME Fireface UCX II, described in a text file.** Write down what the
desk should look like -- routing, faders, EQ, dynamics, reverb, the clock --
and oscmix-desk applies it whenever the interface is plugged in or the
machine boots, then reads the state back from the device.

It builds on [oscmix] by Michael Forney, which speaks the Fireface's MIDI
SysEx protocol and provides a GTK mixer similar to TotalMix FX. oscmix-desk
adds the declarative desk, profiles, verification and the Linux integration.

The latest release is **0.9.0**.

![oscmix-gtk showing the Fireface UCX II hardware mixer](docs/img/oscmix-gtk.png)

## Features

| | |
|---|---|
| `routing.conf` | routes, faders, per-channel state, EQ, room EQ, dynamics, low cut, auto level, crossfeed, reverb, echo, control room, clock |
| `--diff` | what an apply would change, without changing it |
| `--dump-config` | the reported device state exported as config; `--include-written` adds the playback routes desk last wrote, marked unconfirmed |
| `--snapshot` | every register the device reports |
| `--status [--json]` | read-only diagnosis of installation, device, backend and mixer |
| profiles | named alternatives, switched in one operation with a stated outcome; `--capture NAME` saves the current desk as one, `--diff-profile A B` compares two without the device |
| `[pin]` | per-option choice whether the config or the mixer wins after the first write |
| `[desk]` | `base` makes a desk the whole matrix (TotalMix "Reset Mix", DAW mode); `enforce = watch` reconciles when a report contradicts a pinned value; `peak-safe = true` quiets a switch |
| `--pipewire-sinks` | named outputs ("Monitors", "Headphones") in the desktop's sound settings |
| service integration | systemd user service, udev hotplug and resume; OpenRC, runit, NixOS and Fedora Silverblue |
| mixer | the upstream GTK mixer can stay open; desk operations and mixer edits do not interleave |

## Requirements

- Linux with ALSA; a Fireface UCX II in class-compliant mode
- Python 3.9 or newer (standard library only)
  CI covers 3.9 through 3.14 and Python 3.15 RC2 for early compatibility.
  Release 0.9.0 waits until final stable Python 3.15 is available and
  passes qualification.
- To build the backend: `git`, `make`, a C compiler, `pkg-config`, the ALSA
  headers and, for the mixer GUI, GTK 3

```sh
sudo apt install build-essential git pkg-config libasound2-dev libgtk-3-dev libglib2.0-dev-bin  # Debian/Ubuntu
sudo dnf install gcc make git pkgconf-pkg-config alsa-lib-devel gtk3-devel                     # Fedora
sudo pacman -S --needed base-devel git alsa-lib gtk3                                            # Arch
```

## Install

From a [verified source release](docs/RELEASE-ARTIFACTS.md):

```sh
./install.sh --check
./install.sh
~/.local/bin/oscmix-session --dry-run --timeout 0
```

The first installation does not start anything. Review `~/.config/oscmix/routing.conf`,
then enable automatic operation with `./install.sh --no-build --enable`.
From 0.9.0, `./install.sh --disable` stops automatic operation and blocks
hotplug starts while keeping the installed desk.
Native packages, signed package repositories, OpenRC/runit, NixOS and
Silverblue are covered in the [installation guide](docs/INSTALLATION.md).
Read the [upgrade notes](docs/UPGRADING.md) before upgrading an existing desk.

The user running the desk must be in the `audio` group.

## Configure

Edit `~/.config/oscmix/routing.conf`:

```ini
[route:main-out]          # rear line outputs 1/2
playback = 1/2
output = 1/2

[route:monitors]          # speakers on rear outputs 5/6
playback = 1/2
output = 5/6
level = 0.0               # mix gain in dB (0 = unity, -65 = mute)

[input:3]
gain = 12.0
hi-z = true

[eq:input:3]
enabled = true
band1freq = 80
band1gain = -3.0
band1type = Low Shelf

[clock]
source = Internal
```

Apply with `systemctl --user restart oscmix.service`. Values are checked
against the device's bounds before anything is sent. The shipped
[example](config/routing.conf.example) documents every section.

A config is a **partial** desired state: omitting a route does not mute it.
The UCX II's front headphones are outputs 7/8.

The config is the first of: `--config FILE`, `$OSCMIX_CONFIG`,
`$XDG_CONFIG_HOME/oscmix/routing.conf` (or `~/.config/oscmix/routing.conf`),
`/etc/oscmix/routing.conf`. Profiles live in `profiles/` beside it. Other
options: `--device NAME` and `--timeout SECONDS` for the interface search,
`--dry-run` to print what would be sent, `--pipewire-target NODE` for
`--pipewire-sinks` when auto-detection picks the wrong node, and `--verbose`
for debug logging. `--osc-port` is still accepted from old unit files and
selects nothing.

In the 0.9.0 development tree, `--strict` refuses unknown or unsupported
sections and an unusable active profile instead of ignoring them or choosing
another desk. `oscmix-session --check` strictly validates the main config,
all profiles and the stored selection offline, with no device access.
See [the development upgrade notes](docs/UPGRADING.md#developing-090).

The 0.9.0 development tree also accepts `fx = -30.0` in `[input:N]`
(FX send) and `[output:N]` (FX return). The range is -65..0 dB; the
backend rounds to 0.1 dB. Omitted FX is left alone. Declared values are
written on initial application or explicit profile selection, then
remembered unless `[pin] input.fx = pin` or `output.fx = pin` overrides it.

Output sections also accept `pan = -25` for output balance, using integers
from -100 to 100. DEVICE feedback is verifiable. Balance is written on
initial application or explicit profile selection and remembered later;
`[pin] output.pan = pin` enables repair to the declared value. Linked
partners follow an output-pan change. This is separate from route pan.

Input sections accept `msproc = true` or `false` for M/S processing,
with verifiable DEVICE feedback. The starting value is remembered;
`[pin] input.msproc = pin` enables later repair. Linked partners follow
an M/S change; unlinked inputs 13/14 were measured independently.
These measurements confirm the register flags and restoration; they
do not qualify acoustic M/S decoding or cold-start completeness.

UCX II inputs 1–4 accept `autoset = true` or `false`. The flag is remembered
after its initial value. Later repair preserves gains owned by declared or
live AutoSet, including linked partners; missing dependency feedback also
withholds gain writes. Explicit `input.gain = pin` conflicts with declared
AutoSet-on for an affected gain and is refused before device access.
An initial or explicit gain change requires fresh AutoSet-off feedback.
Declared `autoset = false` is written and read back first; AutoSet-on is
written after gains. An already active, omitted AutoSet is never disabled
implicitly. Measurements cover warm linked flags and restoration, without
testing overload-driven gain reduction or acoustic protection.

### Who wins: pin and remember

Every setting is either **pinned** -- the config wins and is re-sent if the
device disagrees -- or **remembered** -- written at the start of a session,
after which the mixer wins. Routing, channel links, `reflevel`, `gain` and
`hi-z` are pinned; `volume`, `mute` and `phase` are remembered. Override
per option:

```ini
[pin]
output.volume = pin
input.gain = remember
```

Pinning does not snap a value back while you work: the device reports
changes only when asked, so the desk insists during its read-back after a
start, a reload or a resume. Remembered values are never reset by those
later checks. The playback mix matrix is re-established on each start but
cannot be read back from the device; `--status` says whether it is known
from a CC Mix preset, re-established by the desk, or unknown.
`[desk] enforce = watch` in routing.conf goes one step further and
reconciles as soon as a report that contradicts a pinned value arrives -- a
change at the front panel or a link change, which the device announces
itself, or any value in a state dump another client asks for.

## Profiles

Profiles are variants of `routing.conf` in `~/.config/oscmix/profiles/`:

```sh
oscmix-session --list-profiles
oscmix-session --profile tracking
oscmix-session --dry-run --profile tracking   # preview, writes nothing
oscmix-session --no-profile                   # back to routing.conf
```

A switch validates the whole profile before writing and reports one of:
applied and verified, applied with settings the backend cannot report,
refused (nothing written), or written in part (with the paths). The active
profile is remembered and applied again after a restart, replug or resume.
Leave `[osc]` and `[device]` out of profiles; they come from `routing.conf`.

## Named outputs (PipeWire)

```sh
mkdir -p ~/.config/pipewire/pipewire.conf.d
oscmix-session --pipewire-sinks > ~/.config/pipewire/pipewire.conf.d/oscmix-sinks.conf
systemctl --user restart pipewire wireplumber
```

One sink per stereo output pair, named after the first route that targets
it -- any route with a two-channel `output`, an input-monitoring route
included. The sink plays into the device's playback channels with that
pair's numbers, so it is heard only where a route sends them to the pair
(`playback = output`); the generated file names each pair that lacks one.

## Troubleshooting

```sh
systemctl --user status oscmix.service
journalctl --user -u oscmix.service -e
oscmix-session --status
```

See [troubleshooting](docs/TROUBLESHOOTING.md) and
[status and the mixer](docs/STATUS.md).

## Other Fireface models

Only the UCX II is supported. The pinned backend cannot operate a Fireface
802; its entry here only validates channel numbers.

## Documentation

- [Installation](docs/INSTALLATION.md), [upgrading](docs/UPGRADING.md),
  [package repositories](docs/PACKAGE-REPOSITORIES.md), [Silverblue](docs/SILVERBLUE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md), [status and the mixer](docs/STATUS.md),
  [security model](docs/SECURITY-MODEL.md), [verifying releases](docs/RELEASE-ARTIFACTS.md)
- [Architecture](docs/ARCHITECTURE.md), [OSC interface](docs/OSC-PROTOCOL.md),
  [backend control protocol](docs/BACKEND-CONTROL.md),
  [numeric values](docs/NUMERIC-CONTRACT.md), [register addresses](docs/register-addresses.md),
  [feature layers](docs/FEATURE-SURFACE.md)
- [Contributing](CONTRIBUTING.md) and the [changelog](CHANGELOG.md)

## Uninstall

```sh
./uninstall.sh          # keeps ~/.config/oscmix
./uninstall.sh --purge  # removes the config too
```

## Credits and license

The protocol work is done by [oscmix] (ISC license). oscmix-desk is MIT
licensed, see [LICENSE](LICENSE). The backend and mixer built from oscmix
include its `intpack.h`, which is under the Unlicense, so the packages
declare MIT, ISC and Unlicense.

[oscmix]: https://github.com/michaelforney/oscmix
