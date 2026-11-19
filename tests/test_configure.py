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
    assert cfg["gnss"] == {"port": "/dev/serial0", "baud": 38400}
    assert cfg["laser_altimeter"]["unit"] == "cm"
    assert cfg["laser_altimeter"]["mounting_offset_m"] == 0.55
    assert cfg["imu_ahrs"]["baud"] == 921600
    assert cfg["payload_sync"]["gpio_pin"] == 27 and cfg["payload_sync"]["trigger_hz"] == 2.0
    assert cfg["logging"]["primary_dir"] == "/media/flightssd/aerolab_logs"
    assert cfg["hud"]["low_agl_warn_ft"] == 500.0 and cfg["server"]["port"] == 8080
    assert BASE["gnss"]["port"] == "/dev/serial0"   # base untouched


def test_sim_mode_skips_sensor_questions():
    asked = []

    class FakeUI:
        def menu(self, text, items, default=None):
            asked.append(text)
            return "sim" if text.startswith("How should") else default
        def text(self, prompt, default):
            asked.append(prompt)
            return str(default)
        def password(self, prompt):
            return "x" * 10
        def yesno(self, prompt, default_yes=True):
            return False
        def info(self, text):
            pass

    answers, extras = configure.interview(FakeUI(), BASE)
    assert answers["mode"] == "sim"
    assert not any("port is the" in q for q in asked)
    assert answers["trigger.enabled"] is False and extras == {}


def test_written_config_loads_in_daemon(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
    import main
    cfg = configure.build_config(BASE, {"mode": "sim"})
    cfg["logging"]["fallback_dir"] = str(tmp_path)
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
