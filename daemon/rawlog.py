"""Never lose science data: raw byte capture and per-reading logs.

Two layers, both written as data arrives:

1. Raw capture (.aeroraw files, one per sensor, rotated hourly). Every byte read
   from a sensor's serial port, in frames:
       header line : b"AEROLABRAW1 " + JSON (sensor, port, baud, driver, flight) + b"\\n"
       frame       : <q utc_ns> <q mono_ns> <I length> <bytes>
   utc_ns is the system clock (CLOCK_REALTIME) and mono_ns the monotonic clock
   (CLOCK_MONOTONIC) at the moment the bytes were read. Nothing is parsed or
   dropped, so post-processing can re-decode everything, including messages
   AeroLab-Pi doesn't understand (raw GNSS observations, status lines).
   tools/raw_export.py turns a file back into the plain stream + a timestamp CSV.
   Files are fsynced every second; a power cut loses at most about a second.

2. Per-reading tables in the flight database (every laser range and every GNSS
   fix, not samples): laser_readings, gnss_fixes, pps_edges, clock_samples.
   Written by one task in batches, committed every 0.5 s.

Every write path survives errors: a failed write closes the file, reports it,
and reopens on the next write, falling back to the next directory in the list.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import struct
import time
from collections import deque
from pathlib import Path

MAGIC = b"AEROLABRAW1 "
FRAME = struct.Struct("<qqI")


def read_raw(path: Path):
    """Yield (header dict) then (utc_ns, mono_ns, bytes) frames. Stops cleanly at a torn last frame."""
    with open(path, "rb") as f:
        line = f.readline()
        if not line.startswith(MAGIC):
            raise ValueError(f"{path} is not an AeroLab raw file")
        yield json.loads(line[len(MAGIC):])
        while True:
            hdr = f.read(FRAME.size)
            if len(hdr) < FRAME.size:
                return
            utc, mono, n = FRAME.unpack(hdr)
            data = f.read(n)
            if len(data) < n:
                return          # power cut mid-frame: everything before it is intact
            yield utc, mono, data


class RawWriter:
    """Raw capture for one sensor, written to every directory in `dirs` (primary + mirrors)."""

    def __init__(self, dirs: list[Path], name: str, meta: dict, rotate_s: float = 3600.0,
                 on_error=None):
        self.dirs = [Path(d) for d in dirs]
        self.name = name
        self.meta = meta
        self.rotate_s = rotate_s
        self.on_error = on_error or (lambda msg: None)
        self.files: dict[Path, object] = {}
        self.opened = 0.0
        self.last_sync = 0.0
        self.bytes = 0
        self.frames = 0
        self.errors = 0

    def _open(self):
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        for d in self.dirs:
            if d in self.files:
                continue
            try:
                raw_dir = d / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                f = open(raw_dir / f"{self.name}_{stamp}.aeroraw", "ab", buffering=1 << 16)
                f.write(MAGIC + json.dumps({**self.meta, "sensor": self.name, "opened_utc": time.time()}).encode() + b"\n")
                self.files[d] = f
            except OSError as exc:
                self.errors += 1
                self.on_error(f"raw log for {self.name} in {d}: {exc}")
        self.opened = time.monotonic()

    def write(self, data: bytes, utc_ns: int, mono_ns: int):
        if not self.files or time.monotonic() - self.opened > self.rotate_s:
            self.close()
            self._open()
        frame = FRAME.pack(utc_ns, mono_ns, len(data)) + data
        for d, f in list(self.files.items()):
            try:
                f.write(frame)
            except OSError as exc:
                self.errors += 1
                self.on_error(f"raw log for {self.name} in {d}: {exc}")
                self._drop(d)
        self.bytes += len(data)
        self.frames += 1
        if time.monotonic() - self.last_sync >= 1.0:
            self.sync()

    def sync(self):
        self.last_sync = time.monotonic()
        for d, f in list(self.files.items()):
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError as exc:
                self.errors += 1
                self.on_error(f"raw log for {self.name} in {d}: {exc}")
                self._drop(d)

    def _drop(self, d):
        f = self.files.pop(d, None)
        if f:
            try:
                f.close()
            except OSError:
                pass

    def close(self):
        for d in list(self.files):
            try:
                self.files[d].flush()
                os.fsync(self.files[d].fileno())
            except OSError:
                pass
            self._drop(d)


READINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS laser_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER,
    utc_ns INTEGER, mono_ns INTEGER, range_m REAL, accepted INTEGER, agl_m REAL,
    roll_deg REAL, pitch_deg REAL);
CREATE TABLE IF NOT EXISTS gnss_fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER,
    utc_ns INTEGER, mono_ns INTEGER, gnss_tod REAL, lat REAL, lon REAL, alt_m REAL, alt_ref TEXT,
    fix INTEGER, sats INTEGER, hdop REAL, gs_ms REAL, track_deg REAL);
CREATE TABLE IF NOT EXISTS pps_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER,
    seq INTEGER, assert_utc_ns INTEGER, mono_ns INTEGER, utc_minus_mono_ns INTEGER);
CREATE TABLE IF NOT EXISTS clock_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER,
    utc_ns INTEGER, mono_ns INTEGER);
CREATE TABLE IF NOT EXISTS triggers_exact (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER, trigger_id INTEGER,
    utc_ns INTEGER, mono_ns INTEGER);
CREATE TABLE IF NOT EXISTS exposures (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER, seq INTEGER, utc_ns INTEGER, mono_ns INTEGER);
CREATE TABLE IF NOT EXISTS camera_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER, utc_ns INTEGER, status TEXT);
CREATE INDEX IF NOT EXISTS laser_readings_mono ON laser_readings (mono_ns);
CREATE INDEX IF NOT EXISTS gnss_fixes_mono ON gnss_fixes (mono_ns);
"""

