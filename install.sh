#!/usr/bin/env bash
# One-command Raspberry Pi installer: headless, starts on boot.
#
#   ./install.sh             asks setup questions with menus (ports, trigger, logging, hotspot)
#   ./install.sh --defaults  no questions; the default profile (airborne snow survey)
#   ./install.sh --sim       no questions; simulated sensors (bench test with nothing wired)
#   ./install.sh --dry-run   print every system change without running it (combines with --defaults or --sim)
#
# Safe to run again after a `git pull`. Change settings later with ./configure.sh.
set -euo pipefail

DRY_RUN=0
WANT_DEFAULTS=0
WANT_SIM=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --sim) WANT_SIM=1 ;;
    --defaults) WANT_DEFAULTS=1 ;;
    *) echo "Unknown option: $arg (use --defaults, --sim, or --dry-run)" >&2; exit 2 ;;
  esac
done

CONFIG_ARGS=()
if [[ "$WANT_SIM" == "1" ]]; then
  CONFIG_ARGS=(--defaults --sim)
elif [[ "$WANT_DEFAULTS" == "1" ]]; then
  CONFIG_ARGS=(--defaults)
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

write_file() {
  local dest="$1"
  local mode="write"
  if [[ "$dest" == "-a" ]]; then
    mode="append"
    dest="$2"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $mode $dest <<"
    cat
    echo "[dry-run] >>"
  elif [[ "$mode" == "append" ]]; then
    sudo tee -a "$dest" > /dev/null
  else
    sudo tee "$dest" > /dev/null
  fi
}

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "=== Dry run: AeroLab-Pi into $APP_DIR (user: $RUN_USER), no changes will be made ==="
else
  echo "=== Installing AeroLab-Pi into $APP_DIR (user: $RUN_USER) ==="
fi

run sudo apt-get update
# gpiozero + lgpio drive the trigger pin on every Pi model, including the Pi 5.
run sudo apt-get install -y python3-venv python3-pip python3-gpiozero python3-lgpio

# --system-site-packages lets the venv see the apt-installed GPIO libraries.
run python3 -m venv --system-site-packages "$APP_DIR/.venv"
run "$APP_DIR/.venv/bin/pip" install --upgrade pip
run "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Serial ports (GNSS, laser, IMU) and GPIO need these groups.
run sudo usermod -aG dialout,gpio "$RUN_USER"

# Free the Pi's hardware UART for the GNSS: serial console off, serial port on.
NEEDS_REBOOT=0
if command -v raspi-config >/dev/null 2>&1; then
  if [[ "$DRY_RUN" == "1" ]]; then
    run sudo raspi-config nonint do_serial_cons 1
    run sudo raspi-config nonint do_serial_hw 0
    NEEDS_REBOOT=1
  elif [[ "$(sudo raspi-config nonint get_serial_cons)" == "0" ]] || [[ "$(sudo raspi-config nonint get_serial_hw)" != "0" ]]; then
    run sudo raspi-config nonint do_serial_cons 1
    run sudo raspi-config nonint do_serial_hw 0
    NEEDS_REBOOT=1
  fi
fi

# Free the serial ports for the setup questions (a reinstall over a running service).
run sudo systemctl stop aerolab.service 2>/dev/null || true

