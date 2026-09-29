#!/usr/bin/env bash
# oscmix-desk uninstaller. Removes everything install.sh created.
# The routing config is kept unless --purge is given.
set -euo pipefail

# Every path below hangs off HOME. An empty one would remove files from /.local
# and /.config, a relative one the working directory's, and `set -u`
# catches neither.
case "${HOME:-}" in
    /*) ;;
    *)  echo "uninstall.sh: HOME must be an absolute path, not '${HOME:-}'" >&2
        exit 2 ;;
esac

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
RESUME_UNIT="${OSCMIX_RESUME_UNIT:-/usr/lib/systemd/system/oscmix-resume.service}"
TMPFILES_CONF="${OSCMIX_TMPFILES_CONF:-/usr/lib/tmpfiles.d/oscmix-desk.conf}"
# Present exactly when a native package is installed (install-preflight
# checks the same file).
PACKAGE_GUARD="${OSCMIX_PACKAGE_GUARD:-/usr/lib/oscmix-desk/package-guard}"

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
# the two can be compared: `this` when it serves this HOME and config base,
# `other` when it serves another, `none` when no user manager answers (ssh
# without lingering, a container). Until 0.8.1 `none` was treated as
# `other`, which left the root files and a dangling enable link behind.
manager_state() {
    local environment session_home session_config
    if ! environment="$(systemctl --user show-environment 2>/dev/null)"; then
        echo none
        return
    fi
    session_home="$(printf '%s\n' "$environment" | sed -n 's/^HOME=//p')"
    if [ -z "$session_home" ]; then
        echo none
        return
    fi
    session_config="$(printf '%s\n' "$environment" | sed -n 's/^XDG_CONFIG_HOME=//p')"
    session_config="$(xdg_base "$session_config" "$session_home/.config")"
    if [ "$session_home" = "$HOME" ] && [ "$session_config" = "$CONFIG_HOME" ]; then
        echo this
    elif [ "$session_home" = "$HOME" ]; then
        echo other-config
    else
        echo other
    fi
}

# Without a manager to ask, the account database says whose home this is:
# a scratch HOME is nobody's, and its uninstall leaves the system files of
# the real one alone. Overridable for the test suite only.
account_home() {
    if [ -n "${OSCMIX_ACCOUNT_HOME:-}" ]; then
        printf '%s\n' "$OSCMIX_ACCOUNT_HOME"
    else
        getent passwd "$(id -un)" 2>/dev/null | cut -d: -f6
    fi
}

MANAGER="$(manager_state)"
OWNS_SYSTEM_FILES=0
if [ "$MANAGER" = this ] || { [ "$MANAGER" = none ] && [ "$(account_home)" = "$HOME" ]; }; then
    OWNS_SYSTEM_FILES=1
fi

# With the same HOME but another config base, removing .local/lib would
# remove the very runtime the unrelated desk may still be using.
if [ "$MANAGER" = other-config ]; then
    warn "systemd's user manager uses another XDG_CONFIG_HOME; no files changed"
    exit 1
fi

# The package owns the root files and the service this script would stop,
# disable and remove. Its own migration moves the per-user files aside.
if [ -e "$PACKAGE_GUARD" ]; then
    warn "an oscmix-desk package is installed. uninstall.sh removes a source"
    warn "installation and would take the package's system files and service with"
    warn "it. Run oscmix-setup --migrate-source to move the per-user files aside, or"
    warn "remove the package with its package manager. No files changed."
    exit 1
fi

if [ -f /etc/oscmix-desk/service.json ]; then
    python3 - "$HOME" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads(Path('/etc/oscmix-desk/service.json').read_text())
if record['home'] == sys.argv[1]:
    raise SystemExit('Disable and remove the registered host service with oscmix-service '
                     'before uninstalling its runtime. No files removed.')
PY
fi

if [ "$MANAGER" = this ]; then
    info "stopping and disabling oscmix.service"
    systemctl --user stop oscmix.service 2>/dev/null || true
    systemctl --user disable --quiet oscmix.service 2>/dev/null || true
elif [ "$MANAGER" = none ]; then
    # Nothing can be running under a manager that is not there; what
    # `disable` would have removed is the enable links.
    info "no user manager answers; removing oscmix.service's enable links"
    for link in "$UNIT_DIR"/*.wants/oscmix.service; do
        if [ -L "$link" ]; then
            rm -f "$link"
        fi
    done
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
      "$DATA_DIR/oscmix-desk/backend-source.json" \
      "$DATA_DIR/glib-2.0/schemas/oscmix.gschema.xml"
if [ -d "$DATA_DIR/glib-2.0/schemas" ]; then
    glib-compile-schemas "$DATA_DIR/glib-2.0/schemas" 2>/dev/null || true
fi
systemctl --user daemon-reload 2>/dev/null || true   # no user bus over ssh without linger

if [ -e "$UDEV_RULE" ] || [ -e "$SLEEP_HOOK" ] || [ -e "$RESUME_UNIT" ] || [ -e "$TMPFILES_CONF" ]; then
  if [ "$OWNS_SYSTEM_FILES" != 1 ]; then
    # The system files serve whichever installation the session's home
    # has. Removing them from a scratch home took away the real desk's
    # hotplug rule, resume hook and lock directory -- the same trap as
    # the service above, one level down.
    warn "not removing $UDEV_RULE, $SLEEP_HOOK, $RESUME_UNIT or $TMPFILES_CONF:"
    if [ "$MANAGER" = none ]; then
        warn "no user manager answers, and $HOME is not this account's home."
    else
        warn "they serve the installation in systemd's session home, not $HOME."
    fi
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
    if [ -e "$RESUME_UNIT" ]; then
        $SUDO systemctl stop oscmix-resume.service \
            || echo "warning: could not stop pending resume work" >&2
        $SUDO rm -f "$RESUME_UNIT" && $SUDO systemctl daemon-reload \
            || echo "warning: remove $RESUME_UNIT and reload the system manager manually" >&2
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
