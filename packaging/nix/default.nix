{
  withGtk ? true,
}:
let
  # The default build is reproducible independently of the caller's channel.
  # The NixOS module instead uses the host's explicitly selected nixpkgs.
  pkgs = import (builtins.fetchTarball {
    url = "https://releases.nixos.org/nixos/26.05/nixos-26.05.10529.c508844df6c2/nixexprs.tar.xz";
    sha256 = "1sngm135201mwkqy1xcann3bybjq9wpkd3vknglxbaza9953ilx9";
  }) { };
in
pkgs.callPackage ./package.nix { inherit withGtk; }
