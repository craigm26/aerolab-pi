"""Tests for GNSS/PPS chrony clock sync, NMEA date/ZDA parsing, and daemon wiring."""

from __future__ import annotations

import asyncio
import json
import platform
import socket
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "scripts"))

import configure  # noqa: E402
import diagnose  # noqa: E402
import drivers  # noqa: E402
import main  # noqa: E402
import timesync  # noqa: E402


def nmea(body: str) -> bytes:
    return f"${body}*{drivers.nmea_xor(body):02X}\r\n".encode()


def test_sock_sample_struct_size_and_fields():
    if platform.architecture()[0] == "64bit":
        assert timesync.SAMPLE_STRUCT.size == 40
    assert timesync.SAMPLE_STRUCT.size == struct.calcsize("@lldiiii")

    pkt = timesync.pack_sample(1795100000.25, 1795100000.75, pulse=False, leap=0)
    assert len(pkt) == timesync.SAMPLE_STRUCT.size
    tv_sec, tv_usec, offset, pulse, leap, pad, magic = struct.unpack("@lldiiii", pkt)
    assert tv_sec == 1795100000
    assert tv_usec == 250000
    assert offset == pytest.approx(0.5)
    assert pulse == 0
    assert leap == 0
    assert pad == 0
    assert magic == 0x534F434B

    # Sub-microsecond rounding carryover into the next whole second.
    pkt2 = timesync.pack_sample(1795100000.9999999, 1795100000.0, pulse=True, leap=1)
    tv_sec2, tv_usec2, offset2, pulse2, leap2, pad2, magic2 = struct.unpack("@lldiiii", pkt2)
    assert (tv_sec2, tv_usec2) == (1795100001, 0)
    assert offset2 == pytest.approx(-0.9999999, abs=1e-6)
    assert (pulse2, leap2, pad2, magic2) == (1, 1, 0, timesync.SOCK_MAGIC)