# Setup questions. Without a terminal (e.g. piped install) fall back to defaults.
if [[ ${#CONFIG_ARGS[@]} -eq 0 && ! -t 0 ]]; then
  CONFIG_ARGS=(--defaults)
fi
if [[ ${#CONFIG_ARGS[@]} -gt 0 && -f "$APP_DIR/config/sensors.local.yaml" && "$WANT_SIM" != "1" ]]; then
  echo "Keeping existing settings in config/sensors.local.yaml"
elif [[ "$DRY_RUN" == "1" ]]; then
  run "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/configure.py" "${CONFIG_ARGS[@]}"
else
  "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/configure.py" "${CONFIG_ARGS[@]}" || {
    echo "Setup cancelled. Run ./install.sh again, or ./install.sh --defaults."
    exit 1
  }
fi

# Safe shutdown from the cockpit display: allow exactly one privileged command, nothing else.
echo "$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" \
  | write_file /etc/sudoers.d/aerolab-poweroff
run sudo chmod 440 /etc/sudoers.d/aerolab-poweroff
if [[ "$DRY_RUN" == "1" ]]; then
  run sudo visudo -cf /etc/sudoers.d/aerolab-poweroff
else
  sudo visudo -cf /etc/sudoers.d/aerolab-poweroff > /dev/null || sudo rm -f /etc/sudoers.d/aerolab-poweroff
fi

# USB-serial adapters: FTDI chips buffer up to 16 ms by default, which smears timestamps.
# This rule sets 1 ms on every FTDI port as it appears. (CP210x/CH340/PL2303 have no such setting.)
echo 'ACTION=="add", SUBSYSTEM=="usb-serial", DRIVERS=="ftdi_sio", ATTR{latency_timer}="1"' \
  | write_file /etc/udev/rules.d/99-aerolab-ftdi-latency.rules
if run sudo udevadm control --reload-rules; then
  run sudo udevadm trigger --subsystem-match=usb-serial || true
fi

# Extras the chosen hardware needs.
PY="$APP_DIR/.venv/bin/python"
if [[ "$DRY_RUN" == "1" && ! -x "$PY" ]]; then
  PY="$(command -v python3 || true)"
fi
NEEDS=""
if [[ "$DRY_RUN" != "1" ]]; then
  NEEDS="$("$PY" "$APP_DIR/scripts/configure.py" --needs)"   # real install: fail loudly, never skip PPS/pymavlink silently
elif [[ -x "$PY" ]] && "$PY" -c "import yaml" 2>/dev/null; then
  NEEDS="$("$PY" "$APP_DIR/scripts/configure.py" --needs)"
fi
if grep -qx pymavlink <<<"$NEEDS"; then
  run "$APP_DIR/.venv/bin/pip" install pymavlink
fi
PPS_PIN="$(grep '^pps:' <<<"$NEEDS" | cut -d: -f2 || true)"
if [[ -n "$PPS_PIN" ]]; then
  BOOTCFG=/boot/firmware/config.txt; [[ -f "$BOOTCFG" ]] || BOOTCFG=/boot/config.txt
  if grep -q "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null; then
    OVERLAY="dtoverlay=pps-rp1,pin=$PPS_PIN"
  else
    OVERLAY="dtoverlay=pps-gpio,gpiopin=$PPS_PIN"
  fi
  if ! grep -qxF "$OVERLAY" "$BOOTCFG" 2>/dev/null; then
    run sudo sed -i '/^dtoverlay=pps-\(rp1\|gpio\)/d' "$BOOTCFG"
    echo "$OVERLAY" | write_file -a "$BOOTCFG"
    NEEDS_REBOOT=1
    echo "PPS: added '$OVERLAY' to $BOOTCFG"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    run sudo apt-get install -y pps-tools
  else
    sudo apt-get install -y pps-tools > /dev/null || true
  fi
fi
if grep -qx chrony <<<"$NEEDS"; then
  run sudo apt-get install -y chrony
  # /run/chrony is owned by _chrony:_chrony (0750), and chronyd creates SOCK
  # refclock sockets as root before dropping privileges. Put the service user
  # in _chrony and make the socket group-writable via a systemd drop-in.
  run sudo usermod -aG _chrony "$RUN_USER"
  run sudo mkdir -p /etc/chrony/conf.d
  if [[ -n "$PPS_PIN" ]]; then
    write_file /etc/chrony/conf.d/aerolab.conf <<'EOF'
# AeroLab-Pi: set the Pi system clock from GNSS and PPS via chrony.
# The Pi has no battery-backed RTC, so allow stepping whenever offset exceeds 1 s.
makestep 1.0 -1

# Coarse GNSS time from the daemon; noselect prevents serial jitter from pulling
# the clock off PPS while still giving PPS the whole second to lock onto.
refclock SOCK /run/chrony/aerolab.sock refid GNSS offset 0.05 delay 0.2 noselect
refclock PPS /dev/pps0 lock GNSS refid PPS prefer
EOF
  else
    write_file /etc/chrony/conf.d/aerolab.conf <<'EOF'
# AeroLab-Pi: set the Pi system clock from GNSS via chrony.
# The Pi has no battery-backed RTC, so allow stepping whenever offset exceeds 1 s.
makestep 1.0 -1

# GNSS time from the daemon over the chrony SOCK interface.
# offset 0.05 compensates for typical NMEA serial latency; delay 0.2 reflects serial jitter.
refclock SOCK /run/chrony/aerolab.sock refid GNSS offset 0.05 delay 0.2
EOF
  fi
  run sudo mkdir -p /etc/systemd/system/chrony.service.d
  write_file /etc/systemd/system/chrony.service.d/aerolab.conf <<'EOF'
[Service]
# chronyd creates the SOCK refclock socket as root before dropping privileges.
# Make it group-writable for _chrony so the non-root aerolab daemon can send samples.
ExecStartPost=/bin/sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do [ -S /run/chrony/aerolab.sock ] && break; sleep 0.2; done; chgrp _chrony /run/chrony/aerolab.sock && chmod 660 /run/chrony/aerolab.sock'
EOF
  run sudo systemctl daemon-reload
  run sudo systemctl restart chrony
fi
MODEL="$(grep '^ollama:' <<<"$NEEDS" | cut -d: -f2- || true)"
if [[ -n "$MODEL" ]]; then
  if ! command -v ollama >/dev/null 2>&1; then
    echo "Buddy AI: installing Ollama (local model runner) from ollama.com"
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "[dry-run] curl -fsSL https://ollama.com/install.sh | sh"
    else
      curl -fsSL https://ollama.com/install.sh | sh
    fi
  fi
  echo "Buddy AI: downloading $MODEL (one time, needs internet)"
  run ollama pull "$MODEL" || echo "Could not download $MODEL. Buddy will use plain answers until it is installed."
fi

write_file /etc/systemd/system/aerolab.service <<EOF
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

run sudo systemctl daemon-reload
run sudo systemctl enable aerolab.service
run sudo systemctl restart aerolab.service

if [[ -f "$APP_DIR/config/sensors.local.yaml" && -x "$PY" ]] && "$PY" -c "import yaml" 2>/dev/null; then
  PORT="$("$PY" -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))["server"]["port"])' "$APP_DIR/config/sensors.local.yaml")"
  MODE="$("$PY" -c 'import yaml,sys;print(yaml.safe_load(open(sys.argv[1])).get("mode","live"))' "$APP_DIR/config/sensors.local.yaml")"
  if [[ "$DRY_RUN" == "1" && "$WANT_SIM" == "1" ]]; then
    MODE="sim"
  fi
else
  PORT="8000"
  MODE="live"
  if [[ "$WANT_SIM" == "1" ]]; then
    MODE="sim"
  fi
fi

echo ""
if [[ "$DRY_RUN" == "1" ]]; then
  echo "=== Dry run complete (mode: $MODE); no changes were made ==="
else
  echo "=== AeroLab-Pi installed (mode: $MODE) ==="
fi
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
