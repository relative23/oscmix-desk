# Installation and package recovery

These instructions are being updated for 0.8.0; qualification remains open
in the [release plan](plans/0.8.0-reliability-integration.md). Use the instructions
for the version you downloaded; 0.7.0/0.7.1 installers do not have the `--check`,
`--manual`, `--enable` or `oscmix-setup` interface.

The common runtime needs Python 3.9 or newer and the pinned `oscmix` C
backend. It has no third-party Python dependencies. `pip` alone does not
provide ALSA, udev, the shared device-lock directory or service integration,
so native packages and a source installer are the whole-product paths.

## Source installation

Download a versioned source archive, verify its SHA-256 manifest and GitHub
attestation, and unpack it into a directory owned by your audio user.
Build and install as that user, not with `sudo ./install.sh`.

```sh
./install.sh --check
./install.sh
~/.local/bin/oscmix-session --dry-run --timeout 0
```

Preflight reports missing commands, ALSA headers, writable destinations,
backend revision, installed version, permissions and available service
integration. An absent interface does not prevent offline installation.
Package-manager hints cover the tested distribution families; other systems
receive the required capabilities without guessed package names.

The installer prepares the exact upstream commit and hash-checked patches
from `patches/backend-series.json` in a fresh build directory. An existing
`build/oscmix` checkout supplies Git objects; its working files are preserved.
Backend, ALSA bridge and optional GTK companion identify the same exact patch
series before installation. `--no-build` accepts only that series, including
any installed GTK companion. An unmodified upstream or 0.7.3 executable is
incompatible. Build all three together when upgrading; install GTK headers
if an existing companion also needs replacement. Changing `OSCMIX_REF` alone
cannot bypass this contract.

The source record is installed at
`$XDG_DATA_HOME/oscmix-desk/backend-source.json` (normally under
`~/.local/share`), or `/usr/share/oscmix-desk/backend-source.json` for native
packages. Each binary's `--desk-build-id` prints the series digest without
opening ALSA, a device or a display. This identity check is not a hardware
measurement or a signature on the executable.

Review `~/.config/oscmix/routing.conf`, or the corresponding absolute
`XDG_CONFIG_HOME` path. Choose your device and routes deliberately. The first
installation copies files without starting a mixer, triggering hotplug or
installing automatic resume activation. To opt into automatic operation:

```sh
./install.sh --no-build --enable
```

This can write to a connected interface. The systemd user manager must use
the same `HOME` and effective `XDG_CONFIG_HOME`. Do not redirect those
variables to another desk while operating its service. An upgrade preserves
the previous active/enabled state: an active service is stopped before
replacement and restarted afterward; a stopped service remains stopped.

The existing upstream GTK companion is optional. Without GTK development
libraries, the installer builds the headless backend and creates no new
desktop shortcut. This does not implement the deferred oscmix-desk GUI.

## Foreground operation without systemd

```sh
./install.sh --check --manual
./install.sh --manual
~/.local/bin/oscmix-session --dry-run --timeout 0
~/.local/bin/oscmix-session
```

A missing user manager selects manual mode automatically. There is no
automatic startup, hotplug restart or resume reconciliation in this mode.
An existing active/enabled service must be stopped and disabled before
switching to manual operation. Close manual sessions before replacing or
removing their installed code.

Live use requires access to `/dev/snd/seq` and the interface. For a shared
writer lock, an administrator must create the `audio` group if absent, add
the intended operator to it and arrange this directory at **every boot**:

```sh
sudo install -d -o root -g audio -m 3770 /run/oscmix-desk
```

Start a new login session after changing group membership. On systemd
systems the supplied tmpfiles rule creates the directory. On other init
systems, use the distribution's boot-time directory mechanism. Without this
directory, the per-user fallback does not coordinate different users.
`--no-udev` skips all root integration, including provisioning this lock.

The source path is exercised on Debian 13, Ubuntu 24.04/26.04, Fedora 44,
openSUSE Leap 16, Arch and Alpine 3.22/musl. Container tests cover building,
installation, the CLI and simulated lifecycle. They do not qualify every
desktop, init adapter, CPU architecture or actual hardware configuration.
The additional 0.8.0 OpenRC/runit adapters are described below; their final VM
qualification remains separate from these historical container results.

