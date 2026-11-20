"""Tests for camera /files tree walker, survey camera_files.json, and backup check tool."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "tools"))
import camera  # noqa: E402
import check_camera_backup  # noqa: E402
import rawlog  # noqa: E402
import survey  # noqa: E402

# Directory tree served by the fake MicaSense RedEdge-P HTTP server, matching
# the documented /files/* JSON format: {"files": [{"name", "size"}], "directories": [...]}.
FAKE_TREE = {
    "/files": {
        "files": [],
        "directories": ["0000SET", "0001SET"],
    },
    "/files/0000SET": {
        "files": [{"name": "paramlog.dat", "size": 120}],
        "directories": ["000"],
    },
    "/files/0000SET/000": {
        "files": [
            {"name": "IMG_0000_1.tif", "size": 500},
            {"name": "IMG_0000_2.tif", "size": 512},
            {"name": "IMG_0000_3.tif", "size": 480},
        ],
        "directories": [],
    },
    "/files/0001SET": {
        "files": [],
        "directories": ["000"],
    },
    "/files/0001SET/000": {
        "files": [
            {"name": "IMG_0001_1.tif", "size": 750},
        ],
        "directories": [],
    },
}

EXPECTED_FILES = [
    {"path": "/files/0000SET/000/IMG_0000_1.tif", "size": 500},
    {"path": "/files/0000SET/000/IMG_0000_2.tif", "size": 512},
    {"path": "/files/0000SET/000/IMG_0000_3.tif", "size": 480},
    {"path": "/files/0000SET/paramlog.dat", "size": 120},
    {"path": "/files/0001SET/000/IMG_0001_1.tif", "size": 750},
]


def start_fake_camera(tree: dict | None = None):
    tree = FAKE_TREE if tree is None else tree
    requests_seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(("GET", self.path))
            key = self.path.rstrip("/") or "/"
            if key == "/status":
                body = {"sd_status": "Ok", "sd_gb_free": 42.0}
                self._send_json(200, body)
                return
            if key in tree:
                self._send_json(200, tree[key])
                return
            self._send_json(404, {"error": f"not found: {self.path}"})

        def _send_json(self, code: int, body: dict):
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, requests_seen


def test_list_files_walks_tree():
    srv, seen = start_fake_camera()
    try:
        cam = camera.Camera({"exposure_gpio": None, "status_url": f"http://127.0.0.1:{srv.server_port}/status"})
        files, err = asyncio.run(cam.list_files())
    finally:
        srv.shutdown()
        srv.server_close()
    assert err is None
    assert files == EXPECTED_FILES
    # Read-only directory walk: only GET requests, and no image file downloaded.
    assert all(method == "GET" for method, _ in seen)
    assert not any(path.endswith(".tif") or path.endswith(".dat") for _, path in seen)


def test_list_files_preserves_optional_mtime():
    tree = {
        "/files": {
            "files": [{"name": "IMG_0000_1.tif", "size": 256, "mtime": "2026-11-19T12:00:00Z"}],
            "directories": [],
        }
    }
    srv, _ = start_fake_camera(tree)
    try:
        cam = camera.Camera({"exposure_gpio": None, "status_url": f"http://127.0.0.1:{srv.server_port}/status"})
        files, err = asyncio.run(cam.list_files())
    finally:
        srv.shutdown()
        srv.server_close()
    assert err is None
    assert files == [{"path": "/files/IMG_0000_1.tif", "size": 256, "mtime": "2026-11-19T12:00:00Z"}]


def test_list_files_cap_and_failures():
    srv, _ = start_fake_camera()
    try:
        cam = camera.Camera({"exposure_gpio": None, "status_url": f"http://127.0.0.1:{srv.server_port}/status"})
        capped, err = asyncio.run(cam.list_files(max_files=2))
    finally:
        srv.shutdown()
        srv.server_close()
    assert capped is not None and len(capped) == 2
    assert err and "cap" in err

    # After shutdown + close the port refuses connections: must return (None, non-empty error), never raise.
    files, err = asyncio.run(cam.list_files(timeout_s=2.0, req_timeout_s=1.0))
    assert files is None and err

    # Missing status_url: returns (None, non-empty error), never raise.
    no_url = camera.Camera({"exposure_gpio": None})
    files, err = asyncio.run(no_url.list_files())
    assert files is None and err


def _make_stub_daemon(tmp_path: Path, cam: camera.Camera, sim_mode: bool):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / "aerolab_flight.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(rawlog.READINGS_SCHEMA)
    conn.commit()
    conn.close()
    return SimpleNamespace(
        log_dir=log_dir,
        db_path=db_path,
        state=SimpleNamespace(flight_id=1),
        camera=cam,
        sim_mode=sim_mode,
        cfg={"camera": {"status_url": cam.status_url}},
        cal=SimpleNamespace(summary=lambda: {}),
        mirror_dirs=[],
        usable_mirrors=lambda: ([], []),
        mounting_offset=lambda: 0.4,
        report_html=lambda: "<html><body>ok</body></html>",
    )


def test_survey_build_package_writes_camera_files_live_and_sim(tmp_path):
    srv, _ = start_fake_camera()
    try:
        base_url = f"http://127.0.0.1:{srv.server_port}"
        cam = camera.Camera({"exposure_gpio": None, "status_url": f"{base_url}/status"})
        d = _make_stub_daemon(tmp_path / "live", cam, sim_mode=False)
        sm = survey.SurveyManager(d)
        now_ns = time.time_ns()
        mono_ns = time.monotonic_ns()
        cur = {"id": 1, "name": "Live 1", "start_utc_ns": now_ns - 1_000_000_000, "start_mono_ns": mono_ns - 1_000_000_000}
        summary = {"id": 1, "name": "Live 1", "stop_utc_ns": now_ns}
        pkg_info = sm.build_package(cur, mono_ns, summary)
    finally:
        srv.shutdown()
        srv.server_close()

    pkg = Path(pkg_info["dir"])
    cf_path = pkg / "camera_files.json"
    assert cf_path.exists()
    cf = json.loads(cf_path.read_text())
    assert cf["fetched_utc"].endswith("Z")
    assert cf["base_url"] == base_url
    assert cf["files"] == EXPECTED_FILES
    assert cf["error"] is None
    # Covered by MANIFEST.sha256 and passes verification.
    assert "camera_files.json" in (pkg / "MANIFEST.sha256").read_text()
    assert survey.verify(pkg)

    # Simulation mode writes camera_files.json with the simulation note.
    sim_cam = camera.Camera({"exposure_gpio": None, "status_url": "http://192.168.1.83/status"}, sim=True)
    d_sim = _make_stub_daemon(tmp_path / "sim", sim_cam, sim_mode=True)
    sm_sim = survey.SurveyManager(d_sim)
    sim_pkg_info = sm_sim.build_package(cur, mono_ns, summary)
    sim_pkg = Path(sim_pkg_info["dir"])
    sim_cf = json.loads((sim_pkg / "camera_files.json").read_text())
    assert sim_cf["note"] == "simulation: no camera"
    assert sim_cf["files"] == []
    assert sim_cf["error"] is None
    assert "camera_files.json" in (sim_pkg / "MANIFEST.sha256").read_text()
    assert survey.verify(sim_pkg)


def _populate_backup(backup_dir: Path, files: list[dict]):
    for entry in files:
        rel = check_camera_backup.rel_card_path(entry["path"])
        target = backup_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * entry["size"])


def test_check_camera_backup_complete_missing_truncated_and_extra(tmp_path):
    srv, _ = start_fake_camera()
    try:
        cam = camera.Camera({"exposure_gpio": None, "status_url": f"http://127.0.0.1:{srv.server_port}/status"})
        files, err = asyncio.run(cam.list_files())
    finally:
        srv.shutdown()
        srv.server_close()
    assert err is None and files == EXPECTED_FILES

    list_path = tmp_path / "camera_files.json"
    list_path.write_text(json.dumps({
        "fetched_utc": "2026-11-19T12:00:00Z",
        "base_url": "http://127.0.0.1",
        "files": files,
        "error": None,
    }))

    backup = tmp_path / "card_backup"
    _populate_backup(backup, files)

    # 1. Complete backup passes with exit code 0.
    cli = [sys.executable, str(ROOT / "tools" / "check_camera_backup.py"),
           "--list", str(list_path), "--backup", str(backup)]
    proc = subprocess.run(cli, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK:" in proc.stdout

    # 2. Extra file in backup is reported, still exits 0 (nothing missing or truncated).
    extra_file = backup / "0002SET" / "extra.tif"
    extra_file.parent.mkdir(parents=True, exist_ok=True)
    extra_file.write_bytes(b"extra")
    res_extra = check_camera_backup.check_backup(list_path, backup)
    assert res_extra["ok"] is True
    assert [e["path"] for e in res_extra["extra"]] == ["0002SET/extra.tif"]
    assert check_camera_backup.main(["--list", str(list_path), "--backup", str(backup)]) == 0

    # 3. Truncated file fails with non-zero exit code and reports expected vs actual size.
    trunc_target = backup / "0000SET" / "000" / "IMG_0000_2.tif"
    trunc_target.write_bytes(b"x" * 100)
    res_trunc = check_camera_backup.check_backup(list_path, backup)
    assert res_trunc["ok"] is False
    assert res_trunc["size_mismatch"] == [
        {"path": "0000SET/000/IMG_0000_2.tif", "expected_size": 512, "actual_size": 100}
    ]
    proc_trunc = subprocess.run(cli, capture_output=True, text=True)
    assert proc_trunc.returncode == 1
    assert "IMG_0000_2.tif" in proc_trunc.stdout

    # 4. Missing file also reported and fails with non-zero exit code.
    (backup / "0001SET" / "000" / "IMG_0001_1.tif").unlink()
    res_both = check_camera_backup.check_backup(list_path, backup)
    assert res_both["ok"] is False
    assert res_both["missing"] == [{"path": "0001SET/000/IMG_0001_1.tif", "expected_size": 750}]
    assert len(res_both["size_mismatch"]) == 1
    proc_both = subprocess.run(cli, capture_output=True, text=True)
    assert proc_both.returncode == 1
    assert "IMG_0001_1.tif" in proc_both.stdout
    assert "IMG_0000_2.tif" in proc_both.stdout
