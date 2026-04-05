#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
KEEPUP_USER="keepup"

echo "[install] Starting KeepUp initial installation"

if ! id -u "$KEEPUP_USER" >/dev/null 2>&1; then
  echo "[install] Creating system user '$KEEPUP_USER'"
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$KEEPUP_USER"
fi

echo "[install] Creating virtual environment (if missing)"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

echo "[install] Installing Python dependencies"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

echo "[install] Setting ownership to $KEEPUP_USER"
sudo chown -R "$KEEPUP_USER":"$KEEPUP_USER" "$ROOT_DIR"

echo "[install] Running configuration and service setup"
sudo "$ROOT_DIR/scripts/check_and_configure.sh"

echo "[install] Installation complete. Use: sudo journalctl -u keepup.service -f"
