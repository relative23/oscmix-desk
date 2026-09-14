#!/usr/bin/env bash
# oscmix-desk installer.
#
# Everything is installed per-user (~/.local, ~/.config); root is only
# needed for the udev hotplug rule. Existing files are backed up before
# being replaced, an existing routing.conf is never touched.
set -euo pipefail

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

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/oscmix"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UDEV_RULE="/etc/udev/rules.d/90-rme-fireface.rules"
SLEEP_HOOK="/usr/lib/systemd/system-sleep/oscmix"
TMPFILES_CONF="/usr/lib/tmpfiles.d/oscmix-desk.conf"

DO_BUILD=1
DO_UDEV=1

usage() {
    cat <<'EOF'
usage: ./install.sh [options]

options:
  --no-build   skip building oscmix (use already installed binaries)
  --no-udev    skip the root steps: the udev rule (no hotplug autostart)
               and the resume hook (no reconcile after suspend)
  -h, --help   show this help

environment:
  OSCMIX_REPO  oscmix git repository (default: upstream on GitHub)
  OSCMIX_REF   git ref to build (default: master)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-build) DO_BUILD=0 ;;
        --no-udev) DO_UDEV=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

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

require() {
    command -v "$1" >/dev/null 2>&1 || fail "missing dependency: $1 ($2)"
}

# --------------------------------------------------------------------------
# Preflight checks
# --------------------------------------------------------------------------

require python3 "needed by oscmix-session"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "python3 >= 3.9 required"

if ! systemctl --user show-environment >/dev/null 2>&1; then
    fail "cannot talk to the systemd user instance (is this a desktop session?)"
fi

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

    info "building oscmix ($GTK_FLAG)"
    make -C "$BUILD_DIR" "$GTK_FLAG" >/dev/null

    install_file 755 "$BUILD_DIR/oscmix" "$BIN_DIR/oscmix"
    install_file 755 "$BUILD_DIR/alsaseqio" "$BIN_DIR/alsaseqio"
    if [ -x "$BUILD_DIR/gtk/oscmix-gtk" ]; then
        GTK_BUILT=1
        install_file 755 "$BUILD_DIR/gtk/oscmix-gtk" "$BIN_DIR/oscmix-gtk"
        # oscmix-gtk aborts without its GSettings schema.
        install_file 644 "$BUILD_DIR/gtk/oscmix.gschema.xml" \
            "$DATA_DIR/glib-2.0/schemas/oscmix.gschema.xml"
        glib-compile-schemas "$DATA_DIR/glib-2.0/schemas"
    fi
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
rm -rf "$LIB_DIR/oscmix_desk"
mkdir -p "$LIB_DIR/oscmix_desk"
for module in "$PROJECT_DIR/src/oscmix_desk/"*.py; do
    install -m 644 "$module" "$LIB_DIR/oscmix_desk/$(basename "$module")"
done

info "installing scripts to $BIN_DIR"
install_file 755 "$PROJECT_DIR/bin/oscmix-session" "$BIN_DIR/oscmix-session"
install_file 755 "$PROJECT_DIR/bin/oscmix-launch" "$BIN_DIR/oscmix-launch"

if [ ! -e "$CONFIG_DIR/routing.conf" ]; then
    info "installing default config to $CONFIG_DIR/routing.conf"
    install -D -m 644 "$PROJECT_DIR/config/routing.conf.example" \
        "$CONFIG_DIR/routing.conf"
else
    info "keeping existing $CONFIG_DIR/routing.conf"
fi
install -D -m 644 "$PROJECT_DIR/config/routing.conf.example" \
    "$CONFIG_DIR/routing.conf.example"

# No lock file to create since 0.6.7: it lives in $XDG_RUNTIME_DIR,
# keyed by the interface, and the unit creates it through
# RuntimeDirectory= (ADR 0022). A leftover from an older install is
# harmless and is left where it is.


# systemd's user instance belongs to the login session, not to $HOME. It
# reads units from the *session's* home whatever HOME this script was
# given, so an install into a scratch home would enable and restart the
# real user's oscmix.service -- and report "backend is running" about a
# service that is not the one just installed.
#
# `systemctl --user show-environment` reports the session's own HOME, so
# the two can be compared. When it reports nothing this proceeds, which
# is what every earlier version did.
manages_this_home() {
    local session_home
    session_home="$(systemctl --user show-environment 2>/dev/null |
                    sed -n 's/^HOME=//p')" || true
    [ -z "$session_home" ] || [ "$session_home" = "$HOME" ]
}

info "installing systemd user service"
install_file 644 "$PROJECT_DIR/systemd/oscmix.service" "$UNIT_DIR/oscmix.service"
if manages_this_home; then
    systemctl --user daemon-reload
    systemctl --user enable --quiet oscmix.service
