#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
KEEPUP_USER="keepup"

echo "[install] Starting KeepUp initial installation"

run_as_root() {
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo "$@"
    else
      echo "This script requires root privileges (no sudo available). Please run as root." >&2
      exit 1
    fi
  else
    "$@"
  fi
}

if ! id -u "$KEEPUP_USER" >/dev/null 2>&1; then
  echo "[install] Creating system user '$KEEPUP_USER'"
  run_as_root useradd --system --no-create-home --shell /usr/sbin/nologin "$KEEPUP_USER"
fi

echo "[install] Creating virtual environment (if missing)"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

echo "[install] Installing Python dependencies"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

echo "[install] Setting ownership to $KEEPUP_USER"
run_as_root chown -R "$KEEPUP_USER":"$KEEPUP_USER" "$ROOT_DIR"

echo "[install] Running configuration and service setup"
run_as_root "$ROOT_DIR/scripts/check_and_configure.sh"

echo "[install] Installation complete."
echo ""

# Show post-install info
if [ -x "$ROOT_DIR/scripts/post_install_info.sh" ]; then
  "$ROOT_DIR/scripts/post_install_info.sh"
else
  echo "[install] Warning: post_install_info.sh not found or not executable"
fi
