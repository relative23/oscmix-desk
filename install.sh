#!/usr/bin/env bash
# oscmix-desk installer.
#
# Everything is installed per-user (~/.local, ~/.config); root is needed
# for three files: the udev hotplug rule, the resume hook, and the
# tmpfiles.d entry that creates the shared lock directory. Existing files
# are backed up before being replaced, an existing routing.conf is never
# touched.
set -euo pipefail

# Every path below hangs off HOME. An empty one would install into /.local
# and /.config, a relative one the working directory's, and `set -u`
# catches neither.
case "${HOME:-}" in
    /*) ;;
    *)  echo "install.sh: HOME must be an absolute path, not '${HOME:-}'" >&2
        exit 2 ;;
esac

OSCMIX_REPO="${OSCMIX_REPO:-https://github.com/michaelforney/oscmix}"
# Pinned to a commit, not a branch. oscmix is the component that actually
# talks to the hardware, and every measurement this project publishes was
# taken against this revision -- building "whatever master was that day"
# would make the word "verified" meaningless, and this clone-and-compile
# is the only path here that executes code from the network.
# Override to track upstream: OSCMIX_REF=master ./install.sh
OSCMIX_REF="${OSCMIX_REF:-f2fdd5ec78338848754aad32cc07f3440de63395}"
USB_VENDOR="2a39"
USB_PRODUCT="3fd9"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$PROJECT_DIR/build/oscmix"
BIN_DIR="$HOME/.local/bin"
LIB_DIR="$HOME/.local/lib/oscmix-desk"
# Where this project installed itself before it was renamed. An upgrade
# would otherwise leave a complete second copy of the package behind,
# and `oscmix-launch` searches `../lib/*` for a package directory -- a
# stale one there is a version nobody chose. Removed by both scripts.
LEGACY_LIB_DIR="$HOME/.local/lib/oscmix-autostart"

# A base directory that is not absolute is invalid and ignored: the XDG
# specification's rule, and the session's and the launcher's since
# 0.6.10, which would otherwise read another routing.conf than the one
# installed here.
xdg_base() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *)  printf '%s\n' "$2" ;;
    esac
}
CONFIG_HOME="$(xdg_base "${XDG_CONFIG_HOME:-}" "$HOME/.config")"
CONFIG_DIR="$CONFIG_HOME/oscmix"
DATA_DIR="$(xdg_base "${XDG_DATA_HOME:-}" "$HOME/.local/share")"
UNIT_DIR="$CONFIG_HOME/systemd/user"
# Overridable for the test suite only, which must never reach the real
# files -- as root, $SUDO is empty and the install would be real.
UDEV_RULE="${OSCMIX_UDEV_RULE:-/etc/udev/rules.d/90-rme-fireface.rules}"
SLEEP_HOOK="${OSCMIX_SLEEP_HOOK:-/usr/lib/systemd/system-sleep/oscmix}"
TMPFILES_CONF="${OSCMIX_TMPFILES_CONF:-/usr/lib/tmpfiles.d/oscmix-desk.conf}"

DO_BUILD=1
DO_UDEV=1
CHECK_ONLY=0
MANUAL=0
ENABLE=0
PREFLIGHT_ARGS=()

usage() {
    cat <<'EOF'
usage: ./install.sh [options]

options:
  --check      report prerequisites and destinations without changing files
  --enable     enable automatic operation and start if the device is attached;
               first review routing.conf and inspect --dry-run
  --manual     install for foreground use without a systemd user manager
  --no-build   skip building oscmix (use already installed binaries)
  --no-udev    skip the root steps: the udev rule (no hotplug autostart),
               the resume hook (no reconcile after suspend) and the
               shared lock directory (the lock falls back to the
               per-user runtime directory, ADR 0023)
  -h, --help   show this help

environment:
  OSCMIX_REPO  oscmix git repository (default: upstream on GitHub)
  OSCMIX_REF   git ref to build (default: the commit this release pins)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --enable) ENABLE=1; PREFLIGHT_ARGS+=(--enable) ;;
        --manual) MANUAL=1; PREFLIGHT_ARGS+=(--manual) ;;
        --no-build) DO_BUILD=0; PREFLIGHT_ARGS+=(--no-build) ;;
        --no-udev) DO_UDEV=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$MANUAL" = 1 ] && [ "$ENABLE" = 1 ]; then
    echo "install.sh: --manual and --enable cannot be combined" >&2
    exit 2
fi

info() { printf '\033[1;34m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Install a file, keeping a timestamped backup if the target differs.
install_file() {
    local mode="$1" src="$2" dst="$3"
    if [ -e "$dst" ] && ! cmp -s "$src" "$dst"; then
        local backup
        backup="$dst.bak.$(date +%Y%m%d-%H%M%S)"
        cp -p "$dst" "$backup"
        info "backed up $dst -> $backup"
    fi
    install -D -m "$mode" "$src" "$dst"
}

# shellcheck source=scripts/install-payload.sh
source "$PROJECT_DIR/scripts/install-payload.sh"

require() {
    command -v "$1" >/dev/null 2>&1 || fail "missing dependency: $1 ($2)"
}

# The user manager belongs to the login session, not to an overridden
# HOME. A scratch installation must not stop or restart the real desk.
manages_this_home() {
    local environment session_home session_config
    environment="$(systemctl --user show-environment 2>/dev/null)" || return 1
    session_home="$(printf '%s\n' "$environment" | sed -n 's/^HOME=//p')"
    session_config="$(printf '%s\n' "$environment" | sed -n 's/^XDG_CONFIG_HOME=//p')"
    session_config="$(xdg_base "$session_config" "$session_home/.config")"
    [ "$session_home" = "$HOME" ] && [ "$session_config" = "$CONFIG_HOME" ]
}

# --------------------------------------------------------------------------
# Preflight checks
# --------------------------------------------------------------------------

require python3 "needed by oscmix-session"
python3 "$PROJECT_DIR/scripts/install-preflight.py" "${PREFLIGHT_ARGS[@]}" \
    || fail "preflight failed; see the missing prerequisites above"
[ "$CHECK_ONLY" = 0 ] || exit 0

HAS_MANAGER=0
if command -v systemctl >/dev/null 2>&1 \
    && systemctl --user show-environment >/dev/null 2>&1; then
    HAS_MANAGER=1
fi
if [ "$ENABLE" = 1 ] && { [ "$HAS_MANAGER" = 0 ] || ! manages_this_home; }; then
    fail "--enable needs the systemd user manager for $HOME; no files changed"
fi
if [ "$HAS_MANAGER" = 0 ]; then
    MANUAL=1
fi
if [ "$HAS_MANAGER" = 1 ] && [ "$MANUAL" = 0 ] && ! manages_this_home; then
    fail "systemd's user manager must match HOME and XDG_CONFIG_HOME; no files changed"
fi
# The manual path still shares the per-user runtime with any user unit.
if [ "$HAS_MANAGER" = 1 ] && ! manages_this_home \
    && systemctl --user show-environment 2>/dev/null | sed -n 's/^HOME=//p' | grep -Fxq "$HOME"; then
    fail "systemd's user manager uses another XDG_CONFIG_HOME; no files changed"
fi
WAS_ACTIVE=0
WAS_ENABLED=0
if [ "$HAS_MANAGER" = 1 ] && manages_this_home; then
    systemctl --user is-active --quiet oscmix.service && WAS_ACTIVE=1
    systemctl --user is-enabled --quiet oscmix.service && WAS_ENABLED=1
fi
if [ "$MANUAL" = 1 ] && { [ "$WAS_ACTIVE" = 1 ] || [ "$WAS_ENABLED" = 1 ]; }; then
    fail "an active or enabled oscmix.service exists; stop and disable it before choosing --manual"
fi

# Hold this through the build too: two installers for the same home
# must not replace its binaries or shared build checkout concurrently.
require flock "to serialise installation"
mkdir -p "$LIB_DIR"
exec 9>"$LIB_DIR/.install.lock"
flock -n 9 || fail "another installer is using $LIB_DIR"

# --------------------------------------------------------------------------
# Build oscmix (backend, alsaseqio bridge, GTK mixer)
# --------------------------------------------------------------------------

GTK_BUILT=0
if [ "$DO_BUILD" = 1 ]; then
    require git "to fetch oscmix"
    require make "to build oscmix"
    require cc "to build oscmix (install gcc or clang)"
    require pkg-config "to build oscmix"
    pkg-config --exists alsa \
        || fail "ALSA development files missing (Debian/Ubuntu: libasound2-dev, Fedora: alsa-lib-devel, Arch: alsa-lib)"

    GTK_FLAG="GTK=n"
    if pkg-config --exists 'gtk+-3.0'; then
        GTK_FLAG="GTK=y"
        require glib-compile-resources "to build oscmix-gtk (libglib2.0-dev-bin)"
        require glib-compile-schemas "to build oscmix-gtk"
    else
        warn "GTK 3 development files not found; building without the GUI"
        warn "(Debian/Ubuntu: libgtk-3-dev, Fedora: gtk3-devel, Arch: gtk3)"
    fi

    if [ -d "$BUILD_DIR/.git" ]; then
        info "updating oscmix source in $BUILD_DIR"
        git -C "$BUILD_DIR" fetch --quiet origin "$OSCMIX_REF" 2>/dev/null \
            || git -C "$BUILD_DIR" fetch --quiet origin
        git -C "$BUILD_DIR" checkout --quiet "$OSCMIX_REF" 2>/dev/null \
            || git -C "$BUILD_DIR" checkout --quiet FETCH_HEAD
    else
        info "cloning $OSCMIX_REPO ($OSCMIX_REF)"
        mkdir -p "$BUILD_DIR"
        # `git clone --depth 1 --branch` accepts a branch or a tag but
        # not a commit, and the pinned default ref is a commit. init +
        # fetch does take one, so the shallow clone the pin cost us is
        # back: one commit instead of upstream's full history.
        #
        # Servers may refuse to serve an arbitrary SHA
        # (uploadpack.allowReachableSHA1InWant); GitHub does not, but a
        # mirror might, so a failed shallow fetch falls back to a full
        # clone rather than aborting the install.
        git -C "$BUILD_DIR" init --quiet
        git -C "$BUILD_DIR" remote add origin "$OSCMIX_REPO"
        if git -C "$BUILD_DIR" fetch --quiet --depth 1 origin "$OSCMIX_REF"; then
            git -C "$BUILD_DIR" checkout --quiet FETCH_HEAD
        else
            warn "shallow fetch of $OSCMIX_REF failed; falling back to a full clone"
            git -C "$BUILD_DIR" fetch --quiet origin
            git -C "$BUILD_DIR" checkout --quiet "$OSCMIX_REF"
        fi
    fi

    # State what was actually built. If the ref was a full SHA, the
    # checkout must have landed on exactly it -- a silent fallback to
    # master is the failure this pin exists to prevent.
    OSCMIX_BUILT_SHA="$(git -C "$BUILD_DIR" rev-parse HEAD)"
    case "$OSCMIX_REF" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
            if [ ${#OSCMIX_REF} -eq 40 ] && [ "$OSCMIX_BUILT_SHA" != "$OSCMIX_REF" ]; then
                fail "oscmix checkout is $OSCMIX_BUILT_SHA, expected $OSCMIX_REF"
            fi
            ;;
    esac
    info "building oscmix at $OSCMIX_BUILT_SHA"
    git -C "$BUILD_DIR" diff --quiet HEAD -- \
        || fail "backend checkout has local source changes; preserve them in another checkout before installing"

    info "building oscmix ($GTK_FLAG)"
    # Build the transports this product actually installs. Upstream's
    # `all` also builds alsarawio, an unused transport requiring Linux
    # kernel development headers on musl distributions.
    BUILD_TARGETS=(oscmix alsaseqio)
    [ "$GTK_FLAG" != GTK=y ] || BUILD_TARGETS+=(gtk)
    # Command-line CC propagates into GTK's recursive POSIX make too.
    # Otherwise that make defaults to c99, absent on e.g. openSUSE Leap 16.
    make -C "$BUILD_DIR" "CC=${CC:-cc -std=c11}" "$GTK_FLAG" "${BUILD_TARGETS[@]}" >/dev/null

else
    info "skipping build (--no-build); checking for existing binaries"
    for tool in oscmix alsaseqio; do
        found=0
        for dir in "$BIN_DIR" /usr/local/bin /usr/bin; do
            [ -x "$dir/$tool" ] && found=1 && break
        done
        [ "$found" = 1 ] || fail "$tool not found; run without --no-build"
    done
fi

# Prepare the complete Python package before stopping the service or
# replacing any installed code. A failed copy used to leave half a
# package behind. The lock serialises installers for this installation.
RUNTIME_PREVIOUS="$LIB_DIR/oscmix_desk.previous"
if [ ! -e "$LIB_DIR/oscmix_desk" ] && [ -d "$RUNTIME_PREVIOUS" ]; then
    mv "$RUNTIME_PREVIOUS" "$LIB_DIR/oscmix_desk"
fi
RUNTIME_STAGE="$(mktemp -d "$LIB_DIR/.stage.XXXXXX")"
STOPPED_SERVICE=0
finish_install() {
    local status=$?
    if [ ! -e "$LIB_DIR/oscmix_desk" ] && [ -d "$RUNTIME_PREVIOUS" ]; then
        mv "$RUNTIME_PREVIOUS" "$LIB_DIR/oscmix_desk"
    fi
    [ ! -d "$RUNTIME_STAGE" ] || rm -rf "$RUNTIME_STAGE"
    if [ "$status" -ne 0 ] && [ "$STOPPED_SERVICE" = 1 ]; then
        warn "installation incomplete; the service remains stopped."
        warn "Rerun install.sh from the chosen release before starting it."
    fi
    return "$status"
}
trap finish_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
payload_runtime "$PROJECT_DIR" "$RUNTIME_STAGE"
if [ "$WAS_ACTIVE" = 1 ]; then
    info "stopping the service before replacing installed code"
    systemctl --user stop oscmix.service || fail "could not stop oscmix.service"
    STOPPED_SERVICE=1
fi

if [ "$DO_BUILD" = 1 ]; then
    payload_backend "$BUILD_DIR" "$BIN_DIR" "$DATA_DIR"
    if [ -x "$BUILD_DIR/gtk/oscmix-gtk" ]; then
        GTK_BUILT=1
        glib-compile-schemas "$DATA_DIR/glib-2.0/schemas"
    fi
fi

# --------------------------------------------------------------------------
if [ -d "$LEGACY_LIB_DIR" ]; then
    info "removing the pre-rename install at $LEGACY_LIB_DIR"
    rm -rf "$LEGACY_LIB_DIR"
fi

# A pre-rename install put root-owned binaries in /usr/local/bin. The
# session prefers $BIN_DIR since the 2026-08-26 shadowing incident, but
# a stale copy is still a trap for manual PATH use -- say so instead of
# silently coexisting.
for tool in oscmix alsaseqio oscmix-gtk; do
    if [ -x "/usr/local/bin/$tool" ] && [ -e "$BIN_DIR/$tool" ]         && ! cmp -s "/usr/local/bin/$tool" "$BIN_DIR/$tool"; then
        warn "stale $tool in /usr/local/bin differs from $BIN_DIR/$tool; remove it: sudo rm /usr/local/bin/$tool"
    fi
done

# Install oscmix-desk components
# --------------------------------------------------------------------------

# The runtime package sits next to the entry point, which locates it as
# <bin>/../lib/oscmix-desk. Stale modules from an earlier version
# would be importable and silently win, so the directory is replaced
# wholesale rather than merged into.
info "installing the runtime package to $LIB_DIR"
rm -rf "$RUNTIME_PREVIOUS"
if [ -e "$LIB_DIR/oscmix_desk" ]; then
    mv "$LIB_DIR/oscmix_desk" "$RUNTIME_PREVIOUS"
fi
mv "$RUNTIME_STAGE" "$LIB_DIR/oscmix_desk"

info "installing scripts to $BIN_DIR"
payload_entry_points "$PROJECT_DIR" "$BIN_DIR"

if [ ! -e "$CONFIG_DIR/routing.conf" ]; then
    info "installing default config to $CONFIG_DIR/routing.conf"
    install -D -m 644 "$PROJECT_DIR/config/routing.conf.example" \
        "$CONFIG_DIR/routing.conf"
else
    info "keeping existing $CONFIG_DIR/routing.conf"
fi
install -D -m 644 "$PROJECT_DIR/config/routing.conf.example" \
    "$CONFIG_DIR/routing.conf.example"

# No lock file to create: the lock lives in /run/oscmix-desk, which the
# root step below creates through tmpfiles.d (ADR 0023, 0024), or in the
# per-user runtime directory the unit creates itself. A lock file left by
# an older install beside the config is harmless and stays.


# systemd's user instance belongs to the login session, not to $HOME. It
# reads units from the *session's* home whatever HOME this script was
# given, so an install into a scratch home would enable and restart the
# real user's oscmix.service -- and report "backend is running" about a
# service that is not the one just installed.
#
# `systemctl --user show-environment` reports the session's own HOME, so
# both the home and configuration base must match. Missing identity is
# not evidence that the manager belongs to this installation.
if [ "$MANUAL" = 0 ]; then
    info "installing systemd user service"
    install_file 644 "$PROJECT_DIR/systemd/oscmix.service" "$UNIT_DIR/oscmix.service"
    if manages_this_home; then
        systemctl --user daemon-reload
        if [ "$ENABLE" = 1 ]; then
            systemctl --user enable --quiet oscmix.service
        fi
    else
        warn "unit installed but not enabled: systemd's user instance serves a"
        warn "different home than $HOME. Run --enable from the intended session."
    fi
else
    info "manual foreground installation; no systemd service installed or enabled"
fi

HAS_GTK=0
for directory in "$BIN_DIR" /usr/local/bin /usr/bin; do
    [ ! -x "$directory/oscmix-gtk" ] || HAS_GTK=1
done
if [ "$HAS_GTK" = 1 ]; then
    info "installing desktop entry and icon"
    payload_desktop "$PROJECT_DIR" "$BIN_DIR" "$DATA_DIR"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$DATA_DIR/applications" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t "$DATA_DIR/icons/hicolor" 2>/dev/null || true
    fi
fi

# --------------------------------------------------------------------------
# The steps that need root: the udev rule, the resume hook, and the
# shared lock directory
# --------------------------------------------------------------------------

if [ "$DO_UDEV" = 1 ]; then
    if SUDO=""; [ "$(id -u)" != 0 ]; then SUDO="sudo"; fi
    # SYSTEMD_USER_WANTS can start even a disabled unit. Installing and
    # triggering this rule on a fresh install would therefore write the
    # example desk before its owner had asked for a first apply.
    if [ "$MANUAL" = 0 ] && { [ "$ENABLE" = 1 ] || [ "$WAS_ENABLED" = 1 ]; }; then
    info "installing udev rule (needs root)"
    if $SUDO install -m 644 "$PROJECT_DIR/udev/90-rme-fireface.rules" "$UDEV_RULE" \
        && $SUDO udevadm control --reload-rules; then
        # Apply the ASM4242 host-controller runtime-PM workaround now; the
        # rule will apply automatically on subsequent boots.
        if [ "$ENABLE" = 1 ] || [ "$WAS_ACTIVE" = 1 ]; then
            $SUDO udevadm trigger --subsystem-match=pci \
                --attr-match="vendor=0x1b21" \
                --attr-match="device=0x2426" --action=add 2>/dev/null || true
            $SUDO udevadm trigger --subsystem-match=usb \
                --attr-match="idVendor=$USB_VENDOR" \
                --attr-match="idProduct=$USB_PRODUCT" --action=add 2>/dev/null || true
        fi
    else
        warn "could not install $UDEV_RULE -- hotplug autostart is disabled."
        warn "To finish manually:"
        warn "  sudo install -m 644 udev/90-rme-fireface.rules $UDEV_RULE"
        warn "  sudo udevadm control --reload-rules"
    fi

    # Reconcile after resume. A system-sleep hook and not a user unit:
    # there is no user-level sleep.target to hang one on, checked rather
    # than assumed. It runs `systemctl --user ... reload`, which reaches
    # the session process alone -- signalling the unit kills the backend,
    # measured.
    if [ -d "$(dirname "$SLEEP_HOOK")" ]; then
        if $SUDO install -m 755 "$PROJECT_DIR/systemd/system-sleep/oscmix" \
            "$SLEEP_HOOK"; then
            info "installed resume hook $SLEEP_HOOK"
        else
            warn "could not install $SLEEP_HOOK -- the mixer state will not"
            warn "be reconciled after suspend. To finish manually:"
            warn "  sudo install -m 755 systemd/system-sleep/oscmix $SLEEP_HOOK"
        fi
    else
        warn "no system-sleep directory; skipping the resume hook"
    fi
    else
        info "hotplug and resume activation deferred; use --enable after reviewing the desk"
    fi

    # The lock directory every writer of an interface shares. Without
    # it the path falls back to $XDG_RUNTIME_DIR, which sudo, cron and a
    # bare ssh command do not have -- and a writer that computes a
    # different path does not contend with the holder (ADR 0023).
    info "installing the shared lock directory (needs root)"
    if command -v systemd-tmpfiles >/dev/null 2>&1 \
        && $SUDO install -m 644 "$PROJECT_DIR/systemd/tmpfiles.d/oscmix-desk.conf" \
            "$TMPFILES_CONF" \
        && $SUDO systemd-tmpfiles --create "$TMPFILES_CONF"; then
        info "lock directory: /run/oscmix-desk (group audio)"
        # Only members of `audio` can take the lock; everyone else is
        # refused, the unit included (ADR 0024).
        if ! id -nG "$(id -un)" | tr ' ' '\n' | grep -qx audio; then
            warn "$(id -un) is not in the group audio, so it cannot take the"
            warn "device lock and every start and switch will be refused:"
            warn "  sudo usermod -aG audio $(id -un)"
            warn "then log out of every session, or reboot, so the user"
            warn "manager starts again with the new group"
        fi
    else
        warn "shared locks were not provisioned. Before live use, create the"
        warn "audio group if absent, add the intended operator to it, and run:"
        warn "  sudo install -d -o root -g audio -m 3770 /run/oscmix-desk"
        warn "Arrange that directory creation on every boot (see docs/INSTALLATION.md)."
        warn "Without it, locking is per user and does not exclude other users."
    fi
else
    info "skipping the root steps (--no-udev): no hotplug autostart, no"
    info "reconcile after resume, and the device lock falls back to the"
    info "per-user runtime directory (ADR 0023)"
fi

# --------------------------------------------------------------------------
# Start now if the device is already connected
# --------------------------------------------------------------------------

device_present() {
    local dev
    for dev in "${OSCMIX_SYSFS_USB:-/sys/bus/usb/devices}"/*; do
        [ -f "$dev/idVendor" ] || continue
        [ "$(cat "$dev/idVendor")" = "$USB_VENDOR" ] \
            && [ "$(cat "$dev/idProduct")" = "$USB_PRODUCT" ] && return 0
    done
    return 1
}

if [ "$MANUAL" = 1 ]; then
    info "inspect the desk with oscmix-session --dry-run, then start oscmix-session in a terminal"
elif device_present && ! manages_this_home; then
    info "Fireface detected, but not restarting the backend: systemd's user"
    info "instance serves a different home than $HOME"
elif [ "$WAS_ACTIVE" = 1 ] || { [ "$ENABLE" = 1 ] && device_present; }; then
    info "Fireface detected; (re)starting backend"
    # Under set -e a failed start job would end the installer here, before
    # the lines that say what to do about it.
    systemctl --user restart oscmix.service || true
    sleep 2
    if systemctl --user is-active --quiet oscmix.service; then
        info "backend is running"
    else
        warn "backend did not start; check: journalctl --user -u oscmix.service"
    fi
elif [ "$ENABLE" = 1 ]; then
    info "Fireface not connected; the backend will start automatically on plug-in"
elif [ "$WAS_ENABLED" = 1 ]; then
    info "automatic startup remains enabled; the previously stopped service remains stopped"
else
    info "files installed; automatic operation has not been enabled"
    info "review $CONFIG_DIR/routing.conf and run: $BIN_DIR/oscmix-session --dry-run"
    info "then enable explicitly: ./install.sh --no-build --enable"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in your PATH (the desktop entry works anyway)" ;;
esac

if [ "$DO_BUILD" = 1 ] && [ "$GTK_BUILT" = 0 ]; then
    warn "the GTK mixer was not built; only the headless backend is installed"
fi

info "installation complete."
if [ "$HAS_GTK" = 1 ]; then
    info "The companion mixer opens with 'RME Fireface Mixer'."
fi
info "Routing config: $CONFIG_DIR/routing.conf"
rm -rf "$RUNTIME_PREVIOUS"