INSERTS = {
    "laser": "INSERT INTO laser_readings (flight_id, utc_ns, mono_ns, range_m, accepted, agl_m, roll_deg, pitch_deg) "
             "VALUES (?,?,?,?,?,?,?,?)",
    "gnss": "INSERT INTO gnss_fixes (flight_id, utc_ns, mono_ns, gnss_tod, lat, lon, alt_m, alt_ref, fix, sats, hdop, "
            "gs_ms, track_deg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    "pps": "INSERT INTO pps_edges (flight_id, seq, assert_utc_ns, mono_ns, utc_minus_mono_ns) VALUES (?,?,?,?,?)",
    "clock": "INSERT INTO clock_samples (flight_id, utc_ns, mono_ns) VALUES (?,?,?)",
    "exposure": "INSERT INTO exposures (flight_id, seq, utc_ns, mono_ns) VALUES (?,?,?,?)",
    "camstatus": "INSERT INTO camera_status (flight_id, utc_ns, status) VALUES (?,?,?)",
    "trigger": "INSERT INTO triggers_exact (flight_id, trigger_id, utc_ns, mono_ns) VALUES (?,?,?,?)",
}


class ReadingLog:
    """Queue of per-reading rows, written in batches by one task."""

    def __init__(self, db_path_fn, on_error=None, max_queue: int = 200_000):
        self.db_path_fn = db_path_fn            # returns the current DB path (it can move to a fallback disk)
        self.on_error = on_error or (lambda msg: None)
        self.q: deque = deque()
        self.max_queue = max_queue
        self.dropped = 0
        self.written = 0
        self.last_commit = 0.0
        self.healthy = True

    def add(self, kind: str, row: tuple):
        if len(self.q) >= self.max_queue:
            self.q.popleft()                     # only if the disk has been gone for many minutes
            self.dropped += 1
        self.q.append((kind, row))

    def _flush(self, conn) -> int:
        n = 0
        batch: dict[str, list] = {}
        while self.q and n < 20000:
            kind, row = self.q.popleft()
            batch.setdefault(kind, []).append(row)
            n += 1
        try:
            for kind, rows in batch.items():
                conn.executemany(INSERTS[kind], rows)
            conn.commit()
        except sqlite3.Error:
            # put them back in front, in order, then let the caller reconnect
            for kind, rows in reversed(list(batch.items())):
                for row in reversed(rows):
                    self.q.appendleft((kind, row))
            raise
        self.written += n
        return n

    async def run(self, interval: float = 0.5):
        conn = None
        while True:
            try:
                if conn is None:
                    conn = sqlite3.connect(self.db_path_fn(), timeout=5)
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                    conn.executescript(READINGS_SCHEMA)
                while self.q:
                    self._flush(conn)
                self.healthy = True
                self.last_commit = time.time()
            except (sqlite3.Error, OSError) as exc:
                self.healthy = False
                self.on_error(f"reading log: {exc}")
                try:
                    if conn:
                        conn.close()
                except sqlite3.Error:
                    pass
                conn = None
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(interval)
