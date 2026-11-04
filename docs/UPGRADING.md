# Upgrade and recovery

## Developing 0.9.0

The following changes are implemented in the development tree; 0.9.0 is
not released yet.

UCX II inputs 1–4 support `autoset = true` or `false`. AutoSet flags default
to REMEMBER. Later repair/reconcile preserves affected gains when AutoSet
is declared on, observed on, or its last feedback is missing or malformed.
A fresh mono link observation limits partner ownership; a planned link
joins the pair again. Gain PIN behavior on inputs 5–8 is unchanged.
An explicit `input.gain = pin` together with declared AutoSet-on for a
configured affected gain is refused before a connection or write lease.

Initial application and explicit profile selection require fresh AutoSet-off
feedback before changing an affected gain. An explicitly declared off flag
is written and confirmed before dependent writes; enabling flags follow gains.
Already active AutoSet is never disabled implicitly. A refusal after an off
write or link change reports those submitted paths as partial writes; it is
not permission to repeat the operation. Gain mismatches remain in read-back
results even when ownership preserves them. The hardware evidence covers
warm linked Boolean flags and restoration, not overload-driven gain reduction,
cold completeness or acoustic protection.

Python callers now open one coordinated operation:

```python
from oscmix_desk import ApplyIntent, load_config, open_desk

with open_desk(load_config(path), path) as desk:
    desk.apply(ApplyIntent.EXPLICIT)
    result = desk.verify()
```

Entering checks the selected interface and backend while holding the device
lock. The first apply, verify or repair takes one write lease; successful
exit acknowledges its end. Every exit closes the connection and releases
the lock. Failed operations cannot be reused or replayed through the same
object. `Desk.repair()` retains REMEMBER values and permits at most one PIN
repair; `Desk.verify()` compares every declared expectation. `config` and
`config_path` are read-only. `open_desk()` discovers an omitted config path
once, when the operation is prepared. It does not update the profile marker
or reload the installed service; named profile selection uses
`switch_profile()` or `restore_main()`.

`Desk`, `ApplyIntent` and `VerifyResult` are exported. The lower-level
`apply_routing`, `verify_routing`, `verify_and_repair` and CLI-only
`run_session` leave the root API; they remain importable from their modules.
`Policy` is exported for `Config.policies` (`Policy.PIN` and `Policy.REMEMBER`).
`Control` is exported for the existing optional `backend` argument of
`switch_profile` and `restore_main`; callers can annotate those values without
importing an undeclared type. Ordinary profile calls manage their own checked
connection. An explicitly supplied connection must belong to that desk;
activation takes ownership and closes it. If parsing or maintenance refuses
before activation begins, the supplied connection remains the caller's.
Low-level connection construction and protocol options stay documented in
their owning modules.
The documented `EXIT_DIFFERS`, `EXIT_NOT_PERSISTED` and `EXIT_RELOAD_FAILED`
constants are now exported beside the existing exit codes.

`Overrides` names the config overrides, including the validation device:
`load_config(path, overrides=Overrides(device_name="Fireface 802"))`.
`load_profile` and `effective_config` accept the same argument. The third
positional config/profile argument retains its meaning. `CommandLine` is
an alias for the same type, and `said=` remains a keyword alias. Passing
both non-None inputs raises `TypeError` before file lookup or reads, even
when they contain equal values. File settings stay in `Config.loaded` and
the invocation stays in `Config.overrides`.
`Machine.under`, `status.collect_status` and `status.print_status` accept
the same preferred and legacy names; status JSON keeps its existing schema.

`BackendControlError` names failed coordinated backend operations,
including connection loss, invalid observations, cancellation and missing
acknowledgements. `ReceivePortError` is the same exception class under its
legacy name; existing handlers still catch it. The errno and submitted/
pending write facts retain their meaning. A control error does not imply
that no hardware writes happened or that an automatic retry is safe.