## Alpine/OpenRC and Void/runit (0.8.0 development)

Install the ordinary source payload as the intended audio user. The host kernel
must provide ALSA sequencer support (`/dev/snd/seq`); a cloud kernel without the
sound modules cannot operate the interface. Add the user to `audio` and start a
new login session before using it. Build prerequisites are reported by
`./install.sh --check --manual`; GTK additionally needs `gtk+3.0-dev` on Alpine
or `gtk+3-devel glib-devel` on Void.

```sh
./install.sh --manual --no-udev
~/.local/bin/oscmix-session --dry-run --timeout 0
sudo python3 scripts/install-service.py install --manager openrc --user YOUR_USER
```

On Void select `--manager runit` instead. The administrator registration takes
the user's home from the account database; `--config /absolute/file.conf` selects
an alternate desk. One registered service is supported per host. Registration
does not enable or start the mixer. Inspect and explicitly activate it with:

```sh
oscmix-service status
sudo oscmix-service enable
~/.local/bin/oscmix-session --status --json
```

The native manager drops to the selected user's UID/groups before loading any
user-installed code. It creates the shared `root:audio` lock directory with mode
`3770` at startup. It retries discovery after a missing/disconnected interface
or a crashed backend with at least five seconds between process starts. A
running desk process does not certify connected or verified hardware.
`start`, `stop`, `reload` and `disable` are explicit administrator operations.
The GTK launcher reuses a matching running session; it cannot enable a host
service implicitly. A profile switch signals only the identified desk process.

OpenRC sends output to the host syslog with tag `oscmix-desk`. Runit's `svlogd`
rotates files in `/var/log/oscmix-desk`. If the host already supplies
`/etc/zzz.d/resume` or `/etc/elogind/system-sleep`, registration adds the matching
resume hook. It requests selective reconciliation only for an enabled, running
desk outside maintenance. Install the host's sleep integration first, or update
the adapters under maintenance after adding it. An absent hook directory is
not a claim of automatic resume support.

For an update or software rollback, keep the service fenced while replacing
both the user payload and root-owned adapters from the selected source tree:

```sh
sudo oscmix-service maintenance-begin
./install.sh --manual --no-udev
sudo python3 scripts/install-service.py update
~/.local/bin/oscmix-session --dry-run --timeout 0
sudo oscmix-service maintenance-finish
```

An interrupted update leaves a persistent fence across reboot; repeat the
failed installation/update step before finishing maintenance. The installer
refuses replacement of a registered home without a confirmed stop. Finish
checks configuration and the exact core/bridge/GTK build pairing as the audio
user, then restores the previous active/enabled state. Configuration, profiles
and the active marker retain their user ownership and contents. Software
rollback does not roll hardware state back.

To remove host integration, run `sudo oscmix-service disable` followed by
`sudo oscmix-service remove`, then run `./uninstall.sh` as the audio user if the
payload should also be removed. The shared lock directory and user desk are
preserved. These adapters use UID/group isolation; they do not provide the
systemd unit's sandbox.

`tests/native_service_lifecycle.py` is the reproducible absent-device lifecycle
check for the disposable qualification VMs. Its two phases surround a real
reboot with maintenance left open. It refuses a non-qualification host, another
runtime account or a connected RME USB device. This covers native supervision
and state retention, not physical-device routing or the remaining VM fault cases.

## NixOS (0.8.0 development)

The [Nix package and module](../packaging/nix/README.md) build the common payload
in one immutable store output, with or without the matching upstream GTK mixer.
`services.oscmix-desk.enable` installs integration for a selected normal user;
the separate `activate` setting defaults to false. Configuration, profiles and
the marker remain writable user files. The module uses the existing systemd
user service, shared lock rules and persistent package-maintenance fence.

Follow that guide for generation switches, reboot, rollback and removal.
`tests/nixos_lifecycle.py` checks these operations in two phases around an
actual VM reboot. Development VM checks do not replace the final 0.8.0 software,
device-I/O and release qualification.

