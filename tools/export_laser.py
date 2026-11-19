"""Match every laser reading to a GNSS position, on GNSS time.

    python tools/export_laser.py                         # latest flight, default log dir
    python tools/export_laser.py --db /mnt/ssd/aerolab_logs/aerolab_flight.db --flight 3
    python tools/export_laser.py --laser-latency-ms 12 --gnss-latency-ms 0

How the timing works (all of it is redone here, so it can be refined later
without re-flying):

1. Every laser reading and every GNSS fix was logged with the Pi's monotonic
   clock at the moment its bytes arrived (laser_readings, gnss_fixes).
2. Each fix also carries its own epoch time from the receiver (NMEA/ERB time
   of day). Arrival is always a little after the epoch, so the Pi-to-GNSS
   clock offset is taken from the fastest-arriving fixes (the lower envelope)
   in each 10 s window, and a straight line (offset + drift) is fitted through them.
3. If PPS edges were captured (pps_edges), each edge is the exact top of a GPS
   second: the fit is redone on those, to microseconds.
4. Each laser reading's GNSS time = its arrival on that line, minus the laser's
   own latency (--laser-latency-ms: measurement-to-output delay, from the
   laser's manual or a calibration). Position is interpolated between the two
   fixes either side; readings more than --max-gap-s from a fix are flagged.

Output: <db dir>/flight_<n>_laser_points.csv and a summary of the clock fit.
AGL here is range minus the mounting offset, not tilt-corrected (attitude is
applied in post-processing).
"""

import argparse
import bisect
import csv
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
import calibration as calib  # noqa: E402


