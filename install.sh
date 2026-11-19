#!/usr/bin/env bash
# One-command Raspberry Pi installer: headless, starts on boot.
#
#   ./install.sh          real sensors
#   ./install.sh --sim    simulated sensors (bench test the whole box with nothing wired)
#
# Safe to run again after a `git pull`.
set -euo pipefail

MODE_ARGS=""
if [[ "${1:-}" == "--sim" ]]; then
  MODE_ARGS="--sim"
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"

echo "=== Installing AeroLab-Pi into $APP_DIR (user: $RUN_USER) ==="

sudo apt-get update
# gpiozero + lgpio drive the trigger pin on every Pi model, including the Pi 5.
sudo apt-get install -y python3-venv python3-pip python3-gpiozero python3-lgpio

# --system-site-packages lets the venv see the apt-installed GPIO libraries.
python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Serial ports (GNSS, laser, IMU) and GPIO need these groups.
sudo usermod -aG dialout,gpio "$RUN_USER"

# Free the Pi's hardware UART for the GNSS: serial console off, serial port on.
NEEDS_REBOOT=0
if command -v raspi-config >/dev/null 2>&1; then
  if [[ "$(sudo raspi-config nonint get_serial_cons)" == "0" ]] || [[ "$(sudo raspi-config nonint get_serial_hw)" != "0" ]]; then
    sudo raspi-config nonint do_serial_cons 1
    sudo raspi-config nonint do_serial_hw 0
    NEEDS_REBOOT=1
  fi
fi

sudo tee /etc/systemd/system/aerolab.service > /dev/null <<EOF
[Unit]
Description=AeroLab-Pi flight telemetry and cockpit HUD
After=network.target time-sync.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/daemon/main.py --config $APP_DIR/config/sensors.yaml $MODE_ARGS
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable aerolab.service
sudo systemctl restart aerolab.service

PORT="$(grep -E '^\s*port:\s*[0-9]+' "$APP_DIR/config/sensors.yaml" | head -1 | awk '{print $2}')"
echo ""
echo "=== AeroLab-Pi installed ${MODE_ARGS:+(SIMULATION MODE)} ==="
echo "Cockpit HUD: http://$(hostname -I | awk '{print $1}'):${PORT:-8000}"
echo "Service log: journalctl -u aerolab -f"
if [[ "$NEEDS_REBOOT" == "1" ]]; then
  echo ""
  echo "REBOOT NEEDED: the serial port settings changed (sudo reboot)."
fi
echo ""
echo "Before flight (see README):"
echo "  1. Mount an external USB SSD at /mnt/ssd for flight logs."
echo "  2. Turn on the Pi's Wi-Fi hotspot for the cockpit iPad."
echo "  3. Turn on the read-only overlay filesystem LAST, after everything works."
