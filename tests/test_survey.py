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


def test_shutdown_prepare_sync_and_confirm_ok(tmp_path):
    from fastapi.testclient import TestClient
    d = main.AeroLabDaemon(her_cfg(tmp_path), sim_mode=True)
    # Seed a raw capture file so final_sync copies and verifies both DB and raw files.
    raw_dir = d.log_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "laser_sample.aeroraw").write_bytes(b"AEROLABRAW1 {}\nrawbytes")
    with TestClient(main.create_app(d)) as c:
        time.sleep(1.5)
        prep = c.post("/api/shutdown/prepare")
        assert prep.status_code == 200
        st = prep.json()
        assert st["state"] == "review"
        assert st["all_ok"] is True
        assert all(s["severity"] == "ok" and s["ok"] for s in st["steps"]), st["steps"]
        sync_root = tmp_path / "usb2" / "final_sync"
        assert sync_root.is_dir()
        sync_dirs = list(sync_root.iterdir())
        assert len(sync_dirs) == 1
        sync_dir = sync_dirs[0]
        assert (sync_dir / "aerolab_flight.db").exists()
        assert (sync_dir / "raw" / "laser_sample.aeroraw").exists()
        assert (sync_dir / "MANIFEST.sha256").exists()
        assert survey.verify(sync_dir)
        # Confirm without override powers off (in sim: state "ready", Pi stays on)
        assert c.post("/api/shutdown/confirm", json={}).status_code == 400
        conf = c.post("/api/shutdown/confirm", json={"confirm": "SHUTDOWN", "override": False})
        assert conf.status_code == 200
        res = conf.json()
        assert res["state"] == "ready"
        assert any(s["step"] == "Flush data to disk" for s in res["steps"])


def test_shutdown_blocks_on_failed_backup_and_allows_override(tmp_path):
    from fastapi.testclient import TestClient
    cfg = her_cfg(tmp_path)
    bad_mirror = tmp_path / "not_a_dir"
    bad_mirror.write_text("file blocking mirror directory")
    cfg["logging"]["mirror_dirs"] = [str(bad_mirror)]
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    with TestClient(main.create_app(d)) as c:
        time.sleep(1.0)
        prep = c.post("/api/shutdown/prepare").json()
        assert prep["state"] == "review"
        assert prep["all_ok"] is False
        fails = [s for s in prep["steps"] if s["severity"] == "fail"]
        assert len(fails) == 1
        assert fails[0]["ok"] is False
        # Confirm without override is refused with 409 and lists the failing steps
        blocked = c.post("/api/shutdown/confirm", json={"confirm": "SHUTDOWN", "override": False})
        assert blocked.status_code == 409
        bdata = blocked.json()
        assert len(bdata["failing_steps"]) == 1
        assert bdata["failing_steps"][0]["severity"] == "fail"
        # Old single-shot endpoint also refuses on failure
        legacy = c.post("/api/shutdown", json={"confirm": "SHUTDOWN"})
        assert legacy.status_code == 409
        # Confirm with override proceeds to "ready" and logs a Buddy alert event
        forced = c.post("/api/shutdown/confirm", json={"confirm": "SHUTDOWN", "override": True})
        assert forced.status_code == 200
        assert forced.json()["state"] == "ready"
    assert any(e.level == "alert" and "forced" in e.text.lower() for e in d.buddy.events)


def test_shutdown_cancel_resets_state_and_no_mirror_warns(tmp_path):
    from fastapi.testclient import TestClient
    cfg = her_cfg(tmp_path)
    cfg["logging"]["mirror_dirs"] = []
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    with TestClient(main.create_app(d)) as c:
        time.sleep(1.5)
        r = c.post("/api/survey/start", json={"name": "Abort leg", "force": True}).json()
        assert r["ok"]
        prep = c.post("/api/shutdown/prepare").json()
        assert prep["state"] == "review"
        assert prep["all_ok"] is True
        warn_steps = [s for s in prep["steps"] if s["severity"] == "warn"]
        assert any("no backup drive configured" in s["detail"] and s["ok"] is False for s in warn_steps)
        # Survey was stopped by prepare
        assert c.get("/api/survey").json()["state"] == "idle"
        # Cancel resets shutdown state back to running and does not restart the survey
        canc = c.post("/api/shutdown/cancel")
        assert canc.status_code == 200
        assert canc.json()["state"] == "running"
        assert d.shutdown_state["state"] == "running"
        assert c.get("/api/survey").json()["state"] == "idle"


def test_unmounted_backup_drive_is_never_written_and_is_flagged(tmp_path, monkeypatch):
    """An unplugged USB drive must not turn into 'backups' on the SD card."""
    from fastapi.testclient import TestClient
    cfg = her_cfg(tmp_path)
    real = main.mount_point
    monkeypatch.setattr(main, "mount_point", lambda p: Path("/") if "usb2" in str(p) else real(p))
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    assert d.usable_mirrors() == ([], [tmp_path / "usb2"])
    assert tmp_path / "usb2" not in d.log_dirs()
    with TestClient(main.create_app(d)) as c:
        time.sleep(2.0)
        assert c.post("/api/survey/start", json={"force": True}).json()["ok"]
        time.sleep(3.0)
        pkg = c.post("/api/survey/stop").json()["summary"]["package"]
        bad = [cp for cp in pkg["copies"] if not cp["verified"]]
        assert len(bad) == 1 and "not mounted" in bad[0]["error"]
        assert "verified on 1 of 2" in d.survey.message
        prep = c.post("/api/shutdown/prepare").json()
        fails = [s for s in prep["steps"] if s["severity"] == "fail"]
        assert fails and "not mounted" in fails[0]["detail"]
        assert c.post("/api/shutdown/confirm", json={"confirm": "SHUTDOWN"}).status_code == 409
        fs = c.get("/api/diagnose").json()["findings"]
        assert any(f["title"].startswith("Backup drive not mounted") for f in fs)
    assert not (tmp_path / "usb2").exists()                 # nothing was written there
