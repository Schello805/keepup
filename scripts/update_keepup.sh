#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

run_as_root() {
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo "$@"
    else
      echo "This script requires root privileges for some operations (no sudo available)." >&2
      exit 1
    fi
  else
    "$@"
  fi
}

BACKUP_DIR="$ROOT_DIR/backups"
mkdir -p "$BACKUP_DIR"
TIMESTAMP="$(date +%F-%H%M%S)"

echo "[update] Creating pre-update backups"
# copy sqlite DB if present
if [ -f "$ROOT_DIR/keepup.db" ]; then
  cp "$ROOT_DIR/keepup.db" "$BACKUP_DIR/keepup-db-$TIMESTAMP.db" || true
  echo "[update] Copied keepup.db -> $BACKUP_DIR/keepup-db-$TIMESTAMP.db"
fi

# try to export JSON backup via the package if possible
if [ -x "$VENV_DIR/bin/python" ]; then
  export_cmd="$VENV_DIR/bin/python -c \"from database import export_backup; import json,sys; print(json.dumps(export_backup()))\""
else
  export_cmd="python3 -c \"from database import export_backup; import json,sys; print(json.dumps(export_backup()))\""
fi
set +e
eval $export_cmd > "$BACKUP_DIR/keepup-backup-$TIMESTAMP.json" 2>/dev/null
export_rc=$?
set -e
if [ $export_rc -eq 0 ]; then
  echo "[update] JSON backup created: $BACKUP_DIR/keepup-backup-$TIMESTAMP.json"
else
  echo "[update] JSON backup failed (skipping): export command returned $export_rc"
fi

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

if [ -d "$ROOT_DIR/.git" ]; then
  git -C "$ROOT_DIR" fetch --prune
  git -C "$ROOT_DIR" pull --ff-only
fi

"$VENV_DIR/bin/python" -m py_compile "$ROOT_DIR/main.py" "$ROOT_DIR/database.py" "$ROOT_DIR/monitor.py"

echo "[update] Running configuration checks and ensuring service is configured"
if [ -x "$ROOT_DIR/scripts/check_and_configure.sh" ]; then
  run_as_root "$ROOT_DIR/scripts/check_and_configure.sh"
else
  echo "[update] Warning: check_and_configure.sh not found or not executable"
fi

echo "[update] Restarting keepup service"
run_as_root systemctl restart keepup.service || true

echo "Update finished. Check status: systemctl status keepup.service"
echo "View logs: journalctl -u keepup.service -f"
