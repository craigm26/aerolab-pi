"""Tests for post-landing camera image copy over HTTP (/files/*)."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
import time
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "scripts"))
import camera_copy  # noqa: E402
import diagnose  # noqa: E402
import main  # noqa: E402
import survey  # noqa: E402

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])

SAMPLE_FILES: dict[str, bytes] = {
    "0000SET/paramlog.dat": b"PARAMLOG-HEADER\n" + bytes(range(256)),
    "0000SET/000/IMG_0000_1.tif": b"II*\x00" + (b"band1-data" * 120),
    "0000SET/000/IMG_0000_2.tif": b"II*\x00" + (b"band2-data" * 140),
    "0001SET/000/IMG_0001_1.tif": b"II*\x00" + (b"band3-data" * 160),
}


def _build_tree(files_map: dict[str, bytes]) -> dict[str, dict]:
    """Build MicaSense /files/* directory JSON entries from relative path -> bytes."""
    dirs: dict[str, dict] = {"/files": {"files": [], "directories": []}}
    for rel, data in sorted(files_map.items()):
        parts = rel.split("/")
        cur = "/files"
        for seg in parts[:-1]:
            if seg not in dirs[cur]["directories"]:
                dirs[cur]["directories"].append(seg)
            cur = f"{cur}/{seg}"
            dirs.setdefault(cur, {"files": [], "directories": []})
        dirs[cur]["files"].append({"name": parts[-1], "size": len(data)})
    return dirs


def start_fake_camera_server(
    files_map: dict[str, bytes] | None = None,
    truncate: dict[str, int] | None = None,
    gate_path: str | None = None,
    gate_entered: threading.Event | None = None,
    gate_release: threading.Event | None = None,
    chunk_delay_s: float = 0.0,
):
    """Run a threaded fake MicaSense RedEdge-P HTTP server serving real bytes."""
    files_map = dict(SAMPLE_FILES if files_map is None else files_map)
    truncate = dict(truncate or {})
    tree = _build_tree(files_map)
    file_routes = {f"/files/{rel}": data for rel, data in files_map.items()}
    requests_seen: list[str] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            key = self.path.rstrip("/") or "/"
            with lock:
                requests_seen.append(key)
            if key == "/status":
                self._send_json(200, {"sd_status": "Ok", "sd_gb_free": 42.0, "auto_cap_active": False})
                return
            if key in tree:
                self._send_json(200, tree[key])
                return
            if key in file_routes:
                rel = key[len("/files/"):]
                if gate_path and rel == gate_path:
                    if gate_entered is not None:
                        gate_entered.set()
                    if gate_release is not None:
                        gate_release.wait(timeout=10.0)
                payload = file_routes[key]
                if rel in truncate:
                    payload = payload[: truncate[rel]]
                self.send_response(200)
                self.send_header("content-type", "application/octet-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                try:
                    if chunk_delay_s > 0 and len(payload) > 64:
                        step = max(1, len(payload) // 4)
                        for i in range(0, len(payload), step):
                            self.wfile.write(payload[i : i + step])
                            self.wfile.flush()
                            time.sleep(chunk_delay_s)
                    else:
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._send_json(404, {"error": f"not found: {self.path}"})

        def _send_json(self, code: int, body: dict):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, requests_seen, lock


def sim_cfg(tmp_path: Path, status_url: str = "", start_on_ground: bool = True) -> dict:
    import configure
    cat = configure.load_catalog()
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "sim"), cat)
    cfg["camera"]["expected_hz"] = 2.0
    cfg["camera"]["status_url"] = status_url
    cfg["logging"]["fallback_dir"] = str(tmp_path / "logs")
    cfg["logging"]["mirror_dirs"] = []
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    cfg["self_test"] = {"delay_s": 0.5}
    cfg["sim"] = {"bank_deg": 3.0, "start_on_ground": start_on_ground}
    return cfg


def _wait_for_state(client: TestClient, states: set[str], timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    st = {}
    while time.monotonic() < deadline:
        st = client.get("/api/camera/copy").json()
        if st.get("state") in states:
            return st
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for state in {states}; last status: {st}")


def test_full_copy_sizes_manifest_report_and_callouts(tmp_path):
    srv, _, _ = start_fake_camera_server()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url), sim_mode=True)
        with TestClient(main.create_app(d)) as c:
            time.sleep(0.4)
            state_payload = c.get("/api/state").json()
            assert "_camera_copy" in state_payload
            assert state_payload["_camera_copy"]["state"] == "idle"
            assert state_payload["_camera_copy"]["can_start"] is True

            res = c.post("/api/camera/copy/start").json()
            assert res["ok"] is True, res
            st = _wait_for_state(c, {"done", "failed"})
            assert st["state"] == "done", st
            assert st["files_total"] == len(SAMPLE_FILES)
            assert st["files_done"] == len(SAMPLE_FILES)
            assert st["files_copied"] == len(SAMPLE_FILES)
            assert st["files_skipped"] == 0
            assert st["files_failed"] == 0
            assert st["bytes_done"] == sum(len(b) for b in SAMPLE_FILES.values())
            assert st["bytes_total"] == st["bytes_done"]

            dest = Path(st["dest_dir"])
            assert dest.parent == d.log_dir / "camera_images"
            assert not list(dest.rglob("*.part"))
            for rel, expected in SAMPLE_FILES.items():
                p = dest / rel
                assert p.is_file()
                assert p.read_bytes() == expected

            assert (dest / "MANIFEST.sha256").is_file()
            assert survey.verify(dest)

            # Tampering with a copied file must fail MANIFEST.sha256 verification.
            tampered = dest / "0000SET/paramlog.dat"
            orig_bytes = tampered.read_bytes()
            tampered.write_bytes(b"corrupted")
            assert not survey.verify(dest)
            tampered.write_bytes(orig_bytes)
            assert survey.verify(dest)

            report = json.loads((dest / "copy_report.json").read_text())
            assert report["state"] == "done"
            assert report["started_utc"].endswith("Z")
            assert report["finished_utc"].endswith("Z")
            assert report["files_total"] == len(SAMPLE_FILES)
            assert report["files_copied"] == len(SAMPLE_FILES)
            assert report["files_skipped"] == 0
            assert report["files_failed"] == 0
            assert sorted(report["copied"]) == sorted(SAMPLE_FILES)
            assert report["failed"] == []
            assert report["bytes_copied"] == sum(len(b) for b in SAMPLE_FILES.values())
            assert isinstance(report["average_mb_per_s"], (int, float))
            assert len(report["camera_files"]) == len(SAMPLE_FILES)
    finally:
        srv.shutdown()
        srv.server_close()

    texts = [e.text for e in d.buddy.events]
    assert any("Camera image copy started" in t for t in texts)
    assert any("Camera image copy finished" in t and "4 files" in t for t in texts)


def test_cancel_stops_copy_and_resume_skips_completed_files(tmp_path):
    # Files are sorted alphabetically by path:
    # 0: 0000SET/000/IMG_0000_1.tif
    # 1: 0000SET/000/IMG_0000_2.tif
    # 2: 0000SET/paramlog.dat  <-- hold here, cancel, then resume
    # 3: 0001SET/000/IMG_0001_1.tif
    entered = threading.Event()
    release = threading.Event()
    srv, seen, lock = start_fake_camera_server(
        gate_path="0000SET/paramlog.dat",
        gate_entered=entered,
        gate_release=release,
    )
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url), sim_mode=True)
        with TestClient(main.create_app(d)) as c:
            time.sleep(0.4)
            r1 = c.post("/api/camera/copy/start").json()
            assert r1["ok"] is True
            assert entered.wait(timeout=5.0), "copy never reached gated file"

            canc = c.post("/api/camera/copy/cancel").json()
            release.set()
            assert canc["ok"] is True
            st1 = _wait_for_state(c, {"cancelled"})
            assert st1["files_done"] == 2
            dest = Path(st1["dest_dir"])
            assert (dest / "0000SET/000/IMG_0000_1.tif").is_file()
            assert (dest / "0000SET/000/IMG_0000_2.tif").is_file()
            assert not (dest / "0000SET/paramlog.dat").exists()
            assert not list(dest.rglob("*.part"))

            # Resume: clear request log and start again; completed files must be skipped.
            with lock:
                seen.clear()
            r2 = c.post("/api/camera/copy/start").json()
            assert r2["ok"] is True
            st2 = _wait_for_state(c, {"done", "failed"})
            assert st2["state"] == "done", st2
            assert Path(st2["dest_dir"]) == dest
            assert st2["files_skipped"] == 2
            assert st2["files_copied"] == 2
            assert st2["files_failed"] == 0
            assert survey.verify(dest)

            with lock:
                fetched = list(seen)
            assert "/files/0000SET/000/IMG_0000_1.tif" not in fetched
            assert "/files/0000SET/000/IMG_0000_2.tif" not in fetched
            assert "/files/0000SET/paramlog.dat" in fetched
            assert "/files/0001SET/000/IMG_0001_1.tif" in fetched

            report = json.loads((dest / "copy_report.json").read_text())
            assert report["files_skipped"] == 2
            assert report["files_copied"] == 2
            assert sorted(report["skipped"]) == ["0000SET/000/IMG_0000_1.tif", "0000SET/000/IMG_0000_2.tif"]
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


def test_truncated_file_retried_then_failed_and_diagnose_reports(tmp_path):
    bad_rel = "0000SET/000/IMG_0000_2.tif"
    srv, seen, lock = start_fake_camera_server(truncate={bad_rel: 100})
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url), sim_mode=True)
        d.camera_copy.retry_delay_s = 0.02
        with TestClient(main.create_app(d)) as c:
            time.sleep(0.4)
            assert c.post("/api/camera/copy/start").json()["ok"] is True
            st = _wait_for_state(c, {"done", "failed"})
            assert st["state"] == "failed"
            assert st["files_copied"] == len(SAMPLE_FILES) - 1
            assert st["files_failed"] == 1
            assert [f["path"] for f in st["failed_files"]] == [bad_rel]
            assert "size mismatch" in st["failed_files"][0]["error"]

            with lock:
                bad_attempts = sum(1 for p in seen if p == f"/files/{bad_rel}")
            assert bad_attempts == 1 + camera_copy.MAX_RETRIES

            dest = Path(st["dest_dir"])
            assert not (dest / bad_rel).exists()
            assert not list(dest.rglob("*.part"))
            # Remaining good files are verified by MANIFEST.sha256.
            assert survey.verify(dest)

            # Diagnose reports an info finding naming the failed file with steps.
            diag = c.get("/api/diagnose").json()["findings"]
            cc_finds = [f for f in diag if "camera image copy" in f["title"].lower()]
            assert len(cc_finds) == 1
            assert cc_finds[0]["severity"] == "info"
            assert bad_rel in cc_finds[0]["evidence"]
            assert len(cc_finds[0]["steps"]) >= 2

            # Verify diagnose lists at most 5 failed files when more than 5 failed.
            d.camera_copy.failed_files = [{"path": f"0000SET/IMG_{i}.tif", "error": "size mismatch"} for i in range(7)]
            fs_many = [f for f in diagnose.findings(d, d.catalog) if "camera image copy" in f["title"].lower()]
            assert len(fs_many) == 1
            assert "IMG_0.tif" in fs_many[0]["evidence"] and "IMG_4.tif" in fs_many[0]["evidence"]
            assert "IMG_5.tif" not in fs_many[0]["evidence"]
            assert "+2 more" in fs_many[0]["evidence"]
    finally:
        srv.shutdown()
        srv.server_close()

    assert any(e.level == "warn" and "failed" in e.text.lower() for e in d.buddy.events)


def test_refuses_while_survey_is_recording(tmp_path):
    srv, _, _ = start_fake_camera_server()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url), sim_mode=True)
        with TestClient(main.create_app(d)) as c:
            time.sleep(0.5)
            sv = c.post("/api/survey/start", json={"name": "Leg 1", "force": True}).json()
            assert sv["ok"] is True

            refused = c.post("/api/camera/copy/start").json()
            assert refused["ok"] is False
            assert "survey" in refused["error"].lower()
            assert refused["status"]["can_start"] is False
            assert "survey" in refused["status"]["blocked_reason"].lower()

            c.post("/api/survey/stop")
            assert c.get("/api/camera/copy").json()["can_start"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_refuses_while_moving(tmp_path):
    srv, _, _ = start_fake_camera_server()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        # 1. Direct state check: ground speed 30 m/s with GNSS reporting -> refused.
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url, start_on_ground=True), sim_mode=True)
        d.state.gnss_ok = True
        d.state.gs_ms = 30.0
        d.state.laser_ok = False
        res = asyncio.run(d.camera_copy.start())
        assert res["ok"] is False
        assert "moving" in res["error"].lower() or "speed" in res["error"].lower()

        # GNSS not reporting -> stale ground speed does not block.
        d.state.gnss_ok = False
        assert d.camera_copy.block_reason() is None

        # Valid laser AGL > 5 m -> refused.
        d.state.laser_ok = True
        d.state.agl_m = 25.0
        res_agl = asyncio.run(d.camera_copy.start())
        assert res_agl["ok"] is False
        assert "agl" in res_agl["error"].lower() or "airborne" in res_agl["error"].lower()

        # 2. Live HTTP check with a flying simulation daemon (gs_ms = 55, agl > 50 m).
        d_fly = main.AeroLabDaemon(sim_cfg(tmp_path / "fly", status_url=url, start_on_ground=False), sim_mode=True)
        with TestClient(main.create_app(d_fly)) as c:
            time.sleep(0.4)
            r = c.post("/api/camera/copy/start").json()
            assert r["ok"] is False
            assert r["status"]["can_start"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_refuses_when_disk_space_short(tmp_path, monkeypatch):
    srv, _, _ = start_fake_camera_server()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url), sim_mode=True)
        # Less than the 2 GB reserve -> refused before even downloading.
        monkeypatch.setattr(shutil, "disk_usage", lambda _p: DiskUsage(10 * camera_copy.GB, 9 * camera_copy.GB, 1 * camera_copy.GB))
        with TestClient(main.create_app(d)) as c:
            time.sleep(0.4)
            r = c.post("/api/camera/copy/start").json()
            assert r["ok"] is False
            assert "disk space" in r["error"].lower()
            assert not (d.log_dir / "camera_images").exists()

            # Just over 2 GB free, but less than 2 GB + file sizes -> refused after listing files.
            monkeypatch.setattr(
                shutil,
                "disk_usage",
                lambda _p: DiskUsage(10 * camera_copy.GB, 8 * camera_copy.GB, 2 * camera_copy.GB + 50),
            )
            r2 = c.post("/api/camera/copy/start").json()
            assert r2["ok"] is False
            assert "disk space" in r2["error"].lower()
            assert not (d.log_dir / "camera_images").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_refuses_when_camera_missing_or_no_status_url(tmp_path):
    d_no_url = main.AeroLabDaemon(sim_cfg(tmp_path / "nourl", status_url=""), sim_mode=True)
    r = asyncio.run(d_no_url.camera_copy.start())
    assert r["ok"] is False
    assert "status_url" in r["error"].lower() or "ethernet" in r["error"].lower()

    cfg_no_cam = sim_cfg(tmp_path / "nocam", status_url="")
    cfg_no_cam["camera"] = {"enabled": False}
    d_no_cam = main.AeroLabDaemon(cfg_no_cam, sim_mode=True)
    r2 = asyncio.run(d_no_cam.camera_copy.start())
    assert r2["ok"] is False
    assert "not configured" in r2["error"].lower()


def test_pauses_automatically_and_resumes_when_condition_clears(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    srv, _, _ = start_fake_camera_server(
        gate_path="0000SET/000/IMG_0000_2.tif",
        gate_entered=entered,
        gate_release=release,
    )
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url), sim_mode=True)
        d.camera_copy.safety_check_interval_s = 0.05
        with TestClient(main.create_app(d)) as c:
            time.sleep(0.4)
            assert c.post("/api/camera/copy/start").json()["ok"] is True
            assert entered.wait(timeout=5.0)

            # Trigger pause before the next file starts.
            d.survey.state = "recording"
            release.set()

            st_paused = _wait_for_state(c, {"paused"})
            assert "survey" in st_paused["reason"].lower()

            # Clear condition: copy must resume automatically and complete.
            d.survey.state = "idle"
            st_done = _wait_for_state(c, {"done", "failed"})
            assert st_done["state"] == "done", st_done
            assert survey.verify(Path(st_done["dest_dir"]))
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()

    texts = [e.text for e in d.buddy.events]
    assert any("paused" in t.lower() and "survey" in t.lower() for t in texts)
    assert any("resumed" in t.lower() for t in texts)


def test_shutdown_prepare_warns_when_copy_in_progress(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    srv, _, _ = start_fake_camera_server(
        gate_path="0000SET/000/IMG_0000_2.tif",
        gate_entered=entered,
        gate_release=release,
    )
    try:
        url = f"http://127.0.0.1:{srv.server_port}/status"
        d = main.AeroLabDaemon(sim_cfg(tmp_path, status_url=url), sim_mode=True)
        with TestClient(main.create_app(d)) as c:
            time.sleep(0.4)
            assert c.post("/api/camera/copy/start").json()["ok"] is True
            assert entered.wait(timeout=5.0)

            prep = c.post("/api/shutdown/prepare").json()
            assert prep["state"] == "review"
            assert prep["all_ok"] is True
            cc_steps = [s for s in prep["steps"] if "camera image copy in progress" in s["detail"]]
            assert len(cc_steps) == 1
            assert cc_steps[0]["severity"] == "warn"
            assert f"1 of {len(SAMPLE_FILES)} files; it resumes next time" in cc_steps[0]["detail"]

            c.post("/api/camera/copy/cancel")
            release.set()
            _wait_for_state(c, {"cancelled"})
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()
