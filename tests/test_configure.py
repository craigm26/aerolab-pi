import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import configure  # noqa: E402

BASE = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "sensors.yaml").read_text())


def test_detect_ports_prefers_stable_by_id_names(tmp_path):
    (tmp_path / "ttyUSB0").touch()
    (tmp_path / "ttyACM0").touch()
    (tmp_path / "ttyAMA0").touch()
    byid = tmp_path / "serial" / "by-id"
    byid.mkdir(parents=True)
    os.symlink(tmp_path / "ttyUSB0", byid / "usb-FTDI_FT232R_USB_UART_A10K-if00-port0")
    os.symlink(tmp_path / "ttyAMA0", tmp_path / "serial0")

    ports = dict(configure.detect_serial_ports(str(tmp_path)))
    by_id = str(byid / "usb-FTDI_FT232R_USB_UART_A10K-if00-port0")
    assert "FTDI FT232R USB UART A10K" in ports[by_id]
    assert str(tmp_path / "serial0") in ports
    assert str(tmp_path / "ttyACM0") in ports           # not covered by by-id
    assert str(tmp_path / "ttyUSB0") not in ports       # already listed by its stable name


def test_parse_float_bounds():
    assert configure.parse_float("0.4", 0, 5) == 0.4
    assert configure.parse_float("9", 0, 5) is None
    assert configure.parse_float("abc", 0, 5) is None


def test_build_config_applies_answers_without_touching_base():
    answers = {
        "mode": "live",
        "gnss.port": "/dev/serial0", "gnss.baud": "38400",
        "laser_altimeter.port": "/dev/serial/by-id/x", "laser_altimeter.baud": "115200",
        "laser.unit": "cm", "laser.offset": "0.55",
        "imu_ahrs.port": "/dev/ttyACM0", "imu_ahrs.baud": "921600",
        "trigger.enabled": True, "trigger.pin": "27", "trigger.hz": "2", "trigger.pulse_ms": "10",
        "ssd.mount": "/media/flightssd", "hud.low_agl_ft": "500", "server.port": "8080",
    }
    cfg = configure.build_config(BASE, answers)
    assert cfg["gnss"]["port"] == "/dev/serial0" and cfg["gnss"]["baud"] == 38400
    assert cfg["laser_altimeter"]["unit"] == "cm"
    assert cfg["laser_altimeter"]["mounting_offset_m"] == 0.55
    assert cfg["imu_ahrs"]["baud"] == 921600
    assert cfg["payload_sync"]["gpio_pin"] == 27 and cfg["payload_sync"]["trigger_hz"] == 2.0
    assert cfg["logging"]["primary_dir"] == "/media/flightssd/aerolab_logs"
    assert cfg["hud"]["low_agl_warn_ft"] == 500.0 and cfg["server"]["port"] == 8080
    assert BASE["gnss"]["port"] == "/dev/serial0"   # base untouched


class ScriptedUI:
    """Accepts every default; records what was asked."""

    def __init__(self, picks=None):
        self.asked, self.picks = [], picks or {}

    def menu(self, text, items, default=None):
        self.asked.append(text)
        for k, v in self.picks.items():
            if text.startswith(k):
                return v
        return default if default is not None else items[0][0]

    def checklist(self, text, items, on):
        self.asked.append(text)
        return list(on)

    def text(self, prompt, default):
        self.asked.append(prompt)
        for k, v in self.picks.items():
            if prompt.startswith(k):
                return v
        return str(default)

    def password(self, prompt):
        return "x" * 10

    def yesno(self, prompt, default_yes=True):
        return False

    def info(self, text):
        self.asked.append(text)


def test_sim_mode_skips_port_questions():
    ui = ScriptedUI({"How should": "sim"})
    answers, extras = configure.interview(ui, BASE)
    assert answers["mode"] == "sim"
    assert not any("port is the" in q for q in ui.asked)
    assert answers["profile"] == "airborne-snow-survey" and extras == {}


def test_default_interview_prefills_the_real_snow_survey_setup():
    ui = ScriptedUI()
    answers, _ = configure.interview(ui, BASE)
    cfg = configure.build_config(BASE, answers)
    assert cfg["gnss"]["device"] == "emlid-reach-m2" and cfg["gnss"]["driver"] == "nmea"
    assert cfg["gnss"]["baud"] == 115200 and cfg["gnss"]["rate_hz"] == 10
    assert cfg["laser_altimeter"]["device"] == "aglaser-ilm-150-r"
    assert cfg["laser_altimeter"]["baud"] == 9600 and cfg["laser_altimeter"]["max_range_m"] == 150
    assert cfg["imu_ahrs"]["enabled"] is False and cfg["imu_ahrs"]["name"] == "No attitude sensor"
    assert [p["id"] for p in cfg["payloads"]] == ["cresis-snow-radar", "micasense-rededge-p-timer"]
    assert cfg["payload_sync"]["enabled"] is False          # camera captures on its own; radar is CReSIS's
    cam = cfg["camera"]
    assert cam["exposure_gpio"] == 27 and cam["exposure_edge"] == "falling" and cam["expected_hz"] == 1.0
    assert cam["status_url"] == "http://192.168.1.83/status"
    assert cfg["laser_altimeter"]["driver_options"] == {"invalid": [99999.99]}
    assert cfg["hud"]["survey_agl_ft"] == 450
    warn = [q for q in ui.asked if q.startswith("Laser range check")]
    assert warn and "close to its limit" in warn[0]          # 450 ft = 137 m of a 150 m laser


