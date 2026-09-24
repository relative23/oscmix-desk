#!/usr/bin/env bash
# Assemble a package payload without installing into the host or starting
# any service. Backend binaries are built separately from the pinned source.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTDIR=""
BACKEND=""
WITH_GTK=no
while [ $# -gt 0 ]; do
    case "$1" in
        --destdir) DESTDIR="${2:?--destdir needs a directory}"; shift ;;
        --backend) BACKEND="${2:?--backend needs a directory}"; shift ;;
        --with-gtk) WITH_GTK=yes ;;
        -h|--help)
            echo "usage: scripts/stage-install.sh --destdir ABSOLUTE_EMPTY_DIR --backend BUILT_SOURCE [--with-gtk]"
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
case "$DESTDIR" in
    /*) ;;
    *) echo "--destdir must name an absolute, empty staging directory" >&2; exit 2 ;;
esac
[ "$DESTDIR" != / ] || { echo "refusing to stage into /" >&2; exit 2; }
if [ -d "$DESTDIR" ] && [ -n "$(find "$DESTDIR" -mindepth 1 -print -quit)" ]; then
    echo "staging directory is not empty: $DESTDIR" >&2
    exit 2
fi
for executable in oscmix alsaseqio; do
    [ -x "$BACKEND/$executable" ] || { echo "missing built $BACKEND/$executable" >&2; exit 2; }
done
if [ "$WITH_GTK" = yes ]; then
    if [ ! -x "$BACKEND/gtk/oscmix-gtk" ] || [ ! -f "$BACKEND/gtk/oscmix.gschema.xml" ]; then
        echo "--with-gtk needs the built GTK companion and its schema" >&2
        exit 2
    fi
fi
install_file() { install -D -m "$1" "$2" "$3"; }
# shellcheck source=scripts/install-payload.sh
source "$PROJECT_DIR/scripts/install-payload.sh"
payload_runtime "$PROJECT_DIR" "$DESTDIR/usr/lib/oscmix-desk/oscmix_desk"
payload_entry_points "$PROJECT_DIR" "$DESTDIR/usr/bin"
install_file 755 "$PROJECT_DIR/packaging/oscmix-setup" "$DESTDIR/usr/bin/oscmix-setup"
install_file 755 "$PROJECT_DIR/packaging/package-guard" \
    "$DESTDIR/usr/lib/oscmix-desk/package-guard"
payload_backend "$BACKEND" "$DESTDIR/usr/bin" "$DESTDIR/usr/share" "$WITH_GTK"
if [ "$WITH_GTK" = yes ]; then
    payload_desktop "$PROJECT_DIR" /usr/bin "$DESTDIR/usr/share"
fi
install_file 644 "$PROJECT_DIR/config/routing.conf.example" \
    "$DESTDIR/usr/share/oscmix-desk/routing.conf.example"
install_file 644 "$PROJECT_DIR/LICENSE" "$DESTDIR/usr/share/licenses/oscmix-desk/LICENSE"
install_file 644 "$BACKEND/LICENSE" "$DESTDIR/usr/share/licenses/oscmix-desk/oscmix-LICENSE"
install_file 644 "$PROJECT_DIR/README.md" "$DESTDIR/usr/share/doc/oscmix-desk/README.md"
install_file 644 "$PROJECT_DIR/docs/INSTALLATION.md" \
    "$DESTDIR/usr/share/doc/oscmix-desk/INSTALLATION.md"
install_file 644 "$PROJECT_DIR/systemd/tmpfiles.d/oscmix-desk.conf" \
    "$DESTDIR/usr/lib/tmpfiles.d/oscmix-desk.conf"
install_file 644 "$PROJECT_DIR/udev/90-rme-fireface.rules" \
    "$DESTDIR/usr/lib/udev/rules.d/90-rme-fireface.rules"
install_file 755 "$PROJECT_DIR/systemd/system-sleep/oscmix" \
    "$DESTDIR/usr/lib/systemd/system-sleep/oscmix"
mkdir -p "$DESTDIR/usr/lib/systemd/user"
# The vendor service requires an explicit per-user opt-in. Merely
# installing a package (or having a routing.conf from another install)
# must not start a desk through SYSTEMD_USER_WANTS.
sed -e 's|^ExecStart=.*|ExecStart=/usr/bin/oscmix-session|' \
    -e '/^Description=/a ConditionPathExists=%E/oscmix/service-allowed' \
    -e '/^Description=/a ConditionPathExists=!/var/lib/oscmix-desk/package-update' \
    "$PROJECT_DIR/systemd/oscmix.service" > "$DESTDIR/usr/lib/systemd/user/oscmix.service"
echo "staged package payload in $DESTDIR; no host integration activated"
