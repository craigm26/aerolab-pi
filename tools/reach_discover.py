"""Read-only discovery tool for an Emlid Reach receiver on the local network.

Usage:
    python tools/reach_discover.py http://<reach-ip>
    python tools/reach_discover.py http://192.168.42.1 --listen 15

Performs ONLY read-only HTTP GET requests against candidate Reach Panel paths
(with 5 s timeouts), then listens on Engine.IO v3 HTTP polling for 15 s (by
default) to record every Socket.IO event name seen and the last payload of each.
Writes reach_discovery_<YYYYMMDD_HHMMSS>.json in the current directory and
prints a short summary.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
import reach as reach_mod  # noqa: E402

CANDIDATE_PATHS = [
    "/",
    "/info",
    "/device",
    "/configuration",
    "/configuration/device",
    "/logs?page=1&limit=50",
    "/wifi/status",
]

SENSITIVE_KEY_TOKENS = ("password", "passwd", "token", "secret", "psk", "api_key")


def redact_sensitive(obj: Any) -> Any:
    """Recursively drop any dict key whose name looks like a password or token."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(tok in kl for tok in SENSITIVE_KEY_TOKENS):
                continue
            out[k] = redact_sensitive(v)
        return out
    if isinstance(obj, list):
        return [redact_sensitive(x) for x in obj]
    return obj


def normalize_base_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def probe_http_paths(client: httpx.Client, base_url: str, timeout_s: float = 5.0) -> dict[str, dict[str, Any]]:
    """Run strictly read-only GET requests on each candidate path."""
    results: dict[str, dict[str, Any]] = {}
    for path in CANDIDATE_PATHS:
        url = f"{base_url}{path}"
        try:
            r = client.get(url, timeout=timeout_s)
            ctype = r.headers.get("content-type", "")
            entry: dict[str, Any] = {
                "status_code": r.status_code,
                "content_type": ctype,
                "length": len(r.content),
            }
            if "json" in ctype.lower() or r.text.lstrip().startswith(("{", "[")):
                try:
                    entry["json"] = redact_sensitive(r.json())
                except ValueError:
                    entry["text_preview"] = r.text[:2000]
            else:
                entry["text_preview"] = r.text[:2000]
            results[path] = entry
        except Exception as exc:
            results[path] = {"error": (str(exc).strip() or type(exc).__name__)[:200]}
    return results


def listen_eio3(client: httpx.Client, base_url: str, duration_s: float = 15.0) -> dict[str, Any]:
    """Listen via read-only Engine.IO v3 GET polling for duration_s seconds."""
    endpoint = f"{base_url}/socket.io/"
    events_count: dict[str, int] = {}
    last_payloads: dict[str, Any] = {}
    result: dict[str, Any] = {
        "ok": False,
        "framing": "unknown",
        "handshake": None,
        "events_count": events_count,
        "last_payloads": last_payloads,
        "active_logs_seen": False,
        "raw_is_writing": None,
        "error": None,
    }

    def record_pkt(pkt: str) -> None:
        ev = reach_mod.decode_sio_event(pkt)
        if ev is None:
            return
        name, payload = ev
        events_count[name] = events_count.get(name, 0) + 1
        last_payloads[name] = redact_sensitive(payload)

    try:
        r = client.get(endpoint, params={"EIO": "3", "transport": "polling"}, timeout=5.0)
        r.raise_for_status()
        result["framing"] = reach_mod.detect_eio3_framing(r.content)
        pkts = reach_mod.parse_eio3_payload(r.content)
        open_pkt = next((p for p in pkts if p.startswith("0")), None)
        if not open_pkt:
            result["error"] = "no Engine.IO OPEN packet in handshake"
            return result
        open_info = json.loads(open_pkt[1:])
        sid = str(open_info["sid"])
        result["handshake"] = redact_sensitive({k: v for k, v in open_info.items() if k != "sid"})
        result["ok"] = True

        for pkt in pkts:
            if pkt.startswith("42"):
                record_pkt(pkt)

        deadline = time.monotonic() + max(0.0, float(duration_s))
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                break
            try:
                pr = client.get(
                    endpoint,
                    params={"EIO": "3", "transport": "polling", "sid": sid},
                    timeout=min(5.0, max(0.5, remaining)),
                )
                pr.raise_for_status()
            except httpx.ReadTimeout:
                continue
            poll_pkts = reach_mod.parse_eio3_payload(pr.content)
            closed = False
            for pkt in poll_pkts:
                if pkt in ("1", "41"):
                    closed = True
                elif pkt.startswith("42"):
                    record_pkt(pkt)
            if closed:
                break
    except Exception as exc:
        result["error"] = (str(exc).strip() or type(exc).__name__)[:200]

    if "active_logs" in last_payloads:
        result["active_logs_seen"] = True
        al = last_payloads["active_logs"]
        raw = al.get("raw") if isinstance(al, dict) else None
        if isinstance(raw, dict) and "is_writing" in raw:
            result["raw_is_writing"] = bool(raw["is_writing"])
    return result


