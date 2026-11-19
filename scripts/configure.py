"""Interactive setup for AeroLab-Pi.

Arrow-key menus (whiptail, the same tool raspi-config uses) when run in a
terminal; plain text prompts when whiptail is missing. Writes
config/sensors.local.yaml, which git ignores, so `git pull` never overwrites
your answers.

Start from a profile (a whole-aircraft setup) or pick each device from the
hardware catalog in config/hardware.yaml. Every device preset carries its
driver and factory serial settings, so you mostly pick ports.

    python scripts/configure.py                      # ask everything
    python scripts/configure.py --defaults           # write the default profile, ask nothing
    python scripts/configure.py --defaults --sim     # same, in simulation mode
    python scripts/configure.py --list               # print the hardware catalog
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
CATALOG = ROOT / "config" / "hardware.yaml"
TITLE = "AeroLab-Pi setup"
DEFAULT_PROFILE = "airborne-snow-survey"

BAUD_RATES = ["4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
# BCM pins that are free on a stock Pi: skips 2/3 (I2C), 14/15 (UART for GNSS),
# 18 (often the GNSS PPS input), and 0/1 (HAT EEPROM).
TRIGGER_PINS = ["4", "5", "6", "12", "13", "16", "17", "19", "20", "21", "22", "23", "24", "25", "26", "27"]
BCM_TO_PHYSICAL = {
    "4": 7, "5": 29, "6": 31, "12": 32, "13": 33, "16": 36, "17": 11, "19": 35,
    "20": 38, "21": 40, "22": 15, "23": 16, "24": 18, "25": 22, "26": 37, "27": 13,
}
AI_MODELS = [
    ("none", "No AI: callouts, ground check and plain answers only"),
    ("qwen3.5:2b", "Small local model, 2.7 GB download, Pi 5 with 8 GB"),
    ("qwen3.5:4b", "Larger local model, 3.4 GB, slower, better answers"),
    ("other", "A model already installed in Ollama (type its name)"),
]
FT_PER_M = 3.28084

# Device fields copied into a sensor section. Anything else in the section
# (port, mounting offset) is the user's and survives a device change.
DEVICE_FIELDS = ("name", "driver", "baud", "unit", "max_range_m", "driver_options", "init", "init_script",
                 "poll", "poll_hz", "rate_hz")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def load_catalog(path: Path = CATALOG) -> dict:
    return yaml.safe_load(path.read_text())


def by_id(items: list[dict]) -> dict:
    return {d["id"]: d for d in items}


def device_label(d: dict) -> str:
    bits = []
    if d.get("max_range_m"):
        bits.append(f"{d['max_range_m']:g} m")
    if d.get("interface"):
        iface = d["interface"].split(";")[0].split("(")[0].strip()
        bits.append(iface)
    if d.get("confidence") and d["confidence"] != "manual":
        bits.append(d["confidence"])
    return d["name"] + (f"  [{', '.join(bits)}]" if bits else "")


def range_warning(laser: dict, survey_agl_ft: float) -> str | None:
    rng = float(laser.get("max_range_m") or 0)
    if rng <= 0 or survey_agl_ft <= 0:
        return None
    need_m = survey_agl_ft / FT_PER_M
    if need_m > rng:
        return (f"The {laser['name']} is rated to {rng:g} m ({rng * FT_PER_M:,.0f} ft). Your survey height of "
                f"{survey_agl_ft:,.0f} ft is {need_m:,.0f} m, so it will read nothing up there.\n\n"
                "Pick a longer-range laser, or plan to fly lower.")
    if need_m > 0.8 * rng:
        return (f"The {laser['name']} is rated to {rng:g} m, and {survey_agl_ft:,.0f} ft is {need_m:,.0f} m: "
                "close to its limit. Expect dropouts over dark, wet or glassy surfaces.")
    return None


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


def default_port(device: dict, taken: set[str]) -> str:
    """A sensible first guess: 3.3 V UART devices on the Pi's UART, the rest on USB adapters."""
    iface = (device.get("interface") or "").lower()
    if "3.3 v uart" in iface and "/dev/serial0" not in taken:
        return "/dev/serial0"
    n = 0
    while f"/dev/ttyUSB{n}" in taken:
        n += 1
    return f"/dev/ttyUSB{n}"


