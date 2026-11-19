#!/usr/bin/env bash
# Change AeroLab-Pi settings with menus, then restart the service.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/configure.py" "$@"; then
  if systemctl list-unit-files aerolab.service >/dev/null 2>&1; then
    sudo systemctl restart aerolab.service
    echo "Service restarted with the new settings."
  fi
fi
