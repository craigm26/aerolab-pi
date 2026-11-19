"""Fit the laser boresight from a logged flight, and optionally save it.

The laser is never mounted exactly along the aircraft's down axis. A tilt of
1 degree costs about 4.5 m of AGL at 20 degrees of bank at 700 m. To measure
it, fly a few minutes over flat ground (lake ice, a large flat field, an ice
shelf) at constant power, with banks of 15-25 degrees both ways. Then:

    python tools/boresight.py                      # latest flight in the log dir
    python tools/boresight.py --flight 12          # a given flight
    python tools/boresight.py --start 14:05 --end 14:12   # only the flat-ground part (UTC)
    python tools/boresight.py --apply              # save into config/calibration.local.json

The fit compares the laser height with GNSS altitude (flat ground: their
difference is constant), so climbs and descents are fine. Pitch boresight is
only fitted if pitch varied by 4 degrees or more.
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
import calibration as calib  # noqa: E402
import main as daemon_main  # noqa: E402


def find_db(cfg: dict) -> Path:
    lc = cfg["logging"]
    for d in (Path(lc["primary_dir"]), daemon_main.resolve_log_dir(lc["primary_dir"], lc["fallback_dir"], False)):
        if (d / "aerolab_flight.db").exists():
            return d / "aerolab_flight.db"
    raise SystemExit("No aerolab_flight.db found; pass --db")


def hhmm_to_ts(day_ts: float, hhmm: str) -> float:
    d = datetime.fromtimestamp(day_ts, timezone.utc)
    h, m = (int(x) for x in hhmm.split(":")[:2])
    return d.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    ap.add_argument("--config")
    ap.add_argument("--flight", type=int)
    ap.add_argument("--start", help="UTC HH:MM")
    ap.add_argument("--end", help="UTC HH:MM")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--calibration", default=str(ROOT / "config" / "calibration.local.json"))
    a = ap.parse_args()

    cfg_path = Path(a.config) if a.config else (ROOT / "config" / "sensors.local.yaml")
    if not cfg_path.exists():
        cfg_path = ROOT / "config" / "sensors.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    db = Path(a.db) if a.db else find_db(cfg)
    conn = sqlite3.connect(db)
    fid = a.flight or conn.execute("SELECT MAX(flight_id) FROM telemetry").fetchone()[0]
    rows = conn.execute(
        "SELECT unix_ts, laser_used_m, roll_deg, pitch_deg, alt_msl_m, agl_quality FROM telemetry "
        "WHERE flight_id = ? AND laser_ok = 1 AND imu_ok = 1 AND gnss_ok = 1 AND laser_used_m > 1 "
        "ORDER BY unix_ts", (fid,)).fetchall()
    conn.close()
    if not rows:
        raise SystemExit(f"No usable samples in flight {fid}.")
    if a.start:
        t0 = hhmm_to_ts(rows[0][0], a.start)
        rows = [r for r in rows if r[0] >= t0]
    if a.end:
        t1 = hhmm_to_ts(rows[0][0], a.end)
        rows = [r for r in rows if r[0] <= t1]
    rows = [r for r in rows if r[5] != "range"]

    cal = calib.Calibration.load(Path(a.calibration))
    offset = cal.laser_offset_m if cal.laser_offset_m is not None else float(cfg["laser_altimeter"]["mounting_offset_m"])
    # Remove any boresight already applied, so the fit is absolute.
    samples = [(u, r, p, alt) for _, u, r, p, alt, _ in rows]
    fit = calib.fit_boresight(samples, offset)
    print(f"Flight {fid}: {fit['n']} samples, roll span {fit['roll_span_deg']}°, pitch span {fit['pitch_span_deg']}°")
    if fit["roll_span_deg"] < 20:
        print("Warning: less than 20° of roll change; the roll boresight is poorly determined. Fly banks both ways.")
    print(f"Boresight roll  {fit['boresight_roll_deg']:+.2f}°  (currently {cal.boresight_roll_deg:+.2f}°)")
    print(f"Boresight pitch {fit['boresight_pitch_deg']:+.2f}°  "
          + ("" if fit["pitch_fitted"] else "(not observable: pitch barely changed)"))
    print(f"Laser height vs GNSS altitude spread: {fit['spread_before_m']:.2f} m -> {fit['spread_after_m']:.2f} m")
    if a.apply:
        prop = {"boresight_roll_deg": fit["boresight_roll_deg"]}
        if fit["pitch_fitted"]:
            prop["boresight_pitch_deg"] = fit["boresight_pitch_deg"]
        calib.apply_proposed(cal, prop, Path(a.calibration))
        print(f"Saved to {a.calibration}. Restart the service: sudo systemctl restart aerolab")


if __name__ == "__main__":
    main()
