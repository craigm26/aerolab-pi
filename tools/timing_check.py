"""Validate GNSS timing: PPS pulses, the Pi's clock against GPS, NMEA latency.

    python tools/timing_check.py                    # latest flight in the default log dir
    python tools/timing_check.py --db <file> --flight N

Checks (each PASS / WARN / FAIL):
  PPS present        pulses recorded at all (pps_edges); wiring + overlay working
  PPS interval       every pulse 1.000000 s after the last (kernel timestamps): jitter in microseconds
  Missing pulses     gaps of 2 s or more (lost lock, loose wire)
  Pi clock vs GPS    fractional second at each pulse = how far the Pi's clock is from GPS time,
                     and its drift. Large but steady is fine for processing (export tools correct it)
  Clock steps        jumps in system-minus-monotonic clock (NTP/chrony stepping the clock mid-flight)
  NMEA latency       how long after each GNSS epoch its NMEA sentence reached the Pi
Writes <db dir>/flight_<n>_timing.json too.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import export_laser as el  # noqa: E402


def check(db: Path, fid: int | None = None) -> dict:
    conn = sqlite3.connect(db)
    fid = fid or conn.execute("SELECT MAX(flight_id) FROM gnss_fixes").fetchone()[0]
    pps = conn.execute("SELECT seq, assert_utc_ns, mono_ns FROM pps_edges WHERE flight_id=? ORDER BY seq",
                       (fid,)).fetchall()
    clk = conn.execute("SELECT utc_ns, mono_ns FROM clock_samples WHERE flight_id=? ORDER BY mono_ns", (fid,)).fetchall()
    fixes = conn.execute("SELECT utc_ns, mono_ns, gnss_tod FROM gnss_fixes WHERE flight_id=? AND gnss_tod IS NOT NULL "
                         "ORDER BY mono_ns", (fid,)).fetchall()
    conn.close()
    items = []

    def add(name, status, detail, **x):
        items.append({"check": name, "status": status, "detail": detail, **x})

    if len(pps) < 3:
        add("PPS present", "fail" if not pps else "warn", f"{len(pps)} pulses recorded",
            hint="Check the PPS wire to the configured GPIO, the shared ground, and the pps-rp1 overlay "
                 "(`ls /sys/class/pps/` should list pps0; `sudo ppstest /dev/pps0` should print one line a second).")
    else:
        add("PPS present", "pass", f"{len(pps)} pulses")
        iv = [(b[1] - a[1]) / 1e9 for a, b in zip(pps, pps[1:])]
        one = [x for x in iv if 0.5 < x < 1.5]
        jit = [abs(x - 1.0) * 1e6 for x in one]
        if jit:
            p99 = sorted(jit)[int(0.99 * (len(jit) - 1))]
            add("PPS interval", "pass" if p99 < 100 else ("warn" if p99 < 1000 else "fail"),
                f"{len(one)} intervals, jitter median {statistics.median(jit):.1f} µs, p99 {p99:.1f} µs, "
                f"max {max(jit):.1f} µs", p99_us=p99)
        gaps = [x for x in iv if x >= 1.5]
        add("Missing pulses", "pass" if not gaps else "warn",
            "none" if not gaps else f"{len(gaps)} gap(s), longest {max(gaps):.1f} s (lost lock or a loose wire)")
        offs = []
        for _, a, _m in pps:
            r = a % 1_000_000_000
            offs.append((r - 1_000_000_000 if r >= 500_000_000 else r) / 1e6)
        drift = (offs[-1] - offs[0]) / max(1.0, (pps[-1][1] - pps[0][1]) / 1e9) * 1000   # µs per s = ppm
        big = max(abs(o) for o in offs)
        add("Pi clock vs GPS", "pass" if big < 50 else "warn",
            f"offset {offs[0]:+.3f} ms at start, {offs[-1]:+.3f} ms at end, drift {drift:+.1f} ppm"
            + ("" if big < 50 else ": the Pi's clock is not GPS-synced; the export tools correct for it"),
            start_ms=offs[0], end_ms=offs[-1], drift_ppm=drift)
    if len(clk) >= 2:
        d = [(u - m) for u, m in clk]
        steps = [(b - a) / 1e6 for a, b in zip(d, d[1:]) if abs(b - a) > 1_000_000]
        add("Clock steps", "pass" if not steps else "warn",
            "none: the system clock ran smoothly" if not steps else
            f"{len(steps)} step(s), largest {max(steps, key=abs):+.1f} ms (system clock was adjusted mid-flight; "
            "timestamps in mono_ns are unaffected)")
    if len(fixes) >= 5:
        m = el.clock_model(fixes, [(a, a - mm) for _, a, mm in pps], 0.0)
        lat = []
        A, B = m["a"], m["b"]
        for u, mono, tod in fixes:
            arrive = A + B * mono / 1e9
            lat.append((arrive - el.epoch_utc(tod, u / 1e9)) * 1000)
        if m["method"] == "PPS edges":
            add("NMEA latency", "pass" if statistics.median(lat) < 200 else "warn",
                f"median {statistics.median(lat):.1f} ms, p95 {sorted(lat)[int(0.95 * (len(lat) - 1))]:.1f} ms after "
                "each epoch (measured against PPS)", median_ms=statistics.median(lat))
        else:
            add("NMEA latency", "warn", "needs PPS to measure (without it the export tools use the fastest fixes)")
    st = {i["status"] for i in items}
    overall = "fail" if "fail" in st else ("warn" if "warn" in st else "pass")
    return {"flight": fid, "overall": overall, "checks": items}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    ap.add_argument("--flight", type=int)
    a = ap.parse_args(argv)
    db = Path(a.db) if a.db else next((p for p in (Path("/mnt/ssd/aerolab_logs/aerolab_flight.db"),
                                                   ROOT / "logs" / "aerolab_flight.db") if p.exists()), None)
    if not db or not db.exists():
        raise SystemExit("No flight database found; pass --db")
    r = check(db, a.flight)
    (db.parent / f"flight_{r['flight']}_timing.json").write_text(json.dumps(r, indent=2))
    if argv is None:
        print(f"Timing check, flight {r['flight']}: {r['overall'].upper()}")
        for i in r["checks"]:
            print(f"  {i['status'].upper():4}  {i['check']}: {i['detail']}")
            if i.get("hint"):
                print(f"        {i['hint']}")
    return r


if __name__ == "__main__":
    main()
