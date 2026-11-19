"""Surveys: one button to start, confirm, stop and save a science run.

AeroLab-Pi records whenever the instruments are powered: if START is never
pressed, nothing is lost. A survey marks the window that matters and runs the
things that need running:

  START  1. readiness check (the same findings as Buddy > Checks): go / no-go
         2. camera capture on (RedEdge-P /config auto_cap_mode "timer"), if the
            camera is on Ethernet and control is enabled
         3. marks the survey start in the flight database
         4. confirms, within ~15 s, that every instrument is really recording
            into this survey: new laser readings, new GNSS fixes with a fix,
            new camera exposures, rows reaching the disk, raw bytes growing
  STOP   1. camera capture off
         2. marks the end; flushes the reading queue; fsyncs the raw files
         3. builds the survey package: database snapshot, laser points and
            exposures on GNSS time, camera status, raw files, readiness report,
            and a manifest with a SHA-256 for every file
         4. copies the package to every backup drive and verifies each copy

The Reach M2's own raw log has no documented remote control: set it to start
logging on power-up in Emlid Flow. The Pi confirms the GNSS stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SURVEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS surveys (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER, name TEXT,
    start_utc_ns INTEGER, start_mono_ns INTEGER, stop_utc_ns INTEGER, stop_mono_ns INTEGER,
    readiness TEXT, confirmation TEXT, summary TEXT, package TEXT);
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SurveyManager:
    def __init__(self, daemon):
        self.d = daemon
        self.state = "idle"            # idle | starting | recording | stopping
        self.current: dict | None = None
        self.last: dict | None = None
        self.message = ""
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.d.db_path)
        conn.executescript(SURVEY_SCHEMA)
        conn.commit()
        conn.close()

    # -- counts used for confirmation and the summary ---------------------------
    def counts(self) -> dict:
        d = self.d
        raw = {k: w.bytes for k, w in d.raw.items()}
        return {"laser": d.monitors["laser"].total, "laser_valid": d.laser_valid_total,
                "gnss": d.monitors["gnss"].total, "exposures": d.camera.count,
                "rows_written": d.readings.written, "raw_bytes": sum(raw.values()), "raw": raw,
                "dropped": d.readings.dropped}

    def status(self) -> dict:
        cur = self.current
        out = {"state": self.state, "message": self.message, "last": self.last}
        if cur:
            now_c = self.counts()
            c0 = cur["counts_start"]
            out["current"] = {
                "id": cur["id"], "name": cur["name"], "elapsed_s": round(time.time() - cur["start_utc_ns"] / 1e9, 1),
                "readiness": cur["readiness"], "confirmation": cur.get("confirmation"),
                "camera_command": cur.get("camera_command"),
                "since_start": {"laser_readings": now_c["laser"] - c0["laser"],
                                "laser_valid": now_c["laser_valid"] - c0["laser_valid"],
                                "gnss_fixes": now_c["gnss"] - c0["gnss"],
                                "exposures": now_c["exposures"] - c0["exposures"],
                                "rows_written": now_c["rows_written"] - c0["rows_written"],
                                "raw_bytes": now_c["raw_bytes"] - c0["raw_bytes"]}}
        return out

    # -- camera control ------------------------------------------------------------
    async def camera_capture(self, on: bool) -> dict:
        cam = self.d.camera
        cc = self.d.cfg.get("camera") or {}
        if not cam.enabled or not cc.get("control_capture") or not cam.status_url:
            return {"done": False, "detail": "camera not under Pi control: start/stop its capture on the camera"}
        if self.d.sim_mode:
            return {"done": True, "detail": "simulated"}
        base = cam.status_url.rsplit("/status", 1)[0]
        period = round(1.0 / cam.expected_hz, 3) if cam.expected_hz else 1.0
        body = {"auto_cap_mode": "timer", "timer_period": period} if on else {"auto_cap_mode": "disabled"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.post(f"{base}/config", json=body)
                ok = r.status_code < 300
            return {"done": ok, "detail": f"POST /config {body} -> HTTP {r.status_code}"}
        except Exception as exc:
            return {"done": False, "detail": f"camera not reachable: {exc}"}

    # -- start ---------------------------------------------------------------------
    def readiness(self) -> dict:
        import diagnose
        fs = diagnose.findings(self.d, self.d.catalog)
        return {"verdict": diagnose.readiness(fs), "findings": fs}

    async def start(self, name: str = "", force: bool = False) -> dict:
        if self.state in ("starting", "recording"):
            return {"ok": False, "error": f"a survey is already {self.state}"}
        rd = self.readiness()
        if rd["verdict"]["level"] == "red" and not force:
            return {"ok": False, "needs_confirmation": True, "readiness": rd}
        self.state = "starting"
        utc, mono = time.time_ns(), time.monotonic_ns()
        conn = sqlite3.connect(self.d.db_path)
        conn.executescript(SURVEY_SCHEMA)
        cur = conn.execute("INSERT INTO surveys (flight_id, name, start_utc_ns, start_mono_ns, readiness) "
                           "VALUES (?,?,?,?,?)", (self.d.state.flight_id, name, utc, mono,
                                                  json.dumps(rd["verdict"])))
        conn.commit()
        sid = cur.lastrowid
        conn.close()
        name = name or f"Survey {sid}"
        self.current = {"id": sid, "name": name, "start_utc_ns": utc, "start_mono_ns": mono,
                        "counts_start": self.counts(), "readiness": rd["verdict"], "forced": force}
        self.current["camera_command"] = await self.camera_capture(True)
        self.d.buddy.emit("info", f"{name} started" + (" (readiness overridden)" if force and
                                                       rd["verdict"]["level"] == "red" else ""))
        self.state = "recording"
        self.message = "Confirming every instrument is recording..."
        asyncio.create_task(self._confirm(sid))
        return {"ok": True, "survey": self.status()}

    async def _confirm(self, sid: int, window_s: float = 15.0):
        """Watch the first seconds of the survey: is everything really landing in it?"""
        d = self.d
        conf = d.buddy.configured
        deadline = time.time() + window_s
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            if not self.current or self.current["id"] != sid:
                return
            items = self._confirmation_items()
            if all(i["ok"] for i in items):
                break
        if not self.current or self.current["id"] != sid:
            return
        items = self._confirmation_items()
        self.current["confirmation"] = items
        bad = [i for i in items if not i["ok"]]
        if bad:
            self.message = "Recording, but NOT confirmed: " + "; ".join(f"{i['item']}: {i['detail']}" for i in bad)
            d.buddy.emit("alert", f"{self.current['name']}: not everything is recording "
                                  f"({', '.join(i['item'] for i in bad)})")
        else:
            self.message = "Confirmed: everything is recording"
            d.buddy.emit("info", f"{self.current['name']}: confirmed, everything is recording")
        conn = sqlite3.connect(d.db_path)
        conn.execute("UPDATE surveys SET confirmation=? WHERE id=?", (json.dumps(items), sid))
        conn.commit()
        conn.close()

    def _confirmation_items(self) -> list[dict]:
        d, cur = self.d, self.current
        c0, c1 = cur["counts_start"], self.counts()
        conf = d.buddy.configured
        items = []

        def add(item, ok, detail):
            items.append({"item": item, "ok": bool(ok), "detail": detail})

        names = d.buddy.names()
        if conf.get("laser"):
            n, v = c1["laser"] - c0["laser"], c1["laser_valid"] - c0["laser_valid"]
            add(names["laser"], n > 0, f"{n} readings ({v} with a ground return)" if n else "no readings since start")
        if conf.get("gnss"):
            n = c1["gnss"] - c0["gnss"]
            add(names["gnss"], n > 0 and d.state.gps_fix > 0,
                f"{n} fixes, fix quality {d.state.gps_fix}" if n else "no fixes since start")
        if d.camera.enabled:
            n = c1["exposures"] - c0["exposures"]
            add(d.camera.name, n > 0, f"{n} exposures" if n else "no exposures since start: is the camera capturing?")
        n = c1["rows_written"] - c0["rows_written"]
        add("Disk", n > 0 and d.readings.healthy, f"{n} rows saved to {d.log_dir}" if n else "nothing saved yet")
        if not d.sim_mode and d.raw:
            n = c1["raw_bytes"] - c0["raw_bytes"]
            add("Raw capture", n > 0, f"{n:,} bytes" if n else "no raw bytes since start")
        return items

    # -- stop ------------------------------------------------------------------------
    async def stop(self) -> dict:
        if self.state != "recording" or not self.current:
            return {"ok": False, "error": "no survey is recording"}
        self.state = "stopping"
        self.message = "Stopping: camera off, saving and backing up..."
        cur = self.current
        cam = await self.camera_capture(False)
        utc, mono = time.time_ns(), time.monotonic_ns()
        await self.d.flush_all()
        c1 = self.counts()
        c0 = cur["counts_start"]
        summary = {"id": cur["id"], "name": cur["name"], "flight_id": self.d.state.flight_id,
                   "start_utc_ns": cur["start_utc_ns"], "stop_utc_ns": utc,
                   "duration_s": round((utc - cur["start_utc_ns"]) / 1e9, 1),
                   "laser_readings": c1["laser"] - c0["laser"], "laser_valid": c1["laser_valid"] - c0["laser_valid"],
                   "gnss_fixes": c1["gnss"] - c0["gnss"], "exposures": c1["exposures"] - c0["exposures"],
                   "raw_bytes": c1["raw_bytes"] - c0["raw_bytes"], "readings_lost": c1["dropped"] - c0["dropped"],
                   "readiness_at_start": cur["readiness"], "confirmation": cur.get("confirmation"),
                   "camera_start": cur.get("camera_command"), "camera_stop": cam, "forced": cur.get("forced")}
        conn = sqlite3.connect(self.d.db_path)
        conn.execute("UPDATE surveys SET stop_utc_ns=?, stop_mono_ns=?, summary=? WHERE id=?",
                     (utc, mono, json.dumps(summary), cur["id"]))
        conn.commit()
        conn.close()
        try:
            package = await asyncio.to_thread(self.build_package, cur, mono, summary)
        except Exception as exc:
            package = {"ok": False, "error": str(exc)}
        summary["package"] = package
        conn = sqlite3.connect(self.d.db_path)
        conn.execute("UPDATE surveys SET package=? WHERE id=?", (json.dumps(package), cur["id"]))
        conn.commit()
        conn.close()
        self.last = summary
        self.current = None
        self.state = "idle"
        ok = package.get("ok")
        copies = package.get("copies", [])
        verified = sum(1 for c in copies if c.get("verified"))
        self.message = (f"Saved: {package.get('files', 0)} files, {package.get('bytes', 0) / 1e6:.1f} MB, "
                        f"verified on {verified} of {len(copies)} drive(s)" if ok else
                        f"Saved to the log disk, but packaging failed: {package.get('error')}")
        self.d.buddy.emit("info" if ok and verified == len(copies) else "alert",
                          f"{summary['name']} stopped. {self.message}")
        return {"ok": True, "summary": summary}

    def build_package(self, cur: dict, stop_mono_ns: int, summary: dict) -> dict:
        """Survey folder with everything needed for processing, a SHA-256 manifest, copied and verified."""
        d = self.d
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(cur["start_utc_ns"] / 1e9))
        pkg = d.log_dir / "surveys" / f"survey_{cur['id']:03d}_{stamp}"
        pkg.mkdir(parents=True, exist_ok=True)
        # 1. a consistent snapshot of the whole flight database
        src = sqlite3.connect(d.db_path)
        dst = sqlite3.connect(pkg / "aerolab_flight.db")
        src.backup(dst)
        dst.close()
        src.close()
        # 2. survey window exports on GNSS time
        sys.path.insert(0, str(ROOT / "tools"))
        notes = {}
        win = ["--db", str(pkg / "aerolab_flight.db"), "--flight", str(d.state.flight_id),
               "--from-mono-ns", str(cur["start_mono_ns"]), "--to-mono-ns", str(stop_mono_ns)]
        try:
            import export_laser
            off = d.mounting_offset()
            notes["laser"] = export_laser.main(win + ["--offset-m", str(off), "--out", str(pkg / "laser_points.csv")])
        except SystemExit as e:
            notes["laser"] = str(e)
        if d.camera.enabled:
            try:
                import export_exposures
                notes["exposures"] = export_exposures.main(win + ["--out", str(pkg / "exposures.csv")])
            except SystemExit as e:
                notes["exposures"] = str(e)
            conn = sqlite3.connect(pkg / "aerolab_flight.db")
            rows = conn.execute("SELECT utc_ns, status FROM camera_status WHERE flight_id=? AND utc_ns BETWEEN ? AND ?",
                                (d.state.flight_id, cur["start_utc_ns"], summary["stop_utc_ns"])).fetchall()
            conn.close()
            (pkg / "camera_status.jsonl").write_text("".join(json.dumps({"utc_ns": u, **json.loads(st)}) + "\n"
                                                             for u, st in rows))
        # 3. raw captures that overlap the survey
        raw_dir = d.log_dir / "raw"
        if raw_dir.exists():
            (pkg / "raw").mkdir(exist_ok=True)
            t0 = cur["start_utc_ns"] / 1e9 - 3600
            for f in raw_dir.glob("*.aeroraw"):
                if f.stat().st_mtime >= t0:
                    shutil.copy2(f, pkg / "raw" / f.name)
        # 4. readiness report, summary, manifest
        (pkg / "readiness.html").write_text(d.report_html())
        (pkg / "survey.json").write_text(json.dumps({**summary, "exports": notes,
                                                     "config": d.cfg, "calibration": d.cal.summary()},
                                                    indent=2, default=str))
        files = sorted(p for p in pkg.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256")
        lines = [f"{sha256(p)}  {p.relative_to(pkg)}" for p in files]
        (pkg / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
        total = sum(p.stat().st_size for p in files)
        # 5. copy to every backup drive and verify
        copies = [{"dir": str(pkg), "verified": verify(pkg)}]
        for m in d.mirror_dirs:
            target = Path(m) / "surveys" / pkg.name
            try:
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(pkg, target)
                copies.append({"dir": str(target), "verified": verify(target)})
            except OSError as exc:
                copies.append({"dir": str(target), "verified": False, "error": str(exc)})
        return {"ok": True, "dir": str(pkg), "files": len(files) + 1, "bytes": total, "copies": copies}


def verify(pkg: Path) -> bool:
    """Re-read every file and check it against MANIFEST.sha256."""
    man = pkg / "MANIFEST.sha256"
    if not man.exists():
        return False
    for line in man.read_text().splitlines():
        if not line.strip():
            continue
        h, _, rel = line.partition("  ")
        p = pkg / rel
        if not p.exists() or sha256(p) != h:
            return False
    return True