def discover(base_url: str, listen_s: float = 15.0, out_dir: Path | None = None) -> tuple[dict[str, Any], Path]:
    """Run full read-only discovery and save reach_discovery_<time>.json."""
    base = normalize_base_url(base_url)
    t0 = time.monotonic()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())

    with httpx.Client(follow_redirects=True) as client:
        http_res = probe_http_paths(client, base, timeout_s=5.0)
        sio_res = listen_eio3(client, base, duration_s=listen_s)

    report: dict[str, Any] = {
        "url": base,
        "started_utc": started_utc,
        "duration_s": round(time.monotonic() - t0, 2),
        "http": http_res,
        "socketio": sio_res,
    }
    target_dir = Path(out_dir) if out_dir is not None else Path.cwd()
    out_path = target_dir / f"reach_discovery_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return report, out_path


def format_summary(report: dict[str, Any], out_path: Path) -> str:
    lines = [f"Reach discovery for {report['url']} ({report['duration_s']} s)"]
    lines.append("HTTP GET probes:")
    for path, info in report["http"].items():
        if "error" in info:
            lines.append(f"  {path:<26} ERROR: {info['error']}")
        else:
            lines.append(f"  {path:<26} HTTP {info['status_code']} ({info['length']} bytes)")
    sio = report["socketio"]
    lines.append(
        f"Socket.IO (EIO3 polling): {'ok' if sio['ok'] else 'failed'} "
        f"(framing: {sio['framing']})" + (f" error: {sio['error']}" if sio.get("error") else "")
    )
    if sio["events_count"]:
        ev_str = ", ".join(f"{k} ({v})" for k, v in sorted(sio["events_count"].items()))
        lines.append(f"Events seen: {ev_str}")
    else:
        lines.append("Events seen: none")
    lines.append(f"active_logs seen: {sio['active_logs_seen']}")
    lines.append(f"raw.is_writing: {sio['raw_is_writing']}")
    if sio["active_logs_seen"]:
        al = sio["last_payloads"].get("active_logs")
        raw = al.get("raw") if isinstance(al, dict) else None
        if isinstance(raw, dict):
            lines.append(
                f"raw log details: file={raw.get('file_name')}, size={raw.get('size')}, "
                f"recording_time={raw.get('recording_time')}"
            )
    lines.append(f"Saved: {out_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Emlid Reach local network discovery tool.")
    ap.add_argument("url", help="Reach base URL, e.g. http://192.168.42.1")
    ap.add_argument("--listen", type=float, default=15.0, help="Engine.IO polling listen duration in seconds (default 15)")
    args = ap.parse_args(argv)
    report, out_path = discover(args.url, listen_s=args.listen)
    print(format_summary(report, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
