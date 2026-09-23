# Installation across Linux distributions

**Status: implementation and qualification in progress, 2026-09-22.**
Target: UCX II across Linux distribution families. The common installer,
manual mode, native packaging and migration helpers are implemented in
separate development work and are now being integrated for 0.7.2. They are not published package
releases or changes to the 0.7.1 installer. See the [roadmap](../ROADMAP.md)
for completed checks and the [current instructions](../../README.md#install)
for what users can install now.

## Recommendation

Keep one distribution-independent installation contract. Offer native
packages where they are tested and a verified source archive/installer
elsewhere. Improve the install and recovery experience before introducing
another distribution channel. A single package format does not by itself
provide compatibility with every Linux distribution.

| Route | Fit for oscmix-desk | Decision for this cycle |
| --- | --- | --- |
| Versioned archive + installer | Can deliver core, pinned C backend and host integration; currently needs build tools and systemd. | Common fallback; improve preflight, layout, service modes and recovery. |
| DEB / RPM / Arch recipe | Integrates dependency management, file ownership and normal OS upgrade/removal. Recipes and lifecycle tests cost maintenance. | Build on the same staging contract; publish only tested targets. |
| `pip` / `pipx` | Suitable for a Python CLI/library; does not by itself provide the backend, udev rules, shared locks and service setup. | Not the primary whole-product installer. Revisit for a concrete Python-only consumer. |
| Flatpak | Interesting for an optional desktop frontend; host integration still needs a separate solution. | Investigate after a stable local core boundary exists. |
| AppImage / portable binary | Can simplify launching a GUI, but host integration and libc/architecture compatibility remain. | No additional artifact format in the first packaging increment. |

`pip` is not inherently incompatible with a GUI. It is the wrong answer
to *all* this project's installation requirements. If a Python-only app
is published later, `pipx` provides an isolated environment; never make
`sudo pip` or bypassing an externally managed system Python the normal
installation path. This follows the [PyPA environment specification](https://packaging.python.org/en/latest/specifications/externally-managed-environments/).
Even PyGObject's pip installation requires platform libraries; its
[installation guide](https://pygobject.gnome.org/getting_started.html)
documents native packages and build dependencies.

## I1: make the common path understandable

The intended flow is **check → install files → review setup → enable**.
The user should learn what is missing before a partial installation,
and choose when an attached device first receives a desk.

1. Add a read-only preflight: Python version, build/runtime dependencies,
   backend pin, ALSA sequencer support and permissions, user-session/service
   availability, writable destinations and existing installed versions.
   Distinguish an absent UCX II from a failed install; offline setup works.
2. Detect capabilities before distribution names. Use `/etc/os-release`
   to give verified package-manager instructions. Unknown distributions
   receive the required commands/libraries and the manual route, not a
   misleading "supported" label or guessed package installation.
3. Separate the payload from integration. One staging definition owns
   entry points, Python runtime, backend binaries and optional upstream
   GTK mixer resources. Templates select per-user or system paths.
   Keep the core's standard-library-only runtime contract.
4. Support installation without immediately enabling the service or
   applying the sample config. An explicit setup step selects device
   serial, config and startup behaviour. Existing-desk upgrades preserve
   the operator's prior service state under a documented stop/update/start
   sequence; a newly created desk requires an explicit first apply.
5. Decouple installer success from the presence of a systemd user manager.
   Qualify a manual foreground session on a non-systemd host. Expose which
   hotplug/resume/locking setup is absent and provide its prerequisites;
   do not represent manual operation as equivalent automatic integration.

Full automation without systemd is a separate adapter task. Avoid a
framework for every init system before one manual path and one additional
adapter have been demonstrated. The shared lock directory and permissions
need an equivalent provisioner outside systemd-tmpfiles; do not silently
weaken cross-user exclusion to call a platform supported.

## Distribution and environment matrix

The roadmap records the completed container and Ubuntu VM checks. The
remaining rows below retain their entry conditions; they are not blanket
support claims. Each qualification records its exact image/release, libc,
architecture, service manager and package versions.

| Family/environment | First check | Integration/package follow-up |
| --- | --- | --- |
| Debian and Ubuntu, including LTS | Fresh source install and upgrade in each selected release | DEB; real systemd user session, udev, resume and package transitions |
| Fedora | Same install contract and pinned backend build | RPM; session and distribution policy checks |
| openSUSE | Dependency names, paths and source install | Its own tested RPM recipe/policy, not an assumed Fedora binary |
| Arch | Source build and current dependency floor | Reproducible recipe inputs, package ownership and rolling-release smoke test |
| Alpine/musl without systemd | Build, parser/CLI and manual lifecycle feasibility | OpenRC integration only after that path works |
| Void/runit and other init systems | Explicit dependency/manual-operation recipe | Qualify an adapter when a maintained test target is available |
| Immutable/declarative systems, including NixOS | Identify host-owned integration and writable user paths | Native declarative recipe or supported layering; no blind writes into a read-only OS |

Start with x86_64 environments available to qualification, but do not bake
that architecture into paths or build logic. Qualify aarch64 separately
before publishing its binaries. A build in a container proves neither a
working login session nor hardware: run lifecycle checks in VMs with a
real user manager, and distinguish those results from UCX II measurements.
The GTK companion must also work outside GNOME: include KDE Plasma and
Xfce, and Wayland/X11 where the selected desktop supports them.

## I2: package ownership and migration

Use package staging, not `sudo ./install.sh` in a maintainer script. The
existing installer writes into the user's home and its unit starts a
binary there; a system package needs deliberate vendor paths and unit
templates. Package-managed files and per-user config have separate owners.
Debian's [config-file policy](https://www.debian.org/doc/debian-policy/ch-files.html#user-configuration-files)
and [maintainer-script policy](https://www.debian.org/doc/debian-policy/ch-maintainerscripts.html)
are concrete requirements for the DEB implementation; review other
distributions' policies when implementing their recipes.

Before publishing any native package, settle and test:

- **Payload:** `/usr/bin` entry points, a private runtime location,
  vendor user unit, udev/tmpfiles/resume files and desktop/schema resources
  according to the target distribution. An optional desktop app adds no
  dependency to headless operation. Decide whether the pinned backend is
  bundled or a separately versioned dependency; record its exact revision.
- **Migration:** detect `.local/bin` shadowing, old per-user units overriding
  vendor units, old udev rules, schemas and legacy runtime paths. Show the
  migration actions; back up replaced project files and preserve custom
  units, config, profiles, permissions and the active marker. Do not erase
  unknown files to make the new package win path resolution.
- **Running sessions:** identify the actual affected user/installation;
  never call a root user's service manager as a substitute. Coordinate
  stop/replace/start so no session imports a mixed runtime. Package failure
  or interruption leaves a defined recovery path and reports whether the
  service is still stopped. Removal must not leave a running backend whose
  on-disk installation has disappeared.
- **Updates and provenance:** source release and backend SHA, checksums,
  build recipe/toolchain and authenticated release provenance accompany
  every artifact. Reproducible source archives do not prove reproducible
  C binaries. Use normal package-manager upgrades; an APT/RPM repository
  and its signing-key lifecycle can follow, not an embedded self-updater.

Do not advertise one prebuilt glibc bundle as universal Linux support.
The common source path remains necessary until a binary's ABI, libc and
architecture coverage is measured.

## Acceptance

I1 closes when a new user can identify missing prerequisites, install a
chosen release, select their UCX II and inspect a desk before its first
apply; the distribution/manual-mode matrix has recorded results and
explicit gaps. Help must name the running version, configuration and
installation location rather than only saying "installation failed".

I2 closes **per published target** after fresh install, actual
0.7.0-source → package migration, upgrade, interrupted install, downgrade
and removal are exercised. Config/profile/marker contents survive,
entry points resolve the intended release, lifecycle order is checked
and a restored installation is usable. Keep the existing
[upgrade limits](../UPGRADING.md#interrupted-install-and-rollback): file
rollback cannot undo emitted audio or guarantee hardware rollback.
