"""Serial protocol drivers.

A driver turns raw bytes from a serial port into readings. Every driver has
the same shape: feed(bytes) -> list[dict]. A reading may carry any of:

    range_m                          laser altimeter slant range, metres
    roll, pitch, yaw                 attitude, degrees
    lat, lon, alt_msl, fix, sats     position (fix: 0 none, 1 GPS, 2 DGPS, 4 RTK fixed, 5 RTK float)
    gnss_tod                         GNSS UTC time of day, seconds since midnight

One device can produce several kinds (a GNSS/INS gives position and attitude),
and the daemon applies whatever arrives. Pick a driver per sensor in the
config with `driver: <name>`; config/hardware.yaml maps real devices to drivers.
"""

from __future__ import annotations

import math
import struct
from typing import Callable

import pynmea2

Reading = dict


def _finite(*vals) -> bool:
    return all(v is not None and math.isfinite(v) for v in vals)


# ---------------------------------------------------------------------------
# Line-based drivers
# ---------------------------------------------------------------------------

class BadFrame(Exception):
    """A frame that was meant for this driver but failed its checksum or layout."""


class Stats:
    """Per-driver counters the integrity monitor reads: good readings and bad frames."""

    def __init__(self):
        self.good = 0
        self.bad = 0


class LineDriver:
    """Splits the byte stream into lines and parses each one."""

    max_line = 512

    def __init__(self, parse: Callable[[str], Reading | None], **opts):
        self.buf = b""
        self.parse = parse
        self.opts = opts
        self.stats = Stats()

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while True:
            # Accept \n, \r\n and bare \r line endings.
            idx = min((i for i in (self.buf.find(b"\n"), self.buf.find(b"\r")) if i >= 0), default=-1)
            if idx < 0:
                if len(self.buf) > self.max_line:
                    self.buf = b""          # garbage with no line ending: drop it
                break
            line, self.buf = self.buf[:idx], self.buf[idx + 1:]
            text = line.decode("ascii", errors="ignore").strip()
            if not text:
                continue
            try:
                r = self.parse(text, **self.opts) if self.opts else self.parse(text)
            except BadFrame:
                self.stats.bad += 1
                continue
            if r:
                self.stats.good += 1
                out.append(r)
        return out


