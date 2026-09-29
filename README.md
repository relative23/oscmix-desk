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

The latest release is **0.8.0**.

![oscmix-gtk showing the Fireface UCX II hardware mixer](docs/img/oscmix-gtk.png)

## Features

| | |
|---|---|
| `routing.conf` | routes, faders, per-channel state, EQ, room EQ, dynamics, low cut, auto level, crossfeed, reverb, echo, control room, clock |
| `--diff` | what an apply would change, without changing it |
| `--dump-config` | the reported device state exported as config |
| `--snapshot` | every register the device reports |
| `--status [--json]` | read-only diagnosis of installation, device, backend and mixer |
| profiles | named alternatives, switched in one operation with a stated outcome |
| `[pin]` | per-option choice whether the config or the mixer wins after the first write |
| `--pipewire-sinks` | named outputs ("Monitors", "Headphones") in the desktop's sound settings |
| service integration | systemd user service, udev hotplug and resume; OpenRC, runit, NixOS and Fedora Silverblue |
| mixer | the upstream GTK mixer can stay open; desk operations and mixer edits do not interleave |

## Requirements

- Linux with ALSA; a Fireface UCX II in class-compliant mode
- Python 3.9 or newer (standard library only)
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

Installing does not start anything. Review `~/.config/oscmix/routing.conf`,
then enable automatic operation with `./install.sh --no-build --enable`.
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
cannot be read back from the device.

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

Each stereo route with `playback = output` becomes a sink named after it.

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
licensed, see [LICENSE](LICENSE).

[oscmix]: https://github.com/michaelforney/oscmix
