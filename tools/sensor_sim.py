"""Simulated sensors on virtual serial ports, for testing the real daemon end to end.

Each simulated device writes the same bytes the real one would (NMEA, VectorNav,
Benewake, BNO08x RVC, LightWare binary, Riegl, MAVLink, ...) into a pseudo-terminal.
The daemon opens the pty like any serial port, so drivers, timing, the spike
filter, calibration and the integrity monitors are all exercised.

The truth comes from a Trajectory, so every logged number can be scored.
Faults can be injected: corrupted frames, laser spikes, IMU bias, laser
boresight, wrong mounting offset.
"""

from __future__ import annotations

import math
import os
import pty
import random
import struct
import sys
import threading
import time
import tty
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import drivers  # noqa: E402
import integrity  # noqa: E402


@dataclass
class Installation:
    """The real (unknown to the daemon) errors of the simulated installation."""
    laser_offset_m: float = 0.62        # true laser height above wheels (config says 0.4)
    imu_roll_bias_deg: float = 0.8
    imu_pitch_bias_deg: float = -0.5
    boresight_roll_deg: float = 1.0     # laser beam tilted 1 deg right of the body axis
    boresight_pitch_deg: float = 0.0
    laser_noise_m: float = 0.03
    imu_noise_deg: float = 0.03
    gnss_noise_m: float = 0.4
    corrupt_frac: float = 0.02          # fraction of frames with a flipped byte
    spike_frac: float = 0.01            # fraction of laser readings replaced by garbage ranges


@dataclass
class Trajectory:
    """Parked for park_s seconds, then a survey leg with banks both ways over flat ground."""
    park_s: float = 14.0
    survey_agl_m: float = 600.0
    ground_elev_m: float = 120.0
    gs_ms: float = 55.0
    bank_deg: float = 20.0
    bank_period_s: float = 16.0
    lat0: float = 38.5816
    lon0: float = -121.4944
    heading0: float = 90.0
    _cache: dict = field(default_factory=dict)

    def at(self, t: float) -> dict:
        if t < self.park_s:
            return {"phase": "parked", "lat": self.lat0, "lon": self.lon0, "agl": 0.0,
                    "alt_msl": self.ground_elev_m + 1.5, "roll": 0.0, "pitch": 0.0, "yaw": self.heading0,
                    "gs": 0.0, "track": self.heading0}
        tf = t - self.park_s
        roll = self.bank_deg * math.sin(2 * math.pi * tf / self.bank_period_s)
        pitch = 2.0 + 1.5 * math.sin(2 * math.pi * tf / 23.0)
        # Coordinated turn: heading changes with bank.
        yaw = (self.heading0 + 4.0 * (1 - math.cos(2 * math.pi * tf / self.bank_period_s))) % 360.0
        agl = self.survey_agl_m * (1 + 0.03 * math.sin(2 * math.pi * tf / 40.0))   # climbs/descents ~500 ft/min
        # Integrate position along the (slowly varying) track.
        lat, lon = self.lat0, self.lon0
        step = 0.5
        k = int(tf / step)
        key = k
        if key in self._cache:
            lat, lon = self._cache[key]
        else:
            last = max((c for c in self._cache if c <= k), default=None)
            i0 = 0
            if last is not None:
                lat, lon = self._cache[last]
                i0 = last
            for i in range(i0, k):
                ti = i * step
                yi = (self.heading0 + 4.0 * (1 - math.cos(2 * math.pi * ti / self.bank_period_s))) % 360.0
                lat, lon = integrity.project(lat, lon, yi, self.gs_ms * step)
            self._cache[key] = (lat, lon)
        lat, lon = integrity.project(lat, lon, yaw, self.gs_ms * (tf - k * step))
        return {"phase": "flight", "lat": lat, "lon": lon, "agl": agl, "alt_msl": self.ground_elev_m + agl,
                "roll": roll, "pitch": pitch, "yaw": yaw, "gs": self.gs_ms, "track": yaw}


# ---------------------------------------------------------------------------
# Encoders: truth -> bytes, one per protocol
# ---------------------------------------------------------------------------

def _nmea(body: str) -> bytes:
    return f"${body}*{drivers.nmea_xor(body):02X}\r\n".encode()


def _hhmmss(ts: float) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.strftime("%H%M%S.") + f"{d.microsecond // 10000:02d}"


def _ddmm(v: float, pos: str, neg: str, deg_w: int) -> tuple[str, str]:
    a = abs(v)
    d = int(a)
    m = (a - d) * 60
    return f"{d:0{deg_w}d}{m:09.6f}", pos if v >= 0 else neg


