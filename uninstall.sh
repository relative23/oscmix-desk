#!/usr/bin/env bash
# oscmix-desk uninstaller. Removes everything install.sh created.
# The routing config is kept unless --purge is given.
set -euo pipefail

# Every path below hangs off HOME. An empty one would install into /.local
# and /.config, and `set -u` does not catch empty.
if [ -z "${HOME:-}" ]; then
    echo "uninstall.sh: HOME is not set" >&2
    exit 2
fi

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
# files on a developer machine.
UDEV_RULE="${OSCMIX_UDEV_RULE:-/etc/udev/rules.d/90-rme-fireface.rules}"
SLEEP_HOOK="${OSCMIX_SLEEP_HOOK:-/usr/lib/systemd/system-sleep/oscmix}"
TMPFILES_CONF="${OSCMIX_TMPFILES_CONF:-/usr/lib/tmpfiles.d/oscmix-desk.conf}"

PURGE=0
case "${1:-}" in
    --purge) PURGE=1 ;;
    -h|--help) echo "usage: ./uninstall.sh [--purge]"; exit 0 ;;
    "") ;;
    *) echo "uninstall.sh: unknown option: $1" >&2; exit 2 ;;
esac

info() { printf '\033[1;34m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }

# systemd's user instance belongs to the login session, not to $HOME. It
# reads units from the *session's* home whatever HOME this script was
# given, so installing or uninstalling into a scratch home would enable,
# restart, stop or disable the real user's oscmix.service. That is not
# hypothetical: it happened while running the release checklist, and the
# desk stopped being managed until `systemctl --user enable --now` put it
# back.
#
# `systemctl --user show-environment` reports the session's own HOME, so
# the two can be compared. When it reports nothing -- an unusual systemd,
# or none -- this proceeds, which is what every earlier version did.
manages_this_home() {
    local session_home
    session_home="$(systemctl --user show-environment 2>/dev/null |
                    sed -n 's/^HOME=//p')" || true
    [ -z "$session_home" ] || [ "$session_home" = "$HOME" ]
}

if manages_this_home; then
    info "stopping and disabling oscmix.service"
    systemctl --user stop oscmix.service 2>/dev/null || true
    systemctl --user disable --quiet oscmix.service 2>/dev/null || true
else
    warn "not touching oscmix.service: systemd's user instance serves a"
    warn "different home than $HOME, and stopping it would take down a"
    warn "running desk this uninstall was never asked to touch."
fi

info "removing installed files"
rm -rf "$LIB_DIR" "$LEGACY_LIB_DIR"
rm -f "$UNIT_DIR/oscmix.service" \
      "$BIN_DIR/oscmix-session" \
      "$BIN_DIR/oscmix-launch" \
      "$BIN_DIR/oscmix" \
      "$BIN_DIR/oscmix-gtk" \
      "$BIN_DIR/alsaseqio" \
      "$DATA_DIR/applications/oscmix-gtk.desktop" \
      "$DATA_DIR/icons/hicolor/scalable/apps/oscmix.svg" \
      "$DATA_DIR/glib-2.0/schemas/oscmix.gschema.xml"
if [ -d "$DATA_DIR/glib-2.0/schemas" ]; then
    glib-compile-schemas "$DATA_DIR/glib-2.0/schemas" 2>/dev/null || true
fi
systemctl --user daemon-reload 2>/dev/null || true   # no user bus over ssh without linger

if [ -e "$UDEV_RULE" ] || [ -e "$SLEEP_HOOK" ] || [ -e "$TMPFILES_CONF" ]; then
  if ! manages_this_home; then
    # The system files serve whichever installation the session's home
    # has. Removing them from a scratch home took away the real desk's
    # hotplug rule, resume hook and lock directory -- the same trap as
    # the service above, one level down.
    warn "not removing $UDEV_RULE, $SLEEP_HOOK or $TMPFILES_CONF:"
    warn "they serve the installation in systemd's session home, not $HOME."
  else
    info "removing the root-installed files (needs root)"
    if SUDO=""; [ "$(id -u)" != 0 ]; then SUDO="sudo"; fi
    if [ -e "$UDEV_RULE" ]; then
        $SUDO rm -f "$UDEV_RULE" && $SUDO udevadm control --reload-rules \
            || echo "warning: remove $UDEV_RULE manually" >&2
    fi
    # Left behind, this fires on every wake for a service that is gone.
    if [ -e "$SLEEP_HOOK" ]; then
        $SUDO rm -f "$SLEEP_HOOK" \
            || echo "warning: remove $SLEEP_HOOK manually" >&2
    fi
    if [ -f "$TMPFILES_CONF" ]; then
        # The directory itself lives on tmpfs and goes with the next
        # boot; removing it here would pull it out from under a writer
        # that is still holding a lock in it.
        $SUDO rm -f "$TMPFILES_CONF" \
            || echo "warning: remove $TMPFILES_CONF manually" >&2
    fi
  fi
fi

if [ "$PURGE" = 1 ]; then
    info "removing configuration ($CONFIG_DIR)"
    rm -rf "$CONFIG_DIR"
else
    info "keeping configuration in $CONFIG_DIR (use --purge to remove)"
fi

info "done"
