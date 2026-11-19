"""Camera that captures on its own: exposures watched by the Pi, status over HTTP."""
import asyncio
import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))
import camera  # noqa: E402
import main  # noqa: E402


def test_exposures_rate_and_stop():
    seen = []
    c = camera.Camera({"name": "RedEdge-P", "exposure_gpio": None, "expected_hz": 2.0},
                      on_exposure=lambda *a: seen.append(a))
    for _ in range(5):
        c.exposure()
        time.sleep(0.5)
    snap = c.snapshot()
    assert snap["exposures"] == 5 and [s[0] for s in seen] == [1, 2, 3, 4, 5]
    assert snap["hz"] == pytest.approx(2.0, rel=0.15) and not snap["stopped"]
    c.last -= 4.0                                           # 4 s without a frame at 2 Hz
    assert c.snapshot()["stopped"]


class FakeCamera(BaseHTTPRequestHandler):
    body = {"sd_gb_free": 41.2, "sd_gb_total": 128.0, "sd_status": "Ok", "auto_cap_active": False,
            "bus_volts": 11.9, "gps_used_sats": 9, "time_source": "PPS", "gps_lat": 0.67, "unrelated": 1}

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.body).encode())

    def log_message(self, *a):
        pass


def test_status_poll_reads_documented_fields():
    srv = HTTPServer(("127.0.0.1", 0), FakeCamera)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    got = []
    c = camera.Camera({"exposure_gpio": None, "status_url": f"http://127.0.0.1:{srv.server_port}/status"},
                      on_status=got.append)

    async def run():
        t = asyncio.create_task(c.status_loop(period=0.1))
        await asyncio.sleep(0.5)
        t.cancel()
    asyncio.run(run())
    srv.shutdown()
    assert c.status_ok and got
    assert c.status["sd_gb_free"] == 41.2 and c.status["auto_cap_active"] is False
    assert "unrelated" not in c.status and "gps_lat" not in c.status


def test_daemon_logs_exposures_calls_out_and_exports(tmp_path):
    import configure
    import export_exposures
    from fastapi.testclient import TestClient
    cat = configure.load_catalog()
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "sim"), cat)
    cfg["camera"]["expected_hz"] = 2.0
    cfg["logging"]["fallback_dir"] = str(tmp_path)
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    cfg["self_test"] = {"delay_s": 1.0}
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    with TestClient(main.create_app(d)) as client:
        time.sleep(3.5)
        st = client.get("/api/state").json()
        assert st["_camera"]["exposures"] >= 5 and st["_camera"]["status"]["sd_status"] == "Ok"
        bit = {i["item"]: i for i in client.get("/api/bit").json()["items"]}
        assert bit["MicaSense RedEdge-P exposures"]["status"] == "ok"
        assert bit["MicaSense RedEdge-P status"]["status"] == "ok"
        ans = client.post("/api/buddy/ask", json={"question": "Is the camera taking pictures?"}).text
        assert "exposures this session" in ans and "SD" in ans
        # camera stops: callout
        d.camera.last -= 10.0
        d.camera.sim = False
        d.integrity_tick(time.time())
    assert any("stopped exposing" in e.text for e in d.buddy.events)
    summ = export_exposures.main(["--db", str(d.db_path), "--out", str(tmp_path / "ex.csv")])
    assert summ["exposures"] >= 5 and summ["not_matched"] <= 1
