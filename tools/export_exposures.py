"""Every camera exposure the Pi saw, on GNSS time, with position.

    python tools/export_exposures.py [--db ...] [--flight N] [--latency-ms 0.3]

The Reach M2 time mark is the precise exposure record (in its raw log; extract
it with Emlid Studio / RTKLIB). This is the independent backup: exposures the
Pi saw on its GPIO, put on GNSS time with the same clock model as
tools/export_laser.py (GNSS epoch times, or PPS when wired), and given a
position by interpolating the GNSS fixes. Compare the two lists to check that
no exposure is missing from either.

--latency-ms: GPIO callback delay to remove (typically well under 1 ms).
Output: <db dir>/flight_<n>_exposures.csv and a .summary.json.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_laser as el  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    ap.add_argument("--flight", type=int)
    ap.add_argument("--latency-ms", type=float, default=0.0)
    ap.add_argument("--max-gap-s", type=float, default=0.6)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    db = Path(a.db) if a.db else next((p for p in (Path("/mnt/ssd/aerolab_logs/aerolab_flight.db"),
                                                   el.ROOT / "logs" / "aerolab_flight.db") if p.exists()), None)
    if not db or not db.exists():
        raise SystemExit("No flight database found; pass --db")
    conn = sqlite3.connect(db)
    fid = a.flight or conn.execute("SELECT MAX(flight_id) FROM exposures").fetchone()[0]
    ex = conn.execute("SELECT seq, utc_ns, mono_ns FROM exposures WHERE flight_id=? ORDER BY mono_ns", (fid,)).fetchall()
    fixes = conn.execute("SELECT utc_ns, mono_ns, gnss_tod, lat, lon, alt_m, alt_ref, fix FROM gnss_fixes "
                         "WHERE flight_id=? AND lat IS NOT NULL ORDER BY mono_ns", (fid,)).fetchall()
    pps = conn.execute("SELECT assert_utc_ns, utc_minus_mono_ns FROM pps_edges WHERE flight_id=? ORDER BY seq",
                       (fid,)).fetchall()
    conn.close()
    if not ex:
        raise SystemExit(f"No exposures in flight {fid}.")
    if not fixes:
        raise SystemExit(f"No GNSS fixes in flight {fid}.")
    model = el.clock_model([(u, m, tod) for u, m, tod, *_ in fixes], pps, 0.0)
    A, B = model["a"], model["b"]
    ft = [el.epoch_utc(tod, u / 1e9) if tod is not None else A + B * m / 1e9 for u, m, tod, *_ in fixes]
    order = sorted(range(len(fixes)), key=lambda i: ft[i])
    ft = [ft[i] for i in order]
    fixes = [fixes[i] for i in order]
    out = Path(a.out) if a.out else db.parent / f"flight_{fid}_exposures.csv"
    gaps, intervals, prev = 0, [], None
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "gnss_time_utc", "gnss_time_iso", "lat", "lon", "alt_m", "alt_ref", "fix",
                    "dt_to_nearest_fix_s", "match", "pi_utc_ns", "pi_mono_ns"])
        for seq, utc_ns, mono_ns in ex:
            t = A + B * mono_ns / 1e9 - a.latency_ms / 1000
            if prev is not None:
                intervals.append(t - prev)
            prev = t
            j = bisect.bisect_left(ft, t)
            if 0 < j < len(ft):
                t0, t1, f0, f1 = ft[j - 1], ft[j], fixes[j - 1], fixes[j]
                k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                lat, lon = f0[3] + k * (f1[3] - f0[3]), f0[4] + k * (f1[4] - f0[4])
                alt = None if f0[5] is None or f1[5] is None else f0[5] + k * (f1[5] - f0[5])
                near, fx = min(t - t0, t1 - t), (f0 if k < 0.5 else f1)
                match = "ok" if near <= a.max_gap_s else "gap"
            else:
                fx = fixes[0] if j == 0 else fixes[-1]
                lat, lon, alt = fx[3], fx[4], fx[5]
                near, match = abs(t - (ft[0] if j == 0 else ft[-1])), "outside"
            gaps += match != "ok"
            w.writerow([seq, f"{t:.6f}", datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds"),
                        f"{lat:.8f}", f"{lon:.8f}", "" if alt is None else f"{alt:.3f}", fx[6], fx[7], f"{near:.3f}",
                        match, utc_ns, mono_ns])
    med = statistics.median(intervals) if intervals else None
    missing = sum(max(0, round(iv / med) - 1) for iv in intervals) if med else 0
    summary = {"flight": fid, "exposures": len(ex), "not_matched": gaps, "clock_model": model["method"],
               "median_interval_s": med, "likely_missing_between": missing,
               "sequence_gaps": sum(1 for (s0, *_), (s1, *_) in zip(ex, ex[1:]) if s1 != s0 + 1),
               "output": str(out)}
    out.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    if argv is None:
        print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