def enc_gga(ts, tr, sats=14, hdop=0.8, fix=1) -> bytes:
    la, ns = _ddmm(tr["lat"], "N", "S", 2)
    lo, ew = _ddmm(tr["lon"], "E", "W", 3)
    return _nmea(f"GNGGA,{_hhmmss(ts)},{la},{ns},{lo},{ew},{fix},{sats:02d},{hdop:.1f},{tr['alt_msl']:.3f},M,-30.0,M,,")


def enc_rmc(ts, tr) -> bytes:
    la, ns = _ddmm(tr["lat"], "N", "S", 2)
    lo, ew = _ddmm(tr["lon"], "E", "W", 3)
    d = datetime.fromtimestamp(ts, timezone.utc).strftime("%d%m%y")
    return _nmea(f"GNRMC,{_hhmmss(ts)},A,{la},{ns},{lo},{ew},{tr['gs'] / 0.514444:.3f},{tr['track']:.2f},{d},,,A")


def enc_pashr(ts, roll, pitch, yaw) -> bytes:
    return _nmea(f"PASHR,{_hhmmss(ts)},{yaw:.2f},T,{roll:.2f},{pitch:.2f},0.00,0.05,0.05,0.10,1,0")


def enc_vnins(ts, tr, roll, pitch, yaw) -> bytes:
    vn = tr["gs"] * math.cos(math.radians(tr["track"]))
    ve = tr["gs"] * math.sin(math.radians(tr["track"]))
    tow = (ts - 315964800 + drivers.GPS_UTC_LEAP_S) % 604800        # GPS time of week
    return _nmea(f"VNINS,{tow:.3f},2290,0006,{yaw:+08.3f},{pitch:+07.3f},{roll:+08.3f},"
                 f"{tr['lat']:+012.8f},{tr['lon']:+013.8f},{tr['alt_msl']:+010.3f},"
                 f"{vn:+08.3f},{ve:+08.3f},+000.000,0.5,1.0,0.1")


def enc_vnymr(roll, pitch, yaw) -> bytes:
    return _nmea(f"VNYMR,{yaw:+08.3f},{pitch:+07.3f},{roll:+08.3f},+0.2000,+0.0100,+0.4000,"
                 "+00.000,+00.000,-09.810,+0.000,+0.000,+0.000")


def enc_csv(roll, pitch, yaw) -> bytes:
    return f"{roll:.3f},{pitch:.3f},{yaw:.3f}\r\n".encode()


def enc_benewake(rng_m) -> bytes:
    d = int(round(rng_m * 100))
    if d > 0xFFFF:
        d = 18000
    f = bytes([0x59, 0x59, d & 0xFF, d >> 8, 200, 0, 0, 0])
    return f + bytes([sum(f) & 0xFF])


def enc_bno(roll, pitch, yaw, idx=0) -> bytes:
    y = int(round(((yaw + 180) % 360 - 180) * 100))
    body = bytes([idx & 0xFF]) + struct.pack("<hhhhhh", y, int(round(pitch * 100)), int(round(roll * 100)), 0, 0, 1000) + bytes(3)
    return b"\xAA\xAA" + body + bytes([sum(body) & 0xFF])


def enc_lightware(rng_m) -> bytes:
    payload = struct.pack("<II", int(round(rng_m * 10)), 30)
    flags = (len(payload) + 1) << 6
    body = bytes([0xAA]) + struct.pack("<H", flags) + bytes([44]) + payload
    return body + struct.pack("<H", drivers.crc16_xmodem(body))


def enc_riegl(rng_m) -> bytes:
    return f"r{rng_m:09.3f} a045\r\n".encode()


def enc_ilm(rng_m, max_range_m=150.0) -> bytes:
    """AgLaser ILM-150-R: 10 characters incl. CR, e.g. 00026.56m<CR>; 99999.99m<CR> = no target."""
    if rng_m > max_range_m:
        return b"99999.99m\r"
    return f"{rng_m:08.2f}m\r".encode()


def enc_ascii(rng_m) -> bytes:
    return f"{rng_m:.3f}\r\n".encode()


class MavEnc:
    def __init__(self):
        from pymavlink.dialects.v20 import common as mav2
        self.m = mav2.MAVLink(None, srcSystem=1, srcComponent=1)

    def attitude(self, roll, pitch, yaw):
        return self.m.attitude_encode(int(time.time() * 1000) & 0xFFFFFFFF, math.radians(roll), math.radians(pitch),
                                      math.radians(((yaw + 180) % 360) - 180), 0, 0, 0).pack(self.m)

    def gps(self, tr):
        return self.m.gps_raw_int_encode(int(time.time() * 1e6), 3, int(tr["lat"] * 1e7), int(tr["lon"] * 1e7),
                                         int(tr["alt_msl"] * 1000), 80, 100, int(tr["gs"] * 100),
                                         int(tr["track"] * 100) % 36000, 14).pack(self.m)


