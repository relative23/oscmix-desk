#!/usr/bin/env bash
# Shared file selection for per-user installation and package staging.
# The caller supplies install_file MODE SOURCE TARGET (backup-aware for
# the source installer, plain install -D for an empty package stage).

payload_runtime() {
    local project="$1" destination="$2" module
    mkdir -p "$destination"
    for module in "$project/src/oscmix_desk/"*.py; do
        install_file 644 "$module" "$destination/$(basename "$module")"
    done
}

payload_entry_points() {
    local project="$1" bin_dir="$2" entry
    for entry in oscmix-session oscmix-launch; do
        install_file 755 "$project/bin/$entry" "$bin_dir/$entry"
    done
}

payload_backend() {
    local backend="$1" bin_dir="$2" data_dir="$3" with_gtk="${4:-yes}"
    install_file 755 "$backend/oscmix" "$bin_dir/oscmix"
    install_file 755 "$backend/alsaseqio" "$bin_dir/alsaseqio"
    if [ "$with_gtk" = yes ] && [ -x "$backend/gtk/oscmix-gtk" ]; then
        payload_gtk "$backend" "$bin_dir" "$data_dir"
    fi
}

payload_gtk() {
    local backend="$1" bin_dir="$2" data_dir="$3"
    install_file 755 "$backend/gtk/oscmix-gtk" "$bin_dir/oscmix-gtk"
    install_file 644 "$backend/gtk/oscmix.gschema.xml" \
        "$data_dir/glib-2.0/schemas/oscmix.gschema.xml"
}

payload_desktop() {
    local project="$1" bin_dir="$2" data_dir="$3" temporary
    install_file 644 "$project/desktop/oscmix.svg" \
        "$data_dir/icons/hicolor/scalable/apps/oscmix.svg"
    temporary="$(mktemp)"
    # Desktop values and Exec arguments have separate escaping rules.
    # The literal path must survive both, including spaces in HOME.
    python3 - "$project/desktop/oscmix-gtk.desktop" "$temporary" "$bin_dir/oscmix-launch" <<'PY'
import sys
from pathlib import Path
argument = '"' + ''.join('\\' + ch if ch in '\\"`$' else ch for ch in sys.argv[3]) + '"'
value = argument.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
lines = Path(sys.argv[1]).read_text().splitlines()
Path(sys.argv[2]).write_text('\n'.join('Exec=' + value if line.startswith('Exec=') else line
                                    for line in lines) + '\n')
PY
    install_file 644 "$temporary" "$data_dir/applications/oscmix-gtk.desktop"
    rm -f "$temporary"
}
