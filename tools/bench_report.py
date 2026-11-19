"""One bench report to send back: everything the running AeroLab-Pi knows about the installation.

    ./bench.sh                                   # with the service running
    ./bench.sh --laser-distance 12.40            # also: laser against a tape-measured distance (m)
    ./bench.sh --camera-test                     # also: camera wiring test (one capture, timed to the Pi)

Collects the self-test, the diagnosis with fixes, live sensor health, camera,
data and PPS status, calibration, and a timing check of the current flight,
and writes bench_report_<time>.html and .json into the log folder.
"""

import argparse
import html
import json
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "daemon"))


def api(port, path, body=None, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int)
    ap.add_argument("--laser-distance", type=float, help="true distance from the laser's reference point, metres")
    ap.add_argument("--laser-seconds", type=float, default=15.0)
    ap.add_argument("--camera-test", action="store_true")
    a = ap.parse_args(argv)
    cfgp = ROOT / "config" / "sensors.local.yaml"
    cfg = yaml.safe_load((cfgp if cfgp.exists() else ROOT / "config" / "sensors.yaml").read_text())
    port = a.port or int(cfg["server"]["port"])
    try:
        state = api(port, "/api/state")
    except Exception as exc:
        raise SystemExit(f"AeroLab-Pi is not answering on port {port} ({exc}). Start it: sudo systemctl start aerolab")
    rep = {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "profile": cfg.get("profile"),
           "sim": state.get("sim_mode"), "bit": api(port, "/api/bit"), "diagnose": api(port, "/api/diagnose"),
           "health": api(port, "/api/health"), "camera": state.get("_camera"), "data": state.get("_data"),
           "calibration": api(port, "/api/cal"),
           "live": {k: state.get(k) for k in ("gps_fix", "sats", "hdop", "laser_ok", "laser_alive", "laser_raw_m",
                                              "laser_no_return_pct", "gnss_time_offset_s", "flight_id", "log_dir")}}
    import diagnose
    rep["software"] = diagnose.git_version(ROOT)
    if a.laser_distance is not None:
        print(f"Laser check: {a.laser_seconds:.0f} s of readings against {a.laser_distance} m ...", flush=True)
        rep["laser_check"] = api(port, f"/api/bench/laser?seconds={a.laser_seconds}&distance_m={a.laser_distance}",
                                 timeout=a.laser_seconds + 30)
    if a.camera_test:
        print("Camera wiring test ...", flush=True)
        rep["camera_test"] = api(port, "/api/bench/camera", body={}, timeout=30)
    db = Path(state.get("log_dir") or ".") / "aerolab_flight.db"
    try:
        import timing_check
        rep["timing"] = timing_check.check(db, state.get("flight_id"))
    except Exception as exc:
        rep["timing"] = {"overall": "n/a", "error": str(exc)}
    out_dir = Path(state.get("log_dir") or ".")
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    jpath = out_dir / f"bench_report_{stamp}.json"
    jpath.write_text(json.dumps(rep, indent=2, default=str))
    hpath = jpath.with_suffix(".html")
    hpath.write_text(render(rep))
    r = rep["diagnose"]["readiness"]
    print(f"\n{r['text']}")
    for f in rep["diagnose"]["findings"][:6]:
        print(f"  [{f['severity']}] {f['title']}" + (f": {f['steps'][0]}" if f["steps"] else ""))
    print(f"Timing: {rep['timing'].get('overall', 'n/a').upper()}")
    for k in ("laser_check", "camera_test"):
        if k in rep:
            print(f"{k}: {json.dumps(rep[k])}")
    print(f"\nReport: {hpath}\n        {jpath}\nSend both files.")
    return rep


def render(rep: dict) -> str:
    E = lambda x: html.escape(str(x))
    r = rep["diagnose"]["readiness"]
    color = {"green": "#0a7d3b", "amber": "#a86400", "red": "#b3261e"}[r["level"]]
    rows = lambda items, k1="item", k2="status", k3="detail": "".join(
        f"<tr><td><b>{E(i.get(k2, '')).upper()}</b></td><td>{E(i.get(k1, ''))}</td><td>{E(i.get(k3, ''))}</td></tr>"
        for i in items)
    finds = "".join(f"<li><b>{E(f['severity'])}: {E(f['title'])}</b> {E(f['evidence'])}<ol>" +
                    "".join(f"<li>{E(s)}</li>" for s in f["steps"]) + "</ol></li>" for f in rep["diagnose"]["findings"])
    health = "".join(f"<tr><td>{E(k)}</td><td>{E(v.get('hz'))} Hz (expected {E(v.get('expected_hz'))})</td>"
                     f"<td>{E(v.get('err_pct'))}% bad</td><td>{E(v.get('age_ms'))} ms</td></tr>"
                     for k, v in rep["health"].items())
    extra = "".join(f"<h2>{E(k)}</h2><pre>{E(json.dumps(rep[k], indent=2))}</pre>"
                    for k in ("laser_check", "camera_test", "camera", "calibration", "live", "data") if k in rep)
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>AeroLab-Pi bench report</title><style>body{{font:14px/1.45 system-ui;max-width:900px;margin:20px auto;padding:0 14px}}
.v{{background:{color};color:#fff;padding:10px 14px;border-radius:8px;font-weight:700}}td,th{{border-bottom:1px solid #eee;padding:5px;text-align:left;vertical-align:top}}
table{{border-collapse:collapse;width:100%}}pre{{background:#f6f6f6;padding:8px;overflow:auto}}</style>
<h1>AeroLab-Pi bench report</h1><p>{E(rep['time_utc'])} · profile {E(rep['profile'])} · software {E(rep['software'])}{' · SIMULATION' if rep['sim'] else ''}</p>
<div class=v>{E(r['text'])}</div><h2>What to fix</h2><ul>{finds or '<li>Nothing.</li>'}</ul>
<h2>Self-test</h2><table>{rows(rep['bit'].get('items', []))}</table>
<h2>Sensors, live</h2><table>{health}</table>
<h2>Timing: {E(rep['timing'].get('overall', 'n/a')).upper()}</h2><table>{rows(rep['timing'].get('checks', []), 'check')}</table>
{extra}"""


if __name__ == "__main__":
    main()
