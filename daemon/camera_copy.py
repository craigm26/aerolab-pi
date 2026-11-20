"""Copy camera images to the Pi's log disk over Ethernet after landing.

The MicaSense RedEdge-P stores every image on its own card during flight and
exposes them over HTTP at /files/*. Copying gigabytes of images is slow and
must never run in flight or while a survey is recording, so this module:

  * refuses to start (and pauses automatically while running) if a survey is
    starting or recording, or if the aircraft is moving (ground speed > 5 m/s
    when GNSS is reporting, or valid laser AGL > 5 m);
  * refuses to start if the camera is not configured with an Ethernet
    status_url, or if free space on the log disk is less than the remaining
    bytes to copy plus a configurable reserve (default 2 GB);
  * streams each file to <file>.part and renames it only after the received
    byte count matches the size reported by the camera, retrying mismatches
    up to 3 times before recording the file as failed and continuing;
  * resumes cleanly after a cancel or dropped cable by reusing an incomplete
    copy folder and skipping files already present with the exact size;
  * writes MANIFEST.sha256 (SHA-256 of every copied file, relative paths) and
    copy_report.json when finished or cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

GB = 1024 ** 3
DEFAULT_RESERVE_GB = 2.0
MAX_GROUND_SPEED_MS = 5.0
MAX_AGL_M = 5.0
MAX_RETRIES = 3
CHUNK_SIZE = 256 * 1024
SAFETY_CHECK_INTERVAL_S = 1.0
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 30.0
RETRY_DELAY_S = 0.25


def rel_card_path(api_path: str) -> str:
    """Strip the camera API's leading /files/ prefix and reject unsafe paths."""
    p = str(api_path).replace("\\", "/").lstrip("/")
    if p.startswith("files/"):
        p = p[len("files/"):]
    elif p == "files":
        p = ""
    p = p.lstrip("/")
    parts = [seg for seg in p.split("/") if seg and seg != "."]
    if not parts or any(seg == ".." for seg in parts):
        return ""
    return "/".join(parts)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