Python callers: `Outcome.state` must be an `OutcomeState` member, exported
from `oscmix_desk`. The existing `APPLIED_VERIFIED`, `APPLIED_UNVERIFIED`,
`REFUSED` and `WRITTEN_IN_PART` constants are those members; comparisons
with the existing string values still work. Its `unverified`, `unverifiable`,
`written` and `unwritten` fields are tuples. Incoming lists are copied, so
mutating one cannot change a returned result. Construction and
`dataclasses.replace` raise `TypeError` for wrong types or `ValueError`
for contradictory facts. When constructing results, read-back, persistence
and durability now default to false. A verified result requires
`read_back=True`; a partial result includes every submitted and pending
path in `unverified`. Results returned by profile operations state the facts
explicitly.

Completed profile read-backs retain separate `mismatched`, `unobserved` and
`invalid` tuples. `invalid` is a subset of `mismatched`; `unverified` is the
union of mismatched and unobserved paths, and `unverifiable` is the subset
of unobserved paths the backend cannot report. No completed read-back means
these classifications are empty and `unverified` contains the unknown paths.
Manual construction must supply these consistent facts too.

`APPLIED_MISMATCHED` is a fifth `OutcomeState`, exported from the root. It
means writes completed but the device reported differing or invalid values.
The intended profile selection may still be remembered; `persisted` records
that selection, not confirmation of every value. This is an applied result,
so callers must not treat it as a refusal or blindly repeat the writes.
The CLI returns `EXIT_DIFFERS` (3) for this result, after its existing
partial-write (1), persistence (4) and service-reload (5) priorities.
Missing or fundamentally unavailable feedback remains `APPLIED_UNVERIFIED`,
with those paths explicitly classified; its compatible CLI result stays 0
when persistence and service hand-over succeed.

`--no-profile --dry-run` uses the same main-config validation as the actual
restore. Without a discovered config, both refuse with exit 2. Ordinary
startup without a config still uses the empty default desk.

Strict configuration validation is optional: use `--strict`, or the
keyword-only `strict=True` with `load_config`, `load_profile`,
`effective_config`, `switch_profile`, `restore_main` and `describe_profiles`.
Unknown sections and sections the selected device model cannot apply now
refuse instead of being ignored. Both the main file and a selected profile
are validated. The default keeps compatibility warnings and its fallback
from an unusable active profile to the main desk.

A strict start refuses an unusable active profile. The same mode is retained
when the session re-reads under the device lock: a now-invalid desk stops
startup with exit 2 before writes or `READY=1`; an invalid SIGHUP desk is
skipped while the existing session keeps running. The mode is chosen by the
session's own startup arguments. An explicit valid `--profile` or
`--no-profile` validates its target directly, so it can recover a broken
active selection. Its dry-run validates the same target; the previously
declared desk is only a comparison reference and keeps compatibility fallback.
This parsing mode does not change the read-back outcomes or exit priorities
documented above.

Use `oscmix-session --config FILE --check` to strictly validate the main file,
every `.conf` entry in its profiles directory, and the stored selection
offline. It is always strict, independent of `--strict`, and returns 0 only
after the complete check, or 2 with the first failure. An absent profiles
directory is empty; a directory that cannot be enumerated or a broken profile
entry refuses the check. It takes no device lock, connection or write lease,
and changes no marker or service. A main config is required, and device/port
overrides or other actions cannot be combined with this check.

`switch_profile()` and `restore_main()` now honour installation maintenance
fences and hand over to the running service, as CLI selections do. Refused
operations never reload it. A completed operation reloads only after its
marker change is in effect; partial writes request reconciliation of the
previous declared desk. A service known to run another config is left alone.
`Outcome.reload_failed` is true when the running service refused that reload.
Its device state, read-back and marker facts remain valid; callers must not
replay a completed switch merely because its service hand-over failed.
The CLI maps this flag to exit 5, while partial writes retain exit 1.

