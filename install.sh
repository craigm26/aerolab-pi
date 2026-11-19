#!/usr/bin/env bash
# One-command Raspberry Pi installer: headless, starts on boot.
#
#   ./install.sh             asks setup questions with menus (ports, trigger, logging, hotspot)
#   ./install.sh --defaults  no questions; the default profile (airborne snow survey)
#   ./install.sh --sim       no questions; simulated sensors (bench test with nothing wired)
#
# Safe to run again after a `git pull`. Change settings later with ./configure.sh.
set -euo pipefail

CONFIG_ARGS=""
case "${1:-}" in
  --sim) CONFIG_ARGS="--defaults --sim" ;;
  --defaults) CONFIG_ARGS="--defaults" ;;
  "") ;;
  *) echo "Unknown option: $1 (use --defaults or --sim)"; exit 2 ;;
esac

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

# Free the serial ports for the setup questions (a reinstall over a running service).
sudo systemctl stop aerolab.service 2>/dev/null || true

# Setup questions. Without a terminal (e.g. piped install) fall back to defaults.
if [[ -z "$CONFIG_ARGS" && ! -t 0 ]]; then
  CONFIG_ARGS="--defaults"
fi
if [[ -n "$CONFIG_ARGS" && -f "$APP_DIR/config/sensors.local.yaml" && "${1:-}" != "--sim" ]]; then
  echo "Keeping existing settings in config/sensors.local.yaml"
else
  # shellcheck disable=SC2086
  "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/configure.py" $CONFIG_ARGS || {
    echo "Setup cancelled. Run ./install.sh again, or ./install.sh --defaults."
    exit 1
  }
fi

# USB-serial adapters: FTDI chips buffer up to 16 ms by default, which smears timestamps.
# This rule sets 1 ms on every FTDI port as it appears. (CP210x/CH340/PL2303 have no such setting.)
echo 'ACTION=="add", SUBSYSTEM=="usb-serial", DRIVERS=="ftdi_sio", ATTR{latency_timer}="1"' \
  | sudo tee /etc/udev/rules.d/99-aerolab-ftdi-latency.rules > /dev/null
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb-serial || true

# Extras the chosen hardware needs.
NEEDS="$("$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/configure.py" --needs)"
if grep -qx pymavlink <<<"$NEEDS"; then
  "$APP_DIR/.venv/bin/pip" install pymavlink
fi
PPS_PIN="$(grep '^pps:' <<<"$NEEDS" | cut -d: -f2 || true)"
if [[ -n "$PPS_PIN" ]]; then
  BOOTCFG=/boot/firmware/config.txt; [[ -f $BOOTCFG ]] || BOOTCFG=/boot/config.txt
  if grep -q "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null; then
    OVERLAY="dtoverlay=pps-rp1,pin=$PPS_PIN"
  else
    OVERLAY="dtoverlay=pps-gpio,gpiopin=$PPS_PIN"
  fi
  if ! grep -qxF "$OVERLAY" "$BOOTCFG"; then
    sudo sed -i '/^dtoverlay=pps-\(rp1\|gpio\)/d' "$BOOTCFG"
    echo "$OVERLAY" | sudo tee -a "$BOOTCFG" > /dev/null
    NEEDS_REBOOT=1
    echo "PPS: added '$OVERLAY' to $BOOTCFG"
  fi
  sudo apt-get install -y pps-tools > /dev/null || true
fi
MODEL="$(grep '^ollama:' <<<"$NEEDS" | cut -d: -f2- || true)"
if [[ -n "$MODEL" ]]; then
  if ! command -v ollama >/dev/null 2>&1; then
    echo "Buddy AI: installing Ollama (local model runner) from ollama.com"
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  echo "Buddy AI: downloading $MODEL (one time, needs internet)"
  ollama pull "$MODEL" || echo "Could not download $MODEL. Buddy will use plain answers until it is installed."
fi

sudo tee /etc/systemd/system/aerolab.service > /dev/null <<EOF
[Unit]
Description=AeroLab-Pi flight telemetry and cockpit HUD
After=network.target time-sync.target

[Service]
# notify + watchdog: the daemon pings systemd every 5 s while all its tasks are alive;
# if one wedges, systemd restarts it within 60 s and logging resumes in the same database.
Type=notify
NotifyAccess=main
WatchdogSec=60
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/daemon/main.py --config $APP_DIR/config/sensors.local.yaml
Restart=always
RestartSec=2
# Restart forever, however often it fails (a flight must not end in a "start limit hit").
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable aerolab.service
sudo systemctl restart aerolab.service

PORT="$("$APP_DIR/.venv/bin/python" -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))["server"]["port"])' "$APP_DIR/config/sensors.local.yaml")"
MODE="$("$APP_DIR/.venv/bin/python" -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1])).get("mode","live"))' "$APP_DIR/config/sensors.local.yaml")"
echo ""
echo "=== AeroLab-Pi installed (mode: $MODE) ==="
echo "Cockpit HUD: http://$(hostname -I | awk '{print $1}'):${PORT:-8000}"
echo "Service log: journalctl -u aerolab -f"
echo "Change settings: ./configure.sh"
if [[ "$NEEDS_REBOOT" == "1" ]]; then
  echo ""
  echo "REBOOT NEEDED: the serial port settings changed (sudo reboot)."
fi
echo ""
echo "Before flight (see README):"
echo "  1. Mount an external USB SSD at /mnt/ssd for flight logs."
echo "  2. Turn on the Pi's Wi-Fi hotspot for the cockpit iPad (./configure.sh offers it)."
echo "  3. Turn on the read-only overlay filesystem LAST, after everything works."