def day_start(utc_s: float) -> float:
    d = datetime.fromtimestamp(utc_s, timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def epoch_utc(tod: float, near_utc_s: float) -> float:
    """GNSS time-of-day -> full UTC seconds, choosing the day closest to the Pi's clock."""
    base = day_start(near_utc_s) + tod
    for cand in (base - 86400, base, base + 86400):
        if abs(cand - near_utc_s) < 43200:
            return cand
    return base


def linfit(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 1.0
    return my - b * mx, b


def clock_model(fixes, pps, gnss_latency_s: float, window_s: float = 10.0) -> dict:
    """Fit gnss_time = a + b * mono_s. Returns a, b, method, residual stats."""
    pts = [(m / 1e9, epoch_utc(tod, u / 1e9)) for u, m, tod in fixes if tod is not None]
    if len(pts) < 3:
        # The receiver gave no epoch times: fall back to the Pi's own clock at arrival.
        xs = [m / 1e9 for _, m, _ in fixes]
        ys = [u / 1e9 for u, _, _ in fixes]
        a, b = linfit(xs, [y - x for x, y in zip(xs, ys)])
        return {"method": "Pi clock at arrival (no GNSS epoch times; latency not removed)", "a": a, "b": 1.0 + b,
                "n": len(fixes), "drift_ppm": b * 1e6, "arrival_latency_ms": {"median": float("nan"), "p95": float("nan")}}
    # lower envelope: per window, the fix that arrived soonest after its epoch
    env = {}
    for x, y in pts:
        k = int(x // window_s)
        lat = x - y
        if k not in env or lat < env[k][0]:
            env[k] = (lat, x, y)
    xs = [x for _, x, _ in env.values()]
    ys = [y + gnss_latency_s for _, _, y in env.values()]
    if len(xs) >= 2:
        a, b = linfit(xs, [y - x for x, y in zip(xs, ys)])   # fit offset(mono) = a + b' * mono
        a, b = a, 1.0 + b
    else:
        a, b = ys[0] - xs[0], 1.0
    method = "GNSS epoch times (lower envelope)"
    resid = [(x - (y - a) / b) * b for x, y in pts]   # seconds each fix arrived after the envelope
    out = {"method": method, "a": a, "b": b, "n": len(pts),
           "arrival_latency_ms": {"median": 1000 * statistics.median(resid),
                                  "p95": 1000 * sorted(resid)[int(0.95 * (len(resid) - 1))]}}
    # PPS refinement
    if pps and len(pps) >= 3:
        px, py = [], []
        for assert_ns, utc_minus_mono in pps:
            mono_s = (assert_ns - utc_minus_mono) / 1e9
            sec = round(a + b * mono_s)
            px.append(mono_s)
            py.append(sec)
        a2, b2 = linfit(px, [y - x for x, y in zip(px, py)])
        b2 = 1.0 + b2
        res = [1e6 * ((a2 + b2 * x) - y) for x, y in zip(px, py)]
        out.update(method="PPS edges", a=a2, b=b2, n_pps=len(pps),
                   pps_residual_us={"rms": (sum(r * r for r in res) / len(res)) ** 0.5, "max": max(abs(r) for r in res)})
    out["drift_ppm"] = (out["b"] - 1.0) * 1e6
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    ap.add_argument("--flight", type=int)
    ap.add_argument("--laser-latency-ms", type=float, default=0.0,
                    help="delay from laser measurement to its output on the wire")
    ap.add_argument("--gnss-latency-ms", type=float, default=0.0,
                    help="known extra delay of GNSS output after its epoch, beyond what the fit absorbs")
    ap.add_argument("--max-gap-s", type=float, default=0.6)
    ap.add_argument("--offset-m", type=float, help="laser mounting offset (default: from calibration)")
    ap.add_argument("--out")
    ap.add_argument("--from-mono-ns", type=int, help="only readings at or after this monotonic time (a survey start)")
    ap.add_argument("--to-mono-ns", type=int, help="only readings at or before this monotonic time (a survey stop)")
    a = ap.parse_args(argv)

    db = Path(a.db) if a.db else next((p for p in (Path("/mnt/ssd/aerolab_logs/aerolab_flight.db"),
                                                   ROOT / "logs" / "aerolab_flight.db") if p.exists()), None)
    if not db or not db.exists():
        raise SystemExit("No flight database found; pass --db")
    conn = sqlite3.connect(db)
    fid = a.flight or conn.execute("SELECT MAX(flight_id) FROM laser_readings").fetchone()[0]
    lo, hi = a.from_mono_ns or 0, a.to_mono_ns or 2 ** 62
    laser = conn.execute("SELECT utc_ns, mono_ns, range_m, accepted FROM laser_readings WHERE flight_id=? "
                         "AND mono_ns BETWEEN ? AND ? ORDER BY mono_ns", (fid, lo, hi)).fetchall()
    fixes = conn.execute("SELECT utc_ns, mono_ns, gnss_tod, lat, lon, alt_m, alt_ref, fix, sats, hdop FROM gnss_fixes "
                         "WHERE flight_id=? AND lat IS NOT NULL ORDER BY mono_ns", (fid,)).fetchall()
    pps = conn.execute("SELECT assert_utc_ns, utc_minus_mono_ns FROM pps_edges WHERE flight_id=? ORDER BY seq",
                       (fid,)).fetchall()
    conn.close()
    if not laser:
        raise SystemExit(f"No laser readings in flight {fid}.")
    if not fixes:
        raise SystemExit(f"No GNSS fixes in flight {fid}.")

    model = clock_model([(u, m, tod) for u, m, tod, *_ in fixes], pps, a.gnss_latency_ms / 1000)
    A, B = model["a"], model["b"]
    # GNSS time of each fix = its epoch (from the receiver), not its arrival
    ft = [epoch_utc(tod, u / 1e9) if tod is not None else A + B * m / 1e9 for u, m, tod, *_ in fixes]
    order = sorted(range(len(fixes)), key=lambda i: ft[i])
    ft = [ft[i] for i in order]
    fixes = [fixes[i] for i in order]

    offset = a.offset_m
    if offset is None:
        cal = calib.Calibration.load(ROOT / "config" / "calibration.local.json")
        offset = cal.laser_offset_m if cal.laser_offset_m is not None else 0.0

    out = Path(a.out) if a.out else db.parent / f"flight_{fid}_laser_points.csv"
    n_ok = n_gap = 0
    gaps = []
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gnss_time_utc", "gnss_time_iso", "range_m", "accepted", "agl_level_m", "lat", "lon", "alt_m",
                    "alt_ref", "fix", "sats", "hdop", "dt_to_nearest_fix_s", "match", "pi_utc_ns", "pi_mono_ns"])
        for utc_ns, mono_ns, rng, acc in laser:
            t = A + B * mono_ns / 1e9 - a.laser_latency_ms / 1000
            j = bisect.bisect_left(ft, t)
            if 0 < j < len(ft):
                t0, t1 = ft[j - 1], ft[j]
                f0, f1 = fixes[j - 1], fixes[j]
                k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                lat = f0[3] + k * (f1[3] - f0[3])
                lon = f0[4] + k * (f1[4] - f0[4])
                alt = None if f0[5] is None or f1[5] is None else f0[5] + k * (f1[5] - f0[5])
                near = min(t - t0, t1 - t)
                fx = f0 if k < 0.5 else f1
                match = "ok" if (t1 - t0) <= 2 * a.max_gap_s else "gap"
            else:
                fx = fixes[0] if j == 0 else fixes[-1]
                lat, lon, alt = fx[3], fx[4], fx[5]
                near = abs(t - (ft[0] if j == 0 else ft[-1]))
                match = "outside"
            if near > a.max_gap_s:
                match = "gap"
            if match == "ok":
                n_ok += 1
            else:
                n_gap += 1
                gaps.append(t)
            iso = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")
            w.writerow([f"{t:.6f}", iso, "" if rng is None else f"{rng:.3f}", acc,
                        "" if rng is None else f"{rng - offset:.3f}", f"{lat:.8f}", f"{lon:.8f}",
                        "" if alt is None else f"{alt:.3f}", fx[6], fx[7], fx[8], fx[9], f"{near:.3f}", match,
                        utc_ns, mono_ns])
    summary = {"flight": fid, "laser_readings": len(laser), "matched": n_ok, "not_matched": n_gap,
               "gnss_fixes": len(fixes), "pps_edges": len(pps), "clock_model": model, "offset_m": offset,
               "laser_latency_ms": a.laser_latency_ms, "output": str(out)}
    (out.with_suffix(".summary.json")).write_text(json.dumps(summary, indent=2))
    if argv is None:
        print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
