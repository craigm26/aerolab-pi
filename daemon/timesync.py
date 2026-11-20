"""Set and monitor the Pi's system clock from GNSS (and PPS) via chrony.

Chrony's SOCK reference clock driver (refclock_sock.c) receives UDP-style
datagrams on a Unix domain socket using struct sock_sample:

    #define SOCK_MAGIC 0x534f434b

    struct sock_sample {
      struct timeval tv;  /* receipt system time: tv_sec, tv_usec */
      double offset;      /* true time minus system time, seconds */
      int pulse;          /* non-zero for a PPS pulse without full seconds */
      int leap;           /* 0 normal, 1 insert, 2 delete */
      int _pad;           /* ignored padding */
      int magic;          /* 0x534f434b ("SOCK") */
    };

On 64-bit Linux (aarch64 / x86_64, LP64), struct timeval is two 8-byte signed
longs (16 bytes), followed by an 8-byte double and four 4-byte ints (16 bytes),
for 40 bytes total with natural alignment ("@lldiiii").

The offset sign convention follows chrony's refclock_sock.c and gpsd's
chrony_send() (gpsd/timehint.c): offset = reference_time - system_time.
A positive offset means the system clock is behind true time; negative means
it is ahead.
"""

from __future__ import annotations

import asyncio
import math
import re
import socket
import struct
from datetime import date, datetime, timedelta, timezone

SOCK_MAGIC = 0x534F434B
DEFAULT_SOCKET = "/run/chrony/aerolab.sock"

# Native byte order, sizes and alignment matching C struct sock_sample:
#   long tv_sec, long tv_usec, double offset, int pulse, int leap, int _pad, int magic
# 64-bit Linux (aarch64/x86_64): 8 + 8 + 8 + 4 + 4 + 4 + 4 = 40 bytes.
SAMPLE_STRUCT = struct.Struct("@lldiiii")


def pack_sample(system_time_s: float, gnss_time_s: float, pulse: bool = False, leap: int = 0) -> bytes:
    """Pack one chrony struct sock_sample datagram."""
    if not (math.isfinite(system_time_s) and math.isfinite(gnss_time_s)):
        raise ValueError("timestamps must be finite")
    sec = math.floor(system_time_s)
    usec = int(round((system_time_s - sec) * 1_000_000))
    if usec >= 1_000_000:
        sec += 1
        usec -= 1_000_000
    offset = float(gnss_time_s) - float(system_time_s)
    return SAMPLE_STRUCT.pack(sec, usec, offset, 1 if pulse else 0, int(leap), 0, SOCK_MAGIC)


def next_date(iso_date: str) -> str:
    """Return the next UTC calendar date as YYYY-MM-DD."""
    return (date.fromisoformat(iso_date) + timedelta(days=1)).isoformat()


def gnss_utc(gnss_tod: float, gnss_date: str | None, system_time: float) -> float:
    """Full UTC epoch seconds for a GNSS time of day.

    Uses gnss_date ("YYYY-MM-DD") when known so a Pi that booted with the wrong
    date still gets the true UTC epoch. Without a date, picks the UTC day that
    places the sample nearest to system_time (within +/- 12 h).
    """
    tod = float(gnss_tod)
    if gnss_date:
        try:
            d = date.fromisoformat(str(gnss_date).strip())
            day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
            return day_start + tod
        except ValueError:
            pass
    sys_day_start = math.floor(float(system_time) / 86400.0) * 86400.0
    t = sys_day_start + tod
    diff = t - float(system_time)
    if diff > 43200.0:
        t -= 86400.0
    elif diff < -43200.0:
        t += 86400.0
    return t