Source installations can return to an installed but inactive desk with
`./install.sh --disable`. It requires the matching systemd user manager,
blocks unit starts through the same `oscmix/service-allowed` opt-in as native
packages, then stops and disables the unit. It keeps the installed runtime,
configs and profile selection, needs no backend build and performs no root
steps. An older source unit receives the condition with its custom contents
and a backup retained. Ordinary migration from an older unit adopts its
active/enabled state once; later upgrades honour an absent opt-in instead of
recreating it. `--enable` restores permission explicitly.
Before switching to manual mode or migrating to a native package, remove the
opt-in. Native migration refuses it if still present. Source uninstallation
also removes this service state while retaining desk files.

Each config now selects its own profile marker beside the file.
`routing.conf` retains `active-profile`, so its existing selection carries
over. For `--config selected.conf`, the marker is
`active-profile.selected.conf`. If a custom config used the old shared
marker, explicitly select its intended profile again after upgrading.
The old marker is never automatically adopted by a custom config: it does
not record which file selected it. Back up the per-config markers with the
configs and profiles. Before rolling back, select the intended profile
using the older version, which reads only the shared marker.
`active_profile()` without a path now uses config discovery, as the profile
operations do.

Legacy `[osc]` port changes no longer cause profile or reload refusals.
They remain validated config data, while device name, USB ID and serial
determine compatibility with the selected interface. The checked backend
connection still determines which process may receive writes.

A start with input routes now reads the device once before its first write:
one refresh compares the input mix with the desk (the connect check) and is
then the AutoSet observation the apply used to request itself, so starts
that already needed one read no more often. A difference is a warning; the
start applies as before. `--status` gains `playback_matrix` and
`connect_check`, read from the journal and a new record beside it,
`<interface>.seen.<uid>.json`, in the same runtime directory. Schema 2 is
unchanged: consumers that ignore unknown sections are unaffected. The device
cannot report its playback matrix; the new status says what the desk can
state about it instead of a verification.

## Upgrading to 0.8.1

0.8.1 uses the same backend patch series as 0.8.0, so the backend, ALSA
bridge and GTK companion stay as they are; only the Python runtime changes.
Configs, profiles and the active-profile marker carry over unchanged, and a
rollback to 0.8.0 needs nothing beyond reinstalling it.

Python callers: `switch_profile()`, `restore_main()`, `load_profile()`,
`describe_profiles()` and `effective_config()` without `config_path` now use
the config discovery finds, as the command line does. `restore_main()` with no
config anywhere returns a refusal instead of applying the empty default desk,
and a restore's `Outcome.name` is the config file's name (`routing.conf`
unless `--config` names another). `active_profile(None)` still means "no
config". `WriteFailed.refused` tells a refusal the plan made from a failed
connection.