# ---------------------------------------------------------------------------
# Virtual device: a pty and a writer thread
# ---------------------------------------------------------------------------

class VirtualDevice:
    """A pseudo-terminal the daemon opens like a serial port, fed by a writer thread."""

    def __init__(self, name: str, driver: str, role: str, rate_hz: float, traj: Trajectory,
                 inst: Installation, seed: int = 1, device: str = "", max_range_m: float = 0.0):
        self.name, self.driver, self.role = name, driver, role
        self.device, self.max_range_m = device, max_range_m
        self.rate = rate_hz
        self.traj, self.inst = traj, inst
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        self.rnd = random.Random(seed)
        self.stop = threading.Event()
        self.t0 = 0.0
        self.frames = 0
        self.corrupted = 0
        self.spikes = 0
        self.mav = MavEnc() if driver == "mavlink" else None
        self.received = bytearray()       # commands the daemon sent (init / poll)
        self.sent_log: list[tuple[float, bool]] = []   # (send time, corrupted) for completeness checks
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.reader = threading.Thread(target=self._drain, daemon=True)

    def start(self, t0: float):
        self.t0 = t0
        self.thread.start()
        self.reader.start()

    def _drain(self):
        import select
        while not self.stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.2)
            if r:
                try:
                    data = os.read(self.master, 4096)
                except OSError:
                    return
                if len(self.received) < 65536:
                    self.received += data

    def _measured(self, tr):
        """What the installed sensors report for this truth (with the installation's errors)."""
        i = self.inst
        roll_m = tr["roll"] + i.imu_roll_bias_deg + self.rnd.gauss(0, i.imu_noise_deg)
        pitch_m = tr["pitch"] + i.imu_pitch_bias_deg + self.rnd.gauss(0, i.imu_noise_deg)
        beam_r = math.radians(tr["roll"] + i.boresight_roll_deg)
        beam_p = math.radians(tr["pitch"] + i.boresight_pitch_deg)
        rng = (tr["agl"] + i.laser_offset_m) / (math.cos(beam_r) * math.cos(beam_p))
        rng += self.rnd.gauss(0, i.laser_noise_m)
        return roll_m, pitch_m, rng

    def frame(self, now: float) -> bytes:
        tr = dict(self.traj.at(now - self.t0))
        n = self.inst.gnss_noise_m
        tr["lat"], tr["lon"] = integrity.project(tr["lat"], tr["lon"], self.rnd.uniform(0, 360), abs(self.rnd.gauss(0, n)))
        roll, pitch, rng = self._measured(tr)
        yaw = tr["yaw"]
        d = self.driver
        if self.role == "laser" and self.rnd.random() < self.inst.spike_frac:
            rng = rng * self.rnd.uniform(0.05, 0.6)          # a false return (bird, cloud, multipath)
            self.spikes += 1
        if d == "vectornav_ins":
            out = enc_vnins(now, tr, roll, pitch, yaw)
        elif d == "vectornav":
            out = enc_vnymr(roll, pitch, yaw)
        elif d == "nmea":
            out = enc_gga(now, tr) + enc_rmc(now, tr)
        elif d == "nmea_ins":
            out = enc_gga(now, tr) + enc_rmc(now, tr) + enc_pashr(now, roll, pitch, yaw)
        elif d == "csv_rpy":
            out = enc_csv(roll, pitch, yaw)
        elif d == "bno08x_rvc":
            out = enc_bno(roll, pitch, yaw, self.frames)
        elif d == "mavlink":
            out = self.mav.attitude(roll, pitch, yaw)
            if self.frames % 4 == 0:
                out += self.mav.gps(tr)
        elif d == "benewake":
            out = enc_benewake(rng)
        elif d == "lightware_binary":
            out = enc_lightware(rng)
        elif d == "riegl_ld90":
            out = enc_riegl(rng)
        elif d == "ascii_number" and self.device == "aglaser-ilm-150-r":
            out = enc_ilm(rng, self.max_range_m or 150.0)
        elif d == "ascii_number":
            out = enc_ascii(rng)
        else:
            raise ValueError(f"no encoder for driver {d}")
        if self.rnd.random() < self.inst.corrupt_frac and len(out) > 6:
            b = bytearray(out)
            k = self.rnd.randrange(3, len(b) - 3)
            b[k] ^= 0x10                                      # a flipped bit on the wire
            out = bytes(b)
            self.corrupted += 1
        return out

    def _run(self):
        period = 1.0 / self.rate
        nxt = time.monotonic()
        while not self.stop.is_set():
            t = time.time()
            c0 = self.corrupted
            try:
                os.write(self.master, self.frame(t))
                self.sent_log.append((t, self.corrupted != c0))
            except OSError:
                pass
            self.frames += 1
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.reader.join(timeout=2)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
