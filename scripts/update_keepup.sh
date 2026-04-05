#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

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

echo "Update finished. If running via systemd: sudo systemctl restart keepup"