def test_chrony_feeder_unix_dgram_server_and_offset_sign(tmp_path):
    sock_path = str(tmp_path / "chrony.aerolab.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(1.0)
    feeder = timesync.ChronyFeeder(sock_path)
    try:
        # System clock behind GNSS by 2.5 s -> positive offset (reference minus system).
        assert feeder.send(1795100000.0, 1795100002.5)
        raw = srv.recv(128)
        tv_sec, tv_usec, offset, pulse, leap, pad, magic = struct.unpack("@lldiiii", raw)
        assert (tv_sec, tv_usec) == (1795100000, 0)
        assert offset == pytest.approx(2.5)
        assert (pulse, leap, pad, magic) == (0, 0, 0, 0x534F434B)

        # System clock ahead of GNSS by 1.25 s -> negative offset.
        assert feeder.send(1795100010.5, 1795100009.25)
        raw2 = srv.recv(128)
        tv_sec2, tv_usec2, offset2, *_ = struct.unpack("@lldiiii", raw2)
        assert (tv_sec2, tv_usec2) == (1795100010, 500000)
        assert offset2 == pytest.approx(-1.25)
        assert feeder.sent == 2 and feeder.errors == 0
    finally:
        feeder.close()
        srv.close()

    # Missing socket must never raise, and must increment the error counter.
    bad = timesync.ChronyFeeder(str(tmp_path / "missing.sock"))
    assert bad.send(1795100000.0, 1795100000.0) is False
    assert bad.errors == 1 and bad.sent == 0
    bad.close()


def test_gnss_utc_with_and_without_date():
    # With a known date, a Pi clock that booted years behind still gets the true UTC epoch.
    expected = datetime(2026, 11, 20, 12, 35, 19, 500000, tzinfo=timezone.utc).timestamp()
    years_off = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    tod = 12 * 3600 + 35 * 60 + 19.5
    assert timesync.gnss_utc(tod, "2026-11-20", years_off) == pytest.approx(expected)

    # Without a date, pick the day nearest the system clock.
    sys_midday = datetime(2026, 11, 20, 12, 35, 10, tzinfo=timezone.utc).timestamp()
    assert timesync.gnss_utc(tod, None, sys_midday) == pytest.approx(expected)

    # Midnight wrap: system clock is 00:00:05 on Nov 20, GNSS is 23:59:55 on Nov 19.
    sys_after_mid = datetime(2026, 11, 20, 0, 0, 5, tzinfo=timezone.utc).timestamp()
    gnss_before_mid = datetime(2026, 11, 19, 23, 59, 55, tzinfo=timezone.utc).timestamp()
    assert timesync.gnss_utc(86395.0, None, sys_after_mid) == pytest.approx(gnss_before_mid)

    # Midnight wrap other way: system clock is 23:59:55 on Nov 19, GNSS is 00:00:05 on Nov 20.
    sys_before_mid = datetime(2026, 11, 19, 23, 59, 55, tzinfo=timezone.utc).timestamp()
    gnss_after_mid = datetime(2026, 11, 20, 0, 0, 5, tzinfo=timezone.utc).timestamp()
    assert timesync.gnss_utc(5.0, None, sys_before_mid) == pytest.approx(gnss_after_mid)


SAMPLE_TRACKING_PPS = """\
Reference ID    : 50505300 (PPS)
Stratum         : 1
Ref time (UTC)  : Fri Nov 20 12:34:56 2026
System time     : 0.000001234 seconds fast of NTP time
Last offset     : +0.000000456 seconds
RMS offset      : 0.000002100 seconds
Frequency       : 12.345 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.050 ppm
Root delay      : 0.000000001 seconds
Root dispersion : 0.000015000 seconds
Update interval : 16.0 seconds
Leap status     : Normal
"""

SAMPLE_TRACKING_GNSS_SLOW = """\
Reference ID    : 474E5353 (GNSS)
Stratum         : 1
Ref time (UTC)  : Fri Nov 20 12:34:56 2026
System time     : 0.003450000 seconds slow of NTP time
Last offset     : -0.001200000 seconds
RMS offset      : 0.004100000 seconds
Frequency       : 4.100 ppm fast
Residual freq   : -0.002 ppm
Skew            : 0.120 ppm
Root delay      : 0.000000000 seconds
Root dispersion : 0.012000000 seconds
Update interval : 16.0 seconds
Leap status     : Normal
"""

SAMPLE_TRACKING_UNSYNCED = """\
Reference ID    : 00000000 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000000000 seconds fast of NTP time
Last offset     : +0.000000000 seconds
RMS offset      : 0.000000000 seconds
Frequency       : 0.000 ppm fast
Residual freq   : +0.000 ppm
Skew            : 0.000 ppm
Root delay      : 1.000000000 seconds
Root dispersion : 1.000000000 seconds
Update interval : 0.0 seconds
Leap status     : Not synchronised
"""


def test_parse_tracking_samples():
    p = timesync.parse_tracking(SAMPLE_TRACKING_PPS)
    assert p["synced"] is True
    assert p["source"] == "PPS"
    assert p["ref_id"] == "50505300"
    assert p["stratum"] == 1
    assert p["offset_ms"] == pytest.approx(0.001234)
    assert p["leap"] == "Normal"

    g = timesync.parse_tracking(SAMPLE_TRACKING_GNSS_SLOW)
    assert g["synced"] is True
    assert g["source"] == "GNSS"
    assert g["offset_ms"] == pytest.approx(-3.45)

    u = timesync.parse_tracking(SAMPLE_TRACKING_UNSYNCED)
    assert u["synced"] is False
    assert u["stratum"] == 0
    assert u["leap"] == "Not synchronised"


def test_chrony_sync_status_and_sim_disabled():
    async def fake_ok():
        return 0, SAMPLE_TRACKING_PPS, ""

    sync = timesync.ChronySync(enabled=True, sim=False, runner=fake_ok)
    st = asyncio.run(sync.poll_once())
    assert set(st.keys()) == {"enabled", "synced", "source", "offset_ms", "leap", "detail"}
    assert st["enabled"] is True and st["synced"] is True
    assert st["source"] == "PPS" and st["offset_ms"] == pytest.approx(0.001)
    assert "PPS" in st["detail"]

    called = {"n": 0}

    async def should_not_run():
        called["n"] += 1
        return 0, SAMPLE_TRACKING_PPS, ""

    sim_sync = timesync.ChronySync(enabled=True, sim=True, runner=should_not_run)
    st_sim = asyncio.run(sim_sync.poll_once())
    assert st_sim["enabled"] is False and st_sim["synced"] is False
    assert called["n"] == 0


def test_drivers_rmc_date_and_zda():
    d = drivers.make_driver("nmea")
    rmc = d.feed(nmea("GNRMC,123519.50,A,3834.896,N,12129.664,W,022.4,084.4,201126,,,A"))
    assert len(rmc) == 1
    assert rmc[0]["gnss_date"] == "2026-11-20"
    assert rmc[0]["gs_ms"] == pytest.approx(22.4 * 0.514444)
    assert rmc[0]["track_deg"] == pytest.approx(84.4)

    # RMC with empty date still parses speed/track without gnss_date.
    rmc_nodate = d.feed(nmea("GNRMC,123519.50,A,3834.896,N,12129.664,W,010.0,180.0,,,,A"))
    assert len(rmc_nodate) == 1 and "gnss_date" not in rmc_nodate[0]

    # ZDA gives gnss_tod and gnss_date.
    zda = d.feed(nmea("GPZDA,123519.50,20,11,2026,00,00"))
    assert len(zda) == 1
    assert zda[0]["gnss_tod"] == pytest.approx(12 * 3600 + 35 * 60 + 19.5)
    assert zda[0]["gnss_date"] == "2026-11-20"

    # Empty ZDA yields nothing (no exception).
    assert d.feed(nmea("GNZDA,,,,,,")) == []


def _live_cfg(tmp_path, set_clock=True):
    cfg = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    cfg["logging"]["fallback_dir"] = str(tmp_path)
    cfg["calibration_file"] = str(tmp_path / "cal.json")
    cfg.setdefault("timing", {})["set_clock"] = set_clock
    return cfg


def test_daemon_apply_feeds_chrony_and_remembers_date(tmp_path):
    d = main.AeroLabDaemon(_live_cfg(tmp_path, set_clock=True), sim_mode=False)
    received = []

    class FakeFeeder:
        def send(self, sys_t, gnss_t, pulse=False):
            received.append((sys_t, gnss_t, pulse))
            return True

        def close(self):
            pass

    d.chrony_feeder = FakeFeeder()

    # Receipt timestamp with Pi clock years behind (2020).
    sys_s = datetime(2020, 1, 1, 0, 0, 10, tzinfo=timezone.utc).timestamp()
    ts = (int(sys_s * 1e9), 1_000_000_000)

    # First an RMC provides the date; then a GGA provides time of day and fix.
    d.apply({"gs_ms": 10.0, "track_deg": 90.0, "gnss_date": "2026-11-20"}, "gnss", ts)
    assert d.last_gnss_date == "2026-11-20"
    assert received == []

    d.apply({"fix": 1, "sats": 10, "gnss_tod": 43200.5, "lat": 38.5, "lon": -121.5, "alt_msl": 100.0}, "gnss", ts)
    assert len(received) == 1
    got_sys, got_gnss, got_pulse = received[0]
    expected_gnss = datetime(2026, 11, 20, 12, 0, 0, 500000, tzinfo=timezone.utc).timestamp()
    assert got_sys == pytest.approx(sys_s)
    assert got_gnss == pytest.approx(expected_gnss)
    assert got_pulse is False

    # A GGA with fix=0 must not feed unfixed time.
    d.apply({"fix": 0, "sats": 0, "gnss_tod": 43201.0}, "gnss", ts)
    assert len(received) == 1

    # Payload exposes _timesync with expected keys.
    payload = json.loads(d.payload())
    assert "_timesync" in payload
    assert set(payload["_timesync"].keys()) == {"enabled", "synced", "source", "offset_ms", "leap", "detail"}
    assert payload["_timesync"]["enabled"] is True

    # In sim mode, feeder is never created even when set_clock is true.
    d_sim = main.AeroLabDaemon(_live_cfg(tmp_path / "simrun", set_clock=True), sim_mode=True)
    assert d_sim.chrony_feeder is None
    assert d_sim.timesync.enabled is False


def test_selftest_and_diagnose_system_clock(tmp_path):
    d = main.AeroLabDaemon(_live_cfg(tmp_path, set_clock=True), sim_mode=False)

    # Before chrony syncs: diagnose warns with expected troubleshooting steps, BIT warns.
    fs = diagnose.findings(d, d.catalog)
    clk = [f for f in fs if f["title"] == "System clock not synced to GNSS"]
    assert len(clk) == 1 and clk[0]["severity"] == "warn"
    steps_txt = " ".join(clk[0]["steps"])
    assert "aerolab.conf" in steps_txt
    assert "chronyc sources" in steps_txt
    assert "GNSS" in steps_txt
    assert "PPS" in steps_txt

    bit_unsynced = asyncio.run(d.run_bit(delay=0.0))
    sc_item = next(i for i in bit_unsynced["items"] if i["item"] == "System clock")
    assert sc_item["status"] == "warn"

    # Once chrony reports synced to PPS: diagnose clears and BIT is ok with source + offset.
    async def fake_ok():
        return 0, SAMPLE_TRACKING_PPS, ""

    d.timesync._runner = fake_ok
    asyncio.run(d.timesync.poll_once())
    fs2 = diagnose.findings(d, d.catalog)
    assert not [f for f in fs2 if f["title"] == "System clock not synced to GNSS"]

    bit_synced = asyncio.run(d.run_bit(delay=0.0))
    sc_ok = next(i for i in bit_synced["items"] if i["item"] == "System clock")
    assert sc_ok["status"] == "ok"
    assert "PPS" in sc_ok["detail"] and "ms" in sc_ok["detail"]


def test_configure_set_clock_question_and_needs():
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    cat = configure.load_catalog()

    asked_yesno = []

    class RecordingUI:
        def menu(self, text, items, default=None):
            return default if default is not None else items[0][0]

        def checklist(self, text, items, on):
            return list(on)

        def text(self, prompt, default):
            return str(default)

        def password(self, prompt):
            return "x" * 10

        def yesno(self, prompt, default_yes=True):
            asked_yesno.append((prompt, default_yes))
            return default_yes if prompt.startswith("Set the Pi's clock") else False

        def info(self, text):
            pass

    answers, _ = configure.interview(RecordingUI(), base, cat)
    clock_q = [q for q in asked_yesno if q[0].startswith("Set the Pi's clock from GNSS (chrony)?")]
    assert len(clock_q) == 1 and clock_q[0][1] is True
    assert answers["timing.set_clock"] is True

    cfg = configure.build_config(base, answers, cat)
    assert cfg["timing"]["set_clock"] is True
    assert "chrony" in configure.needs_extras(cfg)

    cfg_both = configure.build_config(base, {"pps.pin": "18", "timing.set_clock": True}, cat)
    assert configure.needs_extras(cfg_both) == ["pps:18", "chrony"]
