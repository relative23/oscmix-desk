{
  lib,
  stdenv,
  fetchgit,
  git,
  python3,
  pkg-config,
  alsa-lib,
  gtk3,
  glib,
  makeWrapper,
  wrapGAppsHook3,
  coreutils,
  withGtk ? true,
}:
let
  project = ../..;
  series = builtins.fromJSON (builtins.readFile ../../patches/backend-series.json);
  version = builtins.head (
    builtins.match ".*__version__ = \"([0-9.]+)\".*" (
      builtins.readFile ../../src/oscmix_desk/constants.py
    )
  );
  upstream = fetchgit {
    url = "https://github.com/michaelforney/oscmix.git";
    rev = series.upstream;
    # Keep the commit object for the common git-archive preparation path.
    leaveDotGit = true;
    fetchSubmodules = false;
    hash = "sha256-NHnO1kTOQ1t6CT9AexxSGMjEXDH212KUJ4Zi/tYTJl0=";
  };
in
stdenv.mkDerivation {
  pname = "oscmix-desk";
  inherit version;
  src = lib.fileset.toSource {
    root = project;
    fileset = lib.fileset.unions (
      [
        ../../bin/oscmix-session
        ../../bin/oscmix-launch
        (lib.fileset.fileFilter (file: file.hasExt "py") ../../src)
        ../../scripts/prepare-backend.py
        ../../scripts/install-payload.sh
        ../../patches/backend-series.json
        ../../config/routing.conf.example
        ../../desktop
        ../../systemd/oscmix.service
        ../../packaging/package-guard
        ../../LICENSE
      ]
      ++ map (patch: ../../patches + "/${patch.file}") series.patches
    );
  };

  nativeBuildInputs = [
    git
    python3
    pkg-config
    makeWrapper
  ]
  ++ lib.optionals withGtk [
    glib
    wrapGAppsHook3
  ];
  buildInputs = [ alsa-lib ] ++ lib.optional withGtk gtk3;
  dontConfigure = true;
  # Wrappers retain each executable's basename. /proc identity checks and
  # package maintenance must still recognize the real process after exec.
  dontWrapGApps = true;
  strictDeps = true;
  # Install checks execute Python before Nix makes the output read-only.
  # Timestamp-based .pyc files would otherwise make repeated builds differ.
  env.PYTHONDONTWRITEBYTECODE = "1";

  buildPhase = ''
    runHook preBuild
    # The fixed-output store is owned by root, not this sandbox builder.
    # Trust only this hash-checked repository for the two git reads below.
    export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=${upstream}
    python3 scripts/prepare-backend.py --upstream ${upstream} --destination backend
    make -C backend -j"$NIX_BUILD_CORES" oscmix alsaseqio ${lib.optionalString withGtk "gtk"}
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install_file() { install -D -m "$1" "$2" "$3"; }
    source scripts/install-payload.sh
    payload_runtime "$PWD" "$out/lib/oscmix-desk/oscmix_desk"
    payload_entry_points "$PWD" "$out/bin"
    payload_backend "$PWD/backend" "$out/bin" "$out/share" ${if withGtk then "yes" else "no"}
    install_file 644 config/routing.conf.example "$out/share/oscmix-desk/routing.conf.example"
    install_file 644 LICENSE "$out/share/licenses/oscmix-desk/LICENSE"
    install_file 644 backend/LICENSE "$out/share/licenses/oscmix-desk/oscmix-LICENSE"
    install_file 755 packaging/package-guard "$out/bin/oscmix-package-guard"
    substituteInPlace "$out/bin/oscmix-session" "$out/bin/oscmix-launch" \
      --replace-fail '#!/usr/bin/env python3' '#!${python3}/bin/python3'
    substituteInPlace "$out/bin/oscmix-package-guard" \
      --replace-fail '#!/usr/bin/python3 -I' '#!${python3}/bin/python3 -I'
    install_file 644 systemd/oscmix.service "$out/lib/systemd/user/oscmix.service"
    substituteInPlace "$out/lib/systemd/user/oscmix.service" \
      --replace-fail 'ExecStart=%h/.local/bin/oscmix-session' "ExecStart=$out/bin/oscmix-session" \
      --replace-fail 'ExecReload=/bin/kill' 'ExecReload=${coreutils}/bin/kill'
    ${lib.optionalString withGtk ''
      payload_desktop "$PWD" "$out/bin" "$out/share"
      glib-compile-schemas "$out/share/glib-2.0/schemas"
    ''}
    runHook postInstall
  '';

  postFixup = ''
    mkdir -p "$out/libexec"
    for entry in oscmix-session oscmix-launch; do
      mv "$out/bin/$entry" "$out/libexec/$entry"
      makeWrapper "$out/libexec/$entry" "$out/bin/$entry" \
        --set-default OSCMIX_BIN_BACKEND "$out/bin/oscmix" \
        --set-default OSCMIX_BIN_ALSASEQIO "$out/bin/alsaseqio" \
        --set-default OSCMIX_BIN_GTK "$out/bin/oscmix-gtk"
    done
    ${lib.optionalString withGtk ''
      mv "$out/bin/oscmix-gtk" "$out/libexec/oscmix-gtk"
      makeWrapper "$out/libexec/oscmix-gtk" "$out/bin/oscmix-gtk" \
        "''${gappsWrapperArgs[@]}" --set GSETTINGS_SCHEMA_DIR "$out/share/glib-2.0/schemas"
    ''}
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    python3 scripts/prepare-backend.py --verify "$out/bin/oscmix" "$out/bin/alsaseqio" \
      ${lib.optionalString withGtk ''"$out/bin/oscmix-gtk"''}
    test "$("$out/bin/oscmix-session" --version)" = "${version}"
    runHook postInstallCheck
  '';

  passthru = {
    inherit withGtk upstream;
    backendSeries = series;
  };
  meta = {
    description = "Declarative Fireface mixer state with a coordinated upstream GTK companion";
    homepage = "https://github.com/relative23/oscmix-desk";
    license = [
      lib.licenses.mit
      lib.licenses.isc
      lib.licenses.unlicense # upstream intpack.h
    ];
    platforms = lib.platforms.linux;
    mainProgram = "oscmix-session";
  };
}
