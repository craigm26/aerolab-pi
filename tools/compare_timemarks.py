"""Check camera exposures: Reach M2 time marks against the exposures the Pi saw.

The Reach's time marks (camera Top-of-Frame -> C1 pin 2) are the precise
exposure times; they live in the Reach raw log. Export them with Emlid Studio
(or RTKLIB) as an events .pos file, then:

    python tools/compare_timemarks.py --events reach_events.pos [--db ...] [--flight N]

It pairs every Reach event with the nearest Pi exposure and reports:
  - how many exposures each side has, and which are missing from either
  - the time offset Pi - Reach (mean, spread): the Pi's backup timestamps' error,
    which you can feed back as tools/export_exposures.py --latency-ms
.pos format: RTKLIB solution text, '%' header lines (time system GPST or UTC
named in the header), then "YYYY/MM/DD HH:MM:SS.sss lat lon height Q ns ...".
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "daemon"))
import drivers  # noqa: E402
import export_exposures  # noqa: E402


def read_pos(path: Path) -> list[dict]:
    gpst = True
    out = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if line.startswith("%"):
            if "UTC" in line and "GPST" not in line:
                gpst = False
            continue
        f = line.split()
        if len(f) < 5:
            continue
        try:
            t = datetime.strptime(f[0] + " " + f[1], "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if gpst:
            t -= drivers.GPS_UTC_LEAP_S
        out.append({"utc": t, "lat": float(f[2]), "lon": float(f[3]), "h": float(f[4]),
                    "q": int(f[5]) if len(f) > 5 and f[5].isdigit() else None})
    return sorted(out, key=lambda e: e["utc"])


def compare(events: list[dict], pi: list[float], tol: float | None = None) -> dict:
    if not events or not pi:
        return {"reach": len(events), "pi": len(pi), "error": "nothing to compare"}
    iv = [b["utc"] - a["utc"] for a, b in zip(events, events[1:])]
    med = statistics.median(iv) if iv else 1.0
    tol = tol or min(0.3, 0.4 * med)
    pi = sorted(pi)
    used, pairs, missing_pi = set(), [], []
    import bisect
    for e in events:
        j = bisect.bisect_left(pi, e["utc"])
        best = None
        for k in (j - 1, j):
            if 0 <= k < len(pi) and k not in used and abs(pi[k] - e["utc"]) <= tol:
                if best is None or abs(pi[k] - e["utc"]) < abs(pi[best] - e["utc"]):
                    best = k
        if best is None:
            missing_pi.append(e["utc"])
        else:
            used.add(best)
            pairs.append((pi[best] - e["utc"]) * 1000)
    missing_reach = [pi[k] for k in range(len(pi)) if k not in used
                     and events[0]["utc"] - tol <= pi[k] <= events[-1]["utc"] + tol]
    out = {"reach_events": len(events), "pi_exposures": len(pi), "paired": len(pairs),
           "missing_on_pi": len(missing_pi), "missing_in_reach_log": len(missing_reach),
           "tolerance_s": round(tol, 3)}
    if pairs:
        out.update(offset_ms_mean=round(statistics.fmean(pairs), 3), offset_ms_std=round(statistics.pstdev(pairs), 3),
                   offset_ms_p95=round(sorted(abs(p) for p in pairs)[int(0.95 * (len(pairs) - 1))], 3))
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")
    out["first_missing_on_pi"] = [fmt(t) for t in missing_pi[:10]]
    out["first_missing_in_reach_log"] = [fmt(t) for t in missing_reach[:10]]
    out["verdict"] = ("every exposure is in both records" if not missing_pi and not missing_reach else
                      "exposures missing: see the lists (a Reach gap means the time-mark wire or Reach logging; "
                      "a Pi gap means the GPIO wire)")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", required=True)
    ap.add_argument("--db")
    ap.add_argument("--flight", type=int)
    ap.add_argument("--tolerance-s", type=float)
    a = ap.parse_args(argv)
    events = read_pos(Path(a.events))
    args = (["--db", a.db] if a.db else []) + (["--flight", str(a.flight)] if a.flight else [])
    tmp = Path(a.events).with_suffix(".pi_exposures.csv")
    export_exposures.main(args + ["--out", str(tmp)])
    import csv
    pi = [float(r["gnss_time_utc"]) for r in csv.DictReader(open(tmp))]
    res = compare(events, pi, a.tolerance_s)
    if argv is None:
        print(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    main()
