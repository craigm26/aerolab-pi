"""Check a separate backup of the camera card against camera_files.json.

    python tools/check_camera_backup.py --list <package>/camera_files.json --backup <folder>

At STOP SURVEY, AeroLab-Pi records the camera's file listing (paths and byte
sizes from the camera's HTTP /files/ API) into camera_files.json inside the
survey package. After copying the camera's storage card to a backup folder,
run this tool to verify that no image is missing or truncated.

Matching rule:
  Each path in camera_files.json comes from the camera's HTTP API and begins
  with the leading "/files/" prefix (for example "/files/0000SET/000/IMG_0000_1.tif").
  This tool strips that leading "/files/" prefix (and any leading "/") and
  normalizes path separators to "/" to obtain the path relative to the camera
  card root (for example "0000SET/000/IMG_0000_1.tif"). Every regular file in
  the --backup folder is indexed by its relative POSIX path from --backup, and
  matched on both relative path and byte size.

Exit code:
  0  only when the manifest has no error, no files are missing, and no files
     have a different size (truncated or modified). Extra files in the backup
     folder are reported for visibility and do not cause a non-zero exit code.
  1  when any file is missing or has a size mismatch, or camera_files.json
     recorded an error when fetching the list.
  2  when inputs cannot be read or are invalid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_SHOWN = 50


def rel_card_path(api_path: str) -> str:
    """Strip the camera API's leading /files/ prefix and normalize slashes."""
    p = api_path.replace("\\", "/").lstrip("/")
    if p.startswith("files/"):
        p = p[len("files/"):]
    elif p == "files":
        p = ""
    return p.lstrip("/")


def check_backup(list_path: Path, backup_dir: Path) -> dict:
    """Compare camera_files.json against every regular file under backup_dir."""
    data = json.loads(Path(list_path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ValueError(f"invalid camera file list format in {list_path}")
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"backup folder not found: {backup_dir}")

    expected: dict[str, int | None] = {}
    for entry in data["files"]:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        rel = rel_card_path(str(entry["path"]))
        if not rel:
            continue
        size = entry.get("size")
        expected[rel] = int(size) if size is not None else None

    actual: dict[str, int] = {}
    for p in backup_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(backup_dir).as_posix()
            actual[rel] = p.stat().st_size

    missing: list[dict] = []
    size_mismatch: list[dict] = []
    matched = 0
    for rel in sorted(expected):
        exp_size = expected[rel]
        if rel not in actual:
            missing.append({"path": rel, "expected_size": exp_size})
        elif exp_size is not None and actual[rel] != exp_size:
            size_mismatch.append({"path": rel, "expected_size": exp_size, "actual_size": actual[rel]})
        else:
            matched += 1

    extra = [{"path": rel, "size": actual[rel]} for rel in sorted(actual) if rel not in expected]
    list_error = data.get("error") or None
    ok = not list_error and not missing and not size_mismatch

    result = {
        "ok": ok,
        "list": str(list_path),
        "backup": str(backup_dir),
        "list_error": list_error,
        "totals": {
            "expected_files": len(expected),
            "expected_bytes": sum(s for s in expected.values() if s is not None),
            "backup_files": len(actual),
            "backup_bytes": sum(actual.values()),
            "matched_files": matched,
            "missing_files": len(missing),
            "size_mismatch_files": len(size_mismatch),
            "extra_files": len(extra),
        },
        "missing": missing,
        "size_mismatch": size_mismatch,
        "extra": extra,
    }
    if data.get("note"):
        result["note"] = data["note"]
    return result


def format_report(res: dict) -> str:
    """Format a plain-text report for terminal output."""
    t = res["totals"]
    lines = [
        f"Camera file list: {res['list']}",
        f"Backup folder:    {res['backup']}",
        f"Expected: {t['expected_files']} files ({t['expected_bytes']:,} bytes)",
        f"Backup:   {t['backup_files']} files ({t['backup_bytes']:,} bytes)",
        f"Matched:  {t['matched_files']}  Missing: {t['missing_files']}  "
        f"Size mismatch: {t['size_mismatch_files']}  Extra: {t['extra_files']}",
    ]
    if res.get("note"):
        lines.append(f"Note: {res['note']}")
    if res["list_error"]:
        lines.append(f"List error: {res['list_error']}")
    if res["missing"]:
        lines.append("Missing from backup:")
        for item in res["missing"][:MAX_SHOWN]:
            sz = "unknown size" if item["expected_size"] is None else f"{item['expected_size']:,} bytes"
            lines.append(f"  - {item['path']} ({sz})")
        if len(res["missing"]) > MAX_SHOWN:
            lines.append(f"  ... and {len(res['missing']) - MAX_SHOWN} more")
    if res["size_mismatch"]:
        lines.append("Different size (truncated or changed):")
        for item in res["size_mismatch"][:MAX_SHOWN]:
            lines.append(
                f"  - {item['path']}: expected {item['expected_size']:,} bytes, "
                f"got {item['actual_size']:,} bytes"
            )
        if len(res["size_mismatch"]) > MAX_SHOWN:
            lines.append(f"  ... and {len(res['size_mismatch']) - MAX_SHOWN} more")
    if res["extra"]:
        lines.append("Extra files in backup:")
        for item in res["extra"][:MAX_SHOWN]:
            lines.append(f"  - {item['path']} ({item['size']:,} bytes)")
        if len(res["extra"]) > MAX_SHOWN:
            lines.append(f"  ... and {len(res['extra']) - MAX_SHOWN} more")
    lines.append("OK: backup matches camera file list." if res["ok"] else "FAIL: backup check failed.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", dest="list_path", required=True, help="Path to camera_files.json")
    ap.add_argument("--backup", dest="backup_dir", required=True, help="Path to camera card backup folder")
    ap.add_argument("--json", dest="as_json", action="store_true", help="Print report as JSON")
    a = ap.parse_args(argv)
    try:
        res = check_backup(Path(a.list_path), Path(a.backup_dir))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2) if a.as_json else format_report(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
