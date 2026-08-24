#!/usr/bin/env bash

set -Eeuo pipefail
umask 0027

APP_ROOT=/opt/ppt-web
DATA_DIR="$APP_ROOT/data"
BACKUP_ROOT="$APP_ROOT/backups/daily"
LOCK_FILE="$APP_ROOT/backups/.backup.lock"
STAMP="$(date -u '+%Y%m%d-%H%M%S')"
TARGET="$BACKUP_ROOT/$STAMP"

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

install -d -o root -g ppt-web -m 0750 "$BACKUP_ROOT"
install -d -o root -g ppt-web -m 0750 "$TARGET"

if [[ -f "$DATA_DIR/app.db" ]]; then
  python3 - "$DATA_DIR/app.db" "$TARGET/app.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY
fi

archive_items=()
for item in library uploads outputs; do
  [[ -d "$DATA_DIR/$item" ]] && archive_items+=("$item")
done
if (( ${#archive_items[@]} )); then
  tar --ignore-failed-read --warning=no-file-changed \
    -C "$DATA_DIR" -czf "$TARGET/files.tar.gz" "${archive_items[@]}"
fi

chown -R root:ppt-web "$TARGET"
chmod -R g+rX,o-rwx "$TARGET"
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +14 \
  -exec rm -rf -- {} +