# ---------------------------------------------------------------------------
# Answer validation and config building (pure, unit tested)
# ---------------------------------------------------------------------------

def parse_float(text: str, lo: float, hi: float) -> float | None:
    try:
        v = float(text.strip())
    except ValueError:
        return None
    return v if lo <= v <= hi else None


def apply_device(section: dict, device: dict) -> dict:
    """Load a device preset into a sensor section, keeping the user's port and offsets."""
    out = {k: v for k, v in section.items() if k not in DEVICE_FIELDS and k not in ("enabled", "device")}
    out["device"] = device["id"]
    for k in DEVICE_FIELDS:
        if k in device:
            out[k] = copy.deepcopy(device[k])
    out["enabled"] = True
    return out


def build_config(base: dict, answers: dict, catalog: dict | None = None) -> dict:
    """Apply interview answers onto a copy of the base config."""
    catalog = catalog or load_catalog()
    cfg = copy.deepcopy(base)
    cfg["mode"] = answers.get("mode", cfg.get("mode", "live"))
    if "profile" in answers:
        cfg["profile"] = answers["profile"]

    gnss_dev = by_id(catalog["gnss"]).get(answers.get("gnss.device", ""))
    laser_dev = by_id(catalog["lasers"]).get(answers.get("laser.device", ""))
    att_dev = by_id(catalog["attitude"]).get(answers.get("attitude.device", ""))
    if gnss_dev:
        cfg["gnss"] = apply_device(cfg.get("gnss", {}), gnss_dev)
    if laser_dev:
        cfg["laser_altimeter"] = apply_device(cfg.get("laser_altimeter", {}), laser_dev)
        cfg["laser_altimeter"].setdefault("mounting_offset_m", 0.4)
        cfg["laser_altimeter"].setdefault("unit", "m")
    if att_dev:
        if att_dev["driver"] == "none":
            src = gnss_dev or by_id(catalog["gnss"]).get((cfg.get("gnss") or {}).get("device", ""), {})
            label = (src.get("name", "GNSS/INS").split(" (")[0]) + " attitude"
            cfg["imu_ahrs"] = {"device": att_dev["id"], "name": label, "enabled": False, "port": "none"}
        else:
            cfg["imu_ahrs"] = apply_device(cfg.get("imu_ahrs", {}), att_dev)

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
    if "payloads" in answers:
        pays = [by_id(catalog["payloads"])[p] for p in answers["payloads"] if p in by_id(catalog["payloads"])]
        pays = [p for p in pays if p["id"] != "none"]
        cfg["payloads"] = [{"id": p["id"], "name": p["name"], "kind": p.get("kind"), "trigger": p.get("trigger")}
                           for p in pays]
        gpio = [p for p in pays if p.get("trigger") == "gpio"]
        http = [p for p in pays if p.get("trigger") == "http"]
        if gpio:
            ps["enabled"], ps["method"] = True, "gpio"
            ps["pulse_width_ms"] = max(float(p.get("pulse_width_ms", 20)) for p in gpio)
            ps["active_high"] = all(p.get("active_high", True) for p in gpio)
        elif http:
            ps["enabled"], ps["method"] = True, "http"
            ps["http_url"] = answers.get("trigger.http_url", http[0].get("http_url", ""))
        else:
            ps["enabled"] = False
    if "trigger.enabled" in answers:
        ps["enabled"] = bool(answers["trigger.enabled"])
    if "trigger.pin" in answers:
        ps["gpio_pin"] = int(answers["trigger.pin"])
    if "trigger.hz" in answers:
        ps["trigger_hz"] = float(answers["trigger.hz"])
    if "trigger.pulse_ms" in answers:
        ps["pulse_width_ms"] = float(answers["trigger.pulse_ms"])
    if "trigger.http_url" in answers and ps.get("method") == "http":
        ps["http_url"] = answers["trigger.http_url"]

    if "ssd.mount" in answers:
        cfg["logging"]["primary_dir"] = str(Path(answers["ssd.mount"]) / "aerolab_logs")
    if "server.port" in answers:
        cfg["server"]["port"] = int(answers["server.port"])
    hud = cfg.setdefault("hud", {})
    if "hud.low_agl_ft" in answers:
        hud["low_agl_warn_ft"] = float(answers["hud.low_agl_ft"])
    if "survey_agl_ft" in answers:
        hud["survey_agl_ft"] = float(answers["survey_agl_ft"])
    if "buddy.model" in answers:
        b = cfg.setdefault("buddy", {})
        b["ai"] = answers["buddy.model"] != "none"
        if b["ai"]:
            b["model"] = answers["buddy.model"]
    return cfg


