import json
import math
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import calibration as C  # noqa: E402
import integrity as I  # noqa: E402


def capture_with(offset=0.62, rb=0.8, pb=-0.5, laser_sd=0.01, imu_sd=0.03, spikes=5, n=200, seed=2):
    rnd = random.Random(seed)
    g = C.GroundCapture(seconds=1, ref_roll=0.0, ref_pitch=0.0, field_elev_m=100.0, antenna_height_m=1.5)
    for i in range(n):
        las = offset + rnd.gauss(0, laser_sd)
        if i < spikes:
            las = 3.0                                       # someone walked under the laser
        lat, lon = I.project(38.5, -121.4, rnd.uniform(0, 360), abs(rnd.gauss(0, 0.5)))
        g.add(laser_raw=las, roll_raw=rb + rnd.gauss(0, imu_sd), pitch_raw=pb + rnd.gauss(0, imu_sd),
              lat=lat, lon=lon, alt=101.5 + rnd.gauss(0, 0.5), fix=1, sats=12, hdop=0.9, clock_offset=0.05,
              trigger_count=i)
    return g


def test_ground_capture_recovers_offset_and_bias_despite_spikes():
    g = capture_with()
    res = g.finish(C.Calibration(), trigger_enabled=True, configured={"gnss": True, "laser": True, "imu": True})
    assert res["status"] == "ok", res
    assert res["proposed"]["laser_offset_m"] == pytest.approx(0.62, abs=0.005)
    assert res["proposed"]["imu_roll_bias_deg"] == pytest.approx(0.8, abs=0.02)
    assert res["proposed"]["imu_pitch_bias_deg"] == pytest.approx(-0.5, abs=0.02)
    laser = next(i for i in res["items"] if i["item"] == "Laser")
    assert "5 outliers set aside" in laser["detail"]


def test_reference_attitude_is_respected():
    g = capture_with(rb=2.3, pb=1.1)
    g.ref_roll, g.ref_pitch = 1.5, 1.0                     # the ramp slopes; inclinometer says so
    res = g.finish(C.Calibration(), False, {"gnss": True, "laser": True, "imu": True})
    assert res["proposed"]["imu_roll_bias_deg"] == pytest.approx(0.8, abs=0.02)
    assert res["proposed"]["imu_pitch_bias_deg"] == pytest.approx(0.1, abs=0.02)


def test_noisy_or_short_capture_is_graded():
    res = capture_with(laser_sd=0.3, imu_sd=1.0).finish(C.Calibration(), False,
                                                        {"gnss": True, "laser": True, "imu": True})
    st = {i["item"]: i["status"] for i in res["items"]}
    assert st["Laser"] == "warn" and st["Attitude"] == "warn"
    res = capture_with(n=5).finish(C.Calibration(), False, {"gnss": True, "laser": True, "imu": True})
    assert res["status"] == "fail"


def test_field_elevation_check():
    g = capture_with()
    g.field_elev_m = 160.0                                  # GNSS says ~101 m: 60 m off
    res = g.finish(C.Calibration(), False, {"gnss": True, "laser": True, "imu": True})
    alt = next(i for i in res["items"] if i["item"] == "GNSS altitude")
    assert alt["status"] == "warn"


def test_apply_persists_and_keeps_history(tmp_path):
    path = tmp_path / "cal.json"
    cal = C.Calibration()
    C.apply_proposed(cal, {"laser_offset_m": 0.62, "imu_roll_bias_deg": 0.8}, path)
    back = C.Calibration.load(path)
    assert back.laser_offset_m == 0.62 and back.imu_roll_bias_deg == 0.8 and back.updated
    assert back.history[-1]["before"]["laser_offset_m"] is None
    assert back.correct_attitude(10.8, 0.0) == (pytest.approx(10.0), 0.0)
    assert C.Calibration.load(tmp_path / "missing.json").laser_offset_m is None


def flight(br=1.2, bp=0.0, h=650.0, n=1500, climb=True, seed=4):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        ro = 22 * math.sin(i / 60)
        pi = 2 + 1.0 * math.sin(i / 90)
        hh = h + (20 * math.sin(i / 200) if climb else 0)
        rng = (hh + 0.62) / (math.cos(math.radians(ro + br)) * math.cos(math.radians(pi + bp))) + rnd.gauss(0, 0.05)
        out.append((rng, ro, pi, hh + 100 + rnd.gauss(0, 0.5)))
    return out


def test_boresight_fit_vs_gnss_altitude_handles_climbs():
    fit = C.fit_boresight(flight(), 0.62)
    assert fit["method"] == "vs GNSS altitude"
    assert fit["boresight_roll_deg"] == pytest.approx(1.2, abs=0.1)
    assert not fit["pitch_fitted"]                          # pitch barely varies: not observable
    assert fit["spread_after_m"] < fit["spread_before_m"] / 3


def test_boresight_fit_constant_height_without_reference():
    s = [(r, ro, pi) for r, ro, pi, _ in flight(climb=False)]
    assert C.fit_boresight(s, 0.62)["boresight_roll_deg"] == pytest.approx(1.2, abs=0.05)


def test_pitch_boresight_when_observable():
    rnd = random.Random(5)
    s = []
    for i in range(2000):
        ro, pi = 20 * math.sin(i / 50), 3 + 6 * math.sin(i / 37)
        rng = 600.62 / (math.cos(math.radians(ro + 0.5)) * math.cos(math.radians(pi - 0.9))) + rnd.gauss(0, 0.03)
        s.append((rng, ro, pi, 700 + rnd.gauss(0, 0.3)))
    fit = C.fit_boresight(s, 0.62)
    assert fit["pitch_fitted"]
    assert fit["boresight_roll_deg"] == pytest.approx(0.5, abs=0.1)
    assert fit["boresight_pitch_deg"] == pytest.approx(-0.9, abs=0.1)


def test_calibration_endpoints_in_sim(tmp_path):
    """Sim parks the aircraft during a ground capture; the known sim installation errors are recovered."""
    import yaml
    from fastapi.testclient import TestClient
    import main
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "sensors.yaml").read_text())
    cfg["logging"]["fallback_dir"] = str(tmp_path)
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    cfg["self_test"] = {"delay_s": 1.0}
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    with TestClient(main.create_app(d)) as c:
        c.post("/api/cal/ground", json={"seconds": 3})
        for _ in range(60):
            st = c.get("/api/cal").json()
            if st.get("capture", {}).get("done"):
                break
            time.sleep(0.2)
        res = st["capture"]["result"]
        assert res["proposed"]["laser_offset_m"] == pytest.approx(0.62, abs=0.01)
        assert res["proposed"]["imu_roll_bias_deg"] == pytest.approx(0.8, abs=0.05)
        assert c.post("/api/cal/apply").status_code == 200
        assert json.loads((tmp_path / "cal.json").read_text())["laser_offset_m"] == pytest.approx(0.62, abs=0.01)
        bit = c.get("/api/bit").json()
        assert bit["done"] and {i["item"] for i in bit["items"]} >= {"Configuration", "Log storage"}
        fid = c.post("/api/flight/new").json()["flight_id"]
        assert fid >= 2
