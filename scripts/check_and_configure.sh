#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
SERVICE_PATH="/etc/systemd/system/keepup.service"
KEEPUP_USER="keepup"

run_as_root() {
  if [ "$(id -u)" -ne 0 ]; then
    sudo "$@"
  else
    "$@"
  fi
}

echo "[keepup] Checking virtualenv..."
if [ ! -d "$VENV_DIR" ]; then
  echo "- virtualenv not found, creating..."
  run_as_root python3 -m venv "$VENV_DIR"
fi

echo "[keepup] Installing/ensuring Python dependencies..."
run_as_root "$VENV_DIR/bin/python" -m pip install --upgrade pip
run_as_root "$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

echo "[keepup] Byte-compiling Python files..."
run_as_root "$VENV_DIR/bin/python" -m py_compile "$ROOT_DIR/main.py" "$ROOT_DIR/monitor.py" "$ROOT_DIR/database.py" || true

echo "[keepup] Ensuring system user '$KEEPUP_USER' exists..."
if ! id -u "$KEEPUP_USER" >/dev/null 2>&1; then
  echo "- user '$KEEPUP_USER' does not exist, creating system user"
  run_as_root useradd --system --no-create-home --shell /usr/sbin/nologin "$KEEPUP_USER" || true
fi

echo "[keepup] Setting ownership of project files to $KEEPUP_USER..."
run_as_root chown -R "$KEEPUP_USER":"$KEEPUP_USER" "$ROOT_DIR"

echo "[keepup] Ensuring parent directory is accessible to $KEEPUP_USER..."
PARENT_DIR="$(dirname "$ROOT_DIR")"
if [ -d "$PARENT_DIR" ]; then
  # Add execute permission for others on parent dir so keepup user can access subdirs
  run_as_root chmod o+rx "$PARENT_DIR" || true
fi

echo "[keepup] Creating/updating systemd unit at $SERVICE_PATH"
run_as_root tee "$SERVICE_PATH" > /dev/null <<EOF
[Unit]
Description=KeepUp monitoring service
After=network.target

[Service]
Type=simple
User=$KEEPUP_USER
Group=$KEEPUP_USER
WorkingDirectory=$ROOT_DIR
ExecStart=$VENV_DIR/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo "[keepup] Reloading systemd and enabling service"
run_as_root systemctl daemon-reload
run_as_root systemctl enable --now keepup.service

echo "[keepup] Checking service status"
run_as_root systemctl is-active --quiet keepup.service && echo "Service keepup is active" || (echo "Service keepup is not active. See 'sudo journalctl -u keepup.service -n 200' for details." && exit 1)

echo "[keepup] Check and configuration complete."
