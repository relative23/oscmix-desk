# Upgrade and recovery

## Preparing for 0.7.2 (unreleased)

The config format and backend pin are unchanged. Installation now separates
copying files from opting into automatic hardware operation, adds preflight
and manual operation, and includes native-package migration/recovery tools.
A source upgrade preserves the existing service's active/enabled state;
a fresh install waits for explicit activation. Native installation does
not remove a previous per-user installation: follow the
[migration and recovery procedure](INSTALLATION.md#migrating-an-existing-source-installation)
before enabling the package's service. These changes are not yet published;
use the instructions shipped with the version you downloaded.

An apply with playback routes now inspects the active UCX II hardware PCM.
It rejects unavailable playback channels before sending the first write:
8/14/16-channel streams have that many playback channels; at 88.2/96 kHz
the 20-channel stream carries only playback 1--16. At 176.4/192 kHz,
16/20-channel streams are refused because the recorded transfers failed.
Select a measured hardware stream in the audio application or audio-server
configuration; the desk does not change it automatically.

A stopped or absent PCM remains usable for boot/offline routing, with an
explicit warning that its live mode is unvalidated. An unreadable or
inconsistent identified UCX II stream is refused. `--dry-run` checks the
config and register plan, not a future active stream. `--diff` remains a
register comparison, not an audio-transfer or physical-port qualification.

The mode is rechecked before each write phase. If it changes mid-apply,
the result names the writes already sent and leaves the profile marker
unchanged. Let the hardware stream settle and apply again. There is no
automatic rollback or continuous rate enforcement: the device lock cannot
stop a DAW or the interface changing clocks between checks or afterwards.
Input-only routes do not make USB playback-capacity claims. Physical
digital I/O, external clocks and physical delay remain separately unmeasured.

## Upgrading to 0.7.1

The source installer requires the systemd user manager's `HOME` and
effective `XDG_CONFIG_HOME` to match the invocation before changing files
or host integration. Run it from the intended user's login session. The
uninstaller also refuses a different config base under the same home:
both configurations share `~/.local/lib/oscmix-desk`, so deleting it could
remove the running desk's code. A missing manager identity is no longer
permission to enable or restart its service.

0.7.1 has stricter numeric validation. It rejects
nonfinite values, fractional integer settings and wire/backend overflow
that older versions accepted. Correct these values before applying a
desk; run `--dry-run` on each config/profile first.

Unlinked stereo pairs (`stereo = false`) reject `level > 0`, which was
previously silently clamped to unity. Use a level in -65..0 dB. The mute
value -65 now bypasses compensation and writes digital zero on both sides.

Mono routes now explicitly unlink their source and output pairs. Previously
their effect depended on existing stereo flags and could include a
neighbouring channel. A mono source cannot share its pair with a stereo
source route. A mono output can share an output pair only with other routes
that also require it unlinked. Conflicting declarations are now refused;
choose consistent mono/pair routes before applying the config.

Config exports now preserve sub-tenth quantities and identify omitted
state. A partial or unusually panned matrix may produce fewer routes
with explicit warnings. Merge into your original config; an omitted
route is not a mute, and the unreadable playback matrix still needs its
original declarations. Un-commenting a remembered value declares an
initial setting, not a policy override.

Room EQ delay retains its OSC values, but the old seconds label was not
established by physical measurement. Do not infer physical duration from
it; see [numeric contracts and evidence limits](NUMERIC-CONTRACT.md).
Retain old evidence as historical data. Older snapshots rounded to one
decimal place and cannot establish that smaller values were unchanged.

The actual 0.6.11 and 0.7.0 → 0.7.1 → original-version installation
transitions pass in isolated rootless and redirected system-file layouts.
They check installed entry points/module inventory and preservation of
config, profiles and the marker; see [qualification](evidence/0.7.1/).
These software checks do not establish a hardware rollback.

## The 0.7.0 transition

0.7.0 changes config validation and the Python API. The runtime still uses
only the standard library and is installed by `install.sh`.

## Before upgrading

Keep the previous release checkout or archive. Close the mixer GUI and
stop playback before stopping the service. Save a hardware snapshot while
the backend is running:

```sh
oscmix-session --snapshot > before-upgrade.txt
oscmix-session --dump-config > before-upgrade.conf
systemctl --user stop oscmix.service
```

These files contain mixer state; keep them private. Neither captures a
verifiable playback matrix. The original config and profiles remain the
source of truth for those routes.

Back up the complete installed package, all five executables under
`~/.local/bin` (`oscmix-session`, `oscmix-launch`, `oscmix`, `alsaseqio`,
`oscmix-gtk` when present), and the whole config directory, including
`profiles/` and `active-profile`. Also save:

- `~/.config/systemd/user/oscmix.service` and any drop-ins;
- your generated PipeWire config, normally
  `~/.config/pipewire/pipewire.conf.d/oscmix-sinks.conf`;
- the `oscmix-gtk.desktop` entry, `oscmix.svg` icon and oscmix GSettings
  schema under `~/.local/share`;
- `/etc/udev/rules.d/90-rme-fireface.rules`,
  `/usr/lib/systemd/system-sleep/oscmix` and
  `/usr/lib/tmpfiles.d/oscmix-desk.conf` if installed.

Use your actual `XDG_CONFIG_HOME` and `XDG_DATA_HOME` where configured.
The package lives at `~/.local/lib/oscmix-desk`. Backups made by individual
`install.sh` copies do not replace this backup: the package directory is
replaced as a unit, and system integration has separate files.

## Compatibility changes

| Previous use | 0.7.0 action |
| --- | --- |
| A profile names a different device, serial, USB ID or OSC port from `routing.conf` | Remove `[device]` and `[osc]` from the profile, or give the other machine its own config directory and `--config`. Repeating the main config's values remains valid. |
| `--device` overrides a config validated for another interface | The file is now validated for the effective device. Correct any out-of-range routes before applying it. |
| Python code assigns into `Config` or mutates its lists/policies | Use `dataclasses.replace`, tuples of settings and a new policy mapping. Parsed configs are immutable. |
| Python code imports OSC helpers or discovery internals from `oscmix_desk` | Import from the owning module, for example `oscmix_desk.osc`. Only the 38 names in `__all__` are the supported root API. |
| Code depends on plain phase/reason strings | Use `reconcile.Phase` and `reconcile.WriteReason`; use the typed barrier result in `routing.LinkEcho`. These modules are implementation APIs. |
| A switch fails after some sends | Handle `written-in-part` / CLI exit 1, with `written` and `unwritten`; a refusal means no sends. Neither means hardware rollback occurred. |

An invalid remembered profile falls back to `routing.conf` and warns; the
marker is retained so the warning cannot silently disappear. Validate
each profile with `--profile NAME --dry-run` before its first switch.
`--dry-run` sends no writes and also prints the plan without hardware.

## Install and check

Use a released tag or the [verified archive](RELEASE-ARTIFACTS.md), not a
moving `main`. From the selected release directory:

```sh
./install.sh
oscmix-session --version
oscmix-session --dry-run
systemctl --user status oscmix.service
oscmix-session --diff
```

`--no-build` retains installed backend binaries; use it only when their
revision agrees with the selected release's pin. `--no-udev` skips system
files and is appropriate when they already match or when choosing a
rootless installation. It does not remove existing system integration.

The installer prepares a complete package before stopping an active
service and replaces it while the service is stopped. It holds an install
lock for the build and installation. Existing `routing.conf`, profiles
and the active marker are preserved. `--diff` can compare reported state;
it explicitly lists playback writes it cannot check. Readiness after an
apply is followed by asynchronous verification, so inspect the journal
as well as `systemctl`'s active state.

## Interrupted install and rollback

After a failed install, keep the service stopped and rerun `install.sh`
from the desired release. A failed staging copy leaves the old runtime
intact. If killed between the package renames, the next invocation
recovers `oscmix_desk.previous` before preparing the new package. Failures
later in installation can leave files from two versions; rerunning the
installer completes them. This is recovery, not an atomic transaction
across the package, binaries, systemd and system files.

To return to 0.7.0 or 0.6.11, stop the service and run **that release's
installer**. It removes stale modules when replacing the package. Restore
backed-up config/profiles/marker if you changed them, and restore any
custom unit drop-ins or generated PipeWire files you changed. Reload
systemd, start the service and check version, journal and `--diff` again.
If system integration changed, reinstall those files from the selected
release too; 0.6.11, 0.7.0 and 0.7.1 use the same backend pin.

The automated transition tests install the actual 0.6.11 and 0.7.0 commits,
upgrade to this tree and reinstall the respective original in isolated homes. Both the rootless
layout and redirected system-file layout are exercised, including module
inventory, installed entry points, custom config, profiles and marker.
Software rollback restores files and a version. It cannot undo audio
already emitted or promise that every hardware value was restored.
