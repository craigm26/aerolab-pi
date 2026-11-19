"""Integrity monitoring: is each number on the display trustworthy right now?

Three layers:
  * Boot self-test (BIT): configuration, storage, clock and every sensor's
    first seconds of data, checked once after start.
  * Per-sensor monitors: data rate against the device's expected rate, bad
    frame percentage (checksum failures), age of the last sample.
  * Measurement checks, every second in flight: laser spike filter and AGL
    quality (tilt too steep, range near the laser's limit), GNSS HDOP and
    position jumps, heading against GNSS track in straight flight.

All of this feeds Buddy's callouts and the HUD; nothing here changes what is
logged except that laser spikes are left out of AGL (every raw range is
still logged).
"""

from __future__ import annotations

import math
import os
import statistics
import time
from collections import deque
from pathlib import Path

EARTH_R = 6371008.8


def off_nadir_deg(roll_deg: float, pitch_deg: float) -> float:
    """Angle between the aircraft's down axis and true vertical."""
    c = math.cos(math.radians(roll_deg)) * math.cos(math.radians(pitch_deg))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def project(lat: float, lon: float, track_deg: float, dist_m: float) -> tuple[float, float]:
    """Move a position dist_m along track_deg (flat-earth, fine for a few hundred metres)."""
    t = math.radians(track_deg)
    dlat = dist_m * math.cos(t) / EARTH_R
    dlon = dist_m * math.sin(t) / (EARTH_R * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


class SensorMonitor:
    """Rate, error percentage and sample age for one sensor."""

    def __init__(self, expected_hz: float | None = None, window_s: float = 5.0):
        self.expected_hz = expected_hz
        self.window_s = window_s
        self.times: deque = deque()
        self.last = 0.0
        self._counts: deque = deque()      # (time, good, bad) snapshots of the driver counters

    def on_reading(self, now: float):
        self.last = now
        self.times.append(now)
        self._trim(now)

    def on_counters(self, now: float, good: int, bad: int):
        self._counts.append((now, good, bad))
        while self._counts and now - self._counts[0][0] > self.window_s:
            self._counts.popleft()

    def _trim(self, now: float):
        while self.times and now - self.times[0] > self.window_s:
            self.times.popleft()

    def rate_hz(self, now: float) -> float:
        """Readings per second over the window; 0 if the sensor has gone quiet."""
        self._trim(now)
        if len(self.times) < 2 or now - self.times[-1] > 2.0:
            return 0.0
        span = self.times[-1] - self.times[0]
        return (len(self.times) - 1) / span if span > 0 else 0.0

    def error_pct(self) -> float:
        if len(self._counts) < 2:
            return 0.0
        _, g0, b0 = self._counts[0]
        _, g1, b1 = self._counts[-1]
        good, bad = g1 - g0, b1 - b0
        total = good + bad
        return 100.0 * bad / total if total > 0 else 0.0

    def age_s(self, now: float) -> float | None:
        return now - self.last if self.last else None

    def snapshot(self, now: float) -> dict:
        age = self.age_s(now)
        return {"hz": round(self.rate_hz(now), 1), "expected_hz": self.expected_hz,
                "err_pct": round(self.error_pct(), 1), "age_ms": None if age is None else int(age * 1000)}


class LaserGate:
    """Rejects laser spikes without adding lag.

    Each new range is turned into a vertical height with the attitude at that
    moment, then compared with the last accepted height. Vertical height changes
    slowly even while the slant range swings in a bank, so a gate there needs no
    smoothing: a sample more than max(abs_m, rel x height) away is a spike (bird,
    cloud, multipath) and is dropped. Three rejections in a row are accepted as a
    real step (cliff edge, crevasse, building), and the gate re-centres.
    """

    def __init__(self, abs_m: float = 5.0, rel: float = 0.05, max_rejects: int = 3):
        self.abs_m, self.rel, self.max_rejects = abs_m, rel, max_rejects
        self.last: float | None = None
        self.rejects = 0
        self.spikes = 0

    def accept(self, height_m: float) -> bool:
        if self.last is None:
            self.last = height_m
            return True
        if abs(height_m - self.last) <= max(self.abs_m, self.rel * max(self.last, 0.0)):
            self.last, self.rejects = height_m, 0
            return True
        self.rejects += 1
        self.spikes += 1
        if self.rejects >= self.max_rejects:
            self.last, self.rejects = height_m, 0
            self.spikes -= self.max_rejects     # it was a real step, not spikes
            return True
        return False

    def reset(self):
        self.last, self.rejects = None, 0


def agl_quality(roll: float, pitch: float, range_m: float, max_range_m: float, max_tilt_deg: float) -> str:
    """'ok', 'tilt' (too far off vertical to trust the correction), or 'range' (near the laser's limit)."""
    if off_nadir_deg(roll, pitch) > max_tilt_deg:
        return "tilt"
    if max_range_m and range_m > 0.95 * max_range_m:
        return "range"
    return "ok"


class GnssIntegrity:
    """Position jumps (implied speed beyond what the aircraft can do)."""

    def __init__(self, max_speed_ms: float = 120.0):
        self.max_speed = max_speed_ms
        self.prev: tuple | None = None
        self.jumps = 0
        self.last_jump_m = 0.0

    def check(self, now: float, lat: float, lon: float) -> bool:
        """True when this fix is a jump from the previous one."""
        jump = False
        if self.prev:
            t0, la0, lo0 = self.prev
            dt = max(now - t0, 0.05)
            d = haversine_m(la0, lo0, lat, lon)
            if d > self.max_speed * dt + 30.0:
                jump = True
                self.jumps += 1
                self.last_jump_m = d
        self.prev = (now, lat, lon)
        return jump


class HeadingCheck:
    """Heading against GNSS track, only when the comparison means something.

    Straight and level (|roll| < 5 deg), moving (> 25 m/s), averaged over 15 s.
    A steady difference is crab angle (wind) or IMU heading error; more than
    max_diff_deg is flagged.
    """

    def __init__(self, max_diff_deg: float = 20.0, window_s: float = 15.0):
        self.max_diff = max_diff_deg
        self.window = window_s
        self.samples: deque = deque()

    def add(self, now: float, yaw: float, track: float, gs: float, roll: float):
        if gs < 25.0 or abs(roll) > 5.0:
            self.samples.clear()
            return
        self.samples.append((now, wrap180(yaw - track)))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    def diff(self, now: float) -> float | None:
        if not self.samples or now - self.samples[0][0] < self.window * 0.8:
            return None
        return sum(d for _, d in self.samples) / len(self.samples)


# ---------------------------------------------------------------------------
# Boot self-test
# ---------------------------------------------------------------------------

def storage_test(log_dir: Path) -> dict:
    """Write, fsync, read back and delete a file: proves the log disk really takes data."""
    path = Path(log_dir) / ".aerolab_bit"
    payload = os.urandom(4096)
    t0 = time.perf_counter()
    try:
        with open(path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        ok = path.read_bytes() == payload
        path.unlink(missing_ok=True)
    except OSError as exc:
        return {"item": "Log storage", "status": "fail", "detail": f"write test failed: {exc}"}
    ms = (time.perf_counter() - t0) * 1000
    if not ok:
        return {"item": "Log storage", "status": "fail", "detail": "read-back did not match"}
    return {"item": "Log storage", "status": "ok" if ms < 200 else "warn",
            "detail": f"write+fsync+read {ms:.0f} ms" + ("" if ms < 200 else " (slow disk)")}


def config_checks(cfg: dict, sim: bool, enabled: dict, known_drivers: set) -> list[dict]:
    out = []
    ports = {}
    for sec, label in (("gnss", "GNSS"), ("laser_altimeter", "Laser"), ("imu_ahrs", "Attitude")):
        c = cfg.get(sec) or {}
        if not enabled.get(sec):
            continue
        if c.get("driver") and c["driver"] not in known_drivers:
            out.append({"item": f"{label} config", "status": "fail", "detail": f"unknown driver {c['driver']}"})
        port = c.get("port")
        if not sim and port and not os.path.exists(port):
            out.append({"item": f"{label} port", "status": "fail", "detail": f"{port} does not exist (unplugged?)"})
        if port in ports and not sim:
            out.append({"item": f"{label} port", "status": "fail", "detail": f"{port} is also used by {ports[port]}"})
        ports[port] = label
    lc = cfg.get("laser_altimeter") or {}
    off = float(lc.get("mounting_offset_m", 0) or 0)
    if not 0 <= off <= 5:
        out.append({"item": "Laser offset", "status": "warn", "detail": f"mounting_offset_m {off} m looks wrong"})
    hud = cfg.get("hud") or {}
    rng = float(lc.get("max_range_m", 0) or 0)
    survey_m = float(hud.get("survey_agl_ft", 0) or 0) / 3.28084
    if rng and survey_m > rng:
        out.append({"item": "Laser range", "status": "warn",
                    "detail": f"survey height {survey_m:.0f} m is beyond the laser's {rng:.0f} m rating"})
    if not out:
        out.append({"item": "Configuration", "status": "ok", "detail": "drivers, ports and limits valid"})
    return out


def sensor_bit(label: str, mon: SensorMonitor, now: float, plausible: tuple[bool, str] | None) -> dict:
    snap = mon.snapshot(now)
    if snap["age_ms"] is None or snap["age_ms"] > 2000:
        return {"item": label, "status": "fail", "detail": "no data"}
    bits, status = [f"{snap['hz']:g} Hz"], "ok"
    exp = mon.expected_hz
    if exp:
        bits[0] += f" (expected {exp:g})"
        if snap["hz"] < 0.5 * exp:
            status = "warn"
    if snap["err_pct"] > 1.0:
        bits.append(f"{snap['err_pct']:.0f}% bad frames: check baud and wiring")
        status = "warn" if snap["err_pct"] < 10 else "fail"
    if plausible is not None:
        ok, why = plausible
        if not ok:
            bits.append(why)
            status = "fail"
    return {"item": label, "status": status, "detail": ", ".join(bits)}


def overall(items: list[dict]) -> str:
    st = {i["status"] for i in items}
    return "fail" if "fail" in st else ("warn" if "warn" in st else "ok")
