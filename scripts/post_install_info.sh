#!/usr/bin/env bash
# Display installation and status info after setup

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
KEEPUP_PORT=8000
KEEPUP_USER="keepup"

echo ""
echo "==========================================="
echo "KeepUp Installation / Update Summary"
echo "==========================================="
echo ""

# Try to get local IP
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")

echo "📍 Access Information:"
echo "   URL: http://${LOCAL_IP}:${KEEPUP_PORT}"
echo "   Localhost: http://127.0.0.1:${KEEPUP_PORT}"
echo ""

# Check if port is listening
if command -v ss >/dev/null 2>&1; then
  if ss -ltnp 2>/dev/null | grep -q ":${KEEPUP_PORT}"; then
    echo "✓ Port ${KEEPUP_PORT} is listening"
  else
    echo "⚠ Port ${KEEPUP_PORT} is NOT listening (service may be starting)"
  fi
elif command -v netstat >/dev/null 2>&1; then
  if netstat -ltn 2>/dev/null | grep -q ":${KEEPUP_PORT}"; then
    echo "✓ Port ${KEEPUP_PORT} is listening"
  else
    echo "⚠ Port ${KEEPUP_PORT} is NOT listening (service may be starting)"
  fi
fi
echo ""

# Check systemd service status if available
if command -v systemctl >/dev/null 2>&1; then
  echo "🔧 Service Status:"
  if systemctl is-active --quiet keepup.service; then
    echo "   ✓ keepup.service is ACTIVE"
  else
    echo "   ✗ keepup.service is INACTIVE"
  fi
  echo ""
  echo "📋 Recent Logs (last 10 lines):"
  echo "   Run: journalctl -u keepup.service -n 10"
  echo "   Or for live tail: journalctl -u keepup.service -f"
  echo ""
fi

echo "📦 Project Directory:"
echo "   ${ROOT_DIR}"
echo ""

echo "📁 Backups Directory:"
echo "   ${ROOT_DIR}/backups"
echo "   DB backups: keepup-db-*.db"
echo "   JSON backups: keepup-backup-*.json"
echo ""

echo "🔄 Update & Maintenance:"
echo "   Update app: ${ROOT_DIR}/scripts/update_keepup.sh"
echo "   Check config: ${ROOT_DIR}/scripts/check_and_configure.sh"
echo ""

echo "🔐 User & Permissions:"
echo "   Service user: ${KEEPUP_USER}"
echo "   Files owned by: $(ls -ld ${ROOT_DIR} | awk '{print $3":"$4}')"
echo ""

echo "📚 Quick Commands:"
echo "   Check service: systemctl status keepup.service"
echo "   Restart service: systemctl restart keepup.service"
echo "   View logs: journalctl -u keepup.service -f"
echo "   Stop service: systemctl stop keepup.service"
echo ""

echo "✨ Next Steps:"
echo "   1. Open http://${LOCAL_IP}:${KEEPUP_PORT} in your browser"
echo "   2. Configure monitors in the UI"
echo "   3. Set up Telegram/SMTP notifications if needed (Settings page)"
echo "   4. Keep backups safe: check ${ROOT_DIR}/backups periodically"
echo ""

echo "==========================================="
echo ""