def _nmea_checksum_ok(text: str) -> bool:
    if "*" not in text:
        return False
    body, _, cs = text[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(cs[:2], 16)
    except ValueError:
        return False


def parse_nmea(text: str) -> Reading | None:
    """GGA (position, fix, sats, time) and HDT/PASHR (heading/attitude) from any talker."""
    if not text.startswith("$"):
        return None
    if text[3:6] == "SHR" or text.startswith("$PASHR"):
        return parse_pashr(text)
    if "*" in text and not _nmea_checksum_ok(text):
        raise BadFrame("NMEA checksum")
    try:
        msg = pynmea2.parse(text, check=True)
    except pynmea2.ChecksumError as exc:
        raise BadFrame("NMEA checksum") from exc
    except (pynmea2.ParseError, AttributeError, ValueError):
        return None
    st = getattr(msg, "sentence_type", "")
    if st == "GGA":
        try:
            fix = int(msg.gps_qual or 0)
            r: Reading = {"fix": fix, "sats": int(msg.num_sats or 0)}
            if msg.timestamp:
                t = msg.timestamp
                r["gnss_tod"] = t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
            if msg.horizontal_dil:
                r["hdop"] = float(msg.horizontal_dil)
            if fix > 0:
                r.update(lat=float(msg.latitude), lon=float(msg.longitude), alt_msl=float(msg.altitude or 0.0))
            return r
        except (ValueError, TypeError):
            return None
    if st == "RMC":
        try:
            if msg.status != "A" or msg.spd_over_grnd in (None, ""):
                return None
            r = {"gs_ms": float(msg.spd_over_grnd) * 0.514444}
            if msg.true_course not in (None, ""):
                r["track_deg"] = float(msg.true_course) % 360.0
            return r
        except (ValueError, TypeError, AttributeError):
            return None
    if st == "VTG":
        try:
            if msg.spd_over_grnd_kmph in (None, ""):
                return None
            r = {"gs_ms": float(msg.spd_over_grnd_kmph) / 3.6}
            if msg.true_track not in (None, ""):
                r["track_deg"] = float(msg.true_track) % 360.0
            return r
        except (ValueError, TypeError, AttributeError):
            return None
    if st == "HDT":
        try:
            return {"yaw": float(msg.heading)}
        except (ValueError, TypeError):
            return None
    return None


def parse_pashr(text: str) -> Reading | None:
    """$PASHR,hhmmss.ss,heading,T,roll,pitch,heave,roll_acc,pitch_acc,heading_acc,gps_q,ins_q*cs"""
    if "*" in text and not _nmea_checksum_ok(text):
        raise BadFrame("PASHR checksum")
    f = text.split("*")[0].split(",")
    try:
        heading, roll, pitch = float(f[2]), float(f[4]), float(f[5])
    except (IndexError, ValueError):
        return None
    if not _finite(heading, roll, pitch):
        return None
    return {"roll": roll, "pitch": pitch, "yaw": heading % 360.0}


def parse_number(text: str, unit: str = "m") -> Reading | None:
    """First number on the line is the range. Handles '12.34', '12.34 m', 'D 0012.345'."""
    for tok in text.replace(",", " ").split():
        tok = tok.rstrip("mM")
        try:
            v = float(tok)
        except ValueError:
            continue
        if not math.isfinite(v) or v < 0:
            return None
        return {"range_m": v / 100.0 if unit == "cm" else (v / 1000.0 if unit == "mm" else v)}
    return None


def parse_number_filtered(text: str, unit: str = "m", invalid: tuple = ()) -> Reading | None:
    """parse_number, dropping sentinel values a sensor uses for 'no target' (e.g. SF11/C 130.00)."""
    r = parse_number(text, unit=unit)
    if r and any(abs(r["range_m"] - v) < 1e-6 for v in invalid):
        return None
    return r


def parse_csv_rpy(text: str) -> Reading | None:
    """roll,pitch,yaw in degrees, e.g. from a microcontroller sketch."""
    parts = text.split(",")
    if len(parts) < 3:
        return None
    try:
        roll, pitch, yaw = (float(p) for p in parts[:3])
    except ValueError:
        return None
    if not _finite(roll, pitch, yaw):
        return None
    return {"roll": roll, "pitch": pitch, "yaw": yaw % 360.0}


# ---------------------------------------------------------------------------
# Device-specific drivers
# ---------------------------------------------------------------------------

def nmea_xor(body: str) -> int:
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return calc


def parse_vectornav(text: str) -> Reading | None:
    """VectorNav ASCII: $VNYMR (VN-100) and $VNINS (VN-200/VN-300).

    $VNYMR,yaw,pitch,roll,magX,magY,magZ,accX,accY,accZ,gyrX,gyrY,gyrZ*CS
    $VNINS,tow,week,status(hex),yaw,pitch,roll,lat,lon,alt_ellipsoid,velN,velE,velD,attU,posU,velU*CS
    Checksum: XOR of the bytes between $ and *. Source: VectorNav UM001 / UM004.
    """
    if not text.startswith("$VN") or "*" not in text:
        return None
    body, _, cs = text[1:].partition("*")
    try:
        if nmea_xor(body) != int(cs[:2], 16):
            raise BadFrame("VectorNav checksum")
    except ValueError as exc:
        raise BadFrame("VectorNav checksum") from exc
    f = body.split(",")
    try:
        if f[0] == "VNYMR":
            yaw, pitch, roll = float(f[1]), float(f[2]), float(f[3])
            return {"roll": roll, "pitch": pitch, "yaw": yaw % 360.0}
        if f[0] == "VNINS":
            status = int(f[3], 16)
            yaw, pitch, roll = float(f[4]), float(f[5]), float(f[6])
            r: Reading = {"roll": roll, "pitch": pitch, "yaw": yaw % 360.0}
            gps_fix = bool(status & 0x4)
            mode = status & 0x3          # 0 not tracking, 1 aligning/degraded, 2 OK
            r["fix"] = 1 if (gps_fix and mode >= 1) else 0
            r["sats"] = -1               # not reported in $VNINS
            if r["fix"]:
                r.update(lat=float(f[7]), lon=float(f[8]), alt_hae=float(f[9]))
                vn, ve = float(f[10]), float(f[11])
                r["gs_ms"] = math.hypot(vn, ve)
                r["track_deg"] = math.degrees(math.atan2(ve, vn)) % 360.0
            return r
    except (IndexError, ValueError):
        return None
    return None


def parse_riegl(text: str) -> Reading | None:
    """Riegl LD90-3 ASCII blocks: r<range> [a<amp>] ... ; m<message> means no valid range.

    The block layout is from the LD90-3 manual; the exact numeric formatting of
    the r block is not confirmed against hardware, so this takes the first
    number after an 'r'. Status messages (mOVERFLOW, m........) yield nothing.
    """
    t = text.strip()
    if t.startswith("m"):
        return None
    for tok in t.replace(",", " ").split():
        if tok[0] in "rR":
            try:
                v = float(tok[1:])
            except ValueError:
                continue
            return {"range_m": v} if math.isfinite(v) and v > 0 else None
    # Some configurations print only the number.
    return parse_number(t)


class BenewakeDriver:
    """Benewake TF02-Pro / TF03 9-byte frames.

    59 59 DIST_L DIST_H b4 b5 b6 b7 CHK, distance cm little-endian,
    CHK = sum of bytes 0..7 & 0xFF. Over-range sentinels (18000 on TF03-180,
    10000 on TF03-100, 4500 on TF02-Pro) mean "no target" and are dropped.
    """

    def __init__(self, invalid_cm=(18000, 10000, 4500), **_):
        self.stats = Stats()
        self.buf = bytearray()
        self.invalid = set(int(v) for v in invalid_cm)

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while len(self.buf) >= 9:
            if self.buf[0] != 0x59 or self.buf[1] != 0x59:
                del self.buf[0]
                continue
            frame = self.buf[:9]
            if (sum(frame[:8]) & 0xFF) != frame[8]:
                self.stats.bad += 1
                del self.buf[0]
                continue
            del self.buf[:9]
            dist = frame[2] | (frame[3] << 8)
            if dist == 0 or dist in self.invalid:
                continue
            self.stats.good += 1
            out.append({"range_m": dist / 100.0})
        if len(self.buf) > 256:
            del self.buf[:-9]
        return out


class BNO08xRVCDriver:
    """BNO085/BNO086 UART-RVC: 19-byte frames at 100 Hz, 115200 baud.

    AA AA idx yaw pitch roll accX accY accZ (int16 LE; angles 0.01 deg, accel mg)
    + 3 bytes + csum, csum = sum of bytes 2..17 & 0xFF. Source: BNO08X datasheet.
    Yaw is relative to power-on, not magnetic heading.
    """

    def __init__(self, **_):
        self.stats = Stats()
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while len(self.buf) >= 19:
            if self.buf[0] != 0xAA or self.buf[1] != 0xAA:
                del self.buf[0]
                continue
            frame = bytes(self.buf[:19])
            if (sum(frame[2:18]) & 0xFF) != frame[18]:
                self.stats.bad += 1
                del self.buf[0]
                continue
            del self.buf[:19]
            yaw, pitch, roll = struct.unpack_from("<hhh", frame, 3)
            self.stats.good += 1
            out.append({"roll": roll / 100.0, "pitch": pitch / 100.0, "yaw": (yaw / 100.0) % 360.0})
        if len(self.buf) > 512:
            del self.buf[:-19]
        return out


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def lightware_packet(msg_id: int, payload: bytes = b"", write: bool = True) -> bytes:
    """LightWare binary request: AA, flags u16 LE ((len+1)<<6 | write), id, payload, CRC16-XMODEM LE."""
    flags = ((len(payload) + 1) << 6) | (1 if write else 0)
    body = bytes([0xAA]) + struct.pack("<H", flags) + bytes([msg_id]) + payload
    return body + struct.pack("<H", crc16_xmodem(body))


class LightWareBinaryDriver:
    """LightWare binary protocol (GRF-250/GRF-500 class), distance message 44.

    Framing and message IDs follow ArduPilot's AP_LightWareSerial and GRF
    drivers: distance u32 LE in 0.1 m steps (first return). Not yet verified on
    hardware by this project.
    """

    MSG_DISTANCE = 44

    def __init__(self, **_):
        self.stats = Stats()
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while len(self.buf) >= 6:
            if self.buf[0] != 0xAA:
                del self.buf[0]
                continue
            flags = self.buf[1] | (self.buf[2] << 8)
            plen = (flags >> 6) - 1          # payload bytes after the id
            if plen < 0 or plen > 1023:
                del self.buf[0]
                continue
            total = 4 + plen + 2
            if len(self.buf) < total:
                break
            pkt = bytes(self.buf[:total])
            if struct.unpack_from("<H", pkt, total - 2)[0] != crc16_xmodem(pkt[:-2]):
                self.stats.bad += 1
                del self.buf[0]
                continue
            del self.buf[:total]
            if pkt[3] == self.MSG_DISTANCE and plen >= 4:
                d = struct.unpack_from("<I", pkt, 4)[0]
                if d > 0:
                    self.stats.good += 1
                    out.append({"range_m": d / 10.0})
        if len(self.buf) > 4096:
            self.buf.clear()
        return out


LIGHTWARE_GRF_INIT = (
    b"UUU"                                                    # wake the serial interface
    + lightware_packet(74, struct.pack("<B", 5))              # update rate: 5 Hz
    + lightware_packet(27, struct.pack("<I", 0b1))            # distance output: first return
    + lightware_packet(30, struct.pack("<I", 5))              # stream: distance data
)


class MAVLinkDriver:
    """MAVLink from a flight controller: ATTITUDE (rad) and GPS_RAW_INT.

    Needs pymavlink (pip install pymavlink); the installer adds it when a
    MAVLink device is chosen.
    """

    FIX_MAP = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 5, 6: 4}   # MAVLink fix_type -> NMEA-style quality

    def __init__(self, **_):
        try:
            from pymavlink.dialects.v20 import common as mavlink2
        except ImportError as exc:
            raise RuntimeError("MAVLink driver needs pymavlink: .venv/bin/pip install pymavlink") from exc
        self.mav = mavlink2.MAVLink(None)
        self.mav.robust_parsing = True
        self.stats = Stats()

    def feed(self, data: bytes) -> list[Reading]:
        out = []
        try:
            msgs = self.mav.parse_buffer(data) or []
        except Exception:
            return out
        for m in msgs:
            t = m.get_type()
            if t == "BAD_DATA":
                self.stats.bad += 1
                continue
            if t == "ATTITUDE":
                self.stats.good += 1
                out.append({"roll": math.degrees(m.roll), "pitch": math.degrees(m.pitch),
                            "yaw": math.degrees(m.yaw) % 360.0})
            elif t == "GPS_RAW_INT":
                fix = self.FIX_MAP.get(int(m.fix_type), 1)
                r: Reading = {"fix": fix, "sats": int(m.satellites_visible) if m.satellites_visible != 255 else -1}
                if fix:
                    r.update(lat=m.lat / 1e7, lon=m.lon / 1e7, alt_msl=m.alt / 1000.0)
                if m.eph != 65535:
                    r["hdop"] = m.eph / 100.0
                if m.vel != 65535:
                    r["gs_ms"] = m.vel / 100.0
                if m.cog != 65535:
                    r["track_deg"] = (m.cog / 100.0) % 360.0
                self.stats.good += 1
                out.append(r)
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DRIVERS: dict[str, dict] = {
    "nmea": {"make": lambda **o: LineDriver(parse_nmea), "kinds": ["position"],
             "about": "NMEA 0183 GGA position (HDT heading, PASHR attitude if sent)"},
    "nmea_ins": {"make": lambda **o: LineDriver(parse_nmea), "kinds": ["position", "attitude"],
                 "about": "NMEA GGA + PASHR attitude from a GNSS/INS"},
    "ascii_number": {"make": lambda unit="m", invalid=(), **o: LineDriver(parse_number_filtered, unit=unit,
                                                                           invalid=tuple(float(v) for v in invalid)),
                     "kinds": ["range"],
                     "about": "first number on each line is the range"},
    "csv_rpy": {"make": lambda **o: LineDriver(parse_csv_rpy), "kinds": ["attitude"],
                "about": "roll,pitch,yaw degrees per line"},
    "vectornav": {"make": lambda **o: LineDriver(parse_vectornav), "kinds": ["attitude"],
                  "about": "VectorNav $VNYMR"},
    "vectornav_ins": {"make": lambda **o: LineDriver(parse_vectornav), "kinds": ["position", "attitude"],
                      "about": "VectorNav $VNINS (GNSS/INS)"},
    "riegl_ld90": {"make": lambda **o: LineDriver(parse_riegl), "kinds": ["range"],
                   "about": "Riegl LD90-3 ASCII blocks"},
    "benewake": {"make": lambda invalid_cm=(18000, 10000, 4500), **o: BenewakeDriver(invalid_cm), "kinds": ["range"],
                 "about": "Benewake 0x59 0x59 9-byte frames"},
    "bno08x_rvc": {"make": lambda **o: BNO08xRVCDriver(), "kinds": ["attitude"],
                   "about": "BNO085/086 UART-RVC 19-byte frames"},
    "lightware_binary": {"make": lambda **o: LightWareBinaryDriver(), "kinds": ["range"],
                         "about": "LightWare binary protocol, distance message 44"},
    "mavlink": {"make": lambda **o: MAVLinkDriver(), "kinds": ["position", "attitude"],
                "about": "MAVLink ATTITUDE + GPS_RAW_INT (needs pymavlink)"},
}

