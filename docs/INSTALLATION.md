# Installation and package recovery

These instructions describe 0.7.2. Use the instructions for the version
you downloaded; 0.7.0/0.7.1 installers do not have the `--check`,
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
OpenRC/runit automation and immutable-system recipes remain separate adapters.

## Native packages

Use a package built for your distribution release and architecture. The
qualified targets are Ubuntu 24.04 DEB, Fedora 44 RPM,
openSUSE Leap 16 RPM and Arch on x86_64. A Fedora RPM is not an openSUSE
binary, and a new Ubuntu binary is not implicitly compatible with an older
Ubuntu libc. A published artifact must carry its checksum and authenticated
build provenance; no package repository or embedded updater is introduced.

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
Native packages currently qualify the headless path. Optional GTK package
builds need separate desktop qualification before publication.

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

The root-owned `/var/lib/oscmix-desk/package-update` marker prevents vendor
unit startup while files are being changed. It persists across a reboot.
A successful package configuration/removal clears it; an interrupted
transaction leaves automatic operation blocked until the package is repaired.
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
They do not replace VM or hardware qualification.

The [distribution workflow](../.github/workflows/distributions.yml) runs this
same path for the seven source targets and four native targets. Release runs
require the tag to match the source version, generate GitHub attestations and
attach only qualified artifacts. Development runs label their packages and
do not publish them as releases. Verify downloaded provenance with
`gh attestation verify FILE --repo relative23/oscmix-desk`, as described in
[GitHub's attestation guide](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations).
