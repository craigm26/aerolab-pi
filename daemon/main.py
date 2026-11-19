"""AeroLab-Pi flight telemetry daemon and cockpit HUD server.

Run on a laptop with simulated sensors:
    python daemon/main.py --sim

Run on the Pi with real sensors (install.sh sets this up as a service):
    python daemon/main.py --config config/sensors.yaml
"""

import argparse
import asyncio
import contextlib
import csv
import json
import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pynmea2
import serial_asyncio
import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

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


@dataclass
class TelemetryState:
    utc_iso: str = ""
    unix_ts: float = 0.0
    # GNSS
    lat: float = 0.0
    lon: float = 0.0
    alt_msl_m: float = 0.0
    gps_fix: int = 0
    sats: int = 0
    gnss_ok: bool = False
    # Laser altimeter and fused AGL
    laser_raw_m: float = 0.0
    agl_m: float = 0.0
    agl_ft: float = 0.0
    laser_ok: bool = False
    # Attitude (AHRS/IMU)
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
    parts = line.strip().split()
    if not parts:
        return None
    try:
        val = float(parts[0].rstrip("m"))
    except ValueError:
        return None
    if not math.isfinite(val) or val < 0:
        return None
    return val / 100.0 if unit == "cm" else val


def parse_imu_line(line: str) -> tuple[float, float, float] | None:
    """roll,pitch,yaw CSV in degrees. None for anything else."""
    parts = line.strip().split(",")
    if len(parts) < 3:
        return None
    try:
        roll, pitch, yaw = (float(p) for p in parts[:3])
    except ValueError:
        return None
    if not all(math.isfinite(v) for v in (roll, pitch, yaw)):
        return None
    return roll, pitch, yaw


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

        log_cfg = cfg["logging"]
        self.log_dir = resolve_log_dir(log_cfg["primary_dir"], log_cfg["fallback_dir"], sim_mode)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state.log_dir = str(self.log_dir)
        self.db_path = self.log_dir / "aerolab_flight.db"
        self._init_db()

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
        conn.commit()
        conn.close()
        self.session_start = time.time()

    def update_agl(self):
        offset = float(self.cfg["laser_altimeter"]["mounting_offset_m"])
        agl = compute_agl(self.state.laser_raw_m, self.state.roll_deg,
                          self.state.pitch_deg, offset)
        self.state.agl_m = round(agl, 2)
        self.state.agl_ft = round(agl * M_TO_FT, 1)

    # -- sensor loops -------------------------------------------------------

    async def sim_loop(self):
        """Plausible flight telemetry for bench testing without hardware."""
        t = 0.0
        while True:
            now = time.time()
            self.state.lat = round(38.5816 + t * 0.0002, 7)
            self.state.lon = round(-121.4944 + t * 0.0001, 7)
            self.state.alt_msl_m = round(450.0 + 15.0 * math.sin(t * 0.1), 1)
            self.state.gps_fix = 1
            self.state.sats = 11
            self.state.roll_deg = round(12.0 * math.sin(t * 0.25), 1)
            self.state.pitch_deg = round(3.5 * math.cos(t * 0.15), 1)
            self.state.yaw_deg = round((90.0 + t * 1.5) % 360.0, 1)
            self.state.laser_raw_m = round(60.0 + 25.0 * math.sin(t * 0.08), 2)
            self.update_agl()
            for key in self.last_seen:
                self.last_seen[key] = now
            t += 0.1
            await asyncio.sleep(0.1)

    async def _serial_lines(self, name: str, port: str, baud: int):
        """Yield decoded lines forever, reopening the port if it drops."""
        while True:
            writer = None
            try:
                reader, writer = await serial_asyncio.open_serial_connection(url=port, baudrate=baud)
                print(f"[{name}] opened {port} @ {baud}", flush=True)
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionError("port closed")
                    yield raw.decode("ascii", errors="ignore")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[{name}] {port}: {exc}; retrying in 2 s", flush=True)
                await asyncio.sleep(2.0)
            finally:
                if writer is not None:
                    writer.close()

    async def gnss_loop(self):
        c = self.cfg["gnss"]
        async for text in self._serial_lines("gnss", c["port"], int(c["baud"])):
            text = text.strip()
            if not text.startswith("$"):
                continue
            try:
                msg = pynmea2.parse(text)
            except pynmea2.ParseError:
                continue
            if msg.sentence_type != "GGA":
                continue
            try:
                fix = int(msg.gps_qual or 0)
                self.state.gps_fix = fix
                self.state.sats = int(msg.num_sats or 0)
                if fix > 0:
                    self.state.lat = round(msg.latitude, 7)
                    self.state.lon = round(msg.longitude, 7)
                    self.state.alt_msl_m = float(msg.altitude or 0.0)
                self.last_seen["gnss"] = time.time()
            except (ValueError, TypeError):
                continue

    async def laser_loop(self):
        c = self.cfg["laser_altimeter"]
        unit = c.get("unit", "m")
        async for text in self._serial_lines("laser", c["port"], int(c["baud"])):
            val = parse_laser_line(text, unit)
            if val is None:
                continue
            self.state.laser_raw_m = round(val, 3)
            self.update_agl()
            self.last_seen["laser"] = time.time()

    async def imu_loop(self):
        c = self.cfg["imu_ahrs"]
        async for text in self._serial_lines("imu", c["port"], int(c["baud"])):
            parsed = parse_imu_line(text)
            if parsed is None:
                continue
            roll, pitch, yaw = parsed
            self.state.roll_deg = round(roll, 2)
            self.state.pitch_deg = round(pitch, 2)
            self.state.yaw_deg = round(yaw % 360.0, 2)
            self.update_agl()
            self.last_seen["imu"] = time.time()

    # -- payload trigger ----------------------------------------------------

    def _open_trigger_pin(self, pin: int):
        if self.sim_mode or not HAS_GPIO:
            return None
        try:
            return DigitalOutputDevice(pin, initial_value=False)
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
        self.trigger_pin = self._open_trigger_pin(int(c["gpio_pin"]))
        self.state.gpio_live = self.trigger_pin is not None

        conn = self._connect_db()
        next_fire = time.monotonic()
        try:
            while True:
                # Stamp at the rising edge, the moment the payload is told to capture.
                ts, iso = now_utc()
                if self.trigger_pin is not None:
                    self.trigger_pin.on()
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
            conn.close()

    # -- logging and HUD ----------------------------------------------------

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

    async def broadcast_loop(self):
        interval = 1.0 / float(self.cfg["server"]["hud_hz"])
        while True:
            if self.clients:
                self.refresh_health(time.time())
                payload = json.dumps(asdict(self.state))
                dead = set()
                for ws in list(self.clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.add(ws)
                self.clients -= dead
            await asyncio.sleep(interval)

    def start(self):
        loops = [self.sim_loop()] if self.sim_mode else [
            self.gnss_loop(), self.laser_loop(), self.imu_loop()]
        loops += [self.payload_sync_loop(), self.logger_loop(), self.broadcast_loop()]
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
        return JSONResponse(asdict(daemon.state))

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
