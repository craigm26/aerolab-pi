"""START / STOP SURVEY: readiness, confirmation, package with SHA-256 manifest, verified backup copy."""
import json
import sqlite3
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "scripts"))
import main  # noqa: E402
import survey  # noqa: E402


def her_cfg(tmp_path):
    import configure
    cat = configure.load_catalog()
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "sim"), cat)
    cfg["camera"]["expected_hz"] = 2.0
    cfg["logging"]["fallback_dir"] = str(tmp_path / "logs")
    cfg["logging"]["mirror_dirs"] = [str(tmp_path / "usb2")]
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    cfg["self_test"] = {"delay_s": 1.0}
    cfg["sim"] = {"bank_deg": 3.0}
    return cfg


def test_survey_start_confirm_stop_package_and_backup(tmp_path):
    from fastapi.testclient import TestClient
    d = main.AeroLabDaemon(her_cfg(tmp_path), sim_mode=True)
    with TestClient(main.create_app(d)) as c:
        time.sleep(2.5)
        r = c.post("/api/survey/start", json={"name": "Leg 1"}).json()
        if r.get("needs_confirmation"):                     # e.g. not calibrated in this fresh sim
            r = c.post("/api/survey/start", json={"name": "Leg 1", "force": True}).json()
        assert r["ok"]
        assert c.post("/api/survey/start", json={}).json()["ok"] is False   # one at a time
        for _ in range(40):
            st = c.get("/api/survey").json()
            if st["current"].get("confirmation"):
                break
            time.sleep(0.5)
        conf = {i["item"]: i for i in st["current"]["confirmation"]}
        assert all(i["ok"] for i in conf.values()), conf
        assert {"AgLaser ILM-150-R", "Emlid Reach M2 (NMEA)", "MicaSense RedEdge-P", "Disk"} <= set(conf)
        assert "Confirmed" in st["message"]
        time.sleep(2.0)
        res = c.post("/api/survey/stop").json()
        assert res["ok"]
        summ = res["summary"]
        assert summ["laser_readings"] > 20 and summ["gnss_fixes"] > 20 and summ["exposures"] >= 4
        assert summ["readings_lost"] == 0
        pkg = summ["package"]
        assert pkg["ok"] and len(pkg["copies"]) == 2 and all(cp["verified"] for cp in pkg["copies"])
        p = Path(pkg["dir"])
        for f in ("aerolab_flight.db", "laser_points.csv", "exposures.csv", "camera_status.jsonl",
                  "readiness.html", "survey.json", "MANIFEST.sha256"):
            assert (p / f).exists(), f
        # the laser points cover only the survey window
        n_pts = sum(1 for _ in open(p / "laser_points.csv")) - 1
        assert abs(n_pts - summ["laser_readings"]) <= 2
        backup = tmp_path / "usb2" / "surveys" / p.name
        assert survey.verify(backup)
        (backup / "laser_points.csv").write_text("tampered")
        assert not survey.verify(backup)                    # the manifest catches a bad copy
        row = sqlite3.connect(d.db_path).execute("SELECT name, stop_utc_ns, package FROM surveys").fetchone()
        assert row[0] == "Leg 1" and row[1] and json.loads(row[2])["ok"]
        # safe shutdown in simulation: stops nothing real, reports its steps
        assert c.post("/api/shutdown", json={}).status_code == 400
        sd = c.post("/api/shutdown", json={"confirm": "SHUTDOWN"}).json()
        assert sd["state"] == "ready" and any(s["step"] == "Flush data to disk" for s in sd["steps"])


def test_confirmation_names_what_is_missing(tmp_path):
    from fastapi.testclient import TestClient
    d = main.AeroLabDaemon(her_cfg(tmp_path), sim_mode=True)
    with TestClient(main.create_app(d)) as c:
        time.sleep(2.0)
        orig = d.camera.exposure
        d.camera.exposure = lambda *a, **k: None            # camera stops exposing
        r = c.post("/api/survey/start", json={"force": True}).json()
        assert r["ok"]
        for _ in range(40):
            st = c.get("/api/survey").json()
            if st["current"].get("confirmation"):
                break
            time.sleep(0.5)
        bad = [i for i in st["current"]["confirmation"] if not i["ok"]]
        assert [i["item"] for i in bad] == ["MicaSense RedEdge-P"]
        assert "NOT confirmed" in st["message"]
        d.camera.exposure = orig
        c.post("/api/survey/stop")
    assert any("not everything is recording" in e.text for e in d.buddy.events)