def profile_answers(profile: dict, base: dict, catalog: dict, mode: str = "live") -> dict:
    """The answers a profile implies, before the user changes anything."""
    gnss = by_id(catalog["gnss"])[profile["gnss"]]
    laser = by_id(catalog["lasers"])[profile["laser"]]
    att = by_id(catalog["attitude"])[profile["attitude"]]
    taken: set[str] = set()
    a = {"mode": mode, "profile": profile["id"], "gnss.device": gnss["id"], "laser.device": laser["id"],
         "attitude.device": att["id"], "payloads": list(profile.get("payloads", [])),
         "trigger.hz": str(profile.get("trigger_hz", 1.0)),
         "hud.low_agl_ft": str(profile.get("low_agl_warn_ft", 200)),
         "survey_agl_ft": str(profile.get("survey_agl_ft", 0))}
    for key, dev in (("gnss", gnss), ("laser_altimeter", laser), ("imu_ahrs", att)):
        if dev.get("driver") == "none":
            continue
        port = default_port(dev, taken)
        taken.add(port)
        a[f"{key}.port"] = port
    return a


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
        return str(max(60, min(cols - 4, 100)))

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
        list_h = min(len(items), 12)
        return self._call(opts, "--menu", text, list_h + text.count("\n") + 9, str(list_h), *flat)

    def checklist(self, text: str, items: list[tuple[str, str]], on: list[str]) -> list[str]:
        flat = [x for t, d in items for x in (t, d or " ", "ON" if t in on else "OFF")]
        list_h = min(len(items), 10)
        out = self._call(["--separate-output"], "--checklist", text, list_h + text.count("\n") + 9,
                         str(list_h), *flat)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def text(self, prompt: str, default: str) -> str:
        return self._call([], "--inputbox", prompt, 12 + prompt.count("\n"), default)

    def password(self, prompt: str) -> str:
        return self._call([], "--passwordbox", prompt, 10)

    def yesno(self, prompt: str, default_yes: bool = True) -> bool:
        opts = [] if default_yes else ["--defaultno"]
        h = str(min(24, 9 + prompt.count("\n") + len(prompt) // 80))
        cmd = ["whiptail", "--title", TITLE, *opts, "--yesno", prompt, h, self._width()]
        rc = subprocess.run(cmd).returncode
        if rc == 255:
            raise Cancelled()
        return rc == 0

    def info(self, text: str):
        h = str(min(24, 8 + text.count("\n") + len(text) // 80))
        rc = subprocess.run(["whiptail", "--title", TITLE, "--msgbox", text, h, self._width()]).returncode
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

    def checklist(self, text, items, on):
        print(f"\n{text}")
        for i, (tag, desc) in enumerate(items, 1):
            print(f"  {i}) [{'x' if tag in on else ' '}] {tag}  {desc}")
        raw = input("Numbers to select, comma-separated [Enter = as marked]: ").strip()
        if not raw:
            return list(on)
        picks = [items[int(x) - 1][0] for x in raw.replace(" ", "").split(",") if x.isdigit()
                 and 1 <= int(x) <= len(items)]
        return picks

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
            items.append((default, f"suggested: {current} (not plugged in now)"))
    items.append(("other", "type a path"))
    choice = ui.menu(f"Which port is the {label} on?\n{what}", items, default=default)
    if choice == "other":
        return ui.text(f"Path of the {label} serial port:", current)
    return by_tag[choice]


def ask_baud(ui, label: str, current) -> str:
    items = [(b, "preset" if b == str(current) else "") for b in BAUD_RATES]
    if str(current) not in BAUD_RATES:
        items.insert(0, (str(current), "preset"))
    return ui.menu(f"{label} baud rate (Enter keeps the preset):", items, default=str(current))


def ask_device(ui, title: str, devices: list[dict], default: str) -> dict:
    items = [(d["id"], device_label(d)) for d in devices]
    pick = ui.menu(title, items, default=default)
    return by_id(devices)[pick]


def device_note(d: dict) -> str:
    lines = [d["name"], ""]
    if d.get("notes"):
        lines.append(d["notes"])
    if d.get("interface"):
        lines += ["", f"Connection: {d['interface']}"]
    if d.get("confidence") == "unverified":
        lines += ["", "Protocol details for this device are not confirmed. Check it on the bench "
                      "with the Buddy ground check before flight."]
    return "\n".join(lines)


def interview(ui, base: dict, catalog: dict | None = None) -> tuple[dict, dict]:
    catalog = catalog or load_catalog()
    extras: dict = {}
    ports = detect_serial_ports()
    profiles = catalog["profiles"]

    ui.info(
        "This sets up the sensors, payload trigger, logging, cockpit display and Buddy.\n\n"
        "Arrow keys to move, Enter to choose, Esc to quit without saving.\n"
        "Every answer can be changed later with ./configure.sh.\n\n"
        f"Serial ports found right now: {len(ports)}. "
        "Plug all sensors in before starting so they show up in the lists."
    )

    mode = ui.menu("How should AeroLab-Pi run?", [
        ("live", "Real sensors"),
        ("sim", "Simulated flight (bench test, nothing wired)"),
    ], default=base.get("mode", "live"))

    prof_items = [(p["id"], p["name"]) for p in profiles] + [("custom", "Choose every device myself")]
    pid = ui.menu("Start from a profile. Each one presets every device; you can change any of them next.",
                  prof_items, default=base.get("profile", DEFAULT_PROFILE))
    profile = by_id(profiles).get(pid) or by_id(profiles)[DEFAULT_PROFILE]
    if pid != "custom":
        ui.info(profile["name"] + "\n\n" + profile.get("notes", ""))
    a = profile_answers(profile, base, catalog, mode)
    a["profile"] = pid

    # Devices
    gnss = ask_device(ui, "GNSS receiver or GNSS/INS:", catalog["gnss"], a["gnss.device"])
    a["gnss.device"] = gnss["id"]
    att_default = "from-gnss" if gnss.get("provides_attitude") else (
        a["attitude.device"] if a["attitude.device"] != "from-gnss" else "bno085-rvc")
    att = ask_device(ui, "Attitude source (roll, pitch, heading):", catalog["attitude"], att_default)
    a["attitude.device"] = att["id"]
    laser = ask_device(ui, "Laser altimeter:", catalog["lasers"], a["laser.device"])
    a["laser.device"] = laser["id"]

    # Ports (live only)
    if mode == "live":
        taken: set[str] = set()
        for key, dev, label in (("gnss", gnss, "GNSS"), ("laser_altimeter", laser, "laser altimeter"),
                                ("imu_ahrs", att, "attitude sensor")):
            if dev.get("driver") == "none":
                continue
            ui.info(device_note(dev))
            cur = (base.get(key) or {}).get("port") if (base.get(key) or {}).get("device") == dev["id"] else None
            suggested = cur or default_port(dev, taken)
            a[f"{key}.port"] = ask_port(ui, label, dev.get("interface", ""), suggested, ports)
            taken.add(a[f"{key}.port"])
            a[f"{key}.baud"] = ask_baud(ui, dev["name"], dev.get("baud", 115200))
        a["laser.offset"] = ask_number(
            ui, "Laser mounting offset in metres: vertical distance from the laser lens down to the wheels "
                "or belly. The display reads 0 on the ground when this is right.",
            (base.get("laser_altimeter") or {}).get("mounting_offset_m", 0.4), 0.0, 5.0)

    a["survey_agl_ft"] = ask_number(ui, "Planned survey height above ground, in feet:",
                                    a.get("survey_agl_ft", "1000"), 0, 20000)
    warn = range_warning(laser, float(a["survey_agl_ft"]))
    if warn:
        ui.info("Laser range check\n\n" + warn)

    # Payloads and trigger
    pay_items = [(p["id"], p["name"]) for p in catalog["payloads"] if p["id"] != "none"]
    a["payloads"] = ui.checklist("Payloads on board (Space to tick, Enter when done):", pay_items,
                                 on=a.get("payloads", []))
    chosen = [by_id(catalog["payloads"])[p] for p in a["payloads"]]
    gpio = [p for p in chosen if p.get("trigger") == "gpio"]
    http = [p for p in chosen if p.get("trigger") == "http"]
    if gpio:
        pins = [(p, "physical pin " + str(BCM_TO_PHYSICAL[p])) for p in TRIGGER_PINS]
        a["trigger.pin"] = ui.menu(
            f"Trigger output pin (BCM number) for: {', '.join(p['name'] for p in gpio)}",
            pins, default=str(base["payload_sync"].get("gpio_pin", 17)))
    if http:
        a["trigger.http_url"] = ui.text("Camera capture URL:", http[0].get("http_url", ""))
    if gpio or http:
        a["trigger.hz"] = ask_number(ui, "Trigger pulses per second:", a["trigger.hz"], 0.1, 10)

    # Logging and display
    mount = str(Path(base["logging"]["primary_dir"]).parent)
    a["ssd.mount"] = ui.text(
        "Where is the USB SSD mounted? Flight logs go there when it is mounted,\n"
        "otherwise to ./logs on the SD card.", mount)
    a["hud.low_agl_ft"] = ask_number(ui, "Turn the AGL readout amber below how many feet?",
                                     a["hud.low_agl_ft"], 0, 20000)
    a["server.port"] = ask_number(ui, "Web port for the cockpit display:", base["server"]["port"], 1024, 65535)

    # Buddy
    cur_model = (base.get("buddy") or {}).get("model", "none") if (base.get("buddy") or {}).get("ai") else "none"
    model = ui.menu(
        "Buddy's Ask tab can use a small AI model that runs on this Pi, fully offline.\n"
        "Callouts and the ground check work without it.",
        AI_MODELS, default=cur_model if cur_model in [m for m, _ in AI_MODELS] else "none")
    if model == "other":
        model = ui.text("Ollama model name:", cur_model if cur_model != "none" else "qwen3.5:2b")
    a["buddy.model"] = model
    if model != "none":
        extras["ollama.model"] = model

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


def summary(cfg: dict, extras: dict) -> str:
    def dev(sec):
        c = cfg.get(sec) or {}
        if not c.get("enabled", True):
            return c.get("name", "none")
        port = "" if cfg.get("mode") == "sim" else f" on {c.get('port')} @ {c.get('baud')}"
        return f"{c.get('name', 'generic')}{port}"

    lines = [f"Mode:      {cfg.get('mode', 'live')}",
             f"Profile:   {cfg.get('profile', '-')}",
             f"GNSS:      {dev('gnss')}",
             f"Attitude:  {dev('imu_ahrs')}",
             f"Laser:     {dev('laser_altimeter')}"]
    if cfg.get("mode") != "sim":
        lines.append(f"Offset:    {cfg['laser_altimeter'].get('mounting_offset_m')} m")
    pays = cfg.get("payloads") or []
    lines.append("Payloads:  " + (", ".join(p["name"] for p in pays) if pays else "none"))
    ps = cfg["payload_sync"]
    if ps.get("enabled"):
        how = (f"HTTP {ps.get('http_url')}" if ps.get("method") == "http"
               else f"BCM {ps['gpio_pin']}, {ps['pulse_width_ms']:g} ms")
        lines.append(f"Trigger:   {ps['trigger_hz']:g} Hz, {how}")
    else:
        lines.append("Trigger:   off")
    hud = cfg.get("hud", {})
    lines += [f"Logs:      {cfg['logging']['primary_dir']} (if mounted)",
              f"AGL:       survey {hud.get('survey_agl_ft', 0):,.0f} ft, warn below {hud.get('low_agl_warn_ft', 200):,.0f} ft",
              f"Display:   port {cfg['server']['port']}"]
    b = cfg.get("buddy", {})
    lines.append(f"Buddy AI:  {b.get('model') if b.get('ai') else 'off (callouts + rules)'}")
    if "hotspot.ssid" in extras:
        lines.append(f"Hotspot:   {extras['hotspot.ssid']} (will be created)")
    return "\n".join(lines)


def setup_hotspot(ssid: str, psk: str):
    subprocess.run(["sudo", "nmcli", "con", "delete", "AeroLab-HUD"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "nmcli", "con", "add", "type", "wifi", "ifname", "wlan0",
                    "con-name", "AeroLab-HUD", "autoconnect", "yes", "ssid", ssid,
                    "mode", "ap", "ipv4.method", "shared",
                    "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk], check=True)
    subprocess.run(["sudo", "nmcli", "con", "up", "AeroLab-HUD"], check=False)


def print_catalog(catalog: dict):
    for sect, title in (("lasers", "Laser altimeters"), ("gnss", "GNSS and GNSS/INS"),
                        ("attitude", "Attitude"), ("payloads", "Payloads")):
        print(f"\n{title}")
        for d in catalog[sect]:
            print(f"  {d['id']:<24} {device_label(d)}")
    print("\nProfiles")
    for p in catalog["profiles"]:
        print(f"  {p['id']:<24} {p['name']}")


def needs_extras(cfg: dict) -> list[str]:
    """Things install.sh must add for this config: 'pymavlink', 'ollama:<model>'."""
    out = []
    if any((cfg.get(s) or {}).get("driver") == "mavlink" for s in ("gnss", "imu_ahrs")):
        out.append("pymavlink")
    b = cfg.get("buddy") or {}
    if b.get("ai") and b.get("model"):
        out.append(f"ollama:{b['model']}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--defaults", action="store_true", help="write the default profile without asking")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help="with --defaults: which profile")
    ap.add_argument("--plain", action="store_true", help="plain text prompts instead of menus")
    ap.add_argument("--sim", action="store_true", help="with --defaults: set simulation mode")
    ap.add_argument("--list", action="store_true", help="print the hardware catalog and exit")
    ap.add_argument("--needs", action="store_true", help="print what install.sh must add for the saved config")
    args = ap.parse_args()

    catalog = load_catalog()
    if args.list:
        print_catalog(catalog)
        return 0
    if args.needs:
        cfg = yaml.safe_load(LOCAL_CONFIG.read_text()) if LOCAL_CONFIG.exists() else {}
        print("\n".join(needs_extras(cfg)))
        return 0

    base = yaml.safe_load((LOCAL_CONFIG if LOCAL_CONFIG.exists() else BASE_CONFIG).read_text())
    base.setdefault("mode", "live")
    base.setdefault("hud", {}).setdefault("low_agl_warn_ft", 200)

    if args.defaults:
        profile = by_id(catalog["profiles"])[args.profile]
        answers = profile_answers(profile, base, catalog, "sim" if args.sim else "live")
        cfg = build_config(base, answers, catalog)
        write_config(cfg)
        print(f"Wrote {LOCAL_CONFIG}: profile {profile['id']}, mode {cfg['mode']}.")
        return 0

    use_whiptail = shutil.which("whiptail") and sys.stdin.isatty() and sys.stdout.isatty() and not args.plain
    ui = WhiptailUI() if use_whiptail else PromptUI()
    try:
        answers, extras = interview(ui, base, catalog)
        cfg = build_config(base, answers, catalog)
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