## Fedora Silverblue (0.8.0 development)

Use the [host RPM-layering procedure](SILVERBLUE.md) with the matching Fedora
core/GTK pair. It keeps the common systemd user unit, explicit setup opt-in and
user-owned configuration. The live host's maintenance fence must be managed
explicitly across deployment, reboot and rollback; RPM hooks in the deployment
sandbox cannot own it. The guide includes audio-group provisioning and removal.

## Native packages

Use a package built for your distribution release and architecture. The
published 0.7.3 targets are Ubuntu 24.04 DEB, Fedora 44 RPM,
openSUSE Leap 16 RPM and Arch on x86_64. A Fedora RPM is not an openSUSE
binary, and a new Ubuntu binary is not implicitly compatible with an older
Ubuntu libc. A published artifact must carry its checksum and authenticated
build provenance. The 0.8.0 development matrix adds Debian 13 and Ubuntu 26.04
DEBs; all six targets now pass their
[development package checks](evidence/0.8.0/native-development.json).
Final-candidate checks and signed APT/RPM qualification remain required before
publication. The product has no embedded updater.

Use the native package manager (`apt install ./...deb`, `dnf install
./...rpm`, `zypper install ./...rpm`, or `pacman -U ./...pkg.tar.zst`). The
package installs files under `/usr`; it does not create your active desk,
enable a service or trigger the device. As the intended audio user:

```sh
oscmix-setup --check
oscmix-setup --init-config
oscmix-session --dry-run --timeout 0
```

`--init-config` refuses to overwrite an existing config. Review the desk,
device permissions and group membership before `oscmix-setup --enable`.
That command creates a per-user opt-in and enables/starts the vendor unit.
`oscmix-setup --disable` removes the opt-in, stops and disables the unit,
and preserves the desk. The opt-in also prevents udev from starting a
disabled-but-never-approved installation.

The package includes its exact backend revision. Installed provenance is
readable at `/usr/share/oscmix-desk/package.json`; the artifact manifest
also hashes the actual packaged files after distribution build processing.

The optional `oscmix-desk-gtk` package supplies the existing upstream GTK
mixer, GSettings schema, icon and desktop entry. It depends on the exact
core package version/revision and owns none of the core files. Install the
two matching files with your distribution's package manager; GTK is not a
dependency of the headless core. Removing the companion leaves the backend,
configuration and activation state in place. Schema caches are refreshed
on companion installation and removal. Nothing is enabled or started.

Use `oscmix-session --status` to inspect installed resources and connection
settings, then `oscmix-launch` to open the mixer against the reviewed desk.
The [mixer workflow](STATUS.md) explains manual sessions and read-back.

## Migrating an existing source installation

Before installing a native package, stop and disable the old service and
close manual sessions and the upstream mixer. Keep the interface disconnected
through the migration if an old hotplug rule can start the source service.
Installing the new files alone does not remove `.local/bin` programs or
per-user units that override the package.

```sh
systemctl --user disable --now oscmix.service
# Install the selected native package with your package manager.
oscmix-setup --check
oscmix-setup --migrate-source
hash -r
oscmix-session --version
```

Migration prints its backup directory before moving files. Its manifest
records every intended move, so a migration interrupted between moves is
recoverable. Runtime, entry points, old unit and project desktop resources
are preserved there. Config, profiles and active-profile remain in place.
Custom service drop-ins and `/etc/udev` overrides remain visible for review.
Only known project paths are moved. Cross-filesystem renames are refused;
choose a suitable backup location through `XDG_STATE_HOME` or perform an
explicitly reviewed manual migration.

To return to that source installation, first disable native automatic
operation, then use the exact printed backup path:

```sh
oscmix-setup --disable
oscmix-setup --restore-source /absolute/path/from-the-migration
hash -r
~/.local/bin/oscmix-session --version
```

Restore checks all destination conflicts before moving anything and never
overwrites a new user file. It does not enable the restored unit. Review it
before activation. The source installer refuses to create another shadowing
installation while a native package is present; remove the package first
when deliberately returning to source-only ownership.

