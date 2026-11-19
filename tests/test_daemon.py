import sqlite3
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import main  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "config" / "sensors.yaml"


def test_agl_level_flight_subtracts_offset():
    assert main.compute_agl(100.0, 0.0, 0.0, 0.4) == pytest.approx(99.6)


def test_agl_bank_uses_vertical_component():
    # 60 degree bank: slant 100 m is 50 m straight down.
    assert main.compute_agl(100.0, 60.0, 0.0, 0.0) == pytest.approx(50.0)


def test_agl_pitch_and_roll_combine():
    assert main.compute_agl(100.0, 60.0, 60.0, 0.0) == pytest.approx(25.0)


def test_agl_never_negative_on_ground():
    assert main.compute_agl(0.2, 0.0, 0.0, 0.4) == 0.0


@pytest.mark.parametrize("line,unit,expected", [
    ("123.45\r\n", "m", 123.45),
    ("12345 cm\r\n", "cm", 123.45),
    ("45.2m\n", "m", 45.2),
    ("", "m", None),
    ("   \r\n", "m", None),
    ("ERR\n", "m", None),
    ("-1.0\n", "m", None),
    ("nan\n", "m", None),
])
def test_parse_laser_line(line, unit, expected):
    got = main.parse_laser_line(line, unit)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize("line,expected", [
    ("1.5,-2.25,270\r\n", (1.5, -2.25, 270.0)),
    ("1,2,3,extra\n", (1.0, 2.0, 3.0)),
    ("1,2\n", None),
    ("a,b,c\n", None),
    ("\n", None),
])
def test_parse_imu_line(line, expected):
    assert main.parse_imu_line(line) == expected


def test_log_dir_falls_back_when_ssd_not_mounted(tmp_path):
    # tmp_path's parent is a plain directory, not a mount point.
    primary = tmp_path / "ssd" / "aerolab_logs"
    fallback = tmp_path / "logs"
    assert main.resolve_log_dir(str(primary), str(fallback), False) == fallback


def test_log_dir_sim_never_touches_flight_logs(tmp_path):
    assert main.resolve_log_dir("/", str(tmp_path), True) == tmp_path / "sim"


def _cfg(tmp_path):
    cfg = yaml.safe_load(CONFIG.read_text())
    cfg["logging"]["fallback_dir"] = str(tmp_path)
    cfg["payload_sync"]["trigger_hz"] = 10.0
    cfg["logging"]["log_hz"] = 20
    return cfg


def test_sim_end_to_end_logs_and_serves(tmp_path):
    from fastapi.testclient import TestClient

    daemon = main.AeroLabDaemon(_cfg(tmp_path), sim_mode=True)
    app = main.create_app(daemon)
    with TestClient(app) as client:
        assert "AeroLab HUD" in client.get("/").text
        time.sleep(1.0)
        state = client.get("/api/state").json()
        assert state["sim_mode"] is True
        assert state["laser_ok"] and state["imu_ok"] and state["gnss_ok"]
        assert state["agl_m"] > 0
        assert state["logging_active"] is True
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert msg["sim_mode"] is True

    db = tmp_path / "sim" / "aerolab_flight.db"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] > 5
    assert conn.execute("SELECT COUNT(*) FROM payload_triggers").fetchone()[0] > 3
    conn.close()
    csvs = list((tmp_path / "sim").glob("flight_*.csv"))
    assert len(csvs) == 1 and csvs[0].read_text().count("\n") > 5
