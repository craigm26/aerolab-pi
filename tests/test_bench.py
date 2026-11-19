"""Bench tools: timing validation, Reach time marks vs Pi exposures, adapter loopback, bench report."""
import os
import pty
import random
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tty
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
import rawlog  # noqa: E402


def synth_db(path: Path, missing_pulse=True):
    conn = sqlite3.connect(path)
    conn.executescript(rawlog.READINGS_SCHEMA)
    rnd = random.Random(2)
    mono0, utc0 = 5_000 * 10**9, 1795200000 * 10**9          # Pi clock 3 ms ahead of GPS, drifting 10 ppm
    for i in range(120):
        if missing_pulse and i == 60:
            continue
        gps = utc0 + i * 10**9
        pi_err = 3_000_000 + i * 10_000 + int(rnd.gauss(0, 5_000))   # ns
        a = gps + pi_err
        mono = mono0 + i * 10**9
        conn.execute("INSERT INTO pps_edges (flight_id, seq, assert_utc_ns, mono_ns, utc_minus_mono_ns) VALUES (1,?,?,?,?)",
                     (i, a, mono + pi_err, a - (mono + pi_err)))
    for k in range(1200):                                      # 10 Hz NMEA, 30 ms after each epoch
        ep = utc0 + k * 10**8
        arrive_mono = mono0 + k * 10**8 + 30_000_000 + 3_000_000 + (k // 10) * 10_000
        conn.execute("INSERT INTO gnss_fixes (flight_id, utc_ns, mono_ns, gnss_tod, lat, lon, alt_m, fix) "
                     "VALUES (1,?,?,?,?,?,?,1)", (ep + 33_000_000, arrive_mono, (ep / 1e9) % 86400, 38.5, -121.4, 100.0))
    for s in range(120):
        conn.execute("INSERT INTO clock_samples (flight_id, utc_ns, mono_ns) VALUES (1,?,?)",
                     (utc0 + s * 10**9 + (50_000_000 if s >= 80 else 0), mono0 + s * 10**9))
    conn.commit()
    conn.close()


def test_timing_check(tmp_path):
    import timing_check
    synth_db(tmp_path / "f.db")
    r = timing_check.check(tmp_path / "f.db", 1)
    c = {i["check"]: i for i in r["checks"]}
    assert c["PPS present"]["status"] == "pass"
    assert c["PPS interval"]["status"] == "pass" and c["PPS interval"]["p99_us"] < 50
    assert c["Missing pulses"]["status"] == "warn" and "1 gap" in c["Missing pulses"]["detail"]
    assert c["Pi clock vs GPS"]["start_ms"] == pytest.approx(3.0, abs=0.05)
    assert c["Pi clock vs GPS"]["drift_ppm"] == pytest.approx(10.0, abs=0.5)
    assert c["Clock steps"]["status"] == "warn" and "+50.0 ms" in c["Clock steps"]["detail"]
    assert c["NMEA latency"]["median_ms"] == pytest.approx(30.0, abs=1.0)


def test_timing_check_without_pps(tmp_path):
    import timing_check
    conn = sqlite3.connect(tmp_path / "f.db")
    conn.executescript(rawlog.READINGS_SCHEMA)
    conn.commit()
    conn.close()
    r = timing_check.check(tmp_path / "f.db", 1)
    assert r["checks"][0]["check"] == "PPS present" and r["checks"][0]["status"] == "fail"
    assert "ppstest" in r["checks"][0]["hint"]


def test_compare_timemarks(tmp_path):
    import compare_timemarks as ct
    t0 = 1795200000.0
    lines = ["% program   : Emlid Studio", "%  GPST                  latitude(deg) longitude(deg)  height(m)   Q  ns"]
    reach = [t0 + i for i in range(30) if i != 7]                                   # Reach missed #7
    for t in reach:
        from datetime import datetime, timezone
        g = datetime.fromtimestamp(t + 18, timezone.utc).strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]
        lines.append(f"{g}   38.500000000 -121.400000000   100.0000   1  14")
    (tmp_path / "ev.pos").write_text("\n".join(lines) + "\n")
    events = ct.read_pos(tmp_path / "ev.pos")
    assert len(events) == 29 and events[0]["utc"] == pytest.approx(t0, abs=1e-3)
    pi = [t0 + i + 0.0004 for i in range(30) if i != 20]                             # Pi missed #20
    r = ct.compare(events, pi)
    assert r["paired"] == 28 and r["missing_on_pi"] == 1 and r["missing_in_reach_log"] == 1
    assert r["offset_ms_mean"] == pytest.approx(0.4, abs=0.01)


def test_probe_loopback():
    import probe
    master, slave = pty.openpty()
    tty.setraw(slave)
    tty.setraw(master)
    stop = threading.Event()

    def echo():                       # a pty that sends back whatever it gets: a bridged DB9
        import select
        while not stop.is_set():
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                os.write(master, os.read(master, 4096))
    threading.Thread(target=echo, daemon=True).start()
    assert probe.loopback(os.ttyname(slave))["ok"]
    stop.set()
    m2, s2 = pty.openpty()
    tty.setraw(s2)
    r = probe.loopback(os.ttyname(s2))                         # nothing bridged
    assert not r["ok"] and "nothing came back" in r["detail"]


def test_bench_endpoints_and_report(tmp_path):
    import configure
    cat = configure.load_catalog()
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "sim"), cat)
    cfg["logging"]["fallback_dir"] = str(tmp_path / "logs")
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    cfg["self_test"] = {"delay_s": 1.0}
    cfg["sim"] = {"bank_deg": 3.0}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg["server"]["port"] = port
    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(cfg))
    proc = subprocess.Popen([sys.executable, str(ROOT / "daemon" / "main.py"), "--config", str(tmp_path / "cfg.yaml")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import bench_report
        for _ in range(50):
            try:
                bench_report.api(port, "/api/state")
                break
            except Exception:
                time.sleep(0.2)
        time.sleep(3)
        rep = bench_report.main(["--port", str(port), "--laser-distance", "137.8", "--laser-seconds", "3",
                                 "--camera-test"])
        lc = rep["laser_check"]
        assert lc["readings"] >= 20 and abs(lc["bias_m"]) < 25           # the sim aircraft is moving
        assert rep["camera_test"]["edges_seen"] >= 1 and rep["camera_test"]["verdict"].startswith("ok")
        assert rep["bit"]["done"] and rep["diagnose"]["readiness"]["level"] in ("green", "amber", "red")
        assert list((tmp_path / "logs" / "sim").glob("bench_report_*.html"))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