Scripts that read the journal or the unit's status line: `reconcile skipped`
now means that nothing was sent, and `reconcile incomplete` that writes were
sent before the reload ended; see the
[status table](TROUBLESHOOTING.md#3-is-the-backend-running). A stop during
the start or the verifier is logged at info or warning level, not as an
error, and the start is no longer failed with exit 1 for it. The text of
`--status` says `none` instead of `unknown` for a desk without a profile or
configured serial; the JSON is unchanged.

## Upgrading to 0.8.0

Initial session application and explicit profile/main-desk selection still
write declared starting values. Repair and later SIGHUP/resume reconcile now
preserve **all REMEMBER values**, including route volumes whose feedback is
missing, invalid or still matches the file. A full restart creates a new
session and applies the starting values again. Use an explicit profile or
main-desk application when deliberately restoring those values in a running
session; a reload no longer resets them as a side effect of another repair.

`[pin] output.volume = pin` continues to authorize volume restoration.
Link and partner dependencies can refuse a later operation when it could
change a retained setting indirectly. Read the refusal, resolve the routing
or make an explicit desk application; an unknown value is not confirmation.
`[pin] input.stereo` / `output.stereo = remember` retain the route's link
after initial application and require confirmation before dependent matrix
writes. Older versions reject these new policy options, so remove them from
the config before a rollback. Existing syntax remains accepted.

The same observation rule now governs verification and both link-sync paths.
A later contradiction revokes a match, including at the end of an OSC bundle.
Receive failures, cancellation and known wrong links stop dependent writes;
the reported sent/pending paths do not imply an automatic hardware rollback.
Explicit profile verification remains strict even for REMEMBER values.

Upgrade the Python runtime, backend, ALSA bridge and installed GTK companion
together. 0.8.0 uses the versioned ODK1 backend control path and rejects the
old UDP-only backend. The source installer applies the exact recorded patch
series and checks every component's build identifier before replacement;
`--no-build` cannot upgrade a 0.7.3 backend. The upstream base commit alone
does not identify the patched build. Close GTK before upgrading or rolling
back; a disconnected client never replays old writes into a new backend.

`--status --json` now reports schema 2. It removes the obsolete `receive_port`
section, identifies the Unix control endpoint, and describes the GTK control
protocol instead of UDP connection settings. Scripts must check the schema
version; see [the field meanings](STATUS.md#json-schema-2). Status remains
read-only and never connects to the backend. Existing `[osc]` port syntax
is retained and validated for compatibility but no longer selects transport.

Malformed OSC deliveries now invalidate the operation instead of silently
discarding their damaged suffix. A valid prefix cannot confirm link state or
be recorded as a partial successful snapshot. Fix the backend/transport cause
and start a new operation; do not interpret the missing result as device silence.

Python callers: six signatures of the root API changed, and the release
notes did not say so.

- `apply_routing(config, backend, *, leave_alone, require_link_confirmation,
  intent, confirmed, should_stop)` takes a connected backend control object
  instead of `port` and `recv_port`.
- `verify_routing(registers, backend, timeout, ...)` takes it instead of
  `send_port` and `recv_port`, and always returns a `VerifyResult` (never
  `None`); a receive failure raises `ReceivePortError`.
- `verify_and_repair(config, backend, should_stop)` takes it too and returns
  whether the read-back ended without a remaining problem.
- `VerifyResult` has a fourth list, `invalid`.
- `switch_profile()` and `restore_main()` take that control object as
  `backend` instead of the old UDP backend.

The control object (`oscmix_desk.backend.Control`) is not part of the
supported root API, so until a public operation interface replaces the first
three functions, use the `oscmix-session` command line. `switch_profile()`
and `restore_main()` connect themselves when `backend` is left out; in
0.8.0, pass `config_path` explicitly (0.8.1 discovers it). They do not
reload the running service.

For a software rollback, restore the complete 0.7.3 installation or its exact
core/GTK package pair, then restart deliberately with the preserved desk.
Hardware values that changed after the backup are not restored by a package
rollback. Preserve readable state and known declared playback routes separately.

Registered OpenRC/runit installations use the explicit
[host maintenance procedure](INSTALLATION.md#alpineopenrc-and-voidrunit).
Both the user payload and root-owned adapters must be updated while the
persistent fence is present. A failed or interrupted update does not authorize
manual activation; repeat the failed step and then finish maintenance. Explicit
stop/disable during maintenance overrides automatic restart. To return to a
version predating these adapters, disable and remove the host integration before
installing that version, and use its supported foreground or systemd workflow.

NixOS uses its [generation-based maintenance procedure](../packaging/nix/README.md#generation-changes-and-recovery).
Stop the mixers and set the persistent package fence before switching the
package, GTK variant or activation settings. The fence survives an interrupted
switch and reboot. An actual NixOS rollback restores the previous immutable
payload and integration while retaining user configuration, profiles and marker.
Keep the module imported until its disabled/removal transition has completed.
If activation fails, the selected profile may already have changed. Keep
maintenance active until the intended generation and payload are verified.
Choose a saved generation explicitly when older retained generations intervene.

On Fedora Silverblue, follow the [deployment maintenance procedure](SILVERBLUE.md#upgrade-interruption-and-rollback).
Run the common guard on the booted host before replacement or rollback. Leave
the fence in place through the reboot and check the selected deployment before
finishing maintenance. The RPM composition hooks do not clear that host fence.

## Upgrading to 0.7.3

The configuration format, register policies and backend pin are unchanged.
The verifier now revokes a confirmation when a later report in the same
open window contradicts it. Its policy summary counts matching and
deliberately retained values separately; explicit profile confirmation
remains strict. Profile previews compare partial declarations and identify
crosspoints the new profile leaves undeclared.

The launcher now refuses a missing/misconfigured GTK installation or a
backend it cannot associate with the exact interface and reply endpoint.
It reuses a matching manual backend without invoking systemd. Enable the
reviewed default service explicitly if you want the launcher to start it;
start custom services manually. See [status and GUI workflow](STATUS.md).

Native `oscmix-desk-gtk` is optional and depends on the exact core package
version/revision. Upgrade or downgrade both together with the native package
manager. To return to 0.7.2, remove the companion first, then install the
0.7.2 core package. Removing the companion preserves desks and activation.
Interrupted core and GTK maintenance have separate persistent fences;
repair the affected package rather than deleting its marker.

## Upgrading to 0.7.2

The config format and backend pin are unchanged. Installation now separates
copying files from opting into automatic hardware operation, adds preflight
and manual operation, and includes native-package migration/recovery tools.
A source upgrade preserves the existing service's active/enabled state;
a fresh install waits for explicit activation. Native installation does
not remove a previous per-user installation: follow the
[migration and recovery procedure](INSTALLATION.md#migrating-an-existing-source-installation)
before enabling the package's service. Use the instructions shipped
with the version you downloaded.

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
register comparison, not an audio test.

The mode is rechecked before each write phase. If it changes mid-apply,
the result names the writes already sent and leaves the profile marker
unchanged. Let the hardware stream settle and apply again. There is no
automatic rollback or continuous rate enforcement: the device lock cannot
stop a DAW or the interface changing clocks between checks or afterwards.
The capacity check applies to playback routes; input-only routes use their
existing validation.

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
it; see [numeric values](NUMERIC-CONTRACT.md). Snapshots before 0.7.1
rounded to one decimal place and cannot show smaller differences.

Upgrading and rolling back keep the config, profiles and the marker. A
software rollback does not restore mixer state.

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
  `/usr/lib/systemd/system-sleep/oscmix`,
  `/usr/lib/systemd/system/oscmix-resume.service` (since 0.8.0) and
  `/usr/lib/tmpfiles.d/oscmix-desk.conf` if installed.

Use your actual `XDG_CONFIG_HOME` and `XDG_DATA_HOME` where configured.
The package lives at `~/.local/lib/oscmix-desk`. Backups made by individual
`install.sh` copies do not replace this backup: the package directory is
replaced as a unit, and system integration has separate files.

Native RPM installation/removal keeps its maintenance fence if reloading the
system manager fails. Restore the manager connection before repairing the
package transaction; a successful reinstall clears the fence only after its
integration steps finish. Actual openSUSE RPM also returned zero while reporting
a failed scriptlet. Check the diagnostic and maintenance state, not just the
package-manager exit code, before treating a transaction as complete.

## Compatibility changes

| Previous use | 0.7.0 action |
| --- | --- |
| A profile names a different device, serial, USB ID or OSC port from `routing.conf` | Remove `[device]` and `[osc]` from the profile, or give the other machine its own config directory and `--config`. Repeating the main config's values remains valid. |
| `--device` overrides a config validated for another interface | The file is now validated for the effective device. Correct any out-of-range routes before applying it. |
| Python code assigns into `Config` or mutates its lists/policies | Use `dataclasses.replace`, tuples of settings and a new policy mapping. Parsed configs are immutable. |
| Python code imports OSC helpers or discovery internals from `oscmix_desk` | Import from the owning module, for example `oscmix_desk.osc`. The names in `__all__` define the supported root API. |
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
