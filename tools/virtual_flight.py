"""Virtual flight: the real daemon against simulated serial sensors with a known truth.

For each profile it:
  1. builds the config the setup menus would write, with the sensors on virtual
     serial ports (ptys) speaking their real protocols;
  2. starts the daemon as a separate process, exactly as the service does;
  3. parks the aircraft and runs a ground calibration through the API, then applies it;
  4. flies a survey leg with +/-20 deg banks over flat ground;
  5. scores everything the daemon logged against the truth: AGL, attitude,
     position, trigger geotags, plus the self-test, calibration recovery,
     corrupted-frame rejection and laser spike filtering;
  6. fits the laser boresight from the flight (tools/boresight.py method) and
     re-scores AGL with it.

The simulated installation has deliberate errors (IMU bias, wrong hand-entered
laser offset, 1 deg laser boresight, 2% corrupted frames, 1% laser spikes).

    python tools/virtual_flight.py                       # every profile
    python tools/virtual_flight.py airborne-snow-survey  # one
    python tools/virtual_flight.py --markdown docs/accuracy.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))
import calibration as calib  # noqa: E402
import configure  # noqa: E402
import integrity  # noqa: E402
import main as daemon_main  # noqa: E402
import sensor_sim as sim  # noqa: E402

DEFAULT_RATE = {"riegl_ld90": 10, "ascii_number": 10, "csv_rpy": 20, "nmea_ins": 10, "mavlink": 20}
ROLE_OF = {"gnss": "gnss", "laser_altimeter": "laser", "imu_ahrs": "imu"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, path, body=None, timeout=10):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(xs):
    a = [abs(x) for x in xs]
    return {"n": len(xs), "mean": statistics.fmean(xs) if xs else float("nan"),
            "p95": pct(a, 0.95), "max": max(a) if a else float("nan")}


def run_profile(profile_id: str, flight_s: float = 35.0, verbose: bool = True,
                inst: sim.Installation | None = None, keep: bool = False) -> dict:
    cat = configure.load_catalog()
    prof = configure.by_id(cat["profiles"])[profile_id]
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "live"), cat)
    inst = inst or sim.Installation()
    gdrv = cfg["gnss"].get("driver", "nmea")
    has_att = daemon_main.section_enabled(cfg, "imu_ahrs") or "attitude" in daemon_main.drivers.DRIVERS[gdrv]["kinds"]
    survey_m = float(prof.get("survey_agl_ft", 1000)) / 3.28084
    rng = float(cfg["laser_altimeter"].get("max_range_m") or 0)
    if rng:
        survey_m = min(survey_m, 0.92 * rng)         # fly where the laser can see the ground
    # Without an attitude sensor the survey legs are straight (attitude is applied in post-processing).
    traj = sim.Trajectory(survey_agl_m=survey_m, bank_deg=20.0 if has_att else 3.0)
    work = Path(tempfile.mkdtemp(prefix=f"aerolab-vf-{profile_id}-"))

    devices = []
    for sec in ("gnss", "laser_altimeter", "imu_ahrs"):
        if not daemon_main.section_enabled(cfg, sec):
            continue
        c = cfg[sec]
        rate = c.get("rate_hz") or DEFAULT_RATE.get(c["driver"], 10)
        dev = sim.VirtualDevice(c.get("name", sec), c["driver"], ROLE_OF[sec], float(rate), traj, inst,
                                seed=len(devices) + 1, device=c.get("device", ""),
                                max_range_m=float(c.get("max_range_m") or 0))
        c["port"] = dev.port
        devices.append(dev)

    cfg["logging"]["fallback_dir"] = str(work)
    cfg["logging"]["primary_dir"] = str(work / "no-ssd" / "x")
    cfg["calibration_file"] = str(work / "calibration.json")
    cfg["laser_altimeter"]["mounting_offset_m"] = 0.40          # the hand-entered (wrong) value
    cfg["payload_sync"].update(enabled=True, trigger_hz=2.0, method="gpio")
    cfg["buddy"]["ai"] = False
    # Never touch this machine's GPIOs or poll a camera on the local network from a virtual flight.
    cfg.pop("camera", None)
    cfg["mode"] = "live"
    port = free_port()
    cfg["server"]["port"] = port
    cfg_path = work / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))

    t0 = time.time()
    for d in devices:
        d.start(t0)
    proc = subprocess.Popen([sys.executable, str(ROOT / "daemon" / "main.py"), "--config", str(cfg_path)],
                            stdout=open(work / "daemon.log", "w"), stderr=subprocess.STDOUT)
    report: dict = {"profile": profile_id, "devices": {d.role: d.name for d in devices}, "workdir": str(work)}
    try:
        for _ in range(100):
            try:
                http(port, "/api/state")
                break
            except Exception:
                time.sleep(0.2)
        # Ground calibration while parked
        time.sleep(2.0)
        http(port, "/api/cal/ground", {"seconds": 8, "ref_roll": 0, "ref_pitch": 0,
                                       "field_elev_m": traj.ground_elev_m, "antenna_height_m": 1.5})
        cal = None
        for _ in range(60):
            cal = http(port, "/api/cal")
            if cal.get("capture", {}).get("done"):
                break
            time.sleep(0.5)
        result = cal["capture"]["result"]
        report["ground_cal"] = result
        if result["status"] != "fail":
            http(port, "/api/cal/apply", {})
        # wait for the self-test and the flight
        while time.time() - t0 < traj.park_s + flight_s:
            time.sleep(0.5)
        report["bit"] = http(port, "/api/bit")
        report["health"] = http(port, "/api/health")
        report["events"] = http(port, "/api/state")["_events"]
        cal_now = http(port, "/api/cal")["calibration"]
        report["calibration"] = cal_now
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for d in devices:
            d.close()
    report["sim"] = {d.role: {"frames": d.frames, "corrupted": d.corrupted, "spikes": d.spikes,
                              "received_bytes": len(d.received)} for d in devices}
    sent_logs = {d.role: d.sent_log for d in devices}

    # ---- score against the truth ----
    db = sqlite3.connect(work / "aerolab_flight.db")
    rows = db.execute("SELECT unix_ts, agl_m, roll_deg, pitch_deg, yaw_deg, lat, lon, laser_raw_m, "
                      "laser_used_m, laser_ok, imu_ok, gnss_ok, agl_quality, alt_msl_m FROM telemetry "
                      "ORDER BY unix_ts").fetchall()
    trig = db.execute("SELECT unix_ts, lat, lon, fix_lat, fix_lon, gnss_age_ms FROM payload_triggers "
                      "ORDER BY unix_ts").fetchall()
    db.close()
    settle = 3.0
    fl = [r for r in rows if r[0] - t0 > traj.park_s + settle and r[9] and r[11] and (r[10] or not has_att)]
    agl_err, agl_rel, roll_err, pitch_err, pos_err = [], [], [], [], []
    bore_samples = []
    for ts, agl, roll, pitch, yaw, lat, lon, raw, used, _lo, _io, _go, _q, alt in fl:
        tr = traj.at(ts - t0)
        agl_err.append(agl - tr["agl"])
        agl_rel.append(100.0 * (agl - tr["agl"]) / tr["agl"])
        roll_err.append(roll - tr["roll"])
        pitch_err.append(pitch - tr["pitch"])
        pos_err.append(integrity.haversine_m(lat, lon, tr["lat"], tr["lon"]))
        bore_samples.append((used, roll, pitch, alt, ts))
    ftrig = [t for t in trig if t[0] - t0 > traj.park_s + settle]
    geo_ext = [integrity.haversine_m(la, lo, traj.at(ts - t0)["lat"], traj.at(ts - t0)["lon"]) for ts, la, lo, *_ in ftrig]
    geo_fix = [integrity.haversine_m(fla, flo, traj.at(ts - t0)["lat"], traj.at(ts - t0)["lon"])
               for ts, _, _, fla, flo, _ in ftrig]
    report["score"] = {
        "agl_m": stats(agl_err), "agl_pct": stats(agl_rel), "roll_deg": stats(roll_err),
        "pitch_deg": stats(pitch_err), "position_m": stats(pos_err),
        "geotag_extrapolated_m": stats(geo_ext), "geotag_last_fix_m": stats(geo_fix),
        "survey_agl_m": traj.survey_agl_m,
    }

    # ---- boresight fit from the flight, then re-score AGL with it ----
    off = cal_now.get("laser_offset_m") or 0.4
    report["has_attitude"] = has_att
    if has_att:
        fit = calib.fit_boresight([(u, r, p, a) for u, r, p, a, _ in bore_samples], off)
        agl_err2 = []
        for (u, r, p, _a, ts) in bore_samples:
            h = daemon_main.compute_agl(u, r + fit["boresight_roll_deg"], p + fit["boresight_pitch_deg"], off)
            agl_err2.append(h - traj.at(ts - t0)["agl"])
        report["score"]["agl_after_boresight_m"] = stats(agl_err2)
    else:
        fit = None
        report["score"]["agl_after_boresight_m"] = report["score"]["agl_m"]
    report["boresight_fit"] = fit
    # ---- every laser reading logged, and matched to position on GNSS time ----
    import export_laser
    db_path = work / "aerolab_flight.db"
    conn = sqlite3.connect(db_path)
    n_laser = conn.execute("SELECT COUNT(*) FROM laser_readings").fetchone()[0]
    n_fix = conn.execute("SELECT COUNT(*) FROM gnss_fixes").fetchone()[0]
    conn.close()
    sent = report["sim"].get("laser", {}).get("frames", 0)
    corrupt = report["sim"].get("laser", {}).get("corrupted", 0)
    summ = export_laser.main(["--db", str(db_path), "--offset-m", str(off), "--out", str(work / "points.csv")])
    import csv as _csv
    perr = []
    with open(work / "points.csv") as f:
        for row in _csv.DictReader(f):
            if row["match"] != "ok":
                continue
            t = float(row["gnss_time_utc"])
            if t - t0 < traj.park_s + settle:
                continue
            tr = traj.at(t - t0)
            perr.append(integrity.haversine_m(float(row["lat"]), float(row["lon"]), tr["lat"], tr["lon"]))
    # completeness: frames sent while the daemon was reading (between its first and last logged laser reading)
    conn = sqlite3.connect(db_path)
    t_first, t_last = conn.execute("SELECT MIN(utc_ns), MAX(utc_ns) FROM laser_readings").fetchone()
    conn.close()
    window = [(t, c) for t, c in sent_logs.get("laser", []) if t_first / 1e9 <= t <= t_last / 1e9]
    clean_sent = sum(1 for _, c in window if not c)
    report["completeness"] = {"laser_clean_frames_sent": clean_sent, "laser_logged": n_laser,
                              "pct": 100.0 * min(n_laser, clean_sent) / clean_sent if clean_sent else 0.0}
    report["readings"] = {"laser_logged": n_laser, "laser_frames_sent": sent, "laser_frames_corrupted": corrupt,
                          "gnss_fixes_logged": n_fix, "matched": summ["matched"], "not_matched": summ["not_matched"],
                          "clock_method": summ["clock_model"]["method"],
                          "arrival_latency_ms": summ["clock_model"]["arrival_latency_ms"]}
    report["score"]["laser_point_position_m"] = stats(perr)
    report["truth"] = {"laser_offset_m": inst.laser_offset_m, "imu_roll_bias_deg": inst.imu_roll_bias_deg,
                       "imu_pitch_bias_deg": inst.imu_pitch_bias_deg, "boresight_roll_deg": inst.boresight_roll_deg,
                       "boresight_pitch_deg": inst.boresight_pitch_deg}
    if verbose:
        print_report(report)
    if not keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        report["workdir"] = None
    return report


def print_report(r: dict):
    s = r["score"]
    cal = r["calibration"]
    t = r["truth"]
    print(f"\n=== {r['profile']} ===  ({', '.join(f'{k}: {v}' for k, v in r['devices'].items())})")
    print(f"self-test: {r['bit']['status']}  "
          + "; ".join(f"{i['item']}={i['status']}" for i in r["bit"]["items"]))
    print(f"ground calibration: {r['ground_cal']['status']}")
    for i in r["ground_cal"]["items"]:
        print(f"   {i['status']:4} {i['item']}: {i['detail']}")
    print(f"recovered: laser offset {cal.get('laser_offset_m')} m (true {t['laser_offset_m']})"
          + (f", IMU bias roll {cal['imu_roll_bias_deg']:+.3f} (true {t['imu_roll_bias_deg']:+.2f}), "
             f"pitch {cal['imu_pitch_bias_deg']:+.3f} (true {t['imu_pitch_bias_deg']:+.2f})" if r["has_attitude"] else
             ", no attitude sensor (applied in post-processing)"))
    rd = r["readings"]
    print(f"readings: laser {rd['laser_logged']} logged / {rd['laser_frames_sent']} sent "
          f"({rd['laser_frames_corrupted']} corrupted on the wire); GNSS fixes {rd['gnss_fixes_logged']}; "
          f"matched {rd['matched']}, unmatched {rd['not_matched']}; clock: {rd['clock_method']}, "
          f"arrival latency median {rd['arrival_latency_ms']['median']:.1f} ms")
    c = r["completeness"]
    print(f"completeness: {c['laser_logged']} laser readings logged of {c['laser_clean_frames_sent']} clean frames sent "
          f"while the daemon was reading ({c['pct']:.2f}%)")
    for k in ("agl_m", "agl_pct", "roll_deg", "pitch_deg", "position_m", "geotag_last_fix_m",
              "geotag_extrapolated_m", "agl_after_boresight_m", "laser_point_position_m"):
        v = s[k]
        print(f"   {k:24} n={v['n']:4}  mean {v['mean']:+8.3f}  p95 {v['p95']:8.3f}  max {v['max']:8.3f}")
    b = r["boresight_fit"]
    if b is None:
        b = {"boresight_roll_deg": 0, "boresight_pitch_deg": 0, "pitch_fitted": False, "spread_before_m": 0,
             "spread_after_m": 0}
        print("boresight: not applicable without attitude")
        h = r["health"]
        print("health: " + "; ".join(f"{k} {v['hz']} Hz err {v['err_pct']}%" for k, v in h.items() if v["age_ms"] is not None))
        return
    print(f"boresight fit: roll {b['boresight_roll_deg']:+.2f}° (true {t['boresight_roll_deg']:+.2f}), "
          f"pitch {b['boresight_pitch_deg']:+.2f}° ({'fitted' if b['pitch_fitted'] else 'not observable'}); "
          f"height spread {b['spread_before_m']:.2f} -> {b['spread_after_m']:.2f} m")
    h = r["health"]
    print("health: " + "; ".join(f"{k} {v['hz']} Hz err {v['err_pct']}%" for k, v in h.items() if v["age_ms"] is not None))
    print("sim: " + "; ".join(f"{k} {v['frames']} frames, {v['corrupted']} corrupted, {v['spikes']} spikes"
                              for k, v in r["sim"].items()))


def markdown(reports: list[dict]) -> str:
    out = ["# Virtual flight accuracy report", "",
           "Generated by `python tools/virtual_flight.py --markdown docs/accuracy.md`.",
           "The real daemon ran, as a separate process, against simulated serial sensors speaking each device's "
           "protocol, with known truth.",
           "The simulated installation had deliberate errors: IMU bias (+0.8° roll, -0.5° pitch), laser offset "
           "0.62 m against 0.40 m entered by hand, laser boresight 1.0° in roll, 2% corrupted frames, 1% laser spikes, "
           "3 cm laser noise, 0.4 m GNSS noise.",
           "Profiles with an attitude source fly ±20° banks every 16 s at 55 m/s; profiles without one (attitude in "
           "post-processing) fly straight legs (±3°). Height is the profile's survey height, capped at 80% of the "
           "laser's rated range.", "",
           "Every self-test says **warn** on purpose: the aircraft had not been calibrated at boot, and for "
           "checksummed protocols it caught the injected corrupted frames. \"AGL p95\" for attitude profiles is after "
           "ground calibration and the boresight fit. The GRF-500 reports in 0.1 m steps, hence its 0.6 m offset. "
           "Laser readings logged counts every reading saved against the clean frames sent while the daemon was "
           "reading; lasers without a checksum (ILM-150-R, Riegl, generic ASCII) also log the lines corrupted on the "
           "wire (kept out of AGL by the spike gate), so their count can exceed the clean frames.", "",
           "| Profile | Height | Laser readings logged | Laser points p95 | Offset found (true 0.62 m) | "
           "IMU bias found (true +0.80/-0.50°) | Boresight found (true 1.00°) | AGL p95 | Roll p95 | "
           "Geotag p95, last fix → extrapolated |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        s, c = r["score"], r["calibration"]
        att = r.get("has_attitude", True)
        b = r.get("boresight_fit") or {}
        comp = r.get("completeness", {})
        out.append(
            f"| {r['profile']} | {s['survey_agl_m']:.0f} m | {comp.get('pct', float('nan')):.1f}% "
            f"({comp.get('laser_logged', '?')} of {comp.get('laser_clean_frames_sent', '?')}) | "
            f"{s['laser_point_position_m']['p95']:.2f} m | {c.get('laser_offset_m')} m | "
            + (f"{c['imu_roll_bias_deg']:+.2f}/{c['imu_pitch_bias_deg']:+.2f}° | {b['boresight_roll_deg']:+.2f}° | "
               if att else "no attitude sensor | n/a | ")
            + f"{s['agl_after_boresight_m']['p95']:.2f} m ({100 * s['agl_after_boresight_m']['p95'] / s['survey_agl_m']:.2f}%) | "
            + (f"{s['roll_deg']['p95']:.2f}° | " if att else "n/a | ")
            + f"{s['geotag_last_fix_m']['p95']:.1f} → {s['geotag_extrapolated_m']['p95']:.1f} m |")
    out += ["", "## Self-test and calibration details", ""]
    for r in reports:
        out.append(f"### {r['profile']}")
        out.append("")
        for i in r["bit"]["items"]:
            out.append(f"- self-test **{i['status']}** {i['item']}: {i['detail']}")
        for i in r["ground_cal"]["items"]:
            out.append(f"- ground cal **{i['status']}** {i['item']}: {i['detail']}")
        rd = r.get("readings")
        if rd:
            out.append(f"- readings: {rd['laser_logged']} laser, {rd['gnss_fixes_logged']} GNSS fixes; "
                       f"{rd['matched']} laser readings matched to position, {rd['not_matched']} not; "
                       f"clock model: {rd['clock_method']}")
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profiles", nargs="*")
    ap.add_argument("--flight-s", type=float, default=35.0)
    ap.add_argument("--markdown")
    ap.add_argument("--json")
    ap.add_argument("--keep", action="store_true", help="keep each run's work directory (config, logs, DB)")
    a = ap.parse_args()
    ids = a.profiles or [p["id"] for p in configure.load_catalog()["profiles"]]
    reports = [run_profile(pid, a.flight_s, keep=a.keep) for pid in ids]
    if a.markdown:
        Path(a.markdown).write_text(markdown(reports) + "\n")
        print(f"\nwrote {a.markdown}")
    if a.json:
        Path(a.json).write_text(json.dumps(reports, indent=2, default=str))


if __name__ == "__main__":
    main()