## Package upgrades, interrupted transactions and removal

Stop all mixer services and manual/companion processes before changing a
package. Package hooks refuse replacement or removal while a mixer is
running. Do not start manual sessions during the package transaction.
This is coordinated maintenance, not exclusion of arbitrary local writers.

The root-owned `/var/lib/oscmix-desk/package-update` (core) and
`/var/lib/oscmix-desk/gtk-package-update` (companion) markers prevent vendor
unit startup while files are being changed. They persist across a reboot.
A successful package configuration/removal clears its own marker; an interrupted
transaction leaves automatic operation blocked until the package is repaired.
Repairing GTK never clears an incomplete core transaction, or vice versa.
Finish or reinstall the selected package using the package manager before
enabling the service. Do not remove the marker merely to bypass a failure.

DEB/RPM/Arch lifecycle qualification includes first installation, refusal
with a running mixer, upgrade, recovery with a retained marker, downgrade,
removal and preservation of desk/profile/active-marker files. Actual
0.7.0 source-to-native-and-back file migration passes on every native
target using the installed setup CLI and a simulated user bus. The vendor
unit and the migration are additionally tested in an Ubuntu VM with a real
user manager and a simulated backend; its persistent maintenance fence is
also checked across a VM reboot.
File rollback cannot undo emitted audio or establish hardware restoration.

## Building a native artifact

Run the build on the target distribution with its packaging tools installed,
as an ordinary build user. Both project and backend inputs are Git revisions;
release builds require a clean project checkout. `--development` produces
explicit qualification artifacts from an unfinished tree.

```sh
python3 scripts/build-package.py --format deb \
  --backend-source build/oscmix --output build/packages
```

Use `--format rpm` on the selected RPM distribution and `--format arch` on
Arch. For reproducible Arch `.BUILDINFO`, choose the same unused absolute
`--build-dir` in each clean build environment. The builder creates and removes
only that new scratch directory; an existing directory is refused. It records
the truthful build location instead of rewriting package provenance afterward.
`--package-revision` is the native packaging revision, separate from the
application version. The common staging script never enables host services.
Add `--with-gtk` to build both packages from the same pin and build metadata.
It produces a separate companion, not an alternative core package with
overlapping files. The installed companion manifest is
`/usr/share/oscmix-desk/gtk-package.json`.

## Repeating distribution qualification

From a clean commit on a Linux host with Docker:

```sh
python3 scripts/qualify-distribution.py --target alpine322 --output build/alpine-source
python3 scripts/qualify-distribution.py --target ubuntu2404 --native \
  --development --output build/ubuntu-packages
```

Each run creates a new output directory with its log, resolved image identity,
source/backend revisions and result. The Docker context contains a Git bundle,
not the host home or local Git configuration. Containers have no sound devices,
host service bus or runtime network access. Native checks build twice, verify
identical artifacts and exercise the actual package manager's transitions.
Native checks also launch the installed GTK, launcher and desk CLI against
the installed C backend with anonymous simulated MIDI pipes and a private
display/bus. They check shared observations, exclusion over the whole desk
operation, disconnect and both startup orders, then exercise actual
previous-version upgrade/rollback. Every native target runs this test on Xvfb;
Ubuntu 24.04 additionally retains nested GNOME/KDE Wayland and Xfce/X11 sessions.
For 0.7.3, nested GNOME and KDE Wayland sessions and Xfce/X11 also pass
the receive-port contention and close/read-back/reopen checks;
[desktop evidence](evidence/0.7.3/desktop-integration.json) records the actual
compositor versions, software rendering and fresh received labels.
`--previous-tag` selects the transition baseline (now 0.7.3 for 0.8.0 development).
They do not replace VM or hardware qualification.

The [distribution workflow](../.github/workflows/distributions.yml) runs this
same path for seven source targets and six native targets. Release runs
require the tag to match the source version, generate GitHub attestations and
attach only qualified artifacts. Development runs label their packages and
do not publish them as releases. Verify downloaded provenance with
`gh attestation verify FILE --repo relative23/oscmix-desk`, as described in
[GitHub's attestation guide](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations).
