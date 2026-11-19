"""Never lose science data: raw capture, per-reading logs, recovery, supervision, timing."""
import asyncio
import os
import socket
import sqlite3
import struct
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
import diagnose  # noqa: E402
import drivers  # noqa: E402
import main  # noqa: E402
import ppsclock  # noqa: E402
import rawlog  # noqa: E402
from supervise import Supervisor, sd_notify, watchdog  # noqa: E402


def base_cfg(tmp_path):
    cfg = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    cfg["logging"]["fallback_dir"] = str(tmp_path / "logs")
    cfg["logging"]["primary_dir"] = str(tmp_path / "ssd" / "aerolab_logs")
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    return cfg


def test_raw_capture_roundtrip_mirror_and_torn_last_frame(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    w = rawlog.RawWriter([a, b], "laser", {"port": "/dev/ttyUSB0", "baud": 9600})
    chunks = [b"123.45\r\n", b"\x00\xffbinary", b"130.00\r\n"]
    for i, c in enumerate(chunks):
        w.write(c, 1_000_000_000 + i, 5_000 + i)
    w.close()
    for d in (a, b):
        f = next((d / "raw").glob("laser_*.aeroraw"))
        it = rawlog.read_raw(f)
        hdr = next(it)
        assert hdr["sensor"] == "laser" and hdr["baud"] == 9600
        assert [x[2] for x in it] == chunks
    # a power cut mid-frame: everything before it still reads
    f = next((a / "raw").glob("laser_*.aeroraw"))
    data = f.read_bytes()
    f.write_bytes(data[:-3])
    assert [x[2] for x in list(rawlog.read_raw(f))[1:]] == chunks[:2]


def test_raw_capture_survives_a_dead_mirror(tmp_path):
    good = tmp_path / "good"
    bad = tmp_path / "file-not-dir"
    bad.write_text("x")                                     # can't mkdir under a file
    errs = []
    w = rawlog.RawWriter([good, bad], "gnss", {}, on_error=errs.append)
    w.write(b"$GPGGA...\r\n", 1, 1)
    w.close()
    assert errs and next((good / "raw").glob("gnss_*.aeroraw")).stat().st_size > 0


def test_reading_log_keeps_rows_while_the_disk_is_gone(tmp_path):
    target = {"p": tmp_path / "missing" / "flight.db"}     # directory doesn't exist yet
    errs = []
    log = rawlog.ReadingLog(lambda: target["p"], on_error=errs.append)
    for i in range(100):
        log.add("laser", (1, i, i, 50.0 + i, 1, 49.6, 0.0, 0.0))

    async def run():
        t = asyncio.create_task(log.run(interval=0.05))
        await asyncio.sleep(0.3)
        assert not log.healthy and len(log.q) == 100       # nothing lost, nothing written
        (tmp_path / "missing").mkdir()                     # the disk comes back
        await asyncio.sleep(1.5)
        t.cancel()
    asyncio.run(run())
    assert errs and log.healthy and not log.q
    n = sqlite3.connect(target["p"]).execute("SELECT COUNT(*), MIN(range_m), MAX(range_m) FROM laser_readings").fetchone()
    assert n == (100, 50.0, 149.0)


def test_logger_recovers_from_a_database_failure(tmp_path):
    d = main.AeroLabDaemon(base_cfg(tmp_path), sim_mode=True)
    real = d._connect_db
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real()
    d._connect_db = flaky

    async def run():
        t = asyncio.create_task(d.logger_loop())
        await asyncio.sleep(2.0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    assert calls["n"] >= 2 and d.state.logging_active
    assert any("Data write problem" in e.text for e in d.buddy.events)
    rows = sqlite3.connect(d.db_path).execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    assert rows > 3


def test_logging_moves_to_the_ssd_when_it_appears(tmp_path, monkeypatch):
    cfg = base_cfg(tmp_path)
    d = main.AeroLabDaemon(cfg, sim_mode=False)
    assert d.log_dir == tmp_path / "logs" and d.fallback_logging
    (tmp_path / "ssd").mkdir()
    monkeypatch.setattr(main.os.path, "ismount", lambda p: Path(p) == tmp_path / "ssd")
    assert d.recheck_log_dir()
    assert d.log_dir == tmp_path / "ssd" / "aerolab_logs" and not d.fallback_logging
    assert (d.log_dir / "aerolab_flight.db").exists()


def test_supervisor_restarts_a_crashing_task_and_reports():
    reports = []
    sup = Supervisor(report=lambda lvl, txt: reports.append((lvl, txt)))
    runs = {"n": 0}

    async def flaky():
        runs["n"] += 1
        if runs["n"] < 3:
            raise RuntimeError("serial port vanished")
        await asyncio.sleep(10)

    async def run():
        t = sup.run("laser reader", flaky)
        await asyncio.sleep(3.5)
        t.cancel()
    asyncio.run(run())
    assert runs["n"] == 3 and sup.restarts["laser reader"] == 2
    assert reports[0][0] == "alert" and "laser reader crashed" in reports[0][1]


def test_watchdog_pings_systemd_only_while_healthy(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(1)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    sup = Supervisor()
    sup.run_beats = None

    async def run():
        sup.stall_s["logger"] = 0.2
        sup.beat("logger")
        t = asyncio.create_task(watchdog(sup, interval=0.1))
        await asyncio.sleep(0.05)
        msgs = [srv.recv(100), srv.recv(100)]
        await asyncio.sleep(0.4)                           # logger stops beating
        later = [srv.recv(100) for _ in range(3)]
        t.cancel()
        return msgs, later
    msgs, later = asyncio.run(run())
    assert msgs[0] == b"READY=1" and msgs[1] == b"WATCHDOG=1"
    assert any(m.startswith(b"STATUS=stalled: logger") for m in later)
    assert sd_notify("WATCHDOG=1")


def test_pps_parse_and_poll(tmp_path):
    assert ppsclock.parse_assert("1795100000.000012345#42\n") == (1795100000000012345, 42)
    assert ppsclock.parse_assert("") is None
    assert ppsclock.offset_from_second_ns(1795100000_999_500_000) == -500_000
    d = tmp_path / "pps0"
    d.mkdir()
    edges = []
    p = ppsclock.PPSClock("pps0", on_edge=lambda *a: edges.append(a), sysfs_root=str(tmp_path))
    (d / "assert").write_text("1795100000.000250000#7\n")
    assert p.poll() and not p.poll()                       # same sequence: not a new edge
    (d / "assert").write_text("1795100001.000251000#8\n")
    assert p.poll()
    assert [e[0] for e in edges] == [7, 8] and p.status()["ok"]
    assert p.status()["clock_offset_ms"] == pytest.approx(0.251)


def test_erb_driver():
    tow = 200_000_000
    frames = (drivers.erb_frame(3, struct.pack("<IHBBB", tow, 2290, 3, 1, 17))
              + drivers.erb_frame(4, struct.pack("<IHHHH", tow, 120, 150, 140, 80))
              + drivers.erb_frame(2, struct.pack("<Idddd", tow, -121.5, 38.5, 90.0, 120.0) + struct.pack("<II", 20, 30)))
    d = drivers.make_driver("erb")
    out = d.feed(b"\x00ER\x00" + frames[:9]) + d.feed(frames[9:])
    pos = out[0]
    assert pos["fix"] == 4 and pos["sats"] == 17 and pos["hdop"] == pytest.approx(0.8)
    assert pos["lat"] == 38.5 and pos["alt_msl"] == 120.0
    assert pos["gnss_tod"] == pytest.approx((200_000 - 18) % 86400)
    bad = bytearray(frames)
    bad[12] ^= 1
    d2 = drivers.make_driver("erb")
    d2.feed(bytes(bad))
    assert d2.stats.bad >= 1


def test_export_clock_model_removes_arrival_latency():
    import random
    import export_laser
    rnd = random.Random(1)
    fixes = []
    base_mono = 10_000.0
    utc0 = 1795100000.0
    for i in range(600):                                   # 60 s of 10 Hz fixes
        epoch = utc0 + i * 0.1
        lat_s = 0.030 + abs(rnd.gauss(0, 0.015))           # 30 ms + jitter after each epoch
        mono = base_mono + (epoch - utc0) + lat_s
        fixes.append((int((epoch + lat_s) * 1e9), int(mono * 1e9), epoch % 86400))
    m = export_laser.clock_model(fixes, [], 0.0)
    # a laser byte arriving at mono t maps to the GNSS time it really arrived at: epoch timeline + 30 ms floor
    mono_t = base_mono + 20.0 + 0.030
    assert (m["a"] + m["b"] * mono_t) - (utc0 + 20.0) == pytest.approx(0.0, abs=0.003)
    assert 0 < m["arrival_latency_ms"]["median"] < 30


def test_diagnosis_gives_device_specific_steps(tmp_path):
    import configure
    cat = configure.load_catalog()
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "live"), cat)
    cfg["logging"]["fallback_dir"] = str(tmp_path)
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    d = main.AeroLabDaemon(cfg, sim_mode=False)
    fs = diagnose.findings(d, cat)
    laser = next(f for f in fs if f["title"].startswith("AgLaser ILM-150-R: no data"))
    assert laser["severity"] == "blocker"
    assert any("9-24 V supply" in s for s in laser["steps"])          # from the device's troubleshooting
    assert any("probe.py" in s for s in laser["steps"])               # generic serial steps
    assert any(f["title"].startswith("AGL on the display is not tilt-corrected") for f in fs)
    r = diagnose.readiness(fs)
    assert r["level"] == "red" and "Not ready" in r["text"]
    assert "no data" in diagnose.as_text(fs)


def test_probe_scores_specific_protocols_first():
    import probe
    nmea = b"".join(f"$GPGGA,0000{i:02d},3834.896,N,12129.664,W,1,08,0.9,30.0,M,46.9,M,,".encode() for i in range(5))
    nmea = b"".join(f"${l[1:].decode()}*{drivers.nmea_xor(l[1:].decode()):02X}\r\n".encode() for l in nmea.split(b"$")[1:] for l in [b"$" + l])
    assert probe.score(nmea)[0][0] == "nmea"
    assert probe.score(b"12.34\r\n" * 10)[0][0] == "ascii_number"