def test_ilm150_at_radar_height_warns():
    ui = ScriptedUI({"Planned survey height": "2250"})
    configure.interview(ui, BASE)
    warn = [q for q in ui.asked if q.startswith("Laser range check")]
    assert warn and "ILM-150-R" in warn[0] and "read nothing" in warn[0]


def test_short_range_laser_at_survey_height_warns():
    ui = ScriptedUI({"Laser altimeter:": "benewake-tf03-180", "Planned survey height": "2250"})
    configure.interview(ui, BASE)
    warn = [q for q in ui.asked if q.startswith("Laser range check")]
    assert warn and "read nothing" in warn[0]


def test_range_warning_levels():
    tf03 = {"name": "TF03", "max_range_m": 180}
    assert configure.range_warning(tf03, 400) is None
    assert "close to its limit" in configure.range_warning(tf03, 500)
    assert "read nothing" in configure.range_warning(tf03, 2250)
    assert configure.range_warning({"name": "g", "max_range_m": 0}, 9000) is None


def test_apply_device_keeps_port_and_offset_drops_old_driver_fields():
    old = {"port": "/dev/ttyUSB3", "mounting_offset_m": 0.7, "driver": "ascii_number", "poll": "d\r\n"}
    dev = {"id": "x", "name": "X", "driver": "benewake", "baud": 115200, "driver_options": {"invalid_cm": [18000]}}
    out = configure.apply_device(old, dev)
    assert out["port"] == "/dev/ttyUSB3" and out["mounting_offset_m"] == 0.7
    assert out["driver"] == "benewake" and "poll" not in out


def test_needs_extras():
    cat = configure.load_catalog()
    prof = configure.by_id(cat["profiles"])["flight-controller"]
    cfg = configure.build_config(BASE, {**configure.profile_answers(prof, BASE, cat), "buddy.model": "qwen3.5:2b"}, cat)
    assert configure.needs_extras(cfg) == ["pymavlink", "ollama:qwen3.5:2b"]


def test_every_profile_builds_a_config_the_daemon_accepts(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
    import drivers
    import main
    cat = configure.load_catalog()
    for prof in cat["profiles"]:
        for mode in ("live", "sim"):
            cfg = configure.build_config(BASE, configure.profile_answers(prof, BASE, cat, mode), cat)
            cfg["logging"]["fallback_dir"] = str(tmp_path / prof["id"])
            cfg["calibration_file"] = str(tmp_path / "cal.json")
            for sec in ("gnss", "laser_altimeter", "imu_ahrs"):
                c = cfg[sec]
                if main.section_enabled(cfg, sec):
                    assert c["driver"] in drivers.DRIVERS, (prof["id"], sec)
            d = main.AeroLabDaemon(cfg, sim_mode=mode == "sim")
            assert d.buddy.configured["imu"] == (prof["attitude"] != "none-post"), prof["id"]


def test_written_config_loads_in_daemon(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
    import main
    cfg = configure.build_config(BASE, {"mode": "sim"})  # no device answers: base sections kept
    cfg["logging"]["fallback_dir"] = str(tmp_path)
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    out = tmp_path / "local.yaml"
    configure.write_config(cfg, out)
    loaded = yaml.safe_load(out.read_text())
    assert loaded["mode"] == "sim"
    d = main.AeroLabDaemon(loaded, sim_mode=loaded["mode"] == "sim")
    assert d.sim_mode and d.log_dir == tmp_path / "sim"


def test_ask_port_shows_short_tags_returns_stable_path(tmp_path):
    (tmp_path / "ttyUSB0").touch()
    link = tmp_path / "usb-FTDI_X-if00-port0"
    os.symlink(tmp_path / "ttyUSB0", link)
    stable = str(tmp_path / "serial" / "by-id" / "x")  # path text must contain /by-id/
    (tmp_path / "serial" / "by-id").mkdir(parents=True)
    os.symlink(tmp_path / "ttyUSB0", stable)
    seen = {}

    class UI:
        def menu(self, text, items, default=None):
            seen["items"], seen["default"] = items, default
            return "ttyUSB0"

    ports = [(stable, "FTDI X (ttyUSB0)"), ("/dev/serial0", "Pi UART")]
    got = configure.ask_port(UI(), "laser", "", "/dev/ttyUSB1", ports)
    tags = [t for t, _ in seen["items"]]
    assert tags == ["ttyUSB0", "serial0", "ttyUSB1", "other"]
    assert seen["default"] == "ttyUSB1"     # current setting stays selected
    assert got == stable                    # short tag maps back to the stable path
