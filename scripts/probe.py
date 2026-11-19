"""What is on this serial port? Tries every common baud rate with every driver.

    sudo systemctl stop aerolab           # free the port first
    python scripts/probe.py /dev/serial/by-id/usb-FTDI_...
    python scripts/probe.py /dev/ttyUSB0 --seconds 3 --bauds 9600 115200

For each baud it listens for a couple of seconds, feeds the bytes to every
driver, and scores how many valid frames each one decodes (checksums must
pass where the protocol has them). It prints the best match and a sample of
what it heard, so "no data" becomes one of: nothing at all (power, wiring,
TX/RX), noise at every baud (wrong levels: RS-232 on a 3.3 V pin?), or a
clear match at some baud (fix the config).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
import drivers  # noqa: E402

BAUDS = [4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


def score(data: bytes) -> list[tuple[str, int, int]]:
    """(driver, good readings, bad frames) for every driver on this byte sample."""
    out = []
    for name in drivers.DRIVERS:
        try:
            d = drivers.make_driver(name)
        except Exception:
            continue                                   # e.g. MAVLink without pymavlink
        try:
            good = len(d.feed(data))
        except Exception:
            good = 0
        bad = getattr(getattr(d, "stats", None), "bad", 0)
        out.append((name, good, bad))
    # Specific protocols (checksums, framing) beat the generic number/CSV parsers on equal evidence.
    generic = {"ascii_number", "csv_rpy", "riegl_ld90"}   # riegl falls back to "first number"
    return sorted(out, key=lambda t: (t[1] < 3 or t[1] <= 3 * t[2], t[0] in generic, -(t[1] - 3 * t[2]), t[0]))


def printable(data: bytes) -> float:
    if not data:
        return 0.0
    return sum(32 <= b < 127 or b in (9, 10, 13) for b in data) / len(data)


def probe(port: str, bauds: list[int], seconds: float) -> dict:
    import serial
    results = []
    for baud in bauds:
        try:
            with serial.Serial(port, baud, timeout=0.2) as ser:
                ser.reset_input_buffer()
                buf = bytearray()
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    buf += ser.read(4096)
        except serial.SerialException as exc:
            return {"port": port, "error": str(exc)}
        sc = score(bytes(buf))
        best = sc[0] if sc else ("none", 0, 0)
        results.append({"baud": baud, "bytes": len(buf), "printable": printable(bytes(buf)),
                        "best": best[0], "good": best[1], "bad": best[2], "sample": bytes(buf[:160])})
        print(f"  {baud:>7} baud: {len(buf):6} bytes, {100 * printable(bytes(buf)):3.0f}% printable, "
              f"best {best[0]} ({best[1]} good, {best[2]} bad)", flush=True)
    return {"port": port, "results": results}


def verdict(res: dict) -> str:
    if "error" in res:
        return f"Could not open {res['port']}: {res['error']}. Is the service still running, or the adapter unplugged?"
    rs = res["results"]
    if all(r["bytes"] == 0 for r in rs):
        return ("Nothing at all at any baud. Check power to the sensor, the cable, and (3.3 V UART) swap TX/RX. "
                "Some devices only talk after a command (LightWare: 'www'; NovAtel: 'log gpgga'); "
                "AeroLab-Pi sends those itself once configured.")
    good = [r for r in rs if r["good"] >= 3 and r["good"] > 3 * r["bad"]]
    if not good:
        best = max(rs, key=lambda r: r["printable"])
        return (f"Bytes arrive but no protocol decodes cleanly. Most readable at {best['baud']} baud "
                f"({100 * best['printable']:.0f}% printable). If it's text there, the device may use a format "
                "AeroLab-Pi doesn't know yet: send the sample below to the developer. If it's garbage at every "
                "baud, suspect signal levels (RS-232 into a 3.3 V pin, or a TTL device on an RS-232 adapter).")
    g = max(good, key=lambda r: (r["best"] not in ("ascii_number", "csv_rpy", "riegl_ld90"), r["good"] - 3 * r["bad"]))
    return f"Found it: {g['best']} at {g['baud']} baud ({g['good']} good readings, {g['bad']} bad frames)."


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--bauds", type=int, nargs="*", default=BAUDS)
    a = ap.parse_args()
    print(f"Probing {a.port} ...")
    res = probe(a.port, a.bauds, a.seconds)
    print("\n" + verdict(res))
    if "results" in res:
        best = max(res["results"], key=lambda r: (r["good"] - 3 * r["bad"], r["printable"]))
        if best["sample"]:
            print(f"\nSample at {best['baud']} baud: {best['sample']!r}")


if __name__ == "__main__":
    main()
