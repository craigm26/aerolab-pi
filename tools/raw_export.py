"""Turn AeroLab-Pi raw capture files back into plain streams plus a timestamp index.

    python tools/raw_export.py logs/raw/gnss_20261119_140000.aeroraw
      -> gnss_20261119_140000.bin        every byte, in order (NMEA / ERB / UBX / RTCM as received)
      -> gnss_20261119_140000.times.csv  byte_offset, length, utc_ns, mono_ns per read

The .bin file opens in RTKLIB, u-blox u-center, Emlid Studio or a text editor
(for NMEA); the CSV says when each byte arrived at the Pi.

    python tools/raw_export.py --lines logs/raw/laser_*.aeroraw
      -> one CSV row per text line: utc_ns, mono_ns, line   (for line-based sensors such as lasers)
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import rawlog  # noqa: E402


def export(path: Path, lines: bool):
    it = rawlog.read_raw(path)
    header = next(it)
    stem = path.with_suffix("")
    if lines:
        out = stem.with_suffix(".lines.csv")
        buf = b""
        n = 0
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["utc_ns", "mono_ns", "line"])
            for utc, mono, data in it:
                buf += data
                while True:
                    i = min((k for k in (buf.find(b"\n"), buf.find(b"\r")) if k >= 0), default=-1)
                    if i < 0:
                        break
                    line, buf = buf[:i], buf[i + 1:]
                    t = line.decode("ascii", "replace").strip()
                    if t:
                        w.writerow([utc, mono, t])   # time the line's last byte arrived
                        n += 1
        print(f"{path.name}: {header.get('name')} -> {out.name} ({n} lines)")
        return
    out_bin, out_idx = stem.with_suffix(".bin"), stem.with_suffix(".times.csv")
    off = frames = 0
    with open(out_bin, "wb") as fb, open(out_idx, "w", newline="") as fi:
        w = csv.writer(fi)
        w.writerow(["byte_offset", "length", "utc_ns", "mono_ns"])
        for utc, mono, data in it:
            fb.write(data)
            w.writerow([off, len(data), utc, mono])
            off += len(data)
            frames += 1
    print(f"{path.name}: {header.get('name')} -> {out_bin.name} ({off} bytes, {frames} reads), {out_idx.name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--lines", action="store_true", help="one row per text line instead of a byte stream")
    a = ap.parse_args()
    for f in a.files:
        export(Path(f), a.lines)


if __name__ == "__main__":
    main()
