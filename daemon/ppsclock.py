"""GNSS PPS capture: the exact top of every GPS second, from the kernel.

Wire the receiver's PPS output (Reach M2: C1 or C2 pin 3) to a Pi GPIO and
enable the kernel PPS driver (Pi 5: dtoverlay=pps-rp1,pin=18; earlier Pis:
dtoverlay=pps-gpio,gpiopin=18). The kernel timestamps each pulse in
CLOCK_REALTIME and exposes it as /sys/class/pps/ppsN/assert = "<sec>.<nsec>#<seq>".

Each new edge is logged (pps_edges) with the system-minus-monotonic clock
offset at that moment, so tools/export_laser.py can put every monotonic
timestamp onto GPS time to microseconds. The fractional second of each edge
also shows how far the Pi's own clock is from GPS time, live.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path


def parse_assert(text: str) -> tuple[int, int] | None:
    """'1795100000.000012345#42' -> (assert_ns, seq). None when empty or never pulsed."""
    text = text.strip()
    if "#" not in text:
        return None
    ts, seq = text.split("#", 1)
    try:
        sec, _, frac = ts.partition(".")
        ns = int(sec) * 1_000_000_000 + int((frac + "000000000")[:9])
        return ns, int(seq)
    except ValueError:
        return None


def offset_from_second_ns(assert_ns: int) -> int:
    """Pi clock error at a PPS edge: how far the edge is from a whole second, wrapped to +/-0.5 s."""
    r = assert_ns % 1_000_000_000
    return r - 1_000_000_000 if r >= 500_000_000 else r


class PPSClock:
    def __init__(self, device: str | None, on_edge=None, sysfs_root: str = "/sys/class/pps"):
        self.enabled = bool(device)
        name = (device or "").replace("/dev/", "")
        self.path = Path(sysfs_root) / name / "assert"
        self.on_edge = on_edge or (lambda seq, assert_ns, utc_minus_mono: None)
        self.last_seq = None
        self.last_edge_mono = 0.0
        self.edges = 0
        self.last_offset_ns: int | None = None

    def poll(self) -> bool:
        """Read the sysfs file once; True if there was a new edge."""
        try:
            parsed = parse_assert(self.path.read_text())
        except OSError:
            return False
        if not parsed:
            return False
        assert_ns, seq = parsed
        if seq == self.last_seq:
            return False
        first = self.last_seq is None
        self.last_seq = seq
        if first and seq == 0:
            return False
        utc_minus_mono = time.time_ns() - time.monotonic_ns()
        self.last_edge_mono = time.monotonic()
        self.edges += 1
        self.last_offset_ns = offset_from_second_ns(assert_ns)
        self.on_edge(seq, assert_ns, utc_minus_mono)
        return True

    async def run(self, hz: float = 20.0):
        while True:
            self.poll()
            await asyncio.sleep(1.0 / hz)

    def status(self) -> dict:
        if not self.enabled:
            return {"ok": False, "enabled": False, "detail": "not configured"}
        if not self.path.exists():
            return {"ok": False, "enabled": True, "detail": f"{self.path} missing: PPS overlay not loaded?"}
        age = time.monotonic() - self.last_edge_mono if self.last_edge_mono else None
        ok = age is not None and age < 2.5
        detail = (f"{self.edges} pulses, Pi clock {self.last_offset_ns / 1e6:+.3f} ms from GPS second"
                  if ok and self.last_offset_ns is not None else
                  ("no pulse yet" if age is None else f"last pulse {age:.0f} s ago"))
        return {"ok": ok, "enabled": True, "detail": detail, "edges": self.edges,
                "clock_offset_ms": None if self.last_offset_ns is None else self.last_offset_ns / 1e6}
