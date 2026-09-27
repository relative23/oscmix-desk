# 0033 — NixOS keeps the common payload in one immutable generation

**Status: implemented for 0.8.0; final-candidate qualification remains open.**

NixOS replaces whole store outputs and system generations. Running the source
installer against its read-only system paths would duplicate Nix ownership and
make rollback unreliable. A separate desk supervisor is unnecessary: NixOS
already provides the systemd user manager used by the existing runtime.

The package expression pins the upstream commit and fixed-output Git hash,
then invokes the common backend preparation script and shared payload functions.
Its default entry pins the NixOS 26.05 package set; a host module uses that host's
declared `pkgs`. Core, ALSA bridge and optional upstream GTK occupy one output,
with exact build-identity checks during installation. The optional GTK setting
selects a complete output, so separate package versions cannot drift apart.
Nix wrappers select that output's binaries and preserve the executable
basenames used by process-identity checks and maintenance. The Python runtime
and hardware protocol are unchanged by this packaging choice.

The module has separate `enable` and `activate` settings. Enabling installs
integration and shared lock permissions. Activation additionally permits the
selected normal user's service to start, enables its default-target dependency
and keeps its user manager available at boot. A `ConditionUser` and a
generation-owned activation file constrain the global user unit. Old user units
and drop-ins are refused at deployment because they could shadow these rules.
Root is never the runtime account. Resume uses the existing main-process-only
reload; it does not start a stopped desk.

The module records only deployment identity: package store path, selected user,
configuration path and activation setting. A changed deployment requires the
ordinary persistent package-maintenance fence and a stopped mixer set in a
NixOS pre-switch check, before `/etc` and the active generation change. The
fence is not automatically removed on switch or boot. The operator completes
or rolls back the generation, validates it, then explicitly finishes maintenance.
An unrelated rebuild of the same deployment needs no new desk shutdown.

Configuration, profiles, the active marker and GTK preferences stay in writable
user state. A generation rollback changes code and integration; it cannot undo
later device changes or user edits. Keep the module imported while disabling
and removing integration, so its check also covers that transition. Removing
the module itself or bypassing NixOS checks is an administrator's explicit
action outside this adapter's enforcement boundary.

The first real NixOS VM checks cover both package variants, inactive installation,
explicit activation, supervisor identity, three crash/restart cycles, persistent
maintenance through a reboot, actual generation rollback, selected-user scope,
disable/removal and byte/owner preservation of configuration, profile and marker.
They use no connected interface and do not qualify device I/O, physical hotplug,
suspend, a final 0.8.0 payload, or a historical 0.7.3 package migration.
`tests/nixos_lifecycle.py` preserves this lifecycle check as two reproducible
phases around an actual reboot. Both package variants also pass independent
complete-output repeat builds. Build-time Python cache writes are disabled;
their timestamp headers previously made otherwise identical builds differ.
The [development evidence](../evidence/0.8.0/nixos-development.json) records
the definitions, payload, binaries, Nix outputs and measured lifecycle.

A subsequent [native-resume probe](../evidence/0.8.0/nixos-resume-development.json)
checked systemd 260.4 in that VM with a fresh package built from the committed
development source. NixOS's existing `sleep-actions.service` stop action ran
after `user.slice` thawed; three fresh deep-entry/exit cycles delivered one
reload each to the same signal receiver. A fourth cycle left a stopped desk
stopped. The module needed no resume change. Maintenance survived the preceding
reboot, and actual generation rollbacks restored the prior disabled system and
user state. Later virtual-RTC wakeups were immediate: this is evidence for
trigger ordering, not extended sleep, mixer/device restoration or a final release.
The subsequent poweroff panicked after filesystem unmount. A cold-boot control
powered off cleanly; another control without the mixer package, user unit or
mixer resume action reproduced the same panic after four deep cycles. The
evidence records both controls and the RTC-related ACPI errors. The platform
cause is unresolved, and clean shutdown after resume is not qualified.

See the [package and operating instructions](../../packaging/nix/README.md),
[NixOS manual](https://nixos.org/manual/nixos/stable/), and the pinned
[pre-switch-check implementation](https://github.com/NixOS/nixpkgs/blob/c508844df6c28fa6dabc1b6af70f3ccbd65c5201/nixos/modules/system/activation/pre-switch-check.nix).
