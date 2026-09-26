# Fedora Silverblue host installation

Development for 0.8.0. The final release and repository qualification are still
open. These instructions use host RPM layering, with the ordinary systemd user
service and matching upstream GTK companion. Installation and maintenance take
place on the Silverblue host. Configuration and profiles remain in the user's
writable home directory.

## Prepare the user and package pair

Use the Fedora RPMs built for the host's release and architecture. Authenticate
their release provenance and package signatures as described in the release
instructions. Keep the core and GTK package versions identical; the companion
has an exact core dependency. GTK is optional, and can be omitted from each
command below for a headless installation.

The selected normal user needs membership in `audio`. On Silverblue that group
may initially exist only in `/usr/lib/group`. Make its existing definition
available to the local account tools, then add the intended user:

```sh
sudo sh -eu -c 'getent -s files group audio >/dev/null || getent group audio >> /etc/group'
sudo usermod -aG audio "$USER"
```

Start a new login session before activation and check `id -nG`. Do not create a
different numeric audio group. The package's tmpfiles rule creates the shared
lock directory as `root:audio`, mode `3770`, on boot. SELinux remains enforcing.

For a fresh host installation:

```sh
sudo rpm-ostree install ./oscmix-desk-VERSION.x86_64_fedora44.rpm \
  ./oscmix-desk-gtk-VERSION.x86_64_fedora44.rpm
rpm-ostree status
systemctl reboot
```

Use the actual downloaded filenames in place of `VERSION`. The command prepares
a deployment; booting that deployment makes its payload available. Existing
per-user source installations must be stopped and explicitly migrated before
activation, following [the common setup guide](INSTALLATION.md#native-packages).

As the intended audio user after reboot:

```sh
oscmix-setup --check
oscmix-setup --init-config     # only if no configuration exists
oscmix-session --dry-run
```

Review the selected device and every route before `oscmix-setup --enable`.
Merely installing or rebooting does not opt the user into hardware control.
`oscmix-session --status --json` inspects the installation without contacting
the device. A start with no interface waits for discovery and exits cleanly;
it does not imply that a desk was applied. `oscmix-setup --disable` removes the
opt-in and stops/disables the service while keeping user state.

## Upgrade, interruption and rollback

RPM hooks run inside rpm-ostree's deployment sandbox. They cannot inspect the
running host's mixer processes or manage its persistent `/var` state. The
package guard therefore defers those actions only for its explicit RPM-hook
invocation when rpm-ostree supplies both `/run/ostree-booted` and
`SYSTEMD_OFFLINE=1`. Ordinary guard commands on the booted host retain their
checks. See [ADR 0034](decisions/0034-rpm-ostree-maintenance.md).

Before any deployment change affecting desk, close GTK and manual sessions,
stop the user service, and set the host's maintenance fence:

```sh
systemctl --user stop oscmix.service
sudo /usr/lib/oscmix-desk/package-guard install
```

The guard refuses running mixer processes. The fence blocks manual desk writes
and automatic service startup, and persists through deployment and reboot.
Keep it set until the intended booted payload has been checked. Package
composition does not clear it or automatically resume the service.

To replace a pair installed from local files in one transaction:

```sh
sudo rpm-ostree install --uninstall=oscmix-desk --uninstall=oscmix-desk-gtk \
  ./oscmix-desk-NEW_VERSION.x86_64_fedora44.rpm \
  ./oscmix-desk-gtk-NEW_VERSION.x86_64_fedora44.rpm
rpm-ostree status
systemctl reboot
```

After boot, check `rpm -q oscmix-desk oscmix-desk-gtk`, read-only status and
`oscmix-session --dry-run`. Then explicitly finish maintenance and, if desired,
start the already enabled service:

```sh
sudo /usr/lib/oscmix-desk/package-guard finish
systemctl --user start oscmix.service
```

A failed download or composition leaves the booted payload intact. A pending
deployment takes effect on the next boot. Inspect `rpm-ostree status` after an
interruption; complete the intended deployment or discard a known unwanted
pending deployment with `rpm-ostree cleanup --pending`. Retain the fence until
the resulting installed code and configuration have been checked.

For an actual deployment rollback, follow the same stop/fence procedure, run
`sudo rpm-ostree rollback`, and reboot. Check the booted version before finishing
maintenance. Rollback restores code and system integration; user profiles,
configuration, GTK preferences and later hardware changes are not rolled back.
Keep the source of the intended package pair for later updates.

Use deployment/reboot transitions for this installation path. Live replacement
and an unlocked `/usr` are outside the qualified workflow.

## Removal

Disable control with `oscmix-setup --disable`, close all mixer processes, and set
the fence. Save the reviewed guard before removing its package, so maintenance
can be completed after the next boot:

```sh
sudo /usr/lib/oscmix-desk/package-guard install
sudo install -D -m 700 /usr/lib/oscmix-desk/package-guard \
  /var/lib/oscmix-desk/removal-guard
sudo rpm-ostree uninstall oscmix-desk-gtk oscmix-desk
systemctl reboot
```

After confirming removal and preserved user files:

```sh
sudo /var/lib/oscmix-desk/removal-guard check
sudo /var/lib/oscmix-desk/removal-guard finish
```

The service remains disabled. Keep routing files and profiles through package
removal. The administrator controls rpm-ostree and
can bypass this procedure; the maintenance workflow does not constrain root.

The [rpm-ostree administration handbook](https://coreos.github.io/rpm-ostree/administrator-handbook/)
describes the underlying deployment, layering and rollback commands. The package
adapter uses the [2026.1 script sandbox contract](https://github.com/coreos/rpm-ostree/blob/v2026.1/src/libpriv/rpmostree-scripts.cxx).

The [development evidence](evidence/0.8.0/silverblue-development.json) records the
tested image, package payloads, SELinux state and lifecycle results.
`tests/silverblue_lifecycle.py` reproduces the five phases around four actual
VM reboots, including full installed-file checks against the package manifests.
Its docstring gives the isolated VM setup and invocation. Device I/O, physical
hotplug, suspend/resume, desktop GTK, signed repositories and the final 0.8.0
payload require their remaining qualification.
