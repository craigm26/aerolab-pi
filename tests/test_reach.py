"""Tests for read-only Emlid Reach raw-log monitoring, survey/shutdown integration, and reach_discover."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import configure  # noqa: E402
import diagnose  # noqa: E402
import main  # noqa: E402
import reach  # noqa: E402
import reach_discover  # noqa: E402


class FakeReachState:
    """Mutable state served by the fake Emlid Reach HTTP + Engine.IO v3 server."""

    def __init__(self, framing: str = "text"):
        self.framing = framing
        self.is_writing = True
        self.file_name = "Reach_raw_20261119120000_RINEX_3_03"
        self.size = 100_000.0
        self.size_step = 25_000.0
        self.recording_time = 10.0
        self.poll_delay_s = 0.05
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def encode(self, packets: list[str]) -> tuple[bytes, str]:
        if self.framing == "binary":
            return reach.encode_eio3_binary(packets), "application/octet-stream"
        return reach.encode_eio3_text(packets), "text/plain; charset=UTF-8"

    def next_active_logs(self) -> dict:
        with self._lock:
            if self.is_writing:
                self.size += self.size_step
                self.recording_time += 2.0
            return {
                "name": "active_logs",
                "payload": {
                    "raw": {
                        "is_writing": self.is_writing,
                        "format": "RINEX",
                        "file_name": self.file_name,
                        "log_type": "raw",
                        "size": self.size,
                        "recording_time": self.recording_time,
                        "remaining_time": 80_000.0,
                        "start_time": 1770935870.0,
                    },
                    "archive_name": "Reach_20261119120000",
                },
            }


def start_fake_reach(framing: str = "text"):
    state = FakeReachState(framing=framing)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            state.requests.append(("GET", self.path))

            if parsed.path == "/socket.io/":
                if "sid" not in qs:
                    open_pkt = "0" + json.dumps({
                        "sid": "fake-sid-123",
                        "upgrades": ["websocket"],
                        "pingInterval": 25000,
                        "pingTimeout": 5000,
                    })
                    body, ctype = state.encode([open_pkt, "40"])
                    self._send(200, body, ctype)
                    return

                time.sleep(state.poll_delay_s)
                nav = {
                    "name": "navigation",
                    "payload": {"solution": "fix", "satellites": {"rover": 18, "valid": 15}},
                }
                al = state.next_active_logs()
                pkts = [
                    "42" + json.dumps(["broadcast", nav]),
                    "42" + json.dumps(["broadcast", al]),
                ]
                body, ctype = state.encode(pkts)
                self._send(200, body, ctype)
                return

            if parsed.path == "/":
                self._send(200, b"<html><title>Reach Panel</title></html>", "text/html")
            elif parsed.path == "/info":
                self._send_json(200, {"device": {"model": "Reach M2", "app_version": "35.0", "serial_number": "ABC123"}})
            elif parsed.path == "/device":
                self._send_json(200, {"model": "Reach M2"})
            elif parsed.path == "/configuration":
                self._send_json(200, {"positioning_settings": {"update_rate": 10}, "wifi_password": "secret-value"})
            elif parsed.path == "/configuration/device":
                self._send_json(200, {"role": "rover"})
            elif parsed.path == "/logs":
                self._send_json(200, {"logs": [{"name": state.file_name}]})
            elif parsed.path == "/wifi/status":
                self._send_json(200, {
                    "mode": "hotspot",
                    "enabled": True,
                    "current_network": {"ip": "192.168.42.1", "password": "emlidreach", "auth_token": "tok123"},
                })
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            _ = self.rfile.read(length)
            state.requests.append(("POST", self.path))
            if urlparse(self.path).path == "/socket.io/":
                self._send(200, b"ok", "text/plain")
            else:
                self._send_json(405, {"error": "read-only"})

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state, f"http://127.0.0.1:{srv.server_port}"


def _base_sim_cfg(tmp_path: Path) -> dict:
    cat = configure.load_catalog()
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "sim"), cat)
    cfg["camera"]["expected_hz"] = 2.0
    cfg["logging"]["fallback_dir"] = str(tmp_path / "logs")
    cfg["logging"]["mirror_dirs"] = [str(tmp_path / "usb2")]
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    cfg["self_test"] = {"delay_s": 0.5}
    cfg["sim"] = {"bank_deg": 3.0}
    return cfg


def test_eio3_framing_parsers_text_and_binary():
    pkts = ['0{"sid":"abc","pingInterval":25000}', "40", '42["broadcast",{"name":"active_logs","payload":{"raw":{"is_writing":true}}}]']
    assert reach.parse_eio3_payload(reach.encode_eio3_text(pkts)) == pkts
    assert reach.parse_eio3_payload(reach.encode_eio3_binary(pkts)) == pkts
    assert reach.detect_eio3_framing(reach.encode_eio3_text(pkts)) == "text"
    assert reach.detect_eio3_framing(reach.encode_eio3_binary(pkts)) == "binary"
    assert reach.decode_sio_event(pkts[2]) == ("active_logs", {"raw": {"is_writing": True}})


def test_reach_log_status_transitions_text_and_binary():
    for framing in ("text", "binary"):
        srv, fake, url = start_fake_reach(framing=framing)
        try:
            async def run_check():
                rl = reach.ReachLog(url, sim=False)
                task = asyncio.create_task(rl.run())
                try:
                    # Wait until writing + growing is observed
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        st = rl.status()
                        if st["reachable"] and st["writing"] is True and st["growing"] is True:
                            break
                        await asyncio.sleep(0.05)
                    st = rl.status()
                    assert st["enabled"] is True
                    assert st["reachable"] is True
                    assert st["writing"] is True
                    assert st["growing"] is True
                    assert st["file_name"] == fake.file_name
                    assert st["size"] > 100_000
                    assert st["recording_time"] > 10.0
                    assert st["age_s"] is not None and st["age_s"] < 5.0
                    assert st["error"] == ""

                    # Transition to is_writing = False
                    fake.is_writing = False
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        st = rl.status()
                        if st["writing"] is False:
                            break
                        await asyncio.sleep(0.05)
                    st = rl.status()
                    assert st["reachable"] is True
                    assert st["writing"] is False
                    assert st["growing"] is False
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            asyncio.run(run_check())
            # Ensure no REST POST was ever sent
            assert all(m == "GET" or urlparse(p).path == "/socket.io/" for m, p in fake.requests)
        finally:
            srv.shutdown()
            srv.server_close()


def test_survey_manual_reach_confirmation_when_disabled(tmp_path):
    from fastapi.testclient import TestClient

    cfg = _base_sim_cfg(tmp_path)
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    # Simulate Reach check being disabled (as in live mode when gnss.reach_url is empty).
    d.reach = reach.ReachLog("", sim=False)
    with TestClient(main.create_app(d)) as c:
        time.sleep(1.5)
        # BIT reports Reach raw log as warn when disabled
        bit_items = {i["item"]: i for i in c.get("/api/bit").json()["items"]}
        assert bit_items["Reach raw log"]["status"] == "warn"
        assert "not configured: confirm in Emlid Flow" in bit_items["Reach raw log"]["detail"]

        # Without reach_confirmed -> blocked with needs_reach_confirmation
        r0 = c.post("/api/survey/start", json={"name": "Manual Leg", "force": True}).json()
        assert r0["ok"] is False
        assert r0["needs_reach_confirmation"] is True
        assert r0.get("detail")

        # With reach_confirmed -> starts and records manual confirmation
        r1 = c.post("/api/survey/start", json={"name": "Manual Leg", "force": True, "reach_confirmed": True}).json()
        assert r1["ok"] is True

        for _ in range(30):
            st = c.get("/api/survey").json()
            if st["current"].get("confirmation"):
                break
            time.sleep(0.25)
        conf = {i["item"]: i for i in st["current"]["confirmation"]}
        assert conf["Reach raw log"]["ok"] is True
        assert "Reach raw log: confirmed manually by the pilot at " in conf["Reach raw log"]["detail"]

        res = c.post("/api/survey/stop").json()
        assert res["ok"] is True
        pkg_dir = Path(res["summary"]["package"]["dir"])
        sv_json = json.loads((pkg_dir / "survey.json").read_text())
        assert "Reach raw log: confirmed manually by the pilot at " in json.dumps(sv_json)


def test_survey_confirmation_diagnose_and_shutdown_with_fake_reach(tmp_path):
    from fastapi.testclient import TestClient

    srv, fake, url = start_fake_reach(framing="text")
    try:
        cfg = _base_sim_cfg(tmp_path)
        cfg["gnss"]["reach_url"] = url
        d = main.AeroLabDaemon(cfg, sim_mode=True)
        # Point ReachLog at the real fake HTTP server (non-sim ReachLog) while sensors stay simulated.
        d.reach = reach.ReachLog(url, sim=False)

        with TestClient(main.create_app(d)) as c:
            # Wait for ReachLog to observe writing + growing and BIT to finish
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                st = c.get("/api/state").json()
                if st["_reach"]["writing"] and st["_reach"]["growing"] and st["_bit"]["done"]:
                    break
                time.sleep(0.1)
            st = c.get("/api/state").json()
            assert st["_reach"]["enabled"] is True
            assert st["_reach"]["reachable"] is True
            assert st["_reach"]["writing"] is True
            assert st["_reach"]["growing"] is True

            bit_items = {i["item"]: i for i in st["_bit"]["items"]}
            assert bit_items["Reach raw log"]["status"] == "ok"

            # Start survey WITHOUT reach_confirmed: automatic check succeeds
            r = c.post("/api/survey/start", json={"name": "Auto Leg", "force": True}).json()
            assert r["ok"] is True

            for _ in range(30):
                sv = c.get("/api/survey").json()
                if sv["current"].get("confirmation"):
                    break
                time.sleep(0.25)
            conf = {i["item"]: i for i in sv["current"]["confirmation"]}
            assert conf["Reach raw log"]["ok"] is True

            # Flip Reach to not writing while recording -> blocker finding in diagnose
            fake.is_writing = False
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if d.reach.status()["writing"] is False:
                    break
                time.sleep(0.05)
            fs_rec = diagnose.findings(d, d.catalog)
            reach_f = [f for f in fs_rec if "Reach raw log" in f["title"]]
            assert len(reach_f) == 1
            assert reach_f[0]["severity"] == "blocker"
            assert any("Autostart" in step for step in reach_f[0]["steps"])
            assert any("storage" in step.lower() for step in reach_f[0]["steps"])
            assert any("base mode" in step.lower() for step in reach_f[0]["steps"])

            # Stop survey; while idle, not writing -> warn finding
            assert c.post("/api/survey/stop").json()["ok"] is True
            fs_idle = diagnose.findings(d, d.catalog)
            reach_f_idle = [f for f in fs_idle if "Reach raw log" in f["title"]]
            assert len(reach_f_idle) == 1 and reach_f_idle[0]["severity"] == "warn"

            # Shutdown prepare when Reach is NOT writing -> Reach step ok
            prep_not_writing = c.post("/api/shutdown/prepare").json()
            rstep_ok = [s for s in prep_not_writing["steps"] if s["step"] == "Reach raw log"]
            assert len(rstep_ok) == 1 and rstep_ok[0]["severity"] == "ok"
            c.post("/api/shutdown/cancel")

            # Flip back to writing -> shutdown prepare emits warn step (never fail)
            fake.is_writing = True
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if d.reach.status()["writing"] is True:
                    break
                time.sleep(0.05)
            prep = c.post("/api/shutdown/prepare").json()
            assert prep["all_ok"] is True
            rstep = [s for s in prep["steps"] if s["step"] == "Reach raw log"]
            assert len(rstep) == 1
            assert rstep[0]["severity"] == "warn"
            assert "Emlid Flow" in rstep[0]["detail"]
            assert "compressed and complete" in rstep[0]["detail"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_diagnose_unreachable_reach_is_info(tmp_path):
    cfg = _base_sim_cfg(tmp_path)
    cfg["gnss"]["reach_url"] = "http://127.0.0.1:1"
    d = main.AeroLabDaemon(cfg, sim_mode=True)
    d.reach = reach.ReachLog("http://127.0.0.1:1", sim=False)
    d.reach.error = "connection refused"
    fs = diagnose.findings(d, d.catalog)
    reach_f = [f for f in fs if "Reach raw log" in f["title"]]
    assert len(reach_f) == 1
    assert reach_f[0]["severity"] == "info"
    assert any("Reach Panel" in step for step in reach_f[0]["steps"])


def test_reach_discover_writes_json_and_redacts_secrets(tmp_path, monkeypatch, capsys):
    srv, fake, url = start_fake_reach(framing="text")
    try:
        monkeypatch.chdir(tmp_path)
        rc = reach_discover.main([url, "--listen", "0.4"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "active_logs seen: True" in out
        assert "raw.is_writing: True" in out

        files = list(tmp_path.glob("reach_discovery_*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["url"] == url
        for path in reach_discover.CANDIDATE_PATHS:
            assert path in data["http"], path
            assert data["http"][path]["status_code"] == 200

        # Password and token fields must be dropped
        raw_text = files[0].read_text()
        assert "secret-value" not in raw_text
        assert "emlidreach" not in raw_text
        assert "tok123" not in raw_text

        assert data["socketio"]["ok"] is True
        assert data["socketio"]["active_logs_seen"] is True
        assert data["socketio"]["raw_is_writing"] is True
        assert "navigation" in data["socketio"]["events_count"]
        assert "active_logs" in data["socketio"]["events_count"]
        # Strictly read-only GETs only
        assert all(method == "GET" for method, _ in fake.requests)
    finally:
        srv.shutdown()
        srv.server_close()


def test_configure_asks_reach_url_for_emlid_only():
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())

    class ScriptedUI:
        def __init__(self, picks=None):
            self.asked, self.picks = [], picks or {}

        def menu(self, text, items, default=None):
            self.asked.append(text)
            for k, v in self.picks.items():
                if text.startswith(k):
                    return v
            return default if default is not None else items[0][0]

        def checklist(self, text, items, on):
            return list(on)

        def text(self, prompt, default):
            self.asked.append(prompt)
            for k, v in self.picks.items():
                if prompt.startswith(k):
                    return v
            return str(default)

        def yesno(self, prompt, default_yes=True):
            return False

        def info(self, text):
            pass

    ui_reach = ScriptedUI({"Reach address on the network": "http://192.168.42.1"})
    answers, _ = configure.interview(ui_reach, base)
    cfg = configure.build_config(base, answers)
    assert cfg["gnss"]["reach_url"] == "http://192.168.42.1"

    ui_ublox = ScriptedUI({"GNSS receiver": "ublox-zed-f9p"})
    answers2, _ = configure.interview(ui_ublox, base)
    assert not any(q.startswith("Reach address on the network") for q in ui_ublox.asked)
    assert "gnss.reach_url" not in answers2
