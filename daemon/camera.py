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
from collections import deque
import time
from urllib.parse import quote

STATUS_KEYS = ("sd_gb_free", "sd_gb_total", "sd_status", "sd_warn", "auto_cap_active", "bus_volts",
               "gps_used_sats", "gps_type", "time_source", "utc_time_valid", "dls_status")

DEFAULT_MAX_FILES = 200_000
DEFAULT_MAX_DIRS = 50_000


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
    def base_url(self) -> str:
        """Return the camera HTTP base URL (status_url with '/status' removed)."""
        if not self.status_url:
            return ""
        url = self.status_url.rstrip("/")
        if url.endswith("/status"):
            return url[:-len("/status")]
        return url.rsplit("/status", 1)[0].rstrip("/")

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

    async def list_files(
        self,
        timeout_s: float = 30.0,
        req_timeout_s: float = 5.0,
        max_files: int = DEFAULT_MAX_FILES,
        max_dirs: int = DEFAULT_MAX_DIRS,
    ) -> tuple[list[dict] | None, str | None]:
        """Walk the camera's read-only HTTP /files/ tree and list every file.

        Uses the documented MicaSense RedEdge HTTP API (/files/*): GET on a
        directory returns JSON with "files" ([{"name", "size"}, ...]) and
        "directories" (["0000SET", ...]). Never raises; returns (files, None)
        on success, (files, error) if capped, or (None, error) on failure.
        """
        base = self.base_url()
        if not base:
            return None, "no status_url configured"
        try:
            import httpx
            files: list[dict] = []
            visited: set[str] = set()
            queue: deque[str] = deque([""])
            deadline = time.monotonic() + timeout_s
            async with httpx.AsyncClient(follow_redirects=True) as client:
                while queue:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None, f"timed out after {timeout_s:g}s"
                    rel_dir = queue.popleft().strip("/")
                    if rel_dir in visited:
                        continue
                    if len(visited) >= max_dirs:
                        files.sort(key=lambda f: f["path"])
                        return files, f"stopped at cap of {max_dirs} directories"
                    visited.add(rel_dir)
                    req_path = f"/files/{quote(rel_dir, safe='/')}" if rel_dir else "/files/"
                    r = await client.get(f"{base}{req_path}", timeout=min(req_timeout_s, remaining))
                    if r.status_code != 200:
                        return None, f"HTTP {r.status_code} for {req_path}"
                    data = r.json()
                    if not isinstance(data, dict) or ("files" not in data and "directories" not in data):
                        return None, f"unexpected directory listing for {req_path}"
                    for entry in data.get("files") or []:
                        if not isinstance(entry, dict) or not entry.get("name"):
                            continue
                        name = str(entry["name"]).lstrip("/")
                        api_path = f"/files/{rel_dir}/{name}" if rel_dir else f"/files/{name}"
                        size = entry.get("size")
                        item: dict = {"path": api_path, "size": int(size) if size is not None else None}
                        if "mtime" in entry and entry["mtime"] is not None:
                            item["mtime"] = entry["mtime"]
                        files.append(item)
                        if len(files) >= max_files:
                            files.sort(key=lambda f: f["path"])
                            return files, f"stopped at cap of {max_files} files"
                    for sub in data.get("directories") or []:
                        sub_name = str(sub["name"] if isinstance(sub, dict) else sub).strip("/")
                        if not sub_name or ".." in sub_name.split("/") or sub_name == ".":
                            continue
                        child = f"{rel_dir}/{sub_name}" if rel_dir else sub_name
                        if child not in visited:
                            queue.append(child)
            files.sort(key=lambda f: f["path"])
            return files, None
        except Exception as exc:
            msg = str(exc).strip() or type(exc).__name__
            return None, msg[:200]

    def list_files_sync(self, **kwargs) -> tuple[list[dict] | None, str | None]:
        """Synchronous wrapper around list_files() for worker threads."""
        try:
            return asyncio.run(self.list_files(**kwargs))
        except Exception as exc:
            msg = str(exc).strip() or type(exc).__name__
            return None, msg[:200]

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