def ubx_packet(cls: int, msg_id: int, payload: bytes) -> bytes:
    """u-blox UBX frame: B5 62 class id len(u16 LE) payload ck_a ck_b (8-bit Fletcher over class..payload)."""
    body = bytes([cls, msg_id]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])


def ubx_rate_gen9(ms: int) -> bytes:
    """UBX-CFG-VALSET (0x06 0x8A), RAM layer: CFG-RATE-MEAS (key 0x30210001, U2 ms). F9P, M9N."""
    return ubx_packet(0x06, 0x8A, bytes([0x00, 0x01, 0x00, 0x00]) + struct.pack("<IH", 0x30210001, ms))


def ubx_rate_legacy(ms: int) -> bytes:
    """UBX-CFG-RATE (0x06 0x08): measRate ms, navRate 1, timeRef GPS. M8 and older."""
    return ubx_packet(0x06, 0x08, struct.pack("<HHH", ms, 1, 1))


# Bytes some devices need on open (init) or repeatedly (poll), by name, for the config.
SERIAL_SCRIPTS = {
    "lightware_grf_stream": LIGHTWARE_GRF_INIT,
    "ublox9_5hz": ubx_rate_gen9(200),
    "ublox8_5hz": ubx_rate_legacy(200),
}


def make_driver(name: str, **opts):
    if name not in DRIVERS:
        raise KeyError(f"unknown driver '{name}'. Known: {', '.join(sorted(DRIVERS))}")
    return DRIVERS[name]["make"](**opts)
