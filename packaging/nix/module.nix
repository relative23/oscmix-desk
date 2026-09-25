{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.oscmix-desk;
  selectedUser = config.users.users.${cfg.user} or { };
  home = selectedUser.home or "/nonexistent";
  record = pkgs.writeText "oscmix-desk-nixos.json" (
    builtins.toJSON {
      enabled = cfg.enable;
      inherit (cfg) activate user;
      config = cfg.configFile;
      package = if cfg.enable then toString cfg.package else null;
    }
  );
in
{
  options.services.oscmix-desk = {
    enable = lib.mkEnableOption "oscmix-desk package and selected-user integration";
    activate = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Explicitly allow the selected user's mixer service to start on boot,
        hotplug and desktop launch. Review the writable routing configuration
        first. Installing the package or enabling integration alone does not
        allow hardware activation.
      '';
    };
    user = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "alice";
      description = "Existing normal user that owns the mixer configuration and service.";
    };
    withGtk = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Include the upstream GTK mixer from the same exact backend series.";
    };
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { inherit (cfg) withGtk; };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { inherit withGtk; }";
      description = "Complete immutable core/backend/GTK payload used by the service and launcher.";
    };
    configFile = lib.mkOption {
      type = lib.types.str;
      default = "${home}/.config/oscmix/routing.conf";
      description = ''
        Absolute path to the user's writable configuration. This module never
        creates or replaces it, profiles, or the active-profile marker.
      '';
    };
  };

  config = lib.mkMerge [
    {
      # Leave the module imported while disabling it so the old integration
      # is also fenced before its unit is removed. Build-only is harmless;
      # switch/test/boot use the NixOS pre-switch check, before /etc changes.
      system.preSwitchChecks.oscmix-desk = ''
        ${lib.optionalString cfg.enable ''
          for directory in ${lib.escapeShellArg "${home}/.config/systemd/user"} \
                           ${lib.escapeShellArg "${home}/.local/share/systemd/user"}; do
            for name in oscmix.service oscmix.service.d; do
              if [ -e "$directory/$name" ] || [ -L "$directory/$name" ]; then
                echo "oscmix-desk: migrate the existing user unit explicitly before NixOS integration: $directory/$name" >&2
                exit 1
              fi
            done
          done
        ''}
        if [ -f /etc/oscmix-desk/nixos.json ]; then
          if ! ${pkgs.diffutils}/bin/cmp -s /etc/oscmix-desk/nixos.json ${record}; then
            if [ ! -f /var/lib/oscmix-desk/package-update ]; then
              echo 'oscmix-desk: stop the mixers, then run oscmix-package-guard install before changing this deployment' >&2
              exit 1
            fi
            ${pkgs.python3}/bin/python3 -I ${../package-guard} check
          fi
        ${lib.optionalString cfg.enable ''
          else
            ${pkgs.python3}/bin/python3 -I ${../package-guard} check
        ''}
        fi
      '';
    }
    (lib.mkIf cfg.enable {
      assertions = [
        {
          assertion = cfg.user != "root" && (selectedUser.isNormalUser or false);
          message = "services.oscmix-desk.user must name a declared normal user, never root";
        }
        {
          assertion =
            lib.hasPrefix "/" cfg.configFile
            && !(lib.hasInfix "\n" cfg.configFile)
            && !(lib.hasInfix "%" cfg.configFile);
          message = "services.oscmix-desk.configFile must be absolute and contain no newline or systemd specifier";
        }
      ];
      environment.systemPackages = [ cfg.package ];
      environment.pathsToLink = [ "/share/oscmix-desk" ];
      environment.etc."oscmix-desk/nixos.json".source = record;
      environment.etc."oscmix-desk/nixos-active" = lib.mkIf cfg.activate {
        text = "Explicitly activated in the NixOS configuration.\n";
      };
      users.users.${cfg.user} = {
        extraGroups = [ "audio" ];
        linger = lib.mkDefault cfg.activate;
      };
      boot.kernelModules = [ "snd-seq" ];
      systemd.tmpfiles.rules = lib.splitString "\n" (
        builtins.readFile ../../systemd/tmpfiles.d/oscmix-desk.conf
      );
      services.udev.extraRules = builtins.readFile ../../udev/90-rme-fireface.rules;
      systemd.packages = [ cfg.package ];
      systemd.user.services.oscmix = {
        overrideStrategy = "asDropin";
        wantedBy = lib.optional cfg.activate "default.target";
        unitConfig = {
          ConditionUser = cfg.user;
          ConditionPathExists = [
            cfg.configFile
            "/etc/oscmix-desk/nixos-active"
            "!/var/lib/oscmix-desk/package-update"
            "!/var/lib/oscmix-desk/gtk-package-update"
            "!/var/lib/oscmix-desk/service-update"
          ];
        };
        environment = {
          HOME = home;
          OSCMIX_CONFIG = cfg.configFile;
        };
      };
      powerManagement.resumeCommands = ''
        # Reload only a running selected-user service; never activate one.
        ${config.systemd.package}/bin/systemctl --user \
          --machine=${lib.escapeShellArg "${cfg.user}@.host"} reload oscmix.service \
          >/dev/null 2>&1 || :
      '';
    })
  ];
}
