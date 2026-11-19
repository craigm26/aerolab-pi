"""Interactive setup for AeroLab-Pi.

Arrow-key menus (whiptail, the same tool raspi-config uses) when run in a
terminal; plain text prompts when whiptail is missing. Writes
config/sensors.local.yaml, which git ignores, so `git pull` never overwrites
your answers. Defaults come from the existing local file if there is one,
otherwise from config/sensors.yaml.

    python scripts/configure.py              # ask everything
    python scripts/configure.py --defaults   # write defaults, ask nothing
"""

import argparse
import copy
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE_CONFIG = ROOT / "config" / "sensors.yaml"
LOCAL_CONFIG = ROOT / "config" / "sensors.local.yaml"
TITLE = "AeroLab-Pi setup"

BAUD_RATES = ["4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
# BCM pins that are free on a stock Pi: skips 2/3 (I2C), 14/15 (UART for GNSS),
# 18 (often the GNSS PPS input), and 0/1 (HAT EEPROM).
TRIGGER_PINS = ["4", "5", "6", "12", "13", "16", "17", "19", "20", "21", "22", "23", "24", "25", "26", "27"]


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------

def detect_serial_ports(dev: str = "/dev") -> list[tuple[str, str]]:
    """(path, description) for every serial port worth offering.

    USB adapters are listed by their /dev/serial/by-id name, which stays the
    same across reboots (ttyUSB0/ttyUSB1 can swap). The Pi's own UART is
    listed as /dev/serial0.
    """
    ports: list[tuple[str, str]] = []
    covered: set[str] = set()

    for link in sorted(glob.glob(os.path.join(dev, "serial", "by-id", "*"))):
        target = os.path.realpath(link)
        covered.add(target)
        name = os.path.basename(link)
        pretty = name.removeprefix("usb-").rsplit("-if", 1)[0].replace("_", " ")
        ports.append((link, f"{pretty} (USB)"))

    serial0 = os.path.join(dev, "serial0")
    if os.path.exists(serial0):
        covered.add(os.path.realpath(serial0))
        ports.append((serial0, "Pi GPIO UART, pins 8 (TX) and 10 (RX)"))

    for pattern in ("ttyUSB*", "ttyACM*"):
        for path in sorted(glob.glob(os.path.join(dev, pattern))):
            if os.path.realpath(path) not in covered:
                ports.append((path, "serial device"))
    return ports


# ---------------------------------------------------------------------------
# Answer validation and config building (pure, unit tested)
# ---------------------------------------------------------------------------

def parse_float(text: str, lo: float, hi: float) -> float | None:
    try:
        v = float(text.strip())
    except ValueError:
        return None
    return v if lo <= v <= hi else None


def build_config(base: dict, answers: dict) -> dict:
    """Apply interview answers onto a copy of the base config."""
    cfg = copy.deepcopy(base)
    cfg["mode"] = answers.get("mode", cfg.get("mode", "live"))
    for key in ("gnss", "laser_altimeter", "imu_ahrs"):
        if f"{key}.port" in answers:
            cfg[key]["port"] = answers[f"{key}.port"]
        if f"{key}.baud" in answers:
            cfg[key]["baud"] = int(answers[f"{key}.baud"])
    if "laser.unit" in answers:
        cfg["laser_altimeter"]["unit"] = answers["laser.unit"]
    if "laser.offset" in answers:
        cfg["laser_altimeter"]["mounting_offset_m"] = float(answers["laser.offset"])
    ps = cfg["payload_sync"]
    if "trigger.enabled" in answers:
        ps["enabled"] = bool(answers["trigger.enabled"])
    if "trigger.pin" in answers:
        ps["gpio_pin"] = int(answers["trigger.pin"])
    if "trigger.hz" in answers:
        ps["trigger_hz"] = float(answers["trigger.hz"])
    if "trigger.pulse_ms" in answers:
        ps["pulse_width_ms"] = float(answers["trigger.pulse_ms"])
    if "ssd.mount" in answers:
        cfg["logging"]["primary_dir"] = str(Path(answers["ssd.mount"]) / "aerolab_logs")
    if "server.port" in answers:
        cfg["server"]["port"] = int(answers["server.port"])
    if "hud.low_agl_ft" in answers:
        cfg.setdefault("hud", {})["low_agl_warn_ft"] = float(answers["hud.low_agl_ft"])
    return cfg


def write_config(cfg: dict, path: Path = LOCAL_CONFIG):
    header = (
        "# Written by scripts/configure.py. Run ./configure.sh to change it,\n"
        "# or edit by hand and `sudo systemctl restart aerolab`.\n"
        "# Not tracked by git: `git pull` leaves it alone.\n"
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(header + yaml.safe_dump(cfg, sort_keys=False))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# UI: whiptail menus, or plain prompts
# ---------------------------------------------------------------------------

class Cancelled(Exception):
    pass


class WhiptailUI:
    """Thin wrapper over whiptail. Esc or Cancel anywhere raises Cancelled."""

    def _width(self) -> str:
        cols = shutil.get_terminal_size((80, 24)).columns
        return str(max(60, min(cols - 4, 90)))

    def _call(self, opts: list[str], kind: str, prompt: str, height: int, *rest: str) -> str:
        # whiptail draws on the terminal and writes the answer to stderr.
        cmd = ["whiptail", "--title", TITLE, *opts, kind, prompt, str(height), self._width(), *rest]
        p = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            raise Cancelled()
        return p.stderr.strip()

    def menu(self, text: str, items: list[tuple[str, str]], default: str | None = None) -> str:
        opts = ["--default-item", default] if default in [t for t, _ in items] else []
        flat = [x for t, d in items for x in (t, d or " ")]
        list_h = min(len(items), 10)
        return self._call(opts, "--menu", text, list_h + text.count("\n") + 9, str(list_h), *flat)

    def text(self, prompt: str, default: str) -> str:
        return self._call([], "--inputbox", prompt, 12, default)

    def password(self, prompt: str) -> str:
        return self._call([], "--passwordbox", prompt, 10)

    def yesno(self, prompt: str, default_yes: bool = True) -> bool:
        opts = [] if default_yes else ["--defaultno"]
        cmd = ["whiptail", "--title", TITLE, *opts, "--yesno", prompt, "20", self._width()]
        rc = subprocess.run(cmd).returncode
        if rc == 255:
            raise Cancelled()
        return rc == 0

    def info(self, text: str):
        rc = subprocess.run(["whiptail", "--title", TITLE, "--msgbox", text, "16", self._width()]).returncode
        if rc == 255:
            raise Cancelled()


class PromptUI:
    """Fallback when whiptail is not available: numbered choices on stdin."""

    def menu(self, text, items, default=None):
        print(f"\n{text}")
        for i, (tag, desc) in enumerate(items, 1):
            mark = " (default)" if tag == default else ""
            print(f"  {i}) {tag}  {desc}{mark}")
        while True:
            raw = input("Choice [Enter = default]: ").strip()
            if not raw and default is not None:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(items):
                return items[int(raw) - 1][0]
            print("  Pick a number from the list.")

    def text(self, prompt, default):
        raw = input(f"\n{prompt}\n[{default}]: ").strip()
        return raw or default

    def password(self, prompt):
        import getpass
        return getpass.getpass(f"\n{prompt}\n: ")

    def yesno(self, prompt, default_yes=True):
        raw = input(f"\n{prompt} [{'Y/n' if default_yes else 'y/N'}]: ").strip().lower()
        return default_yes if not raw else raw.startswith("y")

    def info(self, text):
        print(f"\n{text}\n")


# ---------------------------------------------------------------------------
# Interview
# ---------------------------------------------------------------------------

def ask_number(ui, prompt: str, default, lo: float, hi: float) -> str:
    while True:
        raw = ui.text(f"{prompt}\n(between {lo:g} and {hi:g})", str(default))
        if parse_float(raw, lo, hi) is not None:
            return raw.strip()
        ui.info(f"'{raw}' is not a number between {lo:g} and {hi:g}. Try again.")


def ask_port(ui, label: str, what: str, current: str, ports: list[tuple[str, str]]) -> str:
    """Menu of short names (ttyACM0, serial0); returns the full, stable path."""
    by_tag: dict[str, str] = {}
    items: list[tuple[str, str]] = []
    for path, desc in ports:
        tag = os.path.basename(os.path.realpath(path)) if "/by-id/" in path else os.path.basename(path)
        if tag in by_tag:
            tag = os.path.basename(path)
        by_tag[tag] = path
        items.append((tag, desc))
    default = next((t for t, p in by_tag.items() if p == current), None)
    if default is None:
        default = os.path.basename(current)
        if default not in by_tag:
            by_tag[default] = current
            items.append((default, f"current setting {current} (not plugged in now)"))
    items.append(("other", "type a path"))
    choice = ui.menu(f"Which port is the {label} on?\n{what}", items, default=default)
    if choice == "other":
        return ui.text(f"Path of the {label} serial port:", current)
    return by_tag[choice]


def ask_baud(ui, label: str, current) -> str:
    items = [(b, "current" if b == str(current) else "") for b in BAUD_RATES]
    if str(current) not in BAUD_RATES:
        items.insert(0, (str(current), "current"))
    return ui.menu(f"{label} baud rate (check the sensor's manual):", items, default=str(current))


def interview(ui, base: dict) -> tuple[dict, dict]:
    a: dict = {}
    extras: dict = {}
    ports = detect_serial_ports()

    ui.info(
        "This sets up the sensors, trigger, logging and cockpit display.\n\n"
        "Arrow keys to move, Enter to choose, Esc to quit without saving.\n"
        "Every answer can be changed later with ./configure.sh.\n\n"
        f"Serial ports found right now: {len(ports)}. "
        "Plug all sensors in before starting so they show up in the lists."
    )

    a["mode"] = ui.menu("How should AeroLab-Pi run?", [
        ("live", "Real sensors"),
        ("sim", "Simulated flight (bench test, nothing wired)"),
    ], default=base.get("mode", "live"))

    if a["mode"] == "live":
        g, l, i = base["gnss"], base["laser_altimeter"], base["imu_ahrs"]
        a["gnss.port"] = ask_port(ui, "GNSS receiver", "It must send NMEA sentences (GGA).", g["port"], ports)
        a["gnss.baud"] = ask_baud(ui, "GNSS", g["baud"])
        a["laser_altimeter.port"] = ask_port(ui, "laser altimeter", "One range reading per line.", l["port"], ports)
        a["laser_altimeter.baud"] = ask_baud(ui, "Laser altimeter", l["baud"])
        a["laser.unit"] = ui.menu("What unit does the laser report?", [
            ("m", "metres"), ("cm", "centimetres")], default=l.get("unit", "m"))
        a["laser.offset"] = ask_number(
            ui, "Laser mounting offset in metres: vertical distance from the laser lens "
                "down to the wheels or belly. The display reads 0 on the ground when this is right.",
            l["mounting_offset_m"], 0.0, 5.0)
        a["imu_ahrs.port"] = ask_port(ui, "AHRS/IMU", "It must send roll,pitch,yaw lines in degrees.", i["port"], ports)
        a["imu_ahrs.baud"] = ask_baud(ui, "AHRS/IMU", i["baud"])

    ps = base["payload_sync"]
    a["trigger.enabled"] = ui.yesno(
        "Send a trigger pulse to the camera/radar from a GPIO pin?\n\n"
        "Each pulse is logged with position, AGL and attitude.",
        default_yes=bool(ps.get("enabled", True)))
    if a["trigger.enabled"]:
        pins = [(p, "physical pin " + str(BCM_TO_PHYSICAL[p])) for p in TRIGGER_PINS]
        a["trigger.pin"] = ui.menu("Trigger output pin (BCM number):", pins, default=str(ps["gpio_pin"]))
        a["trigger.hz"] = ask_number(ui, "Trigger pulses per second:", ps["trigger_hz"], 0.1, 50)
        a["trigger.pulse_ms"] = ask_number(ui, "Pulse width in milliseconds:", ps["pulse_width_ms"], 1, 500)

    mount = str(Path(base["logging"]["primary_dir"]).parent)
    a["ssd.mount"] = ui.text(
        "Where is the USB SSD mounted? Flight logs go there when it is mounted,\n"
        "otherwise to ./logs on the SD card.", mount)

    a["hud.low_agl_ft"] = ask_number(ui, "Turn the AGL readout amber below how many feet?",
                                     base.get("hud", {}).get("low_agl_warn_ft", 200), 0, 10000)
    a["server.port"] = ask_number(ui, "Web port for the cockpit display:", base["server"]["port"], 1024, 65535)

    if shutil.which("nmcli") and ui.yesno(
        "Make this Pi a Wi-Fi hotspot for the cockpit iPad/phone?\n\n"
        "Warning: Wi-Fi then serves the hotspot instead of your home network. "
        "If you are connected over Wi-Fi SSH, you will be disconnected. "
        "Say No if unsure; you can do it later with ./configure.sh.",
        default_yes=False,
    ):
        extras["hotspot.ssid"] = ui.text("Hotspot name (SSID):", "AeroLab-HUD")
        while True:
            pw = ui.password("Hotspot password (8 to 63 characters):")
            if 8 <= len(pw) <= 63:
                extras["hotspot.psk"] = pw
                break
            ui.info("The password must be 8 to 63 characters.")

    return a, extras


# BCM number -> physical header pin, for the pin menu.
BCM_TO_PHYSICAL = {
    "4": 7, "5": 29, "6": 31, "12": 32, "13": 33, "16": 36, "17": 11, "19": 35,
    "20": 38, "21": 40, "22": 15, "23": 16, "24": 18, "25": 22, "26": 37, "27": 13,
}


def summary(cfg: dict, extras: dict) -> str:
    lines = [f"Mode:            {cfg.get('mode', 'live')}"]
    if cfg.get("mode") != "sim":
        lines += [
            f"GNSS:            {cfg['gnss']['port']} @ {cfg['gnss']['baud']}",
            f"Laser:           {cfg['laser_altimeter']['port']} @ {cfg['laser_altimeter']['baud']}, "
            f"{cfg['laser_altimeter']['unit']}, offset {cfg['laser_altimeter']['mounting_offset_m']} m",
            f"AHRS/IMU:        {cfg['imu_ahrs']['port']} @ {cfg['imu_ahrs']['baud']}",
        ]
    ps = cfg["payload_sync"]
    lines.append("Trigger:         " + (
        f"BCM {ps['gpio_pin']}, {ps['trigger_hz']} Hz, {ps['pulse_width_ms']} ms" if ps["enabled"] else "off"))
    lines += [
        f"Logs:            {cfg['logging']['primary_dir']} (if mounted)",
        f"Low AGL warning: {cfg['hud']['low_agl_warn_ft']} ft",
        f"Display port:    {cfg['server']['port']}",
    ]
    if "hotspot.ssid" in extras:
        lines.append(f"Hotspot:         {extras['hotspot.ssid']} (will be created)")
    return "\n".join(lines)


def setup_hotspot(ssid: str, psk: str):
    subprocess.run(["sudo", "nmcli", "con", "delete", "AeroLab-HUD"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "nmcli", "con", "add", "type", "wifi", "ifname", "wlan0",
                    "con-name", "AeroLab-HUD", "autoconnect", "yes", "ssid", ssid,
                    "mode", "ap", "ipv4.method", "shared",
                    "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk], check=True)
    subprocess.run(["sudo", "nmcli", "con", "up", "AeroLab-HUD"], check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--defaults", action="store_true", help="write the defaults without asking")
    ap.add_argument("--plain", action="store_true", help="plain text prompts instead of menus")
    ap.add_argument("--sim", action="store_true", help="with --defaults: set simulation mode")
    args = ap.parse_args()

    base = yaml.safe_load((LOCAL_CONFIG if LOCAL_CONFIG.exists() else BASE_CONFIG).read_text())
    base.setdefault("mode", "live")
    base.setdefault("hud", {}).setdefault("low_agl_warn_ft", 200)

    if args.defaults:
        cfg = build_config(base, {"mode": "sim"} if args.sim else {})
        write_config(cfg)
        print(f"Wrote {LOCAL_CONFIG} with defaults (mode: {cfg['mode']}).")
        return 0

    use_whiptail = shutil.which("whiptail") and sys.stdin.isatty() and sys.stdout.isatty() and not args.plain
    ui = WhiptailUI() if use_whiptail else PromptUI()
    try:
        answers, extras = interview(ui, base)
        cfg = build_config(base, answers)
        if not ui.yesno("Save these settings?\n\n" + summary(cfg, extras)):
            print("Nothing saved.")
            return 1
    except (Cancelled, KeyboardInterrupt, EOFError):
        print("\nSetup cancelled. Nothing saved.")
        return 1

    write_config(cfg)
    print(f"Saved {LOCAL_CONFIG}")
    if "hotspot.ssid" in extras:
        try:
            setup_hotspot(extras["hotspot.ssid"], extras["hotspot.psk"])
            print(f"Hotspot '{extras['hotspot.ssid']}' created. Cockpit display: http://10.42.0.1:{cfg['server']['port']}")
        except subprocess.CalledProcessError as exc:
            print(f"Hotspot setup failed ({exc}). Settings were still saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
