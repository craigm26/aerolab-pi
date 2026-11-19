"""AeroLab-Pi flight telemetry daemon and cockpit HUD server.

Run on a laptop with simulated sensors:
    python daemon/main.py --sim

Run on the Pi with real sensors (install.sh sets this up as a service):
    python daemon/main.py --config config/sensors.local.yaml
"""

import argparse
import asyncio
import contextlib
import csv
import json
import math
import os
import random
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import serial_asyncio
import uvicorn
import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buddy as buddy_mod  # noqa: E402
import calibration as calib  # noqa: E402
import drivers  # noqa: E402
import integrity as integ  # noqa: E402
import camera as camera_mod  # noqa: E402
import diagnose  # noqa: E402
import ppsclock  # noqa: E402
import rawlog  # noqa: E402
import survey as survey_mod  # noqa: E402
from supervise import Supervisor, watchdog  # noqa: E402

# gpiozero works on every Pi model including the Pi 5 (RPi.GPIO does not).
# On a laptop it is missing or has no pin factory, and triggers are simulated.
try:
    from gpiozero import DigitalOutputDevice
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

APP_ROOT = Path(__file__).resolve().parent.parent
HUD_PATH = APP_ROOT / "hud" / "index.html"
DEFAULT_CAL_PATH = APP_ROOT / "config" / "calibration.local.json"
STALE_AFTER_S = 2.0
M_TO_FT = 3.28084
BIT_DELAY_S = 8.0

# Default driver per sensor section when the config does not name one.
DEFAULT_DRIVER = {"gnss": "nmea", "laser_altimeter": "ascii_number", "imu_ahrs": "csv_rpy"}
ROLE = {"gnss": "gnss", "laser_altimeter": "laser", "imu_ahrs": "imu"}


@dataclass
class TelemetryState:
    utc_iso: str = ""
    unix_ts: float = 0.0
    flight_id: int = 0
    # GNSS
    lat: float = 0.0
    lon: float = 0.0
    alt_msl_m: float = 0.0
    alt_ref: str = "MSL"          # "HAE" when the source reports height above the ellipsoid
    gps_fix: int = 0
    sats: int = 0                 # -1 when the source does not report a count
    hdop: float = 0.0
    gs_ms: float = 0.0
    track_deg: float = 0.0
    gnss_ok: bool = False
    gnss_time_offset_s: float | None = None
    # Laser altimeter and fused AGL
    laser_raw_m: float = 0.0      # last reading as received
    laser_used_m: float = 0.0     # after the spike filter and scale
    agl_m: float = 0.0
    agl_ft: float = 0.0
    agl_quality: str = "ok"       # ok | tilt | range
    off_nadir_deg: float = 0.0
    laser_ok: bool = False        # a valid range in the last 2 s
    laser_alive: bool = False     # the laser is talking (valid range or "no target") in the last 2 s
    laser_no_return_pct: float = 0.0   # share of the last 10 s of laser output that was "no target"
    # Attitude (AHRS/IMU or GNSS/INS), after calibration
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_deg: float = 0.0
    imu_ok: bool = False
    # Payload and system
    trigger_count: int = 0
    gpio_live: bool = False
    logging_active: bool = False
    log_file: str = ""
    log_dir: str = ""
    sim_mode: bool = False
    calibrated: bool = False
    low_agl_warn_ft: float = 200.0


# ---------------------------------------------------------------------------
# Pure helpers (unit tested in tests/)
# ---------------------------------------------------------------------------

def compute_agl(laser_raw_m: float, roll_deg: float, pitch_deg: float,
                mounting_offset_m: float) -> float:
    """Tilt-compensated height above ground from a body-fixed, downward laser.

    A laser fixed to the airframe measures slant range when the aircraft is
    banked or pitched. The vertical component is range * cos(pitch) * cos(roll).
    The mounting offset is then subtracted so 0 means wheels/belly on the ground.
    """
    vertical = laser_raw_m * math.cos(math.radians(pitch_deg)) * math.cos(math.radians(roll_deg))
    return max(0.0, vertical - mounting_offset_m)


def parse_laser_line(line: str, unit: str) -> float | None:
    """First number on the line, in metres. None for blank or garbled lines."""
    r = drivers.parse_number(line.strip(), unit=unit) if line.strip() else None
    return r["range_m"] if r else None


def parse_imu_line(line: str) -> tuple[float, float, float] | None:
    """roll,pitch,yaw CSV in degrees. None for anything else."""
    r = drivers.parse_csv_rpy(line.strip())
    return (r["roll"], r["pitch"], r["yaw"]) if r else None


def resolve_log_dir(primary: str, fallback: str, sim_mode: bool) -> Path:
    """Use the SSD only when its mount point is really mounted and writable.

    Checking only that /mnt exists (it always does) would silently write
    "SSD" logs onto the SD card, or crash on a permission error.
    Simulated runs always go to <fallback>/sim so they never mix with flights.
    """
    fallback_path = Path(fallback)
    if not fallback_path.is_absolute():
        fallback_path = APP_ROOT / fallback_path
    if sim_mode:
        return fallback_path / "sim"

    primary_path = Path(primary)
    mount = primary_path.parent
    if os.path.ismount(mount) and os.access(mount, os.W_OK):
        return primary_path
    return fallback_path


def tod_offset(gnss_tod: float, now: float) -> float:
    """System clock minus GNSS time of day, wrapped to +/- 12 h."""
    sys_tod = now % 86400.0
    d = sys_tod - gnss_tod
    return (d + 43200.0) % 86400.0 - 43200.0


def section_enabled(cfg: dict, section: str) -> bool:
    c = cfg.get(section) or {}
    return bool(c) and c.get("enabled", True) and str(c.get("port", "")).lower() not in ("", "none")


def _unescape(s: str) -> bytes:
    """Config strings like 'd\\r\\n' -> bytes."""
    return s.encode("utf-8").decode("unicode_escape").encode("latin-1") if s else b""


def now_utc() -> tuple[float, str]:
    dt = datetime.now(timezone.utc)
    return dt.timestamp(), dt.isoformat()