else
    warn "unit installed but not enabled: systemd's user instance serves a"
    warn "different home than $HOME, so enabling it would arm somebody"
    warn "else's service. Enable it from that session with:"
    warn "  systemctl --user enable --now oscmix.service"
fi

info "installing desktop entry and icon"
install_file 644 "$PROJECT_DIR/desktop/oscmix.svg" \
    "$DATA_DIR/icons/hicolor/scalable/apps/oscmix.svg"
# Desktop files cannot rely on PATH containing ~/.local/bin.
DESKTOP_TMP="$(mktemp)"
sed "s|^Exec=.*|Exec=$BIN_DIR/oscmix-launch|" \
    "$PROJECT_DIR/desktop/oscmix-gtk.desktop" > "$DESKTOP_TMP"
install_file 644 "$DESKTOP_TMP" "$DATA_DIR/applications/oscmix-gtk.desktop"
rm -f "$DESKTOP_TMP"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DATA_DIR/applications" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$DATA_DIR/icons/hicolor" 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# The steps that need root: the udev rule, and the resume hook
# --------------------------------------------------------------------------

if [ "$DO_UDEV" = 1 ]; then
    info "installing udev rule (needs root)"
    if SUDO=""; [ "$(id -u)" != 0 ]; then SUDO="sudo"; fi
    if $SUDO install -m 644 "$PROJECT_DIR/udev/90-rme-fireface.rules" "$UDEV_RULE" \
        && $SUDO udevadm control --reload-rules; then
        # Apply the ASM4242 host-controller runtime-PM workaround now; the
        # rule will apply automatically on subsequent boots.
        $SUDO udevadm trigger --subsystem-match=pci \
            --attr-match="vendor=0x1b21" \
            --attr-match="device=0x2426" --action=add 2>/dev/null || true
        $SUDO udevadm trigger --subsystem-match=usb \
            --attr-match="idVendor=$USB_VENDOR" \
            --attr-match="idProduct=$USB_PRODUCT" --action=add 2>/dev/null || true
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

    # The lock directory every writer of an interface shares. Without
    # it the path falls back to $XDG_RUNTIME_DIR, which sudo, cron and a
    # bare ssh command do not have -- and a writer that computes a
    # different path does not contend with the holder (ADR 0023).
    info "installing the shared lock directory (needs root)"
    if $SUDO install -m 644 "$PROJECT_DIR/systemd/tmpfiles.d/oscmix-desk.conf" \
            "$TMPFILES_CONF" \
        && $SUDO systemd-tmpfiles --create "$TMPFILES_CONF"; then
        info "lock directory: /run/oscmix-desk (group audio)"
        # Only members of `audio` can take the lock; everyone else is
        # refused, the unit included (ADR 0024).
        if ! id -nG "$(id -un)" | tr ' ' '\n' | grep -qx audio; then
            warn "$(id -un) is not in the group audio, so it cannot take the"
            warn "device lock and every start and switch will be refused:"
            warn "  sudo usermod -aG audio $(id -un)   (then log in again)"
        fi
    else
        warn "could not install $TMPFILES_CONF; run these by hand:"
        warn "  sudo install -m 644 systemd/tmpfiles.d/oscmix-desk.conf $TMPFILES_CONF"
        warn "  sudo systemd-tmpfiles --create $TMPFILES_CONF"
    fi
else
    info "skipping the root steps (--no-udev): no hotplug autostart, no"
    info "reconcile after resume"
fi

# --------------------------------------------------------------------------
# Start now if the device is already connected
# --------------------------------------------------------------------------

device_present() {
    local dev
    for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idVendor" ] || continue
        [ "$(cat "$dev/idVendor")" = "$USB_VENDOR" ] \
            && [ "$(cat "$dev/idProduct")" = "$USB_PRODUCT" ] && return 0
    done
    return 1
}

if device_present && ! manages_this_home; then
    info "Fireface detected, but not restarting the backend: systemd's user"
    info "instance serves a different home than $HOME"
elif device_present; then
    info "Fireface detected; (re)starting backend"
    systemctl --user restart oscmix.service
    sleep 2
    if systemctl --user is-active --quiet oscmix.service; then
        info "backend is running"
    else
        warn "backend did not start; check: journalctl --user -u oscmix.service"
    fi
else
    info "Fireface not connected; the backend will start automatically on plug-in"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in your PATH (the desktop entry works anyway)" ;;
esac

if [ "$DO_BUILD" = 1 ] && [ "$GTK_BUILT" = 0 ]; then
    warn "the GTK mixer was not built; only the headless backend is installed"
fi

info "done. Open 'RME Fireface Mixer' from your app menu."
info "Routing config: $CONFIG_DIR/routing.conf"