class ChronyFeeder:
    """Sends GNSS time samples to chronyd's SOCK refclock Unix datagram socket.

    Uses sendto() on an unconnected datagram socket so a chrony restart
    recreating the socket file works automatically. Never raises.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET):
        self.socket_path = str(socket_path)
        self.sent = 0
        self.errors = 0
        self.last_error = ""
        self._sock: socket.socket | None = None

    def send(self, system_time_s: float, gnss_time_s: float, pulse: bool = False, leap: int = 0) -> bool:
        try:
            pkt = pack_sample(system_time_s, gnss_time_s, pulse=pulse, leap=leap)
            if self._sock is None:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                s.setblocking(False)
                self._sock = s
            self._sock.sendto(pkt, self.socket_path)
            self.sent += 1
            self.last_error = ""
            return True
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)
            return False

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


_REF_RE = re.compile(r"^([0-9A-Fa-f]+)\s*\(([^)]*)\)")
_SYS_TIME_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s+seconds\s+(fast|slow)", re.IGNORECASE)


def parse_tracking(text: str) -> dict:
    """Parse `chronyc -n tracking` output into a structured summary.

    offset_ms follows the daemon's clock-offset sign convention: positive when
    the system clock is fast of NTP/UTC time, negative when it is slow.
    """
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fields[key.strip().lower()] = val.strip()

    ref_raw = fields.get("reference id", "")
    ref_id = ""
    source = ""
    m_ref = _REF_RE.match(ref_raw)
    if m_ref:
        ref_id = m_ref.group(1).upper()
        source = m_ref.group(2).strip()
    elif ref_raw:
        ref_id = ref_raw.split()[0].upper()

    stratum: int | None = None
    if "stratum" in fields:
        try:
            stratum = int(fields["stratum"])
        except ValueError:
            stratum = None

    offset_s: float | None = None
    offset_ms: float | None = None
    m_sys = _SYS_TIME_RE.search(fields.get("system time", ""))
    if m_sys:
        mag = float(m_sys.group(1))
        sign = 1.0 if m_sys.group(2).lower() == "fast" else -1.0
        offset_s = sign * mag
        offset_ms = offset_s * 1000.0

    leap = fields.get("leap status", "")
    synced = (
        stratum is not None
        and 1 <= stratum <= 15
        and leap.lower() != "not synchronised"
        and leap.lower() != "not synchronized"
        and ref_id not in ("", "00000000")
        and bool(source)
    )
    return {
        "synced": synced,
        "source": source,
        "ref_id": ref_id,
        "stratum": stratum,
        "offset_s": offset_s,
        "offset_ms": offset_ms,
        "leap": leap,
    }


class ChronySync:
    """Polls `chronyc -n tracking` every 10 s when enabled (never in sim)."""

    def __init__(self, enabled: bool = False, sim: bool = False, interval_s: float = 10.0, runner=None):
        self.enabled = bool(enabled) and not bool(sim)
        self.sim = bool(sim)
        self.interval_s = float(interval_s)
        self._runner = runner
        self.synced = False
        self.source = ""
        self.stratum: int | None = None
        self.offset_ms: float | None = None
        self.leap = ""
        self.checked = False
        if enabled and sim:
            self.detail = "disabled in simulation"
        elif not enabled:
            self.detail = "not enabled"
        else:
            self.detail = "checking chrony"

    async def _run_chronyc(self) -> tuple[int, str, str]:
        if self._runner is not None:
            return await self._runner()
        proc = await asyncio.create_subprocess_exec(
            "chronyc", "-n", "tracking",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise
        return proc.returncode, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")

    async def poll_once(self) -> dict:
        if not self.enabled:
            return self.status()
        try:
            rc, out, err = await self._run_chronyc()
        except FileNotFoundError:
            self.synced, self.source, self.offset_ms, self.leap = False, "", None, ""
            self.detail = "chronyc not installed"
            self.checked = True
            return self.status()
        except asyncio.TimeoutError:
            self.synced = False
            self.detail = "chronyc timed out"
            self.checked = True
            return self.status()
        except OSError as exc:
            self.synced = False
            self.detail = f"chronyc failed: {exc}"
            self.checked = True
            return self.status()

        if rc != 0:
            self.synced, self.source, self.offset_ms, self.leap = False, "", None, ""
            msg = err.strip() or out.strip() or f"chronyc exited {rc}"
            self.detail = msg
            self.checked = True
            return self.status()

        parsed = parse_tracking(out)
        self.synced = parsed["synced"]
        self.source = parsed["source"]
        self.stratum = parsed["stratum"]
        self.offset_ms = parsed["offset_ms"]
        self.leap = parsed["leap"]
        self.checked = True
        if self.synced:
            off_txt = f" ({self.offset_ms:+.3f} ms)" if self.offset_ms is not None else ""
            self.detail = f"synced to {self.source}{off_txt}"
        else:
            self.detail = f"not synchronised ({self.leap})" if self.leap else "not synchronised"
        return self.status()

    async def run(self):
        while True:
            if self.enabled:
                await self.poll_once()
            await asyncio.sleep(self.interval_s)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "synced": self.synced,
            "source": self.source,
            "offset_ms": None if self.offset_ms is None else round(self.offset_ms, 3),
            "leap": self.leap,
            "detail": self.detail,
        }
