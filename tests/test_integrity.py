import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import integrity as I  # noqa: E402
import main  # noqa: E402


def rot(roll, pitch, yaw):
    """Body-to-world rotation (ZYX: yaw, then pitch, then roll), built independently of compute_agl."""
    r, p, y = (math.radians(a) for a in (roll, pitch, yaw))
    Rz = [[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]]
    Ry = [[math.cos(p), 0, math.sin(p)], [0, 1, 0], [-math.sin(p), 0, math.cos(p)]]
    Rx = [[1, 0, 0], [0, math.cos(r), -math.sin(r)], [0, math.sin(r), math.cos(r)]]
    mul = lambda A, B: [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mul(mul(Rz, Ry), Rx)


def test_agl_formula_matches_rotation_geometry():
    """A laser along body +z (down) hits flat ground: vertical drop = range x (world-z of the beam)."""
    rnd = random.Random(3)
    for _ in range(500):
        roll, pitch, yaw = rnd.uniform(-45, 45), rnd.uniform(-20, 20), rnd.uniform(0, 360)
        h = rnd.uniform(1, 3000)
        R = rot(roll, pitch, yaw)
        down_z = R[2][2]                       # world z-component of the body down axis
        rng = h / down_z                       # slant range to flat ground
        assert main.compute_agl(rng, roll, pitch, 0.0) == pytest.approx(h, rel=1e-9)
        assert I.off_nadir_deg(roll, pitch) == pytest.approx(math.degrees(math.acos(down_z)), abs=1e-6)


def test_error_budget_attitude_error_at_bank():
    """1 deg of roll error at 20 deg bank and 700 m: about 4.6 m of AGL error. Documented in the README."""
    h = 700.0
    rng = h / math.cos(math.radians(20))
    err = main.compute_agl(rng, 21, 0, 0) - h
    assert -4.7 < err < -4.4


def test_laser_gate_rejects_spikes_without_lag_and_accepts_real_steps():
    g = I.LaserGate(abs_m=5, rel=0.05)
    hs = [600.0 + 0.5 * i for i in range(10)]
    assert all(g.accept(h) for h in hs)
    assert not g.accept(150.0)                 # a bird
    assert g.accept(605.2)                     # next good sample accepted immediately
    # a real cliff: 3 consecutive readings at the new height are accepted
    assert not g.accept(400.0) and not g.accept(400.5)
    assert g.accept(401.0) and g.accept(401.2)
    assert g.spikes == 1


def test_agl_quality():
    assert I.agl_quality(10, 3, 500, 2500, 20) == "ok"
    assert I.agl_quality(25, 0, 500, 2500, 20) == "tilt"
    assert I.agl_quality(5, 0, 2450, 2500, 20) == "range"


def test_sensor_monitor_rate_errors_age():
    m = I.SensorMonitor(expected_hz=10)
    t = 1000.0
    for i in range(50):
        m.on_reading(t + i * 0.1)
    now = t + 4.95
    assert m.rate_hz(now) == pytest.approx(10.0, rel=0.02)
    m.on_counters(t, 0, 0)
    m.on_counters(t + 4, 95, 5)
    assert m.error_pct() == pytest.approx(5.0)
    assert m.snapshot(now)["age_ms"] == 50
    assert m.rate_hz(now + 3.0) == 0.0         # gone quiet


def test_gnss_jump_detection():
    g = I.GnssIntegrity(max_speed_ms=120)
    assert not g.check(0.0, 38.0, -121.0)
    lat, lon = I.project(38.0, -121.0, 90, 55)  # 55 m in 1 s: fine
    assert not g.check(1.0, lat, lon)
    lat2, lon2 = I.project(lat, lon, 90, 900)   # 900 m in 1 s: a jump
    assert g.check(2.0, lat2, lon2) and g.last_jump_m == pytest.approx(900, rel=0.01)


def test_heading_check_only_in_straight_flight():
    h = I.HeadingCheck(max_diff_deg=20, window_s=15)
    for i in range(40):
        h.add(i * 0.5, 100.0, 70.0, 50.0, 1.0)  # 30 deg off, straight and level
    assert h.diff(20.0) == pytest.approx(30.0)
    h.add(20.5, 100.0, 70.0, 50.0, 15.0)        # banking: comparison suspended
    assert h.diff(20.5) is None


def test_storage_test(tmp_path):
    r = I.storage_test(tmp_path)
    assert r["status"] in ("ok", "warn") and "write+fsync+read" in r["detail"]
    r = I.storage_test(tmp_path / "missing")
    assert r["status"] == "fail"


def test_config_checks_catch_missing_port_and_range():
    cfg = {"gnss": {"driver": "nmea", "port": "/dev/does-not-exist"},
           "laser_altimeter": {"driver": "benewake", "port": "/dev/null", "mounting_offset_m": 0.4, "max_range_m": 180},
           "imu_ahrs": {"driver": "csv_rpy", "port": "/dev/null"}, "hud": {"survey_agl_ft": 2250}}
    items = I.config_checks(cfg, False, {"gnss": True, "laser_altimeter": True, "imu_ahrs": True},
                            {"nmea", "benewake", "csv_rpy"})
    text = " ".join(i["detail"] for i in items)
    assert "does not exist" in text and "also used by" in text and "beyond the laser" in text


def test_ubx_rate_command_matches_published_bytes():
    import drivers
    # UBX-CFG-RATE 1 Hz, as printed in u-blox documentation and countless forum posts.
    assert drivers.ubx_rate_legacy(1000).hex() == "b562060806 00e803010001000139".replace(" ", "")


def test_tilt_callout_is_debounced(tmp_path):
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "sensors.yaml").read_text())
    cfg["logging"]["fallback_dir"] = str(tmp_path)
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    d.bit["done"] = True
    s = d.state
    s.laser_ok = s.imu_ok = True
    t = 1000.0
    texts = lambda: [e.text for e in d.buddy.events]
    s.agl_quality = "tilt"
    d.integrity_tick(t); d.integrity_tick(t + 1)            # a brief bank: no callout
    s.agl_quality = "ok"
    d.integrity_tick(t + 2)
    assert not any("less reliable" in x for x in texts())
    s.agl_quality = "tilt"
    for k in range(5):                                       # a sustained steep turn: one callout
        d.integrity_tick(t + 3 + k)
    assert sum("less reliable" in x for x in texts()) == 1
