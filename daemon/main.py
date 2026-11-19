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
import drivers  # noqa: E402

# gpiozero works on every Pi model including the Pi 5 (RPi.GPIO does not).
# On a laptop it is missing or has no pin factory, and triggers are simulated.
try:
    from gpiozero import DigitalOutputDevice
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

APP_ROOT = Path(__file__).resolve().parent.parent
HUD_PATH = APP_ROOT / "hud" / "index.html"
STALE_AFTER_S = 2.0
M_TO_FT = 3.28084

# Default driver per sensor section when the config does not name one.
DEFAULT_DRIVER = {"gnss": "nmea", "laser_altimeter": "ascii_number", "imu_ahrs": "csv_rpy"}


@dataclass
class TelemetryState:
    utc_iso: str = ""
    unix_ts: float = 0.0
    # GNSS
    lat: float = 0.0
    lon: float = 0.0
    alt_msl_m: float = 0.0
    alt_ref: str = "MSL"          # "HAE" when the source reports height above the ellipsoid
    gps_fix: int = 0
    sats: int = 0                 # -1 when the source does not report a count
    gnss_ok: bool = False
    gnss_time_offset_s: float | None = None
    # Laser altimeter and fused AGL
    laser_raw_m: float = 0.0
    agl_m: float = 0.0
    agl_ft: float = 0.0
    laser_ok: bool = False
    # Attitude (AHRS/IMU or GNSS/INS)
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


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class AeroLabDaemon:
    def __init__(self, cfg: dict, sim_mode: bool = False):
        self.cfg = cfg
        self.sim_mode = sim_mode
        self.state = TelemetryState(
            sim_mode=sim_mode,
            low_agl_warn_ft=float(cfg.get("hud", {}).get("low_agl_warn_ft", 200)),
        )
        self.last_seen = {"gnss": 0.0, "laser": 0.0, "imu": 0.0}
        self.clients: set[WebSocket] = set()
        self.tasks: list[asyncio.Task] = []
        self.trigger_pin = None
        self.max_range_m = float((cfg.get("laser_altimeter") or {}).get("max_range_m", 0) or 0)

        log_cfg = cfg["logging"]
        self.log_dir = resolve_log_dir(log_cfg["primary_dir"], log_cfg["fallback_dir"], sim_mode)
        self.fallback_logging = (not sim_mode) and self.log_dir != Path(log_cfg["primary_dir"])
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state.log_dir = str(self.log_dir)
        self.db_path = self.log_dir / "aerolab_flight.db"
        self._init_db()

        self.buddy = buddy_mod.Buddy(cfg, db_path=self.db_path)
        if sim_mode:
            self.buddy.configured = {"gnss": True, "laser": True, "imu": True}
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

    @classmethod
    def from_file(cls, config_path: str, sim_mode: bool = False) -> "AeroLabDaemon":
        with open(config_path, "r") as f:
            return cls(yaml.safe_load(f), sim_mode=sim_mode)

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
        conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "unix_ts REAL, level TEXT, text TEXT)")
        conn.commit()
        conn.close()
        self.session_start = time.time()

    def update_agl(self):
        offset = float(self.cfg["laser_altimeter"]["mounting_offset_m"])
        agl = compute_agl(self.state.laser_raw_m, self.state.roll_deg,
                          self.state.pitch_deg, offset)
        self.state.agl_m = round(agl, 2)
        self.state.agl_ft = round(agl * M_TO_FT, 1)

    def apply(self, r: dict):
        """Apply one driver reading to the state, whatever kinds it carries."""
        now = time.time()
        s = self.state
        if "fix" in r:
            s.gps_fix, s.sats = int(r["fix"]), int(r.get("sats", s.sats))
            if "gnss_tod" in r:
                s.gnss_time_offset_s = round(tod_offset(r["gnss_tod"], now), 2)
            self.last_seen["gnss"] = now
        if "lat" in r:
            s.lat, s.lon = round(r["lat"], 7), round(r["lon"], 7)
            if r.get("alt_msl") is not None:
                s.alt_msl_m, s.alt_ref = round(float(r["alt_msl"]), 2), "MSL"
            elif r.get("alt_hae") is not None:
                s.alt_msl_m, s.alt_ref = round(float(r["alt_hae"]), 2), "HAE"
            self.last_seen["gnss"] = now
        if "roll" in r and "pitch" in r:
            s.roll_deg, s.pitch_deg = round(r["roll"], 2), round(r["pitch"], 2)
            if "yaw" in r:
                s.yaw_deg = round(r["yaw"] % 360.0, 2)
            self.last_seen["imu"] = now
            self.update_agl()
        elif "yaw" in r:
            s.yaw_deg = round(r["yaw"] % 360.0, 2)
        if "range_m" in r:
            rng = float(r["range_m"])
            if self.max_range_m and rng > self.max_range_m * 1.05:
                return  # beyond the sensor's rated range: treat as no target
            s.laser_raw_m = round(rng, 3)
            self.last_seen["laser"] = now
            self.update_agl()

    # -- sensor loops -------------------------------------------------------

    async def sim_loop(self):
        """Plausible flight telemetry for bench testing without hardware.

        With sim.faults: true, the laser drops out for 4 s every 45 s (as real
        lasers do over open water or specular ice) so the callouts can be seen.
        """
        faults = bool((self.cfg.get("sim") or {}).get("faults", False))
        # Fly around the planned survey height (if set) so the display looks like a real survey.
        survey_m = float((self.cfg.get("hud") or {}).get("survey_agl_ft", 0) or 0) / M_TO_FT
        center = survey_m if survey_m > 0 else 60.0
        swing = 0.4 * center
        t = 0.0
        while True:
            now = time.time()
            s = self.state
            s.lat = round(38.5816 + t * 0.0002, 7)
            s.lon = round(-121.4944 + t * 0.0001, 7)

            s.gps_fix = 1
            s.sats = 11
            s.gnss_time_offset_s = 0.0
            s.roll_deg = round(12.0 * math.sin(t * 0.25), 1)
            s.pitch_deg = round(3.5 * math.cos(t * 0.15), 1)
            s.yaw_deg = round((90.0 + t * 1.5) % 360.0, 1)
            s.laser_raw_m = round(center + swing * math.sin(t * 0.08), 2)
            self.update_agl()
            # MSL = height above ground + rolling terrain about 120 m above sea level.
            s.alt_msl_m = round(s.agl_m + 120.0 + 30.0 * math.sin(t * 0.03), 1)
            self.last_seen["gnss"] = now
            self.last_seen["imu"] = now
            if not (faults and 41.0 <= (t % 45.0)):
                self.last_seen["laser"] = now
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
        label = c.get("name") or section
        init = drivers.SERIAL_SCRIPTS.get(c.get("init_script", ""), b"") + _unescape(c.get("init", ""))
        poll = _unescape(c.get("poll", ""))
        async for data in self._serial_chunks(label, c["port"], int(c["baud"]), init=init, poll=poll,
                                              poll_hz=float(c.get("poll_hz", 0) or 0)):
            for reading in drv.feed(data):
                self.apply(reading)

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
        """Pulse the camera/radar trigger and log where the aircraft was."""
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
                s = self.state
                snapshot = (self.session_start, s.trigger_count, ts, iso,
                            s.lat, s.lon, s.alt_msl_m, s.agl_m,
                            s.roll_deg, s.pitch_deg, s.yaw_deg)
                await asyncio.sleep(pulse_s)
                if self.trigger_pin is not None:
                    self.trigger_pin.off()

                conn.execute(
                    """
                    INSERT INTO payload_triggers
                    (session_start, trigger_id, unix_ts, utc_iso, lat, lon, alt_msl_m,
                     agl_m, roll_deg, pitch_deg, yaw_deg)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self.state.imu_ok = (now - self.last_seen["imu"]) < STALE_AFTER_S

    async def logger_loop(self):
        """Dual-write every sample: SQLite (WAL) plus fsynced 5-minute CSV chunks."""
        rotate_s = float(self.cfg["logging"]["rotate_minutes"]) * 60.0
        interval = 1.0 / float(self.cfg["logging"]["log_hz"])
        conn = self._connect_db()
        csv_file = None
        csv_writer = None
        chunk_start = 0.0
        fields = list(asdict(self.state).keys())

        try:
            while True:
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
                conn.execute(
                    """
                    INSERT INTO telemetry
                    (unix_ts, utc_iso, lat, lon, alt_msl_m, gps_fix, sats,
                     laser_raw_m, agl_m, agl_ft, roll_deg, pitch_deg, yaw_deg,
                     gnss_ok, laser_ok, imu_ok, trigger_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (s.unix_ts, s.utc_iso, s.lat, s.lon, s.alt_msl_m, s.gps_fix, s.sats,
                     s.laser_raw_m, s.agl_m, s.agl_ft, s.roll_deg, s.pitch_deg, s.yaw_deg,
                     int(s.gnss_ok), int(s.laser_ok), int(s.imu_ok), s.trigger_count),
                )
                conn.commit()
                self.state.logging_active = True
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Disk full or SSD pulled: tell the HUD instead of dying silently.
            self.state.logging_active = False
            print(f"[logger] stopped: {exc}", flush=True)
            raise
        finally:
            if csv_file:
                csv_file.close()
            conn.close()

    async def buddy_loop(self):
        n = 0
        warmed = False
        while True:
            if n % 10 == 0:
                await self.buddy.refresh_slow(self.log_dir)
            self.refresh_health(time.time())
            self.buddy.evaluate(self.state, self.log_dir, self.fallback_logging)
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
        return json.dumps(d)

    async def broadcast_loop(self):
        interval = 1.0 / float(self.cfg["server"]["hud_hz"])
        while True:
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
        if self.sim_mode:
            loops = [self.sim_loop()]
        else:
            loops = [self.sensor_loop(sec) for sec in ("gnss", "laser_altimeter", "imu_ahrs")
                     if section_enabled(self.cfg, sec)]
        loops += [self.payload_sync_loop(), self.logger_loop(), self.buddy_loop(), self.broadcast_loop()]
        self.tasks = [asyncio.create_task(c) for c in loops]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(BaseException):
                await t


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
    args = parser.parse_args()

    with open(args.config, "r") as f:
        cfg = yaml.safe_load(f)
    daemon = AeroLabDaemon(cfg, sim_mode=args.sim or cfg.get("mode") == "sim")
    app = create_app(daemon)
    uvicorn.run(app, host=daemon.cfg["server"]["host"],
                port=args.port or int(daemon.cfg["server"]["port"]))


if __name__ == "__main__":
    main()
