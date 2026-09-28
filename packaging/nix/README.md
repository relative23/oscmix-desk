# NixOS package and opt-in integration

These expressions are shipped by oscmix-desk, not part of the NixOS package
collection.

`nix-build packaging/nix` uses the pinned NixOS 26.05 package set
`c508844df6c28fa6dabc1b6af70f3ccbd65c5201`. `--arg withGtk false` builds only the
core. Both variants use the same hash-checked upstream commit and the ordinary
`prepare-backend.py` patch/provenance path. Core, bridge and optional GTK live
in one immutable output; their headless identity checks run during the build.
The Python runtime still uses only the standard library. No pip installation
or extra hardware transport is involved.

The NixOS module uses the host's declared `pkgs`, so a host's security updates
also update its compiler and library closure. Pin that system's nixpkgs input
when recording a qualification. Keep the release source under version control
or use an authenticated, hash-checked release archive.

## Install before activation

Import the module from the reviewed source and name an existing normal user:

```nix
{
  imports = [ /path/to/oscmix-desk/packaging/nix/module.nix ];
  services.oscmix-desk = {
    enable = true;
    user = "alice";
    withGtk = true;
    activate = false;
  };
}
```

`sudo nixos-rebuild switch` installs the complete package, udev rules, shared
`root:audio` lock directory and common systemd user unit. It grants the
selected user audio-group access. No mixer is started. A pre-existing user
unit or drop-in is refused because it would shadow the NixOS configuration;
stop and back it up explicitly before migration. Preserve the routing file,
profiles and active-profile marker.

As the selected user, create the configuration only if it does not exist:

```sh
mkdir -p ~/.config/oscmix
test -e ~/.config/oscmix/routing.conf ||
  install -m 600 /run/current-system/sw/share/oscmix-desk/routing.conf.example \
    ~/.config/oscmix/routing.conf
oscmix-session --dry-run
```

Review the device serial and every routing declaration before enabling
hardware control. `configFile` can select another absolute writable file.
Configuration, profiles, the marker and GTK preferences remain user state;
the module does not copy them into the Nix store or replace them on upgrades.

To activate, set `services.oscmix-desk.activate = true`. Then:

```sh
sudo oscmix-package-guard install
sudo nixos-rebuild switch
sudo oscmix-package-guard finish
systemctl --user start oscmix.service
oscmix-session --status --json
```

Activation enables only the named user's service and lingering user manager.
It permits boot and hotplug startup; no interface is required to boot. Resume
reloads a running service, targeting its main process through the existing
`ExecReload`; it never starts a stopped mixer. Status remains read-only.

## Generation changes and recovery

Before changing the package, selected user, configuration path, GTK variant
or activation setting, close GTK and manual sessions, stop the service and
set the persistent maintenance fence:

```sh
systemctl --user stop oscmix.service
sudo oscmix-package-guard install
sudo nixos-rebuild switch             # or boot, followed by reboot
oscmix-session --dry-run
sudo oscmix-package-guard finish
systemctl --user start oscmix.service # only when activation remains enabled
```

The pre-switch check refuses a changed deployment without that fence, or with
any mixer still running. The fence blocks CLI writes and unit startup across
an interrupted switch or reboot. A failed download/build leaves the previous
immutable payload intact. Complete the rebuild or choose a known generation,
check its configuration, then finish maintenance explicitly. Do not remove
the fence merely to silence a failed check.

For rollback, follow the same stop/fence sequence and run
`sudo nixos-rebuild switch --rollback`; retain its source configuration if
subsequent rebuilds should continue to use it. A generation rollback restores
code and system integration, not mixer state, profiles or GTK preferences.

If the saved generation is not the immediately preceding profile, choose it
explicitly. Repeated `--rollback` calls can traverse other retained test or
deployment branches. Keep maintenance active, list the generations, and replace
`NUMBER` below with the saved generation number:

```sh
sudo nix-env --profile /nix/var/nix/profiles/system --list-generations
sudo nix-env --profile /nix/var/nix/profiles/system --switch-generation NUMBER
sudo /nix/var/nix/profiles/system/bin/switch-to-configuration switch
```

A failed activation may already have changed the selected profile and some
system files. Leave the fence in place, complete activation of the selected
known generation, and verify `/run/current-system` and the expected mixer
integration before finishing maintenance with the saved guard. Preserve the
matching source configuration for later rebuilds. Traversing an older
generation can fail, for example with `Failed to get GID for <user>` after
that user's session has ended; the fence keeps protecting the installation,
and selecting the saved generation explicitly restores it.

To disable, keep the module imported, set `activate = false`, and switch while
fenced. Finish maintenance and leave the unit stopped. To remove integration,
set `enable = false` in a further fenced switch. Keep the old guard's store
path (`readlink -f /run/current-system/sw/bin/oscmix-package-guard`) before
removal so it can finish maintenance afterward. Remove the import only after
this transition. Nix garbage collection is separate; user state is preserved.

The permission boundary is the existing audio group and systemd user sandbox.
These integration rules do not prevent a user from deliberately overriding
their own unit or an administrator from bypassing NixOS switch checks.
