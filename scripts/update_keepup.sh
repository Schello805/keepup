#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

run_as_root() {
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo -n "$@"
    else
      echo "This script requires root privileges for some operations (no sudo available)." >&2
      exit 1
    fi
  else
    "$@"
  fi
}

can_run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

BACKUP_DIR="$ROOT_DIR/backups"
mkdir -p "$BACKUP_DIR"
TIMESTAMP="$(date +%F-%H%M%S)"

ENABLE_BACKUPS="${KEEPUP_ENABLE_BACKUPS:-0}"

run_with_timeout() {
  local seconds="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "${seconds}s" "$@"
  else
    "$@"
  fi
}

if ! command -v timeout >/dev/null 2>&1; then
  echo "[update] 'timeout' not found. Attempting to install coreutils..."
  if command -v apt-get >/dev/null 2>&1; then
    run_as_root apt-get update -y >/dev/null 2>&1 || true
    run_as_root apt-get install -y coreutils >/dev/null 2>&1 || true
  fi
fi

if [ "$ENABLE_BACKUPS" = "1" ]; then
  SERVICE_WAS_ACTIVE=0
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet keepup.service; then
      SERVICE_WAS_ACTIVE=1
      echo "[update] Stopping keepup service for consistent backups"
      run_as_root systemctl stop keepup.service || true
    fi
  fi

  echo "[update] Creating pre-update backups"
  # copy sqlite DB if present (prefer sqlite3 .backup)
  if [ -f "$ROOT_DIR/keepup.db" ]; then
    echo "[update] Backing up SQLite DB"
    if command -v sqlite3 >/dev/null 2>&1; then
      set +e
      run_with_timeout 15 sqlite3 "$ROOT_DIR/keepup.db" "PRAGMA busy_timeout=2000; .backup '$BACKUP_DIR/keepup-db-$TIMESTAMP.db'" 2>/dev/null
      db_backup_rc=$?
      set -e
      if [ $db_backup_rc -eq 0 ]; then
        echo "[update] SQLite backup created: $BACKUP_DIR/keepup-db-$TIMESTAMP.db"
      else
        echo "[update] SQLite backup failed or timed out (rc=$db_backup_rc). Trying file copy fallback."
        set +e
        run_with_timeout 15 cp "$ROOT_DIR/keepup.db" "$BACKUP_DIR/keepup-db-$TIMESTAMP.db" 2>/dev/null
        cp_rc=$?
        set -e
        if [ $cp_rc -eq 0 ]; then
          echo "[update] Copied keepup.db -> $BACKUP_DIR/keepup-db-$TIMESTAMP.db"
        else
          echo "[update] File copy fallback failed (rc=$cp_rc). Skipping DB backup."
        fi
      fi
    else
      set +e
      run_with_timeout 15 cp "$ROOT_DIR/keepup.db" "$BACKUP_DIR/keepup-db-$TIMESTAMP.db" 2>/dev/null
      cp_rc=$?
      set -e
      if [ $cp_rc -eq 0 ]; then
        echo "[update] Copied keepup.db -> $BACKUP_DIR/keepup-db-$TIMESTAMP.db"
      else
        echo "[update] File copy backup failed or timed out (rc=$cp_rc). Skipping DB backup."
      fi
    fi
  fi

  # try to export JSON backup via the package if possible
  if [ -x "$VENV_DIR/bin/python" ]; then
    export_cmd="$VENV_DIR/bin/python -c \"from database import export_backup; import json,sys; print(json.dumps(export_backup()))\""
  else
    export_cmd="python3 -c \"from database import export_backup; import json,sys; print(json.dumps(export_backup()))\""
  fi
  set +e
  if command -v timeout >/dev/null 2>&1; then
    timeout 20s bash -lc "$export_cmd" > "$BACKUP_DIR/keepup-backup-$TIMESTAMP.json" 2>/dev/null
  else
    eval $export_cmd > "$BACKUP_DIR/keepup-backup-$TIMESTAMP.json" 2>/dev/null
  fi
  export_rc=$?
  set -e
  if [ $export_rc -eq 0 ]; then
    echo "[update] JSON backup created: $BACKUP_DIR/keepup-backup-$TIMESTAMP.json"
  else
    echo "[update] JSON backup failed (skipping): export command returned $export_rc"
  fi

  if [ $SERVICE_WAS_ACTIVE -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
    echo "[update] Starting keepup service again"
    run_as_root systemctl start keepup.service || true
  fi
else
  echo "[update] Skipping pre-update backups (set KEEPUP_ENABLE_BACKUPS=1 to enable)"
fi

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

if [ -d "$ROOT_DIR/.git" ]; then
  git -C "$ROOT_DIR" fetch --prune
  git -C "$ROOT_DIR" pull --ff-only
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

"$VENV_DIR/bin/python" -m py_compile "$ROOT_DIR/main.py" "$ROOT_DIR/database.py" "$ROOT_DIR/monitor.py"

echo "[update] Running configuration checks and ensuring service is configured"
if [ -x "$ROOT_DIR/scripts/check_and_configure.sh" ] && can_run_as_root; then
  run_as_root "$ROOT_DIR/scripts/check_and_configure.sh"
elif [ -x "$ROOT_DIR/scripts/check_and_configure.sh" ]; then
  echo "[update] Skipping root-only configuration checks (no passwordless sudo available)."
else
  echo "[update] Warning: check_and_configure.sh not found or not executable"
fi

if command -v systemctl >/dev/null 2>&1 && can_run_as_root; then
  echo "[update] Restarting keepup service"
  run_as_root systemctl restart keepup.service || true
else
  echo "[update] Update completed, but service restart was skipped. Please restart keepup.service manually."
fi

echo "Update finished. Check status: systemctl status keepup.service"
echo "View logs: journalctl -u keepup.service -f"
