"""End-to-end accuracy: the real daemon against simulated serial sensors with known truth.

Slow (about 50 s per profile). Skip with: pytest -m "not slow".
Every profile: python tools/virtual_flight.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("profile", ["airborne-snow-survey", "long-range-survey", "survey-budget"])
def test_virtual_flight_accuracy(profile):
    pytest.importorskip("pymavlink")
    import virtual_flight as vf
    r = vf.run_profile(profile, flight_s=30.0, verbose=False)
    s, cal, t, fit = r["score"], r["calibration"], r["truth"], r["boresight_fit"]

    # Nothing lost: every clean laser frame the daemon could read was logged
    assert r["completeness"]["pct"] >= 99.9
    # Every laser reading matched to a position on GNSS time
    rd = r["readings"]
    assert rd["not_matched"] <= 0.02 * rd["laser_logged"]
    assert s["laser_point_position_m"]["p95"] < 2.0
    # Ground calibration recovers the installation errors
    assert r["ground_cal"]["status"] in ("ok", "warn")
    assert cal["laser_offset_m"] == pytest.approx(t["laser_offset_m"], abs=0.02)
    if not r["has_attitude"]:
        assert s["agl_m"]["p95"] < 0.01 * s["survey_agl_m"] + 0.3     # straight legs, no tilt correction
    else:
        assert cal["imu_roll_bias_deg"] == pytest.approx(t["imu_roll_bias_deg"], abs=0.05)
        assert cal["imu_pitch_bias_deg"] == pytest.approx(t["imu_pitch_bias_deg"], abs=0.05)
        # Boresight fit from banks over flat ground
        assert fit["boresight_roll_deg"] == pytest.approx(t["boresight_roll_deg"], abs=0.15)
        # AGL after calibration: better than 0.5% of height (95th percentile)
        assert s["agl_after_boresight_m"]["p95"] < 0.005 * s["survey_agl_m"] + 0.2
        assert abs(s["agl_after_boresight_m"]["mean"]) < 0.002 * s["survey_agl_m"] + 0.1
        # Attitude after bias correction
        assert s["roll_deg"]["p95"] < 0.3 and abs(s["roll_deg"]["mean"]) < 0.05
    # Geotags: extrapolated to the trigger instant, and better than the raw last fix
    assert s["geotag_extrapolated_m"]["p95"] < 2.0
    assert s["geotag_extrapolated_m"]["p95"] <= s["geotag_last_fix_m"]["p95"]
    # Self-test ran, saw every sensor, and noticed the injected corrupted frames (checksummed protocols)
    items = {i["item"]: i for i in r["bit"]["items"]}
    assert r["bit"]["done"] and r["bit"]["status"] != "fail"
    if profile == "survey-budget":
        assert any("bad frames" in i["detail"] for i in items.values())
    # Spikes and corrupted frames were injected and did not reach AGL (covered by the AGL limits above)
    assert sum(v["corrupted"] for v in r["sim"].values()) > 0