class _PausedSignal(Exception):
    """Internal control flow raised when a safety rule pauses an active stream."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CameraCopier:
    """Copies camera images over HTTP into <log_dir>/camera_images/<stamp>/."""

    def __init__(self, daemon):
        self.d = daemon
        cam_cfg = (daemon.cfg.get("camera") or {}) if hasattr(daemon, "cfg") else {}
        raw_reserve = cam_cfg.get("copy_reserve_gb", cam_cfg.get("reserve_gb", DEFAULT_RESERVE_GB))
        self.reserve_gb: float = float(raw_reserve if raw_reserve is not None else DEFAULT_RESERVE_GB)
        self.safety_check_interval_s: float = SAFETY_CHECK_INTERVAL_S
        self.retry_delay_s: float = RETRY_DELAY_S
        self.chunk_size: int = CHUNK_SIZE

        self.state: str = "idle"
        self.files_done: int = 0
        self.files_total: int = 0
        self.files_copied: int = 0
        self.files_skipped: int = 0
        self.failed_files: list[dict[str, str]] = []
        self.bytes_done: int = 0
        self.bytes_total: int = 0
        self.current_file: str = ""
        self.reason: str = ""
        self.message: str = ""
        self.dest_dir: Path | None = None
        self.started_utc: str = ""
        self.finished_utc: str = ""
        self.last_report: dict[str, Any] | None = None

        self._bytes_transferred: int = 0
        self._active_copy_s: float = 0.0
        self._copy_resumed_at: float | None = None
        self._final_mb_per_s: float = 0.0
        self._resume_dir: Path | None = None

        self._cancel_requested: bool = False
        self._pending_job: dict[str, Any] | None = None
        self._wake: asyncio.Event | None = None
        self._stopped_event: asyncio.Event | None = None
        self._job_task: asyncio.Task | None = None
        self._loop_running: bool = False

        self._load_latest_report()

    @property
    def reserve_bytes(self) -> int:
        return int(round(self.reserve_gb * GB))

    def _load_latest_report(self) -> None:
        """Pick up the latest copy_report.json on disk (for diagnosis and resume after restart)."""
        root = Path(self.d.log_dir) / "camera_images"
        if not root.is_dir():
            return
        dirs = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
        if not dirs:
            return
        latest = dirs[0]
        rep_path = latest / "copy_report.json"
        if not rep_path.is_file():
            self._resume_dir = latest
            return
        try:
            rep = json.loads(rep_path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(rep, dict):
            self.last_report = rep
            if rep.get("state") in ("cancelled", "failed") or rep.get("failed"):
                self._resume_dir = latest

    def in_progress(self) -> bool:
        return self.state in ("checking", "copying", "paused")

    def last_failed_files(self) -> list[dict[str, str]]:
        """Return failed file entries from the most recent copy run."""
        if self.failed_files:
            return list(self.failed_files)
        if self.last_report and isinstance(self.last_report.get("failed"), list):
            return list(self.last_report["failed"])
        return []

    def _moving_or_survey_reason(self) -> str | None:
        """Safety checks that apply both before starting and while copying."""
        sv = getattr(self.d, "survey", None)
        if sv is not None and getattr(sv, "state", "") in ("recording", "starting"):
            return f"survey is {sv.state}"
        s = getattr(self.d, "state", None)
        if s is not None:
            if getattr(s, "gnss_ok", False) and (getattr(s, "gs_ms", 0.0) or 0.0) > MAX_GROUND_SPEED_MS:
                return f"aircraft moving (ground speed {s.gs_ms:.1f} m/s)"
            if getattr(s, "laser_ok", False) and (getattr(s, "agl_m", 0.0) or 0.0) > MAX_AGL_M:
                return f"aircraft airborne (AGL {s.agl_m:.1f} m)"
        return None

    def block_reason(self) -> str | None:
        """Return why a copy cannot start right now, or None if allowed."""
        cam = getattr(self.d, "camera", None)
        if cam is None or not getattr(cam, "enabled", False):
            return "camera is not configured"
        if not cam.base_url():
            return "camera has no Ethernet status_url"
        return self._moving_or_survey_reason()

    def _current_rate_bps(self) -> float:
        if self.state in ("copying", "paused"):
            elapsed = self._active_copy_s
            if self.state == "copying" and self._copy_resumed_at is not None:
                elapsed += max(0.0, time.monotonic() - self._copy_resumed_at)
            if elapsed > 0.02 and self._bytes_transferred > 0:
                return self._bytes_transferred / elapsed
            return 0.0
        return self._final_mb_per_s * 1e6

    def status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of copy progress and readiness."""
        active = self.in_progress()
        block = self.block_reason()
        rate_bps = self._current_rate_bps()
        mb_s = round(rate_bps / 1e6, 2)
        eta_s: float | None = None
        if self.state == "copying" and rate_bps > 0 and self.bytes_total > self.bytes_done:
            eta_s = round((self.bytes_total - self.bytes_done) / rate_bps, 1)
        elif self.state == "done":
            eta_s = 0.0
        return {
            "state": self.state,
            "active": active,
            "can_start": bool(block is None and not active),
            "blocked_reason": block or "",
            "files_done": self.files_done,
            "files_total": self.files_total,
            "files_copied": self.files_copied,
            "files_skipped": self.files_skipped,
            "files_failed": len(self.failed_files),
            "failed_files": list(self.failed_files),
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "mb_per_s": mb_s,
            "eta_s": eta_s,
            "current_file": self.current_file,
            "reason": self.reason,
            "message": self.message,
            "dest_dir": str(self.dest_dir) if self.dest_dir else "",
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
        }

    def _choose_dest_dir(self) -> Path:
        """Reuse an incomplete copy folder on resume, otherwise create a fresh timestamped folder."""
        root = Path(self.d.log_dir) / "camera_images"
        if self._resume_dir is not None and self._resume_dir.is_dir():
            return self._resume_dir
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        candidate = root / stamp
        n = 1
        while candidate.exists():
            candidate = root / f"{stamp}_{n}"
            n += 1
        return candidate

    def _emit(self, level: str, text: str) -> None:
        buddy = getattr(self.d, "buddy", None)
        if buddy is not None:
            buddy.emit(level, text)

    async def start(self) -> dict[str, Any]:
        """Validate safety and disk space, list camera files, and launch the background copy."""
        if self.in_progress():
            err = f"camera image copy is already {self.state}"
            return {"ok": False, "error": err, "status": self.status()}

        block = self.block_reason()
        if block:
            self.reason = block
            self.message = f"Refused: {block}"
            return {"ok": False, "error": block, "status": self.status()}

        try:
            free = shutil.disk_usage(self.d.log_dir).free
        except OSError as exc:
            err = f"could not check log disk space: {exc}"
            self.reason = err
            self.message = f"Refused: {err}"
            return {"ok": False, "error": err, "status": self.status()}

        if free < self.reserve_bytes:
            err = (
                f"not enough free disk space ({free / GB:.1f} GB free, "
                f"need at least {self.reserve_gb:g} GB reserve)"
            )
            self.reason = err
            self.message = f"Refused: {err}"
            return {"ok": False, "error": err, "status": self.status()}

        prev_state = self.state
        self._cancel_requested = False
        self.state = "checking"
        self.reason = ""
        self.message = "Checking camera file list..."
        self.current_file = ""

        cam_files, list_err = await self.d.camera.list_files()
        if self._cancel_requested:
            self.state = "cancelled"
            self.reason = "cancelled by user"
            self.message = "Cancelled while checking camera files"
            return {"ok": False, "error": "cancelled", "status": self.status()}

        if cam_files is None:
            err = list_err or "could not list files from camera"
            self.state = "failed"
            self.reason = err
            self.message = f"Copy failed: {err}"
            self._emit("warn", f"Camera image copy failed: {err}")
            return {"ok": False, "error": err, "status": self.status()}

        block = self.block_reason()
        if block:
            self.state = prev_state
            self.reason = block
            self.message = f"Refused: {block}"
            return {"ok": False, "error": block, "status": self.status()}

        dest = self._choose_dest_dir()
        entries: list[dict[str, Any]] = []
        total_bytes = 0
        remaining_bytes = 0
        for item in cam_files:
            rel = rel_card_path(item.get("path", ""))
            if not rel:
                continue
            size = item.get("size")
            size_int = int(size) if size is not None else None
            entries.append({"api_path": item["path"], "rel": rel, "size": size_int})
            if size_int is not None:
                total_bytes += size_int
                target = dest / rel
                if not (target.is_file() and target.stat().st_size == size_int):
                    remaining_bytes += size_int

        try:
            free = shutil.disk_usage(self.d.log_dir).free
        except OSError as exc:
            self.state = prev_state
            err = f"could not check log disk space: {exc}"
            self.reason = err
            self.message = f"Refused: {err}"
            return {"ok": False, "error": err, "status": self.status()}

        needed = remaining_bytes + self.reserve_bytes
        if free < needed:
            self.state = prev_state
            err = (
                f"not enough free disk space ({free / GB:.1f} GB free, "
                f"need {needed / GB:.1f} GB including {self.reserve_gb:g} GB reserve)"
            )
            self.reason = err
            self.message = f"Refused: {err}"
            return {"ok": False, "error": err, "status": self.status()}

        dest.mkdir(parents=True, exist_ok=True)
        self.dest_dir = dest
        self._resume_dir = dest
        self.started_utc = _utc_iso()
        self.finished_utc = ""
        self.files_done = 0
        self.files_total = len(entries)
        self.files_copied = 0
        self.files_skipped = 0
        self.failed_files = []
        self.bytes_done = 0
        self.bytes_total = total_bytes
        self.current_file = ""
        self._bytes_transferred = 0
        self._active_copy_s = 0.0
        self._copy_resumed_at = time.monotonic()
        self._final_mb_per_s = 0.0
        self.state = "copying"
        self.reason = ""
        self.message = f"Copying 0 of {self.files_total} files..."

        self._emit(
            "info",
            f"Camera image copy started: {self.files_total} files ({self.bytes_total / 1e6:.1f} MB)",
        )

        job = {
            "dest": dest,
            "base_url": self.d.camera.base_url(),
            "entries": entries,
            "camera_files": cam_files,
            "list_error": list_err,
        }
        self._stopped_event = asyncio.Event()
        if self._loop_running and self._wake is not None:
            self._pending_job = job
            self._wake.set()
        else:
            # Fallback when start() is invoked in tests without the supervised run() loop.
            self._job_task = asyncio.create_task(self._run_job(job))

        return {"ok": True, "status": self.status()}

    def cancel(self) -> dict[str, Any]:
        """Request cancellation of an in-progress copy."""
        if not self.in_progress():
            return {"ok": False, "error": "no camera image copy in progress", "status": self.status()}
        self._cancel_requested = True
        self._pending_job = None
        if self._job_task is not None and not self._job_task.done():
            self._job_task.cancel()
        self.state = "cancelled"
        self.reason = "cancelled by user"
        self.message = f"Cancelled ({self.files_done} of {self.files_total} files)"
        return {"ok": True, "status": self.status()}

    async def wait_stopped(self, timeout: float = 2.0) -> None:
        """Wait briefly for a cancelled or finishing job to finish cleanup."""
        ev = self._stopped_event
        if ev is not None and not ev.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout=timeout)

    async def run(self) -> None:
        """Supervised worker loop that executes started copy jobs."""
        self._wake = asyncio.Event()
        self._loop_running = True
        try:
            while True:
                if hasattr(self.d, "sup"):
                    self.d.sup.beat("camera copy")
                if self._pending_job is None:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                    self._wake.clear()
                    continue
                job = self._pending_job
                self._pending_job = None
                self._job_task = asyncio.create_task(self._run_job(job))
                try:
                    await self._job_task
                except asyncio.CancelledError:
                    if not self._cancel_requested:
                        if not self._job_task.done():
                            self._job_task.cancel()
                            with contextlib.suppress(BaseException):
                                await self._job_task
                        raise
                finally:
                    self._job_task = None
        finally:
            self._loop_running = False

    async def _wait_if_paused(self, reason: str | None = None) -> None:
        """Pause while a safety condition is active and resume automatically when clear."""
        why = reason or self._moving_or_survey_reason()
        if why is None:
            return
        if self._copy_resumed_at is not None:
            self._active_copy_s += max(0.0, time.monotonic() - self._copy_resumed_at)
            self._copy_resumed_at = None
        self.state = "paused"
        self.reason = why
        self.message = f"Paused: {why}"
        self._emit("warn", f"Camera image copy paused: {why}")

        last_reason = why
        while True:
            if self._cancel_requested:
                raise asyncio.CancelledError()
            if hasattr(self.d, "sup"):
                self.d.sup.beat("camera copy")
            await asyncio.sleep(min(0.25, self.safety_check_interval_s))
            if self._cancel_requested:
                raise asyncio.CancelledError()
            cur = self._moving_or_survey_reason()
            if cur is None:
                break
            if cur != last_reason:
                last_reason = cur
                self.reason = cur
                self.message = f"Paused: {cur}"

        self.state = "copying"
        self.reason = ""
        self.message = f"Copying {self.files_done} of {self.files_total} files..."
        self._copy_resumed_at = time.monotonic()
        self._emit("info", "Camera image copy resumed")

    async def _download_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        part_path: Path,
        expected_size: int | None,
    ) -> tuple[bool, int, str, str]:
        """Stream one attempt to part_path. Returns (ok, bytes_written, sha256_hex, error)."""
        written = 0
        h = hashlib.sha256()
        last_check = time.monotonic()
        part_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False, 0, "", f"HTTP {resp.status_code}"
                with open(part_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                        if self._cancel_requested:
                            raise asyncio.CancelledError()
                        if chunk:
                            fh.write(chunk)
                            h.update(chunk)
                            n = len(chunk)
                            written += n
                            self.bytes_done += n
                            self._bytes_transferred += n
                        now = time.monotonic()
                        if now - last_check >= self.safety_check_interval_s:
                            last_check = now
                            if hasattr(self.d, "sup"):
                                self.d.sup.beat("camera copy")
                            why = self._moving_or_survey_reason()
                            if why:
                                raise _PausedSignal(why)
        except (_PausedSignal, asyncio.CancelledError):
            self.bytes_done = max(0, self.bytes_done - written)
            self._bytes_transferred = max(0, self._bytes_transferred - written)
            with contextlib.suppress(OSError):
                part_path.unlink()
            raise
        except (httpx.HTTPError, OSError) as exc:
            self.bytes_done = max(0, self.bytes_done - written)
            self._bytes_transferred = max(0, self._bytes_transferred - written)
            with contextlib.suppress(OSError):
                part_path.unlink()
            return False, 0, "", (str(exc).strip() or type(exc).__name__)[:160]

        if expected_size is not None and written != expected_size:
            self.bytes_done = max(0, self.bytes_done - written)
            self._bytes_transferred = max(0, self._bytes_transferred - written)
            with contextlib.suppress(OSError):
                part_path.unlink()
            return False, written, "", f"size mismatch: expected {expected_size} bytes, got {written}"

        return True, written, h.hexdigest(), ""

    def _write_outputs(
        self,
        dest: Path,
        base_url: str,
        camera_files: list[dict],
        list_error: str | None,
        copied_names: list[str],
        skipped_names: list[str],
        hashes: dict[str, str],
        bytes_copied: int,
        bytes_skipped: int,
        final_state: str,
    ) -> dict[str, Any]:
        """Write MANIFEST.sha256 and copy_report.json into dest."""
        manifest_lines = [f"{hashes[rel]}  {rel}" for rel in sorted(hashes)]
        (dest / "MANIFEST.sha256").write_text("\n".join(manifest_lines) + ("\n" if manifest_lines else ""))

        if self._copy_resumed_at is not None:
            self._active_copy_s += max(0.0, time.monotonic() - self._copy_resumed_at)
            self._copy_resumed_at = None
        avg_mb_s = round((self._bytes_transferred / self._active_copy_s) / 1e6, 2) if self._active_copy_s > 0 else 0.0
        self._final_mb_per_s = avg_mb_s
        self.finished_utc = _utc_iso()

        report = {
            "state": final_state,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "dest_dir": str(dest),
            "base_url": base_url,
            "files_total": self.files_total,
            "files_copied": len(copied_names),
            "files_skipped": len(skipped_names),
            "files_failed": len(self.failed_files),
            "copied": copied_names,
            "skipped": skipped_names,
            "failed": list(self.failed_files),
            "bytes_total": self.bytes_total,
            "bytes_copied": bytes_copied,
            "bytes_skipped": bytes_skipped,
            "average_mb_per_s": avg_mb_s,
            "camera_files": camera_files,
            "list_error": list_error,
        }
        (dest / "copy_report.json").write_text(json.dumps(report, indent=2) + "\n")
        self.last_report = report
        return report

    async def _run_job(self, job: dict[str, Any]) -> None:
        dest: Path = job["dest"]
        base_url: str = job["base_url"]
        entries: list[dict[str, Any]] = job["entries"]
        camera_files: list[dict] = job["camera_files"]
        list_error: str | None = job["list_error"]

        copied_names: list[str] = []
        skipped_names: list[str] = []
        hashes: dict[str, str] = {}
        bytes_copied = 0
        bytes_skipped = 0
        dest_resolved = dest.resolve()

        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=10.0, pool=5.0)
        cancelled = False
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                for entry in entries:
                    if self._cancel_requested:
                        cancelled = True
                        break
                    if hasattr(self.d, "sup"):
                        self.d.sup.beat("camera copy")
                    await self._wait_if_paused()

                    rel: str = entry["rel"]
                    expected_size: int | None = entry["size"]
                    target = dest / rel
                    # Defense in depth: ensure target stays inside dest.
                    if not target.resolve().is_relative_to(dest_resolved):
                        self.failed_files.append({"path": rel, "error": "unsafe path"})
                        self.files_done += 1
                        continue

                    self.current_file = rel
                    self.message = f"Copying {self.files_done} of {self.files_total} files..."

                    # Resume: skip any file already present with the exact expected size.
                    if target.is_file() and expected_size is not None and target.stat().st_size == expected_size:
                        digest = await asyncio.to_thread(_sha256_file, target)
                        hashes[rel] = digest
                        skipped_names.append(rel)
                        self.files_skipped += 1
                        self.files_done += 1
                        self.bytes_done += expected_size
                        bytes_skipped += expected_size
                        continue

                    part_path = target.with_name(target.name + ".part")
                    url = f"{base_url}/files/{quote(rel, safe='/')}"
                    attempt = 0
                    last_err = ""
                    succeeded = False
                    while attempt <= MAX_RETRIES:
                        if self._cancel_requested:
                            cancelled = True
                            break
                        await self._wait_if_paused()
                        try:
                            ok, written, digest, err = await self._download_once(client, url, part_path, expected_size)
                        except _PausedSignal as ps:
                            await self._wait_if_paused(ps.reason)
                            continue
                        if ok:
                            part_path.replace(target)
                            hashes[rel] = digest
                            copied_names.append(rel)
                            self.files_copied += 1
                            self.files_done += 1
                            bytes_copied += written
                            succeeded = True
                            break
                        last_err = err
                        attempt += 1
                        if attempt <= MAX_RETRIES and not self._cancel_requested:
                            await asyncio.sleep(self.retry_delay_s)

                    if cancelled:
                        break
                    if not succeeded:
                        with contextlib.suppress(OSError):
                            part_path.unlink()
                        self.failed_files.append({"path": rel, "error": last_err or "download failed"})
                        self.files_done += 1
        except asyncio.CancelledError:
            cancelled = True
            if not self._cancel_requested:
                # Daemon shutdown while copying: save progress cleanly then re-raise.
                self.current_file = ""
                with contextlib.suppress(OSError):
                    self._write_outputs(
                        dest, base_url, camera_files, list_error,
                        copied_names, skipped_names, hashes, bytes_copied, bytes_skipped, "cancelled",
                    )
                self.state = "cancelled"
                if self._stopped_event is not None:
                    self._stopped_event.set()
                raise

        self.current_file = ""
        if cancelled or self._cancel_requested:
            with contextlib.suppress(OSError):
                self._write_outputs(
                    dest, base_url, camera_files, list_error,
                    copied_names, skipped_names, hashes, bytes_copied, bytes_skipped, "cancelled",
                )
            self.state = "cancelled"
            self.reason = "cancelled by user"
            self.message = f"Cancelled ({self.files_done} of {self.files_total} files)"
            self._emit("info", f"Camera image copy cancelled ({self.files_done} of {self.files_total} files)")
        else:
            final_state = "failed" if self.failed_files else "done"
            self._write_outputs(
                dest, base_url, camera_files, list_error,
                copied_names, skipped_names, hashes, bytes_copied, bytes_skipped, final_state,
            )
            self.state = final_state
            mb = self.bytes_done / 1e6
            if self.failed_files:
                self._resume_dir = dest
                self.reason = f"{len(self.failed_files)} file(s) failed"
                self.message = (
                    f"Copied {self.files_copied} files"
                    + (f", skipped {self.files_skipped}" if self.files_skipped else "")
                    + f", {len(self.failed_files)} failed ({mb:.1f} MB)"
                )
                self._emit("warn", f"Camera image copy finished with failed files: {self.message}")
            else:
                self._resume_dir = None
                self.reason = ""
                self.message = (
                    f"Copied {self.files_copied} files"
                    + (f", skipped {self.files_skipped}" if self.files_skipped else "")
                    + f" ({mb:.1f} MB)"
                )
                self._emit("info", f"Camera image copy finished: {self.message}")

        if self._stopped_event is not None:
            self._stopped_event.set()
