#!/usr/bin/env bash
# Runs only inside a disposable qualification container, as an ordinary
# user, without sound devices or a host user bus. See the recorded matrix.
set -euo pipefail
[ "${OSCMIX_QUALIFY_CONTAINER:-}" = 1 ] \
    || { echo "run in the qualification container, not on an audio workstation" >&2; exit 2; }
[ "$(id -u)" != 0 ] || { echo "qualification must run as a non-root user" >&2; exit 2; }
cat /etc/os-release
python3 --version
cc --version
pkg-config --modversion alsa
empty_hardware="$(mktemp -d)"
mkdir -p "$empty_hardware/proc/asound/seq" "$empty_hardware/usb"
touch "$empty_hardware/proc/asound/cards" "$empty_hardware/proc/asound/seq/clients"
export OSCMIX_SYSFS_USB="$empty_hardware/usb" OSCMIX_PROC_ROOT="$empty_hardware/proc"
./install.sh --check --manual
./install.sh --manual --no-udev
"$HOME/.local/bin/oscmix-session" --version
"$HOME/.local/bin/oscmix-session" --dry-run --timeout 0
python3 -m pytest -q tests/test_install_sh.py tests/test_install_modes.py \
    tests/test_numeric.py tests/test_dump_limits.py tests/test_session_integration.py \
    tests/test_two_processes.py tests/test_status.py tests/test_diagnostics.py \
    tests/test_launcher.py tests/test_preview.py
stage="$(mktemp -d)"
bash scripts/stage-install.sh --destdir "$stage" --backend build/oscmix
"$stage/usr/bin/oscmix-session" --version
"$stage/usr/bin/oscmix-session" --dry-run --timeout 0
./uninstall.sh
test ! -f "$HOME/.local/bin/oscmix-session"
test -f "$HOME/.config/oscmix/routing.conf"
echo "SOURCE AND STAGED PAYLOAD QUALIFICATION PASSED"
