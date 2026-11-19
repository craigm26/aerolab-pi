#!/usr/bin/env bash
# Change AeroLab-Pi settings with menus, then restart the service.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Free the serial ports so setup can listen on them; the service restarts afterwards either way.
WAS_RUNNING=0
if systemctl is-active --quiet aerolab.service 2>/dev/null; then
  WAS_RUNNING=1
  sudo systemctl stop aerolab.service
fi
trap '[[ $WAS_RUNNING == 1 ]] && sudo systemctl start aerolab.service' EXIT
if "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/configure.py" "$@"; then
  if systemctl list-unit-files aerolab.service >/dev/null 2>&1; then
    trap - EXIT
    sudo systemctl restart aerolab.service
    echo "Service restarted with the new settings."
  fi
fi