def _add_columns(conn: sqlite3.Connection, table: str, cols: dict):
    """Add columns to a table from an older version, keeping its data."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, typ in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class AeroLabDaemon:
    def __init__(self, cfg: dict, sim_mode: bool = False, cal_path: Path | None = None):
        self.cfg = cfg
        self.sim_mode = sim_mode
        hud = cfg.get("hud") or {}
        self.state = TelemetryState(sim_mode=sim_mode, low_agl_warn_ft=float(hud.get("low_agl_warn_ft", 200)))
        self.last_seen = {"gnss": 0.0, "laser": 0.0, "imu": 0.0}
        self.laser_alive_at = 0.0
        self.laser_valid_total = 0
        from collections import deque as _dq
        self.laser_recent = _dq(maxlen=5000)  # (monotonic s, range m or None) for bench checks
        self.laser_window: list = []          # (time, had a valid range) over the last 10 s
        self.clients: set[WebSocket] = set()
        self.tasks: list[asyncio.Task] = []
        self.trigger_pin = None
        lc = cfg.get("laser_altimeter") or {}
        self.max_range_m = float(lc.get("max_range_m", 0) or 0)
        self.max_tilt_deg = float(hud.get("max_tilt_deg", 20.0))

        # Calibration (config/calibration.local.json unless overridden)
        self.cal_path = Path(cal_path or cfg.get("calibration_file") or DEFAULT_CAL_PATH)
        if not self.cal_path.is_absolute():
            self.cal_path = APP_ROOT / self.cal_path
        self.cal = calib.Calibration.load(self.cal_path)
        self.state.calibrated = bool(self.cal.updated)
        self.capture: calib.GroundCapture | None = None
        self.raw_roll = 0.0
        self.raw_pitch = 0.0
        self.fix_time = 0.0

        # Integrity monitors
        self.monitors = {
            "gnss": integ.SensorMonitor((cfg.get("gnss") or {}).get("rate_hz")),
            "laser": integ.SensorMonitor(lc.get("rate_hz")),
            "imu": integ.SensorMonitor((cfg.get("imu_ahrs") or {}).get("rate_hz")),
        }
        self.laser_gate = integ.LaserGate(float(lc.get("spike_gate_m", 5.0)), float(lc.get("spike_gate_rel", 0.05)))
        self.gnss_check = integ.GnssIntegrity()
        self.heading_check = integ.HeadingCheck()
        self.bit: dict = {"done": False, "status": "running", "items": []}
        self._debounce: dict = {}
        self.drivers: dict = {}

        log_cfg = cfg["logging"]
        self.log_dir = resolve_log_dir(log_cfg["primary_dir"], log_cfg["fallback_dir"], sim_mode)
        self.fallback_logging = (not sim_mode) and self.log_dir != Path(log_cfg["primary_dir"])
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state.log_dir = str(self.log_dir)
        self.db_path = self.log_dir / "aerolab_flight.db"
        self._init_db()

        self.mirror_dirs = [Path(d) for d in (log_cfg.get("mirror_dirs") or [])]
        self.readings = rawlog.ReadingLog(lambda: self.db_path, on_error=self._data_error)
        self.raw: dict[str, rawlog.RawWriter] = {}
        self.camera = camera_mod.Camera(
            cfg.get("camera"), sim=sim_mode,
            on_exposure=lambda seq, u, m: self.readings.add("exposure", (self.state.flight_id, seq, u, m)),
            on_status=lambda st: self.readings.add("camstatus", (self.state.flight_id, time.time_ns(), json.dumps(st))))
        tcfg = cfg.get("timing") or {}
        self.pps = ppsclock.PPSClock(tcfg.get("pps_device") if not sim_mode else None,
                                     on_edge=lambda seq, a, off: self.readings.add(
                                         "pps", (self.state.flight_id, seq, a, a - off, off)))
        self._data_errors: dict[str, float] = {}

        try:
            self.catalog = yaml.safe_load((APP_ROOT / "config" / "hardware.yaml").read_text())
        except (OSError, yaml.YAMLError):
            self.catalog = None
        self.ready: dict = {"level": "amber", "text": "Starting", "blockers": 0, "warnings": 0}
        self.findings: list = []
        self.buddy = buddy_mod.Buddy(cfg, db_path=self.db_path)
        self.buddy.diagnosis = lambda: diagnose.as_text(self.findings)
        self.buddy.camera_fn = lambda: self.camera.snapshot()
        self.sup = Supervisor(report=lambda level, text: self.buddy.emit(level, text))
        if sim_mode:
            # The simulator plays the configured hardware: no attitude sensor -> no attitude.
            gdrv = (cfg.get("gnss") or {}).get("driver", "nmea")
            att = section_enabled(cfg, "imu_ahrs") or "attitude" in drivers.DRIVERS.get(gdrv, {}).get("kinds", [])
            self.buddy.configured = {"gnss": True, "laser": True, "imu": att}
        else:
            used = {"gnss": section_enabled(cfg, "gnss"),
                    "laser": section_enabled(cfg, "laser_altimeter"),
                    "imu": section_enabled(cfg, "imu_ahrs")}
            # A GNSS/INS on the gnss port also supplies attitude.
            gdrv = (cfg.get("gnss") or {}).get("driver", "nmea")
            if not used["imu"] and "attitude" in drivers.DRIVERS.get(gdrv, {}).get("kinds", []):
                used["imu"] = True
            self.buddy.configured = used
        self.ai_status: dict = {"available": False, "reason": "checking"}
        self.new_flight("boot")
        self.survey = survey_mod.SurveyManager(self)
        self.shutdown_state: dict = {"state": "running"}

    @classmethod
    def from_file(cls, config_path: str, sim_mode: bool = False) -> "AeroLabDaemon":
        with open(config_path, "r") as f:
            return cls(yaml.safe_load(f), sim_mode=sim_mode)

    # -- database -----------------------------------------------------------

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        """SQLite in WAL mode: a power cut loses at most the last few rows."""
        conn = self._connect_db()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unix_ts REAL,
                utc_iso TEXT,
                lat REAL, lon REAL, alt_msl_m REAL, gps_fix INTEGER, sats INTEGER,
                laser_raw_m REAL, agl_m REAL, agl_ft REAL,
                roll_deg REAL, pitch_deg REAL, yaw_deg REAL,
                gnss_ok INTEGER, laser_ok INTEGER, imu_ok INTEGER,
                trigger_count INTEGER
            )
            """
        )
        _add_columns(conn, "telemetry", {"flight_id": "INTEGER", "laser_used_m": "REAL", "agl_quality": "TEXT",
                                         "hdop": "REAL", "gs_ms": "REAL", "track_deg": "REAL",
                                         "raw_roll_deg": "REAL", "raw_pitch_deg": "REAL",
                                         "fix_lat": "REAL", "fix_lon": "REAL", "gnss_age_ms": "REAL"})
        # trigger_id restarts at 1 every boot, so it is not the primary key;
        # session_start tells boots apart.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payload_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_start REAL,
                trigger_id INTEGER,
                unix_ts REAL,
                utc_iso TEXT,
                lat REAL, lon REAL, alt_msl_m REAL,
                agl_m REAL, roll_deg REAL, pitch_deg REAL, yaw_deg REAL
            )
            """
        )
        # lat/lon are extrapolated to the trigger instant; the raw fix and the age
        # of each sensor's last sample are kept so processing can redo it.
        _add_columns(conn, "payload_triggers", {"flight_id": "INTEGER", "fix_lat": "REAL", "fix_lon": "REAL",
                                                "gnss_age_ms": "REAL", "laser_age_ms": "REAL",
                                                "imu_age_ms": "REAL", "gs_ms": "REAL", "track_deg": "REAL",
                                                "agl_quality": "TEXT"})
        conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "unix_ts REAL, level TEXT, text TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS flights (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "started REAL, reason TEXT, profile TEXT, calibration TEXT, config TEXT, self_test TEXT)")
        conn.commit()
        conn.close()
        self.session_start = time.time()

    async def flush_all(self):
        """Everything queued or buffered goes to disk now (survey stop, shutdown)."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.executescript(rawlog.READINGS_SCHEMA)
        try:
            while self.readings.q:
                self.readings._flush(conn)
        finally:
            conn.close()
        for w in self.raw.values():
            w.sync()

    async def shutdown(self, dry_run: bool = False) -> dict:
        """Safe power-off: stop any survey, park the camera's card, flush, then power off the Pi."""
        steps = []
        self.shutdown_state = {"state": "stopping", "steps": steps}
        if self.survey.state == "recording":
            r = await self.survey.stop()
            steps.append({"step": "Stop survey", "ok": r.get("ok", False), "detail": self.survey.message})
        cam = self.camera
        if cam.enabled and cam.status_url and not self.sim_mode:
            base = cam.status_url.rsplit("/status", 1)[0]
            ok, detail = False, ""
            try:
                import httpx
                async with httpx.AsyncClient(timeout=3.0) as client:
                    await client.post(f"{base}/powerdownready", json={"power_down": True})
                    for _ in range(30):
                        r = (await client.get(f"{base}/powerdownready")).json()
                        if r.get("ready_for_power_down"):
                            ok = True
                            break
                        await asyncio.sleep(1.0)
                detail = "camera storage unmounted, safe to cut its power" if ok else "camera did not confirm in 30 s"
            except Exception as exc:
                detail = f"camera not reachable ({exc}); hold its button 5 s before cutting power"
            steps.append({"step": "Camera ready for power-off", "ok": ok, "detail": detail})
        await self.flush_all()
        steps.append({"step": "Flush data to disk", "ok": True, "detail": f"{self.readings.written} readings saved"})
        self.buddy.emit("info", "Shutting down safely")
        if dry_run or self.sim_mode:
            self.shutdown_state = {"state": "ready", "steps": steps, "detail": "simulation: the Pi stays on"}
            return self.shutdown_state
        self.shutdown_state = {"state": "powering_off", "steps": steps,
                               "detail": "the Pi powers off in a few seconds; wait for the green LED to stop"}

        async def _off():
            await asyncio.sleep(2.0)
            proc = await asyncio.create_subprocess_exec("sudo", "-n", "/usr/bin/systemctl", "poweroff")
            await proc.wait()
        asyncio.create_task(_off())
        return self.shutdown_state

    def _data_error(self, msg: str):
        """A write failed somewhere. Say so loudly, but at most once a minute per message."""
        print(f"[data] {msg}", flush=True)
        key = msg.split(":")[0]
        now = time.time()
        if now - self._data_errors.get(key, 0) > 60 and hasattr(self, "buddy"):
            self._data_errors[key] = now
            self.buddy.emit("alert", f"Data write problem: {msg}")

    def log_dirs(self) -> list[Path]:
        return [self.log_dir] + [d for d in self.mirror_dirs if d != self.log_dir]

    def recheck_log_dir(self) -> bool:
        """After a write error: is the SSD gone (or back)? Move logging to wherever works. True if moved."""
        lc = self.cfg["logging"]
        new = resolve_log_dir(lc["primary_dir"], lc["fallback_dir"], self.sim_mode)
        if new == self.log_dir:
            return False
        try:
            new.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        old = self.log_dir
        self.log_dir = new
        self.fallback_logging = (not self.sim_mode) and new != Path(lc["primary_dir"])
        self.state.log_dir = str(new)
        self.db_path = new / "aerolab_flight.db"
        self.buddy.db_path = self.db_path
        self._init_db()
        for w in self.raw.values():
            w.close()
            w.dirs = self.log_dirs()
        self.buddy.emit("alert" if self.fallback_logging else "info", f"Logging moved from {old} to {new}")
        return True

    def new_flight(self, reason: str = "new flight") -> int:
        """Start a flight record: snapshots calibration and config so every flight is traceable."""
        conn = self._connect_db()
        cur = conn.execute(
            "INSERT INTO flights (started, reason, profile, calibration, config, self_test) VALUES (?,?,?,?,?,?)",
            (time.time(), reason, self.cfg.get("profile", ""), json.dumps(self.cal.summary()),
             json.dumps(self.cfg, default=str), json.dumps(self.bit)))
        conn.commit()
        conn.close()
        self.state.flight_id = cur.lastrowid
        return cur.lastrowid

    # -- measurement --------------------------------------------------------

    def mounting_offset(self) -> float:
        if self.cal.laser_offset_m is not None:
            return float(self.cal.laser_offset_m)
        return float(self.cfg["laser_altimeter"]["mounting_offset_m"])

    def beam_attitude(self) -> tuple[float, float]:
        """Laser beam roll/pitch: calibrated aircraft attitude plus the laser's boresight."""
        return (self.state.roll_deg + self.cal.boresight_roll_deg,
                self.state.pitch_deg + self.cal.boresight_pitch_deg)

    def on_laser(self, rng: float):
        """A new laser range: gate spikes, then compute AGL with the attitude at this moment.

        AGL is only computed when a laser sample arrives, never by pairing a new
        attitude with an old range: in a bank the slant range changes tens of
        metres per second, and a stale range would be an error of metres.
        """
        s = self.state
        roll, pitch = self.beam_attitude()
        used = rng * self.cal.laser_scale
        vertical = compute_agl(used, roll, pitch, self.mounting_offset())
        if not self.laser_gate.accept(vertical):
            return False
        s.laser_used_m = round(used, 3)
        s.agl_m = round(vertical, 2)
        s.agl_ft = round(vertical * M_TO_FT, 1)
        self.update_quality()
        return True

    def update_quality(self):
        s = self.state
        roll, pitch = self.beam_attitude()
        s.off_nadir_deg = round(integ.off_nadir_deg(roll, pitch), 1)
        s.agl_quality = integ.agl_quality(roll, pitch, s.laser_raw_m, self.max_range_m, self.max_tilt_deg)

    def apply(self, r: dict, role: str | None = None, ts: tuple[int, int] | None = None):
        """Apply one driver reading to the state, whatever kinds it carries.

        ts = (utc_ns, mono_ns) when the bytes were read; every laser range and
        GNSS fix is also queued for the per-reading tables with that time.
        """
        utc_ns, mono_ns = ts or (time.time_ns(), time.monotonic_ns())
        now = utc_ns / 1e9
        s = self.state
        # Monitors follow what a reading carries: a GNSS/INS feeds both GNSS and attitude.
        if "fix" in r or "lat" in r:
            self.monitors["gnss"].on_reading(now)
        if "roll" in r:
            self.monitors["imu"].on_reading(now)
        if "range_m" in r or "no_target" in r:
            self.monitors["laser"].on_reading(now)
        if "fix" in r:
            s.gps_fix, s.sats = int(r["fix"]), int(r.get("sats", s.sats))
            if "gnss_tod" in r:
                s.gnss_time_offset_s = round(tod_offset(r["gnss_tod"], now), 2)
            if "hdop" in r:
                s.hdop = round(float(r["hdop"]), 2)
            self.last_seen["gnss"] = now
        if "gs_ms" in r:
            s.gs_ms = round(float(r["gs_ms"]), 2)
            if "track_deg" in r:
                s.track_deg = round(float(r["track_deg"]), 1)
        if "lat" in r:
            if self.gnss_check.check(now, r["lat"], r["lon"]):
                self.buddy.emit("warn", f"GNSS position jumped {self.gnss_check.last_jump_m:,.0f} m")
            s.lat, s.lon = round(r["lat"], 7), round(r["lon"], 7)
            self.fix_time = now
            if r.get("alt_msl") is not None:
                s.alt_msl_m, s.alt_ref = round(float(r["alt_msl"]), 2), "MSL"
            elif r.get("alt_hae") is not None:
                s.alt_msl_m, s.alt_ref = round(float(r["alt_hae"]), 2), "HAE"
            self.last_seen["gnss"] = now
            if self.capture:
                self.capture.add(lat=s.lat, lon=s.lon, alt=s.alt_msl_m, fix=s.gps_fix, sats=s.sats,
                                 hdop=s.hdop or None, clock_offset=s.gnss_time_offset_s)
        if "fix" in r or "lat" in r:
            self.readings.add("gnss", (s.flight_id, utc_ns, mono_ns, r.get("gnss_tod"), r.get("lat"), r.get("lon"),
                                       r.get("alt_msl", r.get("alt_hae")), "HAE" if "alt_hae" in r else "MSL",
                                       r.get("fix"), r.get("sats"), r.get("hdop"), r.get("gs_ms"), r.get("track_deg")))
        if "roll" in r and "pitch" in r:
            self.raw_roll, self.raw_pitch = float(r["roll"]), float(r["pitch"])
            roll, pitch = self.cal.correct_attitude(self.raw_roll, self.raw_pitch)
            s.roll_deg, s.pitch_deg = round(roll, 2), round(pitch, 2)
            if "yaw" in r:
                s.yaw_deg = round(r["yaw"] % 360.0, 2)
            self.last_seen["imu"] = now
            if self.capture:
                self.capture.add(roll_raw=self.raw_roll, pitch_raw=self.raw_pitch)
            self.update_quality()
        elif "yaw" in r:
            s.yaw_deg = round(r["yaw"] % 360.0, 2)
        if "no_target" in r:
            self._laser_seen(now, False)
            self.readings.add("laser", (s.flight_id, utc_ns, mono_ns, None, 0, None, s.roll_deg, s.pitch_deg))
            return
        if "range_m" in r:
            rng = float(r["range_m"])
            if self.max_range_m and rng > self.max_range_m * 1.05:
                # beyond the sensor's rating: not used for AGL, but kept in the log
                self._laser_seen(now, False)
                self.readings.add("laser", (s.flight_id, utc_ns, mono_ns, rng, 0, None, s.roll_deg, s.pitch_deg))
                return
            self._laser_seen(now, True, rng)
            self.laser_valid_total += 1
            s.laser_raw_m = round(rng, 3)
            if self.capture:
                self.capture.add(laser_raw=rng)
            ok = self.on_laser(rng)
            if ok:
                self.last_seen["laser"] = now
            self.readings.add("laser", (s.flight_id, utc_ns, mono_ns, rng, int(ok), s.agl_m if ok else None,
                                        s.roll_deg, s.pitch_deg))

    def _laser_seen(self, now: float, valid: bool, rng: float | None = None):
        self.laser_recent.append((time.monotonic(), rng if valid else None))
        self.laser_alive_at = now
        w = self.laser_window
        w.append((now, valid))
        while w and now - w[0][0] > 10.0:
            w.pop(0)
        self.state.laser_no_return_pct = round(100.0 * sum(1 for _, v in w if not v) / len(w), 1)

    def extrapolated_position(self, now: float) -> tuple[float, float]:
        """Where the aircraft is now, not where it was at the last fix: last fix + ground speed x age."""
        s = self.state
        age = now - self.fix_time if self.fix_time else 0.0
        if 0 < age < 2.0 and s.gs_ms > 1.0:
            return integ.project(s.lat, s.lon, s.track_deg, s.gs_ms * age)
        return s.lat, s.lon

    # -- sensor loops -------------------------------------------------------

    async def sim_loop(self):
        """Simulated sensors, fed through the same path as real ones.

        Flies around the planned survey height. Readings go through apply(), so
        calibration, the spike filter and the integrity monitors all run. The
        simulated installation has known errors (sim.imu_roll_bias_deg etc.) so
        a ground calibration can be checked against the truth.

        While a ground calibration is running the simulated aircraft is parked:
        laser reads its true mounting height, attitude sits at the bias, GNSS
        stays put with a little scatter.

        With sim.faults: true, the laser reports "no target" for 4 s every 45 s.
        With sim.start_on_ground: true, the aircraft sits on the ramp from boot
        until a ground calibration has been applied, then takes off: boot,
        calibrate, fly, as on a real day.
        """
        sc = self.cfg.get("sim") or {}
        faults = bool(sc.get("faults", False))
        rb, pb = float(sc.get("imu_roll_bias_deg", 0.8)), float(sc.get("imu_pitch_bias_deg", -0.5))
        true_offset = float(sc.get("laser_offset_m", 0.62))
        survey_m = float((self.cfg.get("hud") or {}).get("survey_agl_ft", 0) or 0) / M_TO_FT
        center = survey_m if survey_m > 0 else 60.0
        swing = 0.10 * center
        rnd = random.Random(7)
        t = 0.0
        lat, lon = 38.5816, -121.4944
        on_ground = bool(sc.get("start_on_ground", False))
        while True:
            if on_ground and self.state.calibrated:
                on_ground = False
            parked = on_ground or (self.capture is not None and not self.capture.done)
            if parked:
                self.apply({"fix": 1, "sats": 11, "hdop": 0.9, "gnss_tod": time.time() % 86400,
                            "lat": lat + rnd.gauss(0, 4e-6), "lon": lon + rnd.gauss(0, 5e-6),
                            "alt_msl": 120.0 + 1.5 + rnd.gauss(0, 0.4)}, "gnss")
                self.apply({"gs_ms": 0.0, "track_deg": 0.0})
                if self.buddy.configured.get("imu"):
                    self.apply({"roll": rb + rnd.gauss(0, 0.05), "pitch": pb + rnd.gauss(0, 0.05),
                                "yaw": 90.0}, "imu")
                self.apply({"range_m": true_offset + rnd.gauss(0, 0.01)}, "laser")
            else:
                roll = float(sc.get("bank_deg", 12.0)) * math.sin(t * 0.25)
                pitch = 3.5 * math.cos(t * 0.15)
                yaw = (90.0 + t * 1.5) % 360.0
                agl_true = center + swing * math.sin(t * 0.08)
                gs = 55.0
                lat, lon = integ.project(lat, lon, yaw, gs * 0.1)
                self.apply({"fix": 1, "sats": 11, "hdop": 0.9, "gnss_tod": time.time() % 86400,
                            "lat": lat, "lon": lon, "alt_msl": agl_true + 120.0 + 30.0 * math.sin(t * 0.03)},
                           "gnss")
                self.apply({"gs_ms": gs, "track_deg": yaw})
                if self.buddy.configured.get("imu"):
                    self.apply({"roll": roll + rb, "pitch": pitch + pb, "yaw": yaw}, "imu")
                if not (faults and 41.0 <= (t % 45.0)):
                    rng = (agl_true + true_offset) / (math.cos(math.radians(roll)) * math.cos(math.radians(pitch)))
                    self.apply({"range_m": rng + rnd.gauss(0, 0.03)}, "laser")
                else:
                    # a dropout: the laser keeps talking but reports "no target" (e.g. 99999.99m)
                    self.apply({"no_target": True}, "laser")
                t += 0.1
            await asyncio.sleep(0.1)

    async def _serial_chunks(self, name: str, port: str, baud: int, init: bytes = b"",
                             poll: bytes = b"", poll_hz: float = 0.0):
        """Yield raw bytes forever, reopening the port if it drops.

        init is written once after opening (wake-up / start-streaming commands);
        poll is written poll_hz times a second for sensors that answer on demand.
        """
        while True:
            writer = None
            poller = None
            try:
                reader, writer = await serial_asyncio.open_serial_connection(url=port, baudrate=baud)
                print(f"[{name}] opened {port} @ {baud}", flush=True)
                if init:
                    writer.write(init)
                    await writer.drain()
                if poll and poll_hz > 0:
                    async def _poll(w=writer):
                        while True:
                            w.write(poll)
                            await asyncio.sleep(1.0 / poll_hz)
                    poller = asyncio.create_task(_poll())
                while True:
                    data = await reader.read(4096)
                    if not data:
                        raise ConnectionError("port closed")
                    yield data
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[{name}] {port}: {exc}; retrying in 2 s", flush=True)
                await asyncio.sleep(2.0)
            finally:
                if poller is not None:
                    poller.cancel()
                if writer is not None:
                    writer.close()

    async def sensor_loop(self, section: str):
        c = self.cfg[section]
        name = c.get("driver") or DEFAULT_DRIVER[section]
        opts = {k: v for k, v in (c.get("driver_options") or {}).items()}
        if section == "laser_altimeter" and "unit" in c:
            opts.setdefault("unit", c["unit"])
        drv = drivers.make_driver(name, **opts)
        self.drivers[section] = drv
        role = ROLE[section]
        mon = self.monitors[role]
        label = c.get("name") or section
        init = drivers.SERIAL_SCRIPTS.get(c.get("init_script", ""), b"") + _unescape(c.get("init", ""))
        poll = _unescape(c.get("poll", ""))
        raw = rawlog.RawWriter(self.log_dirs(), section, {"name": label, "port": c["port"], "baud": c["baud"],
                                                          "driver": name, "flight_id": self.state.flight_id},
                               on_error=self._data_error)
        self.raw[section] = raw
        async for data in self._serial_chunks(label, c["port"], int(c["baud"]), init=init, poll=poll,
                                              poll_hz=float(c.get("poll_hz", 0) or 0)):
            ts = (time.time_ns(), time.monotonic_ns())
            raw.write(data, *ts)
            for reading in drv.feed(data):
                self.apply(reading, role, ts)
            st = getattr(drv, "stats", None)
            if st is not None:
                mon.on_counters(time.time(), st.good, st.bad)

    # -- payload trigger ----------------------------------------------------

    def _open_trigger_pin(self, pin: int, active_high: bool):
        if self.sim_mode or not HAS_GPIO:
            return None
        try:
            return DigitalOutputDevice(pin, active_high=active_high, initial_value=False)
        except Exception as exc:  # no pin factory, pin busy, not a Pi
            print(f"[trigger] GPIO {pin} unavailable ({exc}); simulating pulses", flush=True)
            return None

    async def payload_sync_loop(self):
        """Pulse the camera/radar trigger and log where the aircraft was at that instant."""
        c = self.cfg["payload_sync"]
        if not c.get("enabled", False):
            return

        period = 1.0 / float(c["trigger_hz"])
        pulse_s = float(c["pulse_width_ms"]) / 1000.0
        method = c.get("method", "gpio")
        http_url = c.get("http_url", "")
        http = None
        if method == "http" and not self.sim_mode:
            import httpx
            http = httpx.AsyncClient(timeout=2.0)
            self.state.gpio_live = True   # a live trigger path exists
        else:
            self.trigger_pin = self._open_trigger_pin(int(c["gpio_pin"]), bool(c.get("active_high", True)))
            self.state.gpio_live = self.trigger_pin is not None

        conn = self._connect_db()
        next_fire = time.monotonic()
        try:
            while True:
                # Stamp at the leading edge, the moment the payload is told to capture.
                ts, iso = now_utc()
                if self.trigger_pin is not None:
                    self.trigger_pin.on()
                elif http is not None:
                    async def _fire():
                        try:
                            await http.get(http_url)
                        except Exception as exc:
                            print(f"[trigger] HTTP capture failed: {exc}", flush=True)
                    asyncio.create_task(_fire())
                self.state.trigger_count += 1
                self.readings.add("trigger", (self.state.flight_id, self.state.trigger_count,
                                              time.time_ns(), time.monotonic_ns()))
                s = self.state
                lat, lon = self.extrapolated_position(ts)
                ages = {k: (ts - v) * 1000 if v else None for k, v in self.last_seen.items()}
                snapshot = (self.session_start, s.trigger_count, ts, iso, lat, lon, s.alt_msl_m, s.agl_m,
                            s.roll_deg, s.pitch_deg, s.yaw_deg, s.flight_id, s.lat, s.lon,
                            ages["gnss"], ages["laser"], ages["imu"], s.gs_ms, s.track_deg, s.agl_quality)
                await asyncio.sleep(pulse_s)
                if self.trigger_pin is not None:
                    self.trigger_pin.off()

                conn.execute(
                    """
                    INSERT INTO payload_triggers
                    (session_start, trigger_id, unix_ts, utc_iso, lat, lon, alt_msl_m,
                     agl_m, roll_deg, pitch_deg, yaw_deg, flight_id, fix_lat, fix_lon,
                     gnss_age_ms, laser_age_ms, imu_age_ms, gs_ms, track_deg, agl_quality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    snapshot,
                )
                conn.commit()

                # Fixed cadence that does not drift with DB write time.
                next_fire += period
                await asyncio.sleep(max(0.0, next_fire - time.monotonic()))
        finally:
            if self.trigger_pin is not None:
                self.trigger_pin.off()
                self.trigger_pin.close()
            if http is not None:
                await http.aclose()
            conn.close()

    # -- logging, buddy and HUD ---------------------------------------------

    def refresh_health(self, now: float):
        self.state.gnss_ok = (now - self.last_seen["gnss"]) < STALE_AFTER_S
        self.state.laser_ok = (now - self.last_seen["laser"]) < STALE_AFTER_S
        self.state.laser_alive = (now - self.laser_alive_at) < STALE_AFTER_S
        self.state.imu_ok = (now - self.last_seen["imu"]) < STALE_AFTER_S

    async def logger_loop(self):
        """Dual-write a 5 Hz state sample: SQLite (WAL) plus fsynced 5-minute CSV chunks.

        Every laser reading and GNSS fix goes to the per-reading tables and raw
        files separately (rawlog.py); this is the display-rate summary. A write
        error never stops it: it reports, checks whether the SSD went away (and
        moves to the fallback disk if so), reopens and carries on.
        """
        rotate_s = float(self.cfg["logging"]["rotate_minutes"]) * 60.0
        interval = 1.0 / float(self.cfg["logging"]["log_hz"])
        fields = list(asdict(self.state).keys())
        conn = csv_file = csv_writer = None
        chunk_start = 0.0
        while True:
            self.sup.beat("logger")
            try:
                if conn is None:
                    conn = self._connect_db()
                ts, iso = now_utc()
                self.state.unix_ts, self.state.utc_iso = ts, iso
                self.refresh_health(time.time())

                if csv_file is None or (ts - chunk_start) >= rotate_s:
                    if csv_file:
                        csv_file.close()
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    path = self.log_dir / f"flight_{stamp}.csv"
                    csv_file = open(path, "w", newline="")
                    csv_writer = csv.DictWriter(csv_file, fieldnames=fields)
                    csv_writer.writeheader()
                    self.state.log_file = path.name
                    chunk_start = ts

                csv_writer.writerow(asdict(self.state))
                csv_file.flush()
                os.fsync(csv_file.fileno())

                s = self.state
                # lat/lon: extrapolated to this instant; fix_lat/fix_lon: the last fix as received.
                ext_lat, ext_lon = self.extrapolated_position(ts)
                conn.execute(
                    """
                    INSERT INTO telemetry
                    (unix_ts, utc_iso, lat, lon, alt_msl_m, gps_fix, sats,
                     laser_raw_m, agl_m, agl_ft, roll_deg, pitch_deg, yaw_deg,
                     gnss_ok, laser_ok, imu_ok, trigger_count,
                     flight_id, laser_used_m, agl_quality, hdop, gs_ms, track_deg, raw_roll_deg, raw_pitch_deg,
                     fix_lat, fix_lon, gnss_age_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (s.unix_ts, s.utc_iso, ext_lat, ext_lon, s.alt_msl_m, s.gps_fix, s.sats,
                     s.laser_raw_m, s.agl_m, s.agl_ft, s.roll_deg, s.pitch_deg, s.yaw_deg,
                     int(s.gnss_ok), int(s.laser_ok), int(s.imu_ok), s.trigger_count,
                     s.flight_id, s.laser_used_m, s.agl_quality, s.hdop, s.gs_ms, s.track_deg,
                     self.raw_roll, self.raw_pitch, s.lat, s.lon,
                     (ts - self.fix_time) * 1000 if self.fix_time else None),
                )
                conn.commit()
                self.state.logging_active = True
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                for h in (csv_file, conn):
                    try:
                        h and h.close()
                    except Exception:
                        pass
                raise
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.state.logging_active = False
                self._data_error(f"flight log: {exc}")
                for h in (csv_file, conn):
                    try:
                        h and h.close()
                    except Exception:
                        pass
                conn = csv_file = csv_writer = None
                self.recheck_log_dir()
                await asyncio.sleep(1.0)

    async def clock_loop(self):
        """Once a second: the system and monotonic clocks side by side, so a clock step in flight is visible
        afterwards and every timestamp can be moved onto GPS time in processing."""
        while True:
            self.sup.beat("clock")
            self.readings.add("clock", (self.state.flight_id, time.time_ns(), time.monotonic_ns()))
            await asyncio.sleep(1.0)

    # -- self-test and integrity --------------------------------------------

    def enabled_sections(self) -> dict:
        return {sec: (self.sim_mode or section_enabled(self.cfg, sec)) for sec in ROLE}

    def plausibility(self, role: str) -> tuple[bool, str] | None:
        s = self.state
        if role == "laser":
            if s.laser_raw_m <= 0:
                return False, "range is 0"
            if self.max_range_m and s.laser_raw_m > self.max_range_m:
                return False, f"range {s.laser_raw_m:.1f} m beyond rating"
            return True, ""
        if role == "imu":
            if abs(s.roll_deg) > 90 or abs(s.pitch_deg) > 90:
                return False, f"attitude out of range (roll {s.roll_deg:.0f}°, pitch {s.pitch_deg:.0f}°)"
            return True, ""
        if role == "gnss":
            if s.gps_fix and not (-90 <= s.lat <= 90 and -180 <= s.lon <= 180):
                return False, "position out of range"
            return True, ""
        return None

    async def run_bit(self, delay: float | None = None) -> dict:
        """Boot self-test: config and storage at once, sensors after `delay` seconds of data."""
        if delay is None:
            delay = float((self.cfg.get("self_test") or {}).get("delay_s", BIT_DELAY_S))
        self.bit = {"done": False, "status": "running", "items": [], "started": time.time()}
        items = integ.config_checks(self.cfg, self.sim_mode, self.enabled_sections(), set(drivers.DRIVERS))
        items.append(await asyncio.to_thread(integ.storage_test, self.log_dir))
        self.bit["items"] = list(items)
        await asyncio.sleep(delay)
        now = time.time()
        names = self.buddy.names()
        conf = self.buddy.configured
        for role in ("gnss", "laser", "imu"):
            if conf.get(role):
                items.append(integ.sensor_bit(names[role], self.monitors[role], now, self.plausibility(role)))
        s = self.state
        if s.gnss_time_offset_s is not None and s.gps_fix:
            off = s.gnss_time_offset_s
            items.append({"item": "Clock vs GNSS", "status": "ok" if abs(off) < 1 else "warn", "detail": f"{off:+.2f} s"})
        ps = self.cfg.get("payload_sync") or {}
        if ps.get("enabled"):
            items.append({"item": "Payload trigger", "status": "ok" if s.trigger_count > 0 and (s.gpio_live or self.sim_mode) else "warn",
                          "detail": f"{s.trigger_count} pulses" + ("" if s.gpio_live else " (no GPIO pin driven)")})
        if self.camera.enabled:
            c = self.camera.snapshot()
            items.append({"item": f"{c['name']} exposures",
                          "status": "ok" if c["exposures"] and not c["stopped"] else "warn",
                          "detail": (f"{c['exposures']} exposures, {c['hz']:g} Hz" if c["exposures"] else
                                     ("none seen yet (camera not capturing yet, or Top-of-Frame not wired to "
                                      f"GPIO {c['pin']})" if c["gpio_ok"] else f"GPIO {c['pin']} unavailable: {c['gpio_error']}"))})
            if c["status_url"]:
                st = c["status"]
                items.append({"item": f"{c['name']} status", "status": "ok" if c["status_ok"] and st.get("sd_status") == "Ok" else "warn",
                              "detail": (f"SD {st.get('sd_gb_free')} of {st.get('sd_gb_total')} GB free, "
                                         f"auto-capture {'on' if st.get('auto_cap_active') else 'OFF'}, "
                                         f"{st.get('bus_volts')} V, time source {st.get('time_source')}")
                              if c["status_ok"] else f"not reachable at {c['status_url']}"})
        if self.pps.enabled:
            st = self.pps.status()
            items.append({"item": "GNSS PPS timing", "status": "ok" if st["ok"] else "warn", "detail": st["detail"]})
        items.append({"item": "Calibration", "status": "ok" if self.cal.updated else "warn",
                      "detail": f"ground calibration from {self.cal.updated}" if self.cal.updated
                      else "not calibrated: run Calibrate in Buddy before the first flight"})
        status = integ.overall(items)
        self.bit = {"done": True, "status": status, "items": items, "finished": time.time()}
        n_warn = sum(i["status"] == "warn" for i in items)
        n_fail = sum(i["status"] == "fail" for i in items)
        if status == "ok":
            self.buddy.emit("info", "Self-test passed")
        else:
            self.buddy.emit("alert" if n_fail else "warn",
                            f"Self-test: {n_fail} failed, {n_warn} warnings. See Ground check.")
        self.findings = diagnose.findings(self, self.catalog)
        self.ready = diagnose.readiness(self.findings)
        self.write_report()
        conn = self._connect_db()
        conn.execute("UPDATE flights SET self_test = ? WHERE id = ?", (json.dumps(self.bit), self.state.flight_id))
        conn.commit()
        conn.close()
        return self.bit

    def steady(self, key: str, cond: bool, now: float, hold_s: float = 3.0) -> bool:
        """cond, but only after it has held for hold_s: stops a callout firing on every bank."""
        state, since = self._debounce.get(key, (False, now))
        if cond != state:
            if key + ".pending" not in self._debounce or self._debounce[key + ".pending"][0] != cond:
                self._debounce[key + ".pending"] = (cond, now)
            if now - self._debounce[key + ".pending"][1] >= hold_s:
                self._debounce[key] = (cond, now)
                self._debounce.pop(key + ".pending", None)
                return cond
            return state
        self._debounce.pop(key + ".pending", None)
        return state

    def integrity_tick(self, now: float):
        """Edge-triggered integrity callouts, once a second."""
        s, b = self.state, self.buddy
        # Data safety first: are readings reaching the disk?
        r = self.readings
        behind = (not r.healthy) or len(r.q) > 2000
        b._edge("data.behind", self.steady("data", behind, now, 3.0),
                ("alert", f"Science data is NOT being saved ({len(r.q)} readings waiting). Check the log disk."),
                ("info", "Science data saving again; nothing was lost" if not r.dropped else
                 f"Science data saving again; {r.dropped} readings lost while the disk was gone"))
        names = b.names()
        if not self.bit.get("done"):
            return  # let the self-test report first
        if self.camera.enabled:
            c = self.camera.snapshot()
            nm = c["name"]
            b._edge("camera.stopped", c["stopped"], ("alert", f"{nm} stopped exposing"), ("info", f"{nm} exposing again"))
            st = c["status"]
            if c["status_ok"]:
                b._edge("camera.sd", st.get("sd_status") not in (None, "Ok"),
                        ("alert", f"{nm} SD card: {st.get('sd_status')}"), ("info", f"{nm} SD card OK"))
                free = st.get("sd_gb_free")
                b._edge("camera.sdlow", isinstance(free, (int, float)) and free < 5,
                        ("warn", f"{nm} SD card low: {free} GB free"), None)
                b._edge("camera.autocap", st.get("auto_cap_active") is False,
                        ("warn", f"{nm} auto-capture is off"), ("info", f"{nm} auto-capture on"))
            if c["status_url"]:
                b._edge("camera.unreachable", self.steady("camstat", not c["status_ok"] and bool(c["status_age_s"]), now, 15),
                        ("warn", f"{nm} status not reachable"), ("info", f"{nm} status reachable again"))
        for role in ("gnss", "laser", "imu"):
            if not b.configured.get(role) or self.sim_mode:
                continue
            m = self.monitors[role]
            snap = m.snapshot(now)
            if m.expected_hz and snap["age_ms"] is not None and snap["age_ms"] < 2000:
                b._edge(f"{role}.slow", snap["hz"] < 0.5 * m.expected_hz,
                        ("warn", f"{names[role]} data rate low: {snap['hz']:g} Hz (expected {m.expected_hz:g})"),
                        ("info", f"{names[role]} data rate normal"))
            b._edge(f"{role}.errors", snap["err_pct"] > 5.0,
                    ("warn", f"{names[role]}: {snap['err_pct']:.0f}% bad frames. Check baud and wiring."),
                    ("info", f"{names[role]} frames clean again"))
        if s.laser_ok and s.imu_ok:
            b._edge("agl.tilt", self.steady("tilt", s.agl_quality == "tilt", now),
                    ("warn", f"AGL less reliable: {s.off_nadir_deg:.0f}° off vertical"), ("info", "AGL reliable again"))
            b._edge("agl.range", self.steady("range", s.agl_quality == "range", now),
                    ("warn", "Laser near its maximum range"), ("info", "Laser back within range"))
        if s.gnss_ok and s.gps_fix:
            b._edge("gnss.hdop", self.steady("hdop", s.hdop > 4.0, now, 5.0), ("warn", f"Poor GNSS geometry: HDOP {s.hdop:.1f}"),
                    ("info", "GNSS geometry good again"))
            if s.imu_ok:
                self.heading_check.add(now, s.yaw_deg, s.track_deg, s.gs_ms, s.roll_deg)
                d = self.heading_check.diff(now)
                b._edge("heading.track", d is not None and abs(d) > self.heading_check.max_diff,
                        ("warn", f"Heading differs from GNSS track by {abs(d or 0):.0f}°: wind crab or heading error"),
                        ("info", "Heading agrees with GNSS track"))

    def report_html(self) -> str:
        """A self-contained readiness report: what was checked, what was found, what to do."""
        import html as H
        s, r = self.state, self.ready
        color = {"green": "#0a7d3b", "amber": "#a86400", "red": "#b3261e"}[r["level"]]
        rows = lambda items: "".join(
            f"<tr><td class='{H.escape(i['status'])}'>{H.escape(i['status'].upper())}</td><td>{H.escape(i['item'])}</td>"
            f"<td>{H.escape(str(i['detail']))}</td></tr>" for i in items)
        finds = "".join(
            f"<div class='f {f['severity']}'><b>{H.escape(f['title'])}</b>"
            + (f"<div class='ev'>{H.escape(f['evidence'])}</div>" if f["evidence"] else "")
            + "<ol>" + "".join(f"<li>{H.escape(x)}</li>" for x in f["steps"]) + "</ol></div>" for f in self.findings)
        dev = "".join(f"<tr><td>{H.escape(k)}</td><td>{H.escape(str((self.cfg.get(sec) or {}).get('name', '-')))}</td>"
                      f"<td>{H.escape(str((self.cfg.get(sec) or {}).get('port', '-')))}</td>"
                      f"<td>{H.escape(str((self.cfg.get(sec) or {}).get('baud', '-')))}</td></tr>"
                      for k, sec in (("GNSS", "gnss"), ("Laser", "laser_altimeter"), ("Attitude", "imu_ahrs")))
        cal = self.cal.summary()
        data = self.data_status()
        return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AeroLab-Pi readiness, flight {s.flight_id}</title><style>
body{{font:15px/1.45 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#111;background:#fff}}
h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd}}
.v{{background:{color};color:#fff;padding:12px 16px;border-radius:8px;font-weight:700;font-size:18px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #eee;padding:6px;text-align:left;vertical-align:top}}
.ok{{color:#0a7d3b;font-weight:700}}.warn{{color:#a86400;font-weight:700}}.fail{{color:#b3261e;font-weight:700}}
.f{{border-left:5px solid #999;padding:6px 12px;margin:10px 0;background:#fafafa}}.f.blocker{{border-color:#b3261e}}
.f.warn{{border-color:#a86400}}.ev{{color:#555;font-size:13px}}code{{background:#f2f2f2;padding:1px 4px}}
</style></head><body>
<h1>AeroLab-Pi readiness · flight {s.flight_id}</h1>
<div class="v">{H.escape(r['text'])}</div>
<p>{H.escape(time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()))} · profile {H.escape(str(self.cfg.get('profile', '-')))}
 · software {H.escape(diagnose.git_version(APP_ROOT))}{' · <b>SIMULATION</b>' if s.sim_mode else ''}</p>
<h2>What to do</h2>{finds or '<p>Nothing to fix.</p>'}
<h2>Self-test at boot</h2><table>{rows(self.bit.get('items', []))}</table>
<h2>Devices</h2><table><tr><th>Role</th><th>Device</th><th>Port</th><th>Baud</th></tr>{dev}</table>
<h2>Calibration</h2><p>Laser offset {cal.get('laser_offset_m')} m · IMU bias {cal.get('imu_roll_bias_deg')}° / {cal.get('imu_pitch_bias_deg')}°
 · boresight {cal.get('boresight_roll_deg')}° / {cal.get('boresight_pitch_deg')}° · updated {H.escape(cal.get('updated') or 'never')}</p>
<h2>Data</h2><p>{data['readings_written']} readings written, {data['queued']} waiting, {data['dropped']} lost ·
 log dir <code>{H.escape(str(self.log_dir))}</code> · mirrors {H.escape(', '.join(data['mirrors']) or 'none')}</p>
</body></html>"""

    def write_report(self):
        try:
            (self.log_dir / f"flight_{self.state.flight_id}_readiness.html").write_text(self.report_html())
        except OSError as exc:
            self._data_error(f"readiness report: {exc}")

    def health(self) -> dict:
        now = time.time()
        return {role: m.snapshot(now) for role, m in self.monitors.items()}

    async def buddy_loop(self):
        n = 0
        warmed = False
        while True:
            self.sup.beat("buddy")
            now = time.time()
            if n % 10 == 0:
                await self.buddy.refresh_slow(self.log_dir)
            self.refresh_health(now)
            self.buddy.evaluate(self.state, self.log_dir, self.fallback_logging)
            self.integrity_tick(now)
            if self.bit.get("done"):
                self.findings = diagnose.findings(self, self.catalog)
                self.ready = diagnose.readiness(self.findings)
            if self.capture and not self.capture.done:
                self.capture.add(trigger_count=self.state.trigger_count)
                if self.capture.progress >= 1.0:
                    res = self.capture.finish(self.cal, bool(self.cfg["payload_sync"].get("enabled")),
                                              self.buddy.configured)
                    self.buddy.emit("info" if res["status"] == "ok" else "warn",
                                    f"Ground calibration finished: {res['status'].upper()}")
            if n % 30 == 0:  # re-check the local model every 30 s
                self.ai_status = await self.buddy.ai_status()
                if self.ai_status.get("available") and not warmed:
                    warmed = True
                    asyncio.create_task(self.buddy.warm_model())
            n += 1
            await asyncio.sleep(1.0)

    def payload(self) -> str:
        d = asdict(self.state)
        d["_events"] = self.buddy.recent(20)
        d["_ai"] = self.ai_status
        d["_configured"] = self.buddy.configured
        d["_names"] = self.buddy.names()
        d["_bit"] = self.bit
        d["_health"] = self.health()
        d["_cal"] = self.cal_status()
        d["_data"] = self.data_status()
        d["_ready"] = self.ready
        d["_camera"] = self.camera.snapshot()
        d["_survey"] = self.survey.status()
        d["_shutdown"] = self.shutdown_state
        return json.dumps(d)

    def data_status(self) -> dict:
        r = self.readings
        return {"readings_written": r.written, "queued": len(r.q), "dropped": r.dropped, "db_ok": r.healthy,
                "raw": {k: {"bytes": w.bytes, "frames": w.frames, "errors": w.errors, "dirs": [str(d) for d in w.dirs]}
                        for k, w in self.raw.items()},
                "mirrors": [str(d) for d in self.mirror_dirs], "tasks": self.sup.status(),
                "pps": self.pps.status()}

    def cal_status(self) -> dict:
        out = {"calibration": self.cal.summary()}
        if self.capture:
            out["capture"] = {"progress": round(self.capture.progress, 3), "done": self.capture.done,
                              "result": self.capture.result}
        return out

    async def broadcast_loop(self):
        interval = 1.0 / float(self.cfg["server"]["hud_hz"])
        while True:
            self.sup.beat("broadcast")
            if self.clients:
                self.refresh_health(time.time())
                payload = self.payload()
                dead = set()
                for ws in list(self.clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.add(ws)
                self.clients -= dead
            await asyncio.sleep(interval)

    def start(self):
        """Every loop runs under the supervisor: a crash restarts that loop, not the flight."""
        sup = self.sup
        if self.camera.enabled and not self.sim_mode:
            self.camera.start_gpio()
        self.tasks = []
        if self.sim_mode:
            self.tasks.append(sup.run("simulator", self.sim_loop))
        else:
            for sec in ("gnss", "laser_altimeter", "imu_ahrs"):
                if section_enabled(self.cfg, sec):
                    self.tasks.append(sup.run(f"{sec} reader", lambda sec=sec: self.sensor_loop(sec)))
        self.tasks += [
            sup.run("payload trigger", self.payload_sync_loop),
            sup.run("logger", self.logger_loop, stall_s=20),
            sup.run("reading log", self.readings.run),
            sup.run("clock", self.clock_loop, stall_s=20),
            *( [sup.run("pps", self.pps.run)] if self.pps.enabled else [] ),
            *( [sup.run("camera", self.camera.sim_loop)] if self.camera.enabled and self.sim_mode else [] ),
            *( [sup.run("camera status", self.camera.status_loop)]
               if self.camera.enabled and self.camera.status_url and not self.sim_mode else [] ),
            sup.run("buddy", self.buddy_loop, stall_s=60),
            sup.run("broadcast", self.broadcast_loop, stall_s=30),
            asyncio.create_task(self.run_bit()),
            asyncio.create_task(watchdog(sup)),
        ]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(BaseException):
                await t
        for w in self.raw.values():
            w.close()
        self.camera.stop()
        # final flush of queued readings
        try:
            conn = sqlite3.connect(self.db_path)
            conn.executescript(rawlog.READINGS_SCHEMA)
            while self.readings.q:
                self.readings._flush(conn)
            conn.close()
        except sqlite3.Error:
            pass


def create_app(daemon: AeroLabDaemon) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        daemon.start()
        mode = "SIMULATION" if daemon.sim_mode else "LIVE SENSORS"
        print(f"AeroLab-Pi running ({mode}); logs -> {daemon.log_dir}", flush=True)
        yield
        await daemon.stop()

    app = FastAPI(title="AeroLab-Pi", lifespan=lifespan)

    @app.get("/")
    async def get_hud():
        return FileResponse(HUD_PATH)

    @app.get("/api/state")
    async def get_state():
        daemon.refresh_health(time.time())
        return JSONResponse(json.loads(daemon.payload()))

    @app.get("/api/bit")
    async def get_bit():
        return JSONResponse(daemon.bit)

    @app.post("/api/bit/run")
    async def run_bit():
        return JSONResponse(await daemon.run_bit(delay=float(os.environ.get("AEROLAB_BIT_DELAY", 3.0))))

    @app.get("/api/survey")
    async def survey_status():
        return JSONResponse(daemon.survey.status())

    @app.post("/api/survey/readiness")
    async def survey_readiness():
        return JSONResponse(daemon.survey.readiness())

    @app.post("/api/survey/start")
    async def survey_start(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return JSONResponse(await daemon.survey.start(str(body.get("name", ""))[:60], bool(body.get("force"))))

    @app.post("/api/survey/stop")
    async def survey_stop():
        return JSONResponse(await daemon.survey.stop(), headers={"cache-control": "no-store"})

    @app.post("/api/shutdown")
    async def shutdown(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body.get("confirm") != "SHUTDOWN":
            return JSONResponse({"error": "send {\"confirm\": \"SHUTDOWN\"}"}, status_code=400)
        return JSONResponse(await daemon.shutdown(dry_run=bool(body.get("dry_run"))))

    @app.get("/api/bench/laser")
    async def bench_laser(seconds: float = 10.0, distance_m: float | None = None):
        """Collect `seconds` of laser readings now; compare with a tape-measured distance if given."""
        import statistics as st
        t0 = time.monotonic()
        await asyncio.sleep(min(max(seconds, 1.0), 120.0))
        rows = [r for t, r in list(daemon.laser_recent) if t >= t0]
        vals = [r for r in rows if r is not None]
        out = {"seconds": seconds, "readings": len(rows), "valid": len(vals), "no_return": len(rows) - len(vals),
               "rate_hz": round(len(rows) / seconds, 2)}
        if vals:
            out.update(mean_m=round(st.fmean(vals), 4), median_m=round(st.median(vals), 4),
                       std_m=round(st.pstdev(vals), 4), min_m=min(vals), max_m=max(vals))
            if distance_m is not None:
                out["distance_m"] = distance_m
                out["bias_m"] = round(st.median(vals) - distance_m, 4)
                out["verdict"] = ("ok" if abs(out["bias_m"]) <= 0.15 and out["std_m"] <= 0.10 else "check")
        return JSONResponse(out)

    @app.post("/api/bench/camera")
    async def bench_camera(request: Request):
        """Camera wiring test: ask the camera for one capture (HTTP) and time the exposure edge on the Pi GPIO.
        Without Ethernet, press the camera's capture button during the 10 s listening window instead."""
        cam = daemon.camera
        if not cam.enabled:
            return JSONResponse({"error": "no camera configured"}, status_code=400)
        n0, t0 = cam.count, time.monotonic()
        fired = None
        if cam.status_url and not daemon.sim_mode:
            base = cam.status_url.rsplit("/status", 1)[0]
            try:
                import httpx
                async with httpx.AsyncClient(timeout=3.0) as client:
                    r = await client.get(f"{base}/capture")
                    fired = {"http_status": r.status_code}
            except Exception as exc:
                fired = {"error": str(exc)}
        listen = 10.0 if fired is None else 3.0
        first = None
        while time.monotonic() - t0 < listen:
            if cam.count > n0 and first is None:
                first = time.monotonic()
            await asyncio.sleep(0.005)
        edges = cam.count - n0
        out = {"edges_seen": edges, "listened_s": listen, "capture_request": fired, "pin": cam.pin}
        if first is not None and fired is not None:
            out["request_to_edge_ms"] = round((first - t0) * 1000, 1)
        out["verdict"] = ("ok: the exposure signal reaches the Pi" if edges else
                          "no edge: check the Top-of-Frame pin mode, the wire to GPIO "
                          f"{cam.pin}, and the shared ground")
        return JSONResponse(out)

    @app.get("/api/diagnose")
    async def get_diagnose():
        daemon.findings = diagnose.findings(daemon, daemon.catalog)
        daemon.ready = diagnose.readiness(daemon.findings)
        return JSONResponse({"readiness": daemon.ready, "findings": daemon.findings, "data": daemon.data_status()})

    @app.get("/report")
    async def get_report():
        from fastapi.responses import HTMLResponse
        daemon.findings = diagnose.findings(daemon, daemon.catalog)
        daemon.ready = diagnose.readiness(daemon.findings)
        return HTMLResponse(daemon.report_html())

    @app.get("/api/health")
    async def get_health():
        return JSONResponse(daemon.health())

    @app.post("/api/flight/new")
    async def flight_new():
        fid = daemon.new_flight("new flight")
        daemon.buddy.emit("info", f"New flight {fid} started")
        asyncio.create_task(daemon.run_bit(delay=3.0))
        return JSONResponse({"flight_id": fid})

    @app.get("/api/cal")
    async def cal_get():
        return JSONResponse(daemon.cal_status())

    @app.post("/api/cal/ground")
    async def cal_ground(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        fe = body.get("field_elev_m")
        daemon.capture = calib.GroundCapture(
            seconds=float(body.get("seconds", 30)), ref_roll=float(body.get("ref_roll", 0) or 0),
            ref_pitch=float(body.get("ref_pitch", 0) or 0),
            field_elev_m=None if fe in (None, "") else float(fe),
            antenna_height_m=float(body.get("antenna_height_m", 1.5) or 1.5))
        daemon.buddy.emit("info", "Ground calibration started: keep the aircraft still")
        return JSONResponse({"started": True, "seconds": daemon.capture.seconds})

    @app.post("/api/cal/apply")
    async def cal_apply():
        cap = daemon.capture
        if not cap or not cap.done:
            return JSONResponse({"error": "no finished ground calibration"}, status_code=400)
        if cap.result["status"] == "fail":
            return JSONResponse({"error": "calibration failed; fix the failed items and run it again"},
                                status_code=400)
        calib.apply_proposed(daemon.cal, cap.result["proposed"], daemon.cal_path)
        daemon.state.calibrated = True
        daemon.laser_gate.reset()
        daemon.buddy.emit("info", "Calibration saved and applied")
        return JSONResponse(daemon.cal_status())

    @app.post("/api/cal/reset")
    async def cal_reset():
        daemon.cal = calib.Calibration()
        with contextlib.suppress(OSError):
            daemon.cal_path.unlink()
        daemon.state.calibrated = False
        daemon.buddy.emit("warn", "Calibration cleared")
        return JSONResponse(daemon.cal_status())

    @app.get("/api/buddy/check")
    async def buddy_check():
        daemon.refresh_health(time.time())
        return JSONResponse(daemon.buddy.ground_check(daemon.state, daemon.cfg, daemon.log_dir,
                                                      daemon.fallback_logging))

    @app.get("/api/buddy/status")
    async def buddy_status():
        daemon.ai_status = await daemon.buddy.ai_status()
        return JSONResponse(daemon.ai_status)

    @app.post("/api/buddy/ask")
    async def buddy_ask(request: Request):
        body = await request.json()
        question = str(body.get("question", "")).strip()[:500]
        if not question:
            return JSONResponse({"error": "empty question"}, status_code=400)
        daemon.refresh_health(time.time())
        stats = buddy_mod.flight_stats(daemon.db_path, daemon.session_start)
        ctx = daemon.buddy.context(daemon.state, daemon.cfg, stats)
        return StreamingResponse(daemon.buddy.ask_stream(question, ctx), media_type="text/plain; charset=utf-8")

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        daemon.clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            daemon.clients.discard(ws)

    return app


def main():
    local = APP_ROOT / "config" / "sensors.local.yaml"
    default_cfg = local if local.exists() else APP_ROOT / "config" / "sensors.yaml"
    parser = argparse.ArgumentParser(description="AeroLab-Pi flight telemetry daemon")
    parser.add_argument("--config", default=str(default_cfg),
                        help="defaults to config/sensors.local.yaml (written by ./configure.sh) if present")
    parser.add_argument("--sim", action="store_true", help="run with simulated flight sensors")
    parser.add_argument("--port", type=int, help="override server.port from the config")
    parser.add_argument("--calibration", help="calibration file (default config/calibration.local.json)")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        cfg = yaml.safe_load(f)
    daemon = AeroLabDaemon(cfg, sim_mode=args.sim or cfg.get("mode") == "sim",
                           cal_path=Path(args.calibration) if args.calibration else None)
    app = create_app(daemon)
    uvicorn.run(app, host=daemon.cfg["server"]["host"],
                port=args.port or int(daemon.cfg["server"]["port"]))


if __name__ == "__main__":
    main()
