"""Camera that captures on its own, watched by the Pi.

The camera (e.g. a MicaSense RedEdge-P on its own capture timer) stores its
images itself. Its Top-of-Frame output goes to the Reach M2 time mark, which
stamps every exposure in the GNSS raw log: that is the precise record. The
same signal is split to a Pi GPIO, and this module:

  * logs every exposure (exposures table: sequence, utc_ns, mono_ns) as a
    backup record, put on GNSS time by tools/export_exposures.py;
  * measures the exposure rate, so Buddy can call out "camera stopped";
  * polls the camera's HTTP /status (when it is on Ethernet) for SD space,
    SD status, auto-capture state, supply volts, GPS and time source.

Timestamps are taken in the GPIO callback, so they carry the Linux scheduling
delay (typically well under a millisecond, occasionally more): good for a
backup and for health, while the Reach time mark is the exact one.
"""

from __future__ import annotations

import asyncio
import time

STATUS_KEYS = ("sd_gb_free", "sd_gb_total", "sd_status", "sd_warn", "auto_cap_active", "bus_volts",
               "gps_used_sats", "gps_type", "time_source", "utc_time_valid", "dls_status")


class Camera:
    def __init__(self, cfg: dict | None, sim: bool = False, on_exposure=None, on_status=None):
        cfg = cfg or {}
        self.enabled = bool(cfg) and cfg.get("enabled", True)
        self.name = cfg.get("name", "Camera")
        self.pin = cfg.get("exposure_gpio")
        self.edge = cfg.get("exposure_edge", "falling")
        self.expected_hz = float(cfg.get("expected_hz") or 0) or None
        self.status_url = cfg.get("status_url") or ""
        self.sim = sim
        self.on_exposure = on_exposure or (lambda seq, utc_ns, mono_ns: None)
        self.on_status = on_status or (lambda d: None)
        self.count = 0
        self.times: list[float] = []
        self.last = 0.0
        self.device = None
        self.gpio_ok = False
        self.gpio_error = ""
        self.status: dict = {}
        self.status_ok = False
        self.status_at = 0.0
        self.status_error = ""

    # -- exposures -----------------------------------------------------------
    def exposure(self, utc_ns: int | None = None, mono_ns: int | None = None):
        utc_ns = utc_ns or time.time_ns()
        mono_ns = mono_ns or time.monotonic_ns()
        self.count += 1
        now = time.monotonic()
        self.last = now
        self.times.append(now)
        while self.times and now - self.times[0] > 10.0:
            self.times.pop(0)
        self.on_exposure(self.count, utc_ns, mono_ns)

    def start_gpio(self):
        if self.sim or self.pin is None:
            return
        try:
            from gpiozero import DigitalInputDevice
            # Top of Frame rests high, falls at the top of frame: active-low input, no pull.
            active_low = self.edge == "falling"
            self.device = DigitalInputDevice(int(self.pin), pull_up=None, active_state=not active_low)
            self.device.when_activated = lambda: self.exposure(time.time_ns(), time.monotonic_ns())
            self.gpio_ok = True
        except Exception as exc:
            self.gpio_error = str(exc)
            self.gpio_ok = False

    def stop(self):
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass

    def rate_hz(self) -> float:
        now = time.monotonic()
        ts = [t for t in self.times if now - t <= 10.0]
        if len(ts) < 2 or now - ts[-1] > 3.0 * (1.0 / self.expected_hz if self.expected_hz else 1.0):
            return 0.0
        return (len(ts) - 1) / (ts[-1] - ts[0]) if ts[-1] > ts[0] else 0.0

    def stopped(self) -> bool:
        """It was exposing, and now hasn't for 3 periods (at least 3 s)."""
        if not self.count:
            return False
        gap = max(3.0, 3.0 / self.expected_hz) if self.expected_hz else 5.0
        return time.monotonic() - self.last > gap

    # -- status over HTTP ----------------------------------------------------
    async def status_loop(self, period: float = 5.0):
        if not self.status_url:
            return
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            while True:
                try:
                    r = await client.get(self.status_url)
                    d = r.json()
                    self.status = {k: d.get(k) for k in STATUS_KEYS if k in d}
                    self.status_ok, self.status_at, self.status_error = True, time.time(), ""
                    self.on_status(self.status)
                except Exception as exc:
                    self.status_ok, self.status_error = False, str(exc)[:120]
                await asyncio.sleep(period)

    async def sim_loop(self):
        """Simulated camera: exposures at the expected rate, plausible status."""
        hz = self.expected_hz or 1.0
        free = 118.0
        while True:
            self.exposure()
            free -= 0.0006
            self.status = {"sd_gb_free": round(free, 2), "sd_gb_total": 128.0, "sd_status": "Ok", "sd_warn": False,
                           "auto_cap_active": True, "bus_volts": 12.1, "gps_used_sats": 12, "gps_type": "Ublox",
                           "time_source": "PPS", "utc_time_valid": True, "dls_status": "Ok"}
            self.status_ok, self.status_at = True, time.time()
            await asyncio.sleep(1.0 / hz)

    def snapshot(self) -> dict:
        age = time.monotonic() - self.last if self.last else None
        return {"enabled": self.enabled, "name": self.name, "exposures": self.count, "hz": round(self.rate_hz(), 2),
                "expected_hz": self.expected_hz, "age_s": None if age is None else round(age, 1),
                "stopped": self.stopped(), "gpio_ok": self.gpio_ok or self.sim, "gpio_error": self.gpio_error,
                "pin": self.pin, "status_url": self.status_url, "status_ok": self.status_ok,
                "status_age_s": round(time.time() - self.status_at, 1) if self.status_at else None,
                "status": self.status, "status_error": self.status_error}
