"""Buddy: a second set of eyes on the instruments.

Three parts, all local:
  * Callouts  - rule-based alerts on edges (sensor lost/back, fix lost, low AGL,
                logging stopped, disk, heat, undervoltage). Always on, no AI.
  * Ground check - a preflight checklist computed from live data.
  * Ask       - chat with a local model through Ollama, grounded in the live
                state and this flight's logs. Falls back to plain rule-based
                answers when no model is installed.

Buddy reports instrument status only. It never gives flight, navigation or
weather advice; that stays with the pilot.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx

SYSTEM_PROMPT = """You are Buddy, the instrument assistant for AeroLab-Pi, a science payload \
logger on a light aircraft. You see live telemetry, this flight's statistics, recent callouts \
and the configured hardware, summarised below.

Rules:
- Answer only from that data. If something is not in it, say you don't have it.
- Never guess why something happened. The data records what and when, not why.
- Be brief: at most three short sentences, plain words, numbers with units.
- You report instrument and payload status only. Never give flight, navigation, weather, \
terrain-clearance or aircraft-control advice. If asked, say that is the pilot's call and give \
the relevant instrument status instead.
- AGL comes from a laser and is advisory, not certified avionics.
- When asked how to fix something, use the Diagnosis steps; say which step to try first."""


@dataclass
class Event:
    id: int
    ts: float
    level: str          # "info" | "warn" | "alert"
    text: str
    key: str = ""

    def as_dict(self):
        return {"id": self.id, "ts": self.ts, "level": self.level, "text": self.text}


@dataclass
class Buddy:
    cfg: dict
    started: float = field(default_factory=time.time)
    events: deque = field(default_factory=lambda: deque(maxlen=100))
    _next_id: int = 1
    _flags: dict = field(default_factory=dict)
    _last_slow: float = 0.0
    _slow: dict = field(default_factory=dict)
    _trigger_seen: tuple = (0, 0.0)
    db_path: Path | None = None
    configured: dict = field(default_factory=lambda: {"gnss": True, "laser": True, "imu": True})
    diagnosis: object = None          # callable -> plain-text findings with fixes (set by the daemon)

    # -- events ------------------------------------------------------------

    def emit(self, level: str, text: str, key: str = "") -> Event:
        ev = Event(self._next_id, time.time(), level, text, key)
        self._next_id += 1
        self.events.append(ev)
        if self.db_path:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                             "unix_ts REAL, level TEXT, text TEXT)")
                conn.execute("INSERT INTO events (unix_ts, level, text) VALUES (?, ?, ?)", (ev.ts, level, text))
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass
        return ev

    def _edge(self, key: str, active: bool, on: tuple[str, str] | None, off: tuple[str, str] | None):
        """Emit once when a condition starts (on) and once when it clears (off)."""
        was = self._flags.get(key, False)
        if active and not was and on:
            self.emit(on[0], on[1], key)
        elif not active and was and off:
            self.emit(off[0], off[1], key)
        self._flags[key] = active

    def recent(self, n: int = 20) -> list[dict]:
        return [e.as_dict() for e in list(self.events)[-n:]]

    # -- watchdog ----------------------------------------------------------

    def names(self) -> dict:
        def nm(section, fallback):
            return (self.cfg.get(section) or {}).get("name") or fallback
        return {"gnss": nm("gnss", "GNSS"), "laser": nm("laser_altimeter", "Laser altimeter"),
                "imu": nm("imu_ahrs", "Attitude sensor")}

    def evaluate(self, s, log_dir: Path, fallback_logging: bool, now: float | None = None):
        """Run every rule once. `s` is the TelemetryState."""
        now = now or time.time()
        warmup = now - self.started < 10.0
        names = self.names()
        configured = self.configured

        for key, ok in (("gnss", s.gnss_ok), ("laser", s.laser_ok), ("imu", s.imu_ok)):
            if not configured.get(key, True):
                continue
            seen = self._flags.get(f"{key}.ever", False) or ok
            self._flags[f"{key}.ever"] = seen
            if seen:
                self._edge(f"{key}.lost", not ok, ("alert", f"{names[key]} lost"), ("info", f"{names[key]} back"))
            elif not warmup:
                self._edge(f"{key}.silent", True, ("warn", f"{names[key]}: no data yet. Check port and baud."), None)

        if s.gnss_ok:
            self._edge("gnss.nofix", s.gps_fix == 0, ("warn", "GNSS has no position fix"), ("info", "GNSS fix acquired"))
            self._edge("gnss.fewsats", s.gps_fix > 0 and 0 <= s.sats < 6,
                       ("warn", f"Only {s.sats} satellites"), ("info", "Satellite count recovered"))

        warn_ft = float(s.low_agl_warn_ft)
        low = self._flags.get("agl.low", False)
        if s.laser_ok:
            if not low and s.agl_ft < warn_ft and s.agl_ft > 0:
                self._edge("agl.low", True, ("warn", f"Below {warn_ft:g} feet AGL"), None)
            elif low and s.agl_ft > warn_ft + 20:
                self._edge("agl.low", False, None, ("info", f"Back above {warn_ft:g} feet AGL"))

        self._edge("log.stopped", self._flags.get("log.ever", False) and not s.logging_active,
                   ("alert", "Logging stopped"), ("info", "Logging resumed"))
        self._flags["log.ever"] = self._flags.get("log.ever", False) or s.logging_active
        if fallback_logging and not s.sim_mode:
            self._edge("log.sdcard", True, ("warn", "Logging to the SD card: SSD not mounted"), None)

        ps = self.cfg.get("payload_sync", {})
        if ps.get("enabled"):
            count, since = self._trigger_seen
            if s.trigger_count != count:
                since = now
                self._trigger_seen = (s.trigger_count, now)
            period = 1.0 / float(ps.get("trigger_hz", 1.0))
            # Generous margin: a busy Pi can pause the event loop for a few seconds.
            stalled = since and (now - since) > max(8.0, 5 * period)
            self._edge("trigger.stalled", bool(stalled), ("warn", "Payload trigger stalled"),
                       ("info", "Payload trigger running"))

        h = self._slow
        if h.get("disk_free_gb") is not None:
            free = h["disk_free_gb"]
            self._edge("disk.low", free < 2.0, ("warn", f"Log disk low: {free:.1f} GB free"), None)
            self._edge("disk.critical", free < 0.5, ("alert", f"Log disk almost full: {free:.2f} GB"), None)
        if h.get("cpu_c") is not None:
            self._edge("cpu.hot", h["cpu_c"] >= 80, ("warn", f"Pi is hot: {h['cpu_c']:.0f} °C"),
                       ("info", "Pi temperature normal"))
        if h.get("undervoltage_now"):
            self._edge("power.uv", True, ("alert", "Pi undervoltage: check the power supply"), None)
        else:
            self._edge("power.uv", False, None, ("info", "Pi supply voltage OK"))

    # -- ground check ------------------------------------------------------

    def ground_check(self, s, cfg: dict, log_dir: Path, fallback_logging: bool) -> list[dict]:
        names = self.names()
        configured = self.configured
        out = []

        def add(item, status, detail):
            out.append({"item": item, "status": status, "detail": detail})

        if configured.get("gnss", True):
            if not s.gnss_ok:
                add(names["gnss"], "fail", "no data")
            elif s.gps_fix == 0:
                add(names["gnss"], "fail", f"no fix ({s.sats} satellites)")
            elif s.sats < 0:
                add(names["gnss"], "ok", "fix (this receiver does not report a satellite count)")
            elif s.sats < 6:
                add(names["gnss"], "warn", f"fix, but only {s.sats} satellites")
            else:
                add(names["gnss"], "ok", f"fix, {s.sats} satellites")

        if configured.get("laser", True):
            if not s.laser_ok:
                add(names["laser"], "fail", "no data")
            elif s.agl_m <= 0.5:
                add(names["laser"], "ok", f"reads {s.agl_m:.2f} m AGL on the ground: mounting offset looks right")
            elif s.agl_m <= 3.0:
                add(names["laser"], "warn",
                    f"reads {s.agl_m:.2f} m AGL. On the ground this should be about 0: check mounting_offset_m")
            else:
                add(names["laser"], "ok", f"reading {s.agl_m:.1f} m (airborne or not level)")

        if configured.get("imu", True):
            if not s.imu_ok:
                add(names["imu"], "fail", "no data")
            elif getattr(s, "gs_ms", 0) > 10 or s.agl_m > 10:
                add(names["imu"], "ok", f"roll {s.roll_deg:.1f}°, pitch {s.pitch_deg:.1f}° (airborne: the level check is for the ramp)")
            elif abs(s.roll_deg) > 5:
                add(names["imu"], "warn", f"roll {s.roll_deg:.1f}°: expected near 0 on the ramp")
            else:
                add(names["imu"], "ok", f"roll {s.roll_deg:.1f}°, pitch {s.pitch_deg:.1f}°, heading {s.yaw_deg:.0f}°")

        if not s.logging_active:
            add("Logging", "fail", "not writing")
        elif fallback_logging and not s.sim_mode:
            add("Logging", "warn", f"writing to the SD card ({log_dir}); mount the SSD")
        else:
            add("Logging", "ok", f"writing {s.log_file}")

        h = system_health(log_dir)
        if h.get("disk_free_gb") is not None:
            free = h["disk_free_gb"]
            add("Log disk", "ok" if free >= 2 else ("warn" if free >= 0.5 else "fail"), f"{free:.1f} GB free")

        ps = cfg.get("payload_sync", {})
        if ps.get("enabled"):
            if s.trigger_count == 0:
                add("Payload trigger", "fail", "no pulses yet")
            elif not s.gpio_live:
                add("Payload trigger", "warn", f"{s.trigger_count} pulses, but no GPIO pin is being driven")
            else:
                add("Payload trigger", "ok", f"{s.trigger_count} pulses on BCM {ps.get('gpio_pin')}")

        if h.get("cpu_c") is not None:
            c = h["cpu_c"]
            add("Pi temperature", "ok" if c < 75 else ("warn" if c < 82 else "fail"), f"{c:.0f} °C")
        if h.get("throttled") is not None:
            if h.get("undervoltage_now"):
                add("Pi power", "fail", "undervoltage right now")
            elif h.get("undervoltage_ever"):
                add("Pi power", "warn", "undervoltage happened since boot")
            else:
                add("Pi power", "ok", "no undervoltage since boot")

        if s.gnss_ok and s.gps_fix > 0 and getattr(s, "gnss_time_offset_s", None) is not None:
            off = s.gnss_time_offset_s
            add("Clock vs GNSS", "ok" if abs(off) < 1 else "warn",
                f"system clock is {off:+.1f} s from GNSS time" + ("" if abs(off) < 1 else ": timestamps will be off"))

        if s.sim_mode:
            add("Mode", "warn", "SIMULATION: these are not real sensors")
        return out

    # -- ask ---------------------------------------------------------------

    def context(self, s, cfg: dict, stats: dict) -> dict:
        state = {k: v for k, v in s.__dict__.items() if not k.startswith("_")}
        hw = {}
        for sect, label in (("gnss", "gnss"), ("laser_altimeter", "laser"), ("imu_ahrs", "attitude")):
            c = cfg.get(sect) or {}
            hw[label] = {"model": c.get("name", "generic"), "driver": c.get("driver"), "port": c.get("port")}
        hw["payloads"] = [p.get("name") for p in cfg.get("payloads", [])]
        return {"live": state, "this_flight": stats, "recent_callouts": self.recent(10), "hardware": hw,
                "names": self.names(), "system": self._slow or system_health(None),
                "diagnosis": self.diagnosis() if callable(self.diagnosis) else ""}

    async def ask_stream(self, question: str, ctx: dict):
        """Yield answer text chunks.

        Questions the rules answer exactly (counts, AGL, fix, logging, and anything
        that sounds like flight advice) are answered at once without a model. The
        rest go to Ollama when it is reachable, else get the rule-based status.
        """
        quick = rule_answer(question, ctx, strict=True)
        if quick:
            yield quick
            return
        bc = self.cfg.get("buddy", {})
        url = bc.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
        model = bc.get("model", "qwen3.5:2b")
        if bc.get("ai", True):
            body = {
                "model": model, "stream": True, "think": False,
                "keep_alive": bc.get("keep_alive", "60m"),
                "options": {"temperature": 0.2, "num_predict": 160, "num_ctx": 2048},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT + "\n\nDATA:\n" + context_text(ctx)},
                    {"role": "user", "content": question},
                ],
            }
            sent = False
            try:
                timeout = float(bc.get("timeout_s", 120))
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=2.0)) as client:
                    async with client.stream("POST", f"{url}/api/chat", json=body) as r:
                        if r.status_code == 200:
                            async for line in r.aiter_lines():
                                if not line.strip():
                                    continue
                                msg = json.loads(line)
                                chunk = (msg.get("message") or {}).get("content", "")
                                if chunk:
                                    sent = True
                                    yield chunk
                                if msg.get("done"):
                                    return
                            return
            except (httpx.HTTPError, json.JSONDecodeError, OSError):
                if sent:
                    yield " [answer cut off: the model timed out]"
                    return
            yield "(The AI model did not answer, so here is the plain status.) "
        yield rule_answer(question, ctx)

    async def refresh_slow(self, log_dir: Path):
        """Disk, temperature and power checks, off the event loop."""
        import asyncio
        self._slow = await asyncio.to_thread(system_health, log_dir)

    async def warm_model(self):
        """Load the model into memory now so the first question is not a cold start."""
        bc = self.cfg.get("buddy", {})
        if not bc.get("ai", True):
            return
        url = bc.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=2.0)) as client:
                await client.post(f"{url}/api/generate", json={
                    "model": bc.get("model", "qwen3.5:2b"), "keep_alive": bc.get("keep_alive", "60m")})
        except (httpx.HTTPError, OSError):
            pass

    async def ai_status(self) -> dict:
        bc = self.cfg.get("buddy", {})
        if not bc.get("ai", True):
            return {"available": False, "reason": "AI turned off in settings"}
        url = bc.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
        model = bc.get("model", "qwen3.5:2b")
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{url}/api/tags")
                names = [m["name"] for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError, OSError):
            return {"available": False, "reason": "Ollama not running", "model": model}
        if model not in names and f"{model}:latest" not in names:
            return {"available": False, "reason": f"model {model} not installed", "model": model}
        return {"available": True, "model": model}


def rule_answer(question: str, ctx: dict, strict: bool = False) -> str | None:
    """Plain answers without a model. Covers the questions people actually ask.

    strict=True returns None unless a rule clearly matches, so open questions
    can go to the model instead.
    """
    q = " " + question.lower() + " "
    s, f = ctx["live"], ctx["this_flight"]
    names = ctx.get("names") or {"gnss": "GNSS", "laser": "Laser altimeter", "imu": "Attitude sensor"}

    def status_line():
        parts = []
        for key, ok in (("gnss", s["gnss_ok"]), ("laser", s["laser_ok"]), ("imu", s["imu_ok"])):
            parts.append(f"{names[key] or key}: {'OK' if ok else 'NO DATA'}")
        return "; ".join(parts) + "."

    if any(w in q for w in (" weather", " land", " descend", " climb", " turn ", " fuel", "should i",
                            " fly ", " go around", " divert", " safe to", " ice on", " icing")):
        return "That's the pilot's call; I only watch the instruments. " + status_line()
    if any(w in q for w in ("wrong", "problem", "troubleshoot", "diagnos", "not working", "isn't working", "issue",
                            "how do i fix", "how to fix", "ready to fly", "are we ready", "anything to check",
                            "no data", "not reading", "broken")):
        return ctx.get("diagnosis") or status_line()
    if " why" in q and any(w in q for w in ("drop", "lost", "lose", "cut out", "fail", "stop", "gap")):
        return why_answer(q, ctx)
    if any(w in q for w in ("how many trigger", "trigger count", "triggers so far", "how many photo",
                            "how many picture", "how many capture")):
        return f"{s['trigger_count']} payload triggers so far this session."
    if any(w in q for w in (" agl", "how high", "height above", "what altitude", "my altitude")):
        if not s["laser_ok"]:
            return "No laser data right now, so no AGL."
        line = f"{s['agl_ft']:.0f} ft AGL ({s['agl_m']:.1f} m), {s['alt_msl_m']:.0f} m MSL."
        if f.get("agl_ft_min") is not None:
            line += f" This flight: {f['agl_ft_min']:.0f} to {f['agl_ft_max']:.0f} ft."
        return line
    if any(w in q for w in (" gps", " gnss", " fix", "satellite", "position", "where am i")):
        if not s["gnss_ok"]:
            return "No GNSS data right now."
        return f"Fix quality {s['gps_fix']} with {s['sats']} satellites at {s['lat']:.5f}, {s['lon']:.5f}."
    if any(w in q for w in (" log", " disk", "recording", "saving", " ssd", "storage")):
        free = ctx["system"].get("disk_free_gb")
        extra = f" {free:.1f} GB free." if free is not None else ""
        return (f"Logging {'is on' if s['logging_active'] else 'is OFF'}, file {s['log_file'] or 'none'} in "
                f"{s['log_dir']}.{extra} {f.get('rows', 0)} samples this session.")
    if any(w in q for w in ("summar", "so far", "how long", "status")):
        mins = f.get("duration_min") or 0
        return (f"{mins:.0f} min logged, {f.get('rows', 0)} samples, {s['trigger_count']} triggers, "
                f"{f.get('callouts', 0)} callouts. " + status_line())
    if strict:
        return None
    return status_line() + " Ask about AGL, GNSS, triggers, logging, or a flight summary."


LIKELY_CAUSES = {
    "laser": "no return over open water, glassy ice or fresh snow at a shallow angle, beyond the laser's rated "
             "range, cloud or fog under the aircraft, or a loose cable",
    "gnss": "antenna shadowed in a steep bank, interference from other avionics, or a loose antenna cable",
    "imu": "a loose cable or the sensor resetting on a power dip",
}


def why_answer(q: str, ctx: dict) -> str:
    """What the callout log shows, plainly, without inventing a cause."""
    names = ctx.get("names") or {}
    which = None
    for key, words in (("laser", ("laser", "agl", "altimeter", "lidar")), ("gnss", ("gnss", "gps", "fix")),
                       ("imu", ("imu", "attitude", "ahrs", "ins"))):
        if any(w in q for w in words):
            which = key
    evs = ctx.get("recent_callouts") or []
    keys = [which] if which else ["laser", "gnss", "imu"]
    lines = []
    for key in keys:
        nm = names.get(key, key)
        lost = [e for e in evs if e["text"] == f"{nm} lost"]
        back = [e for e in evs if e["text"] == f"{nm} back"]
        if not lost:
            continue
        last_lost = lost[-1]
        t = time.strftime("%H:%M:%S", time.gmtime(last_lost["ts"]))
        after = [e for e in back if e["ts"] >= last_lost["ts"]]
        if after:
            dur = after[0]["ts"] - last_lost["ts"]
            lines.append(f"{nm} was lost at {t}Z for {dur:.0f} s, {len(lost)} time(s) in recent callouts.")
        else:
            lines.append(f"{nm} was lost at {t}Z and has not come back.")
        lines.append(f"The data shows when, not why. Common causes: {LIKELY_CAUSES[key]}.")
    if not lines:
        return "No dropouts in the recent callouts."
    return " ".join(lines)


def context_text(ctx: dict) -> str:
    """Short plain-text summary for the model: small prompt, fast on a Pi CPU."""
    s, f, n, sysh = ctx["live"], ctx["this_flight"], ctx.get("names", {}), ctx.get("system", {})
    lines = []
    if s.get("sim_mode"):
        lines.append("MODE: SIMULATION (not real sensors).")
    lines.append(f"{n.get('laser', 'Laser')}: " + (
        f"AGL {s['agl_ft']:.0f} ft ({s['agl_m']:.1f} m), raw range {s['laser_raw_m']:.1f} m"
        if s["laser_ok"] else "NO DATA"))
    lines.append(f"{n.get('gnss', 'GNSS')}: " + (
        f"fix {s['gps_fix']}, {s['sats']} satellites, {s['lat']:.5f},{s['lon']:.5f}, {s['alt_msl_m']:.0f} m MSL"
        if s["gnss_ok"] else "NO DATA"))
    lines.append(f"{n.get('imu', 'Attitude')}: " + (
        f"roll {s['roll_deg']:.1f}, pitch {s['pitch_deg']:.1f}, heading {s['yaw_deg']:.0f} deg"
        if s["imu_ok"] else "NO DATA"))
    lines.append(f"Payload triggers this session: {s['trigger_count']}"
                 + ("" if s.get("gpio_live") else " (no GPIO pin driven)"))
    lines.append(f"Logging: {'on' if s['logging_active'] else 'OFF'}, file {s['log_file']}, dir {s['log_dir']}"
                 + (f", {sysh['disk_free_gb']:.1f} GB free" if sysh.get("disk_free_gb") is not None else ""))
    if sysh.get("cpu_c") is not None:
        lines.append(f"Pi: {sysh['cpu_c']:.0f} C, undervoltage now: {bool(sysh.get('undervoltage_now'))}")
    if f:
        lines.append(f"This flight: {f.get('duration_min', 0)} min, {f.get('rows', 0)} samples, AGL range "
                     f"{f.get('agl_ft_min') or 0:.0f}-{f.get('agl_ft_max') or 0:.0f} ft, {f.get('callouts', 0)} callouts")
    hw = ctx.get("hardware", {})
    if hw.get("payloads"):
        lines.append("Payloads: " + ", ".join(p for p in hw["payloads"] if p))
    if ctx.get("diagnosis"):
        lines.append("Diagnosis (problems found, with fixes):")
        lines.append(ctx["diagnosis"])
    ev = ctx.get("recent_callouts") or []
    if ev:
        lines.append("Recent callouts (oldest first):")
        for e in ev[-8:]:
            t = time.strftime("%H:%M:%S", time.gmtime(e["ts"]))
            lines.append(f"  {t}Z {e['level']}: {e['text']}")
    return "\n".join(lines)


def system_health(log_dir: Path | None) -> dict:
    h: dict = {}
    try:
        h["cpu_c"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        h["cpu_c"] = None
    if log_dir is not None:
        try:
            du = shutil.disk_usage(log_dir)
            h["disk_free_gb"] = du.free / 1e9
        except OSError:
            h["disk_free_gb"] = None
    if shutil.which("vcgencmd"):
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
            val = int(out.strip().split("=")[1], 16)
            h["throttled"] = val
            h["undervoltage_now"] = bool(val & 0x1)
            h["undervoltage_ever"] = bool(val & 0x10000)
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
    return h


def flight_stats(db_path: Path, since: float) -> dict:
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*), MIN(unix_ts), MAX(unix_ts), MIN(agl_ft), MAX(agl_ft), AVG(sats) "
            "FROM telemetry WHERE unix_ts >= ? AND laser_ok = 1", (since,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM telemetry WHERE unix_ts >= ?", (since,)).fetchone()[0]
        try:
            callouts = conn.execute("SELECT COUNT(*) FROM events WHERE unix_ts >= ?", (since,)).fetchone()[0]
        except sqlite3.Error:
            callouts = 0
        conn.close()
    except sqlite3.Error:
        return {}
    n, t0, t1, amin, amax, sats = row
    return {
        "rows": total,
        "duration_min": round(((t1 or 0) - (t0 or 0)) / 60.0, 1) if t0 else 0,
        "agl_ft_min": amin, "agl_ft_max": amax,
        "avg_sats": round(sats, 1) if sats is not None else None,
        "callouts": callouts,
    }
