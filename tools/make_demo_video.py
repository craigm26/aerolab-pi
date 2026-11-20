"""Build the AeroLab-Pi demo video from the real running code.

Starts the daemon in simulation mode (snow-survey profile, laser dropouts on,
uncalibrated), drives the cockpit display in headless Chromium over the
DevTools protocol, records each scene in real time, and cuts it together with
title cards and captions into an MP4. Setup-menu scenes come from PNGs made
by tools/setup_screenshots.py; the accuracy card from tools/virtual_flight.py --json.

    python tools/make_demo_video.py --out docs/aerolab-demo.mp4 \\
        --setup-dir <dir of setup PNGs> --accuracy <virtual_flight json> \\
        --work-dir <scratch directory>

Reuse previously generated accuracy JSON on machines where live-mode virtual
serial tests are not allowed. --reuse-recording re-edits the saved frames.
All daemon runs use --sim on a free localhost port in 8780-8799. The shutdown
scene only prepares and cancels; it never calls the confirmation endpoint.

Needs chromium, ffmpeg, websockets, Pillow.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets
import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import configure  # noqa: E402

W, H, FPS = 1280, 720, 30
VIEW_H = 630                 # recorded display height; the caption band sits below it
VIEW_SCALE = 1.25            # record at 1024x504 CSS px, 1.25x, so panel text is readable
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BG = (5, 8, 12)
ACCENT = (0, 229, 255)
MUTED = (139, 148, 158)


def font(path, size):
    return ImageFont.truetype(path, size)


# ---------------------------------------------------------------------------
# Cards and captions
# ---------------------------------------------------------------------------

def card(title: str, lines: list[str], kicker: str = "") -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    y = 200
    if kicker:
        d.text((110, y - 60), kicker, font=font(FONT_B, 22), fill=ACCENT)
    d.text((110, y), title, font=font(FONT_B, 52), fill=(255, 255, 255))
    y += 95
    for ln in lines:
        d.text((112, y), ln, font=font(FONT, 28), fill=(200, 208, 216))
        y += 46
    d.line([(110, 640), (1170, 640)], fill=(30, 40, 50), width=2)
    d.text((110, 655), "AeroLab-Pi  ·  github.com/craigm26/aerolab-pi", font=font(FONT, 20), fill=MUTED)
    return im


def caption(im: Image.Image, text: str, chapter: str = "", band: bool = False) -> Image.Image:
    """band=True: the picture sits above a caption band and is never covered (live display scenes)."""
    im = im.convert("RGB").copy()
    if band:
        bg = Image.new("RGB", (W, H), (0, 0, 0))
        im = im.resize((W, VIEW_H)) if im.size != (W, VIEW_H) else im
        bg.paste(im, (0, 0))
        im = bg
    elif im.size != (W, H):
        bg = Image.new("RGB", (W, H), BG)
        im.thumbnail((W, H - 0))
        bg.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
        im = bg
    if not text and not chapter:
        return im
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    if text:
        f = font(FONT_B, 25)
        words, lines, cur = text.split(), [], ""
        for w_ in words:
            t = (cur + " " + w_).strip()
            if d.textlength(t, font=f) > W - 160:
                lines.append(cur)
                cur = w_
            else:
                cur = t
        lines.append(cur)
        if band:
            if len(lines) > 2:
                raise ValueError(f"Caption exceeds two lines: {text}")
            y = VIEW_H + 22
        else:
            bh = 26 + 38 * len(lines)
            d.rectangle([0, H - bh - 18, W, H], fill=(0, 0, 0, 200))
            y = H - bh - 4
        for ln in lines:
            d.text((80, y), ln, font=f, fill=(255, 255, 255, 255))
            y += 32
    if chapter:
        f2 = font(FONT_B, 18)
        tw = d.textlength(chapter, font=f2)
        if band:
            d.text((80, VIEW_H + 1), chapter, font=f2, fill=ACCENT + (255,))
        else:
            d.rounded_rectangle([20, 18, 44 + tw, 52], radius=8, fill=(0, 0, 0, 190))
            d.text((32, 24), chapter, font=f2, fill=ACCENT + (255,))
    return Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")


def accuracy_card(reports: list[dict]) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((60, 40), "10  ACCURACY, MEASURED", font=font(FONT_B, 22), fill=ACCENT)
    d.text((60, 72), "Real daemon vs simulated sensors speaking each device's protocol, known truth",
           font=font(FONT, 24), fill=(220, 226, 232))
    d.text((60, 108), "Injected: IMU bias +0.8/-0.5°, laser offset 0.62 m (0.40 entered), 1.0° boresight, "
                      "2% corrupt frames, 1% laser spikes", font=font(FONT, 18), fill=MUTED)
    cols = [("Profile", 60), ("Height", 395), ("Offset", 470), ("IMU bias", 545), ("Boresight", 700),
            ("AGL p95", 815), ("Laser pts p95", 925), ("Logged", 1075)]
    y = 170
    for name, x in cols:
        d.text((x, y), name, font=font(FONT_B, 17), fill=MUTED)
    y += 36
    names = {"airborne-snow-survey": "Snow survey (Reach M2, ILM-150-R)", "long-range-survey": "Long-range (Riegl, VN-200)",
             "survey-budget": "Budget (TF03, F9P, BNO085)", "midrange-aircraft": "Mid-range (GRF-500, VN-100)",
             "flight-controller": "Flight controller (MAVLink)", "bench-generic": "Generic serial"}
    na = lambda v, f: "n/a" if v is None or v != v else f.format(v)
    for r in reports:
        s_, c, b = r["score"], r["calibration"], r.get("boresight_fit") or {}
        att = r.get("has_attitude", True)
        vals = [names.get(r["profile"], r["profile"]), f"{s_['survey_agl_m']:.0f} m", f"{c['laser_offset_m']:.2f} m",
                (f"{c['imu_roll_bias_deg']:+.2f}/{c['imu_pitch_bias_deg']:+.2f}°" if att else "no sensor"),
                (na(b.get("boresight_roll_deg"), "{:+.2f}°") if att else "n/a"),
                f"{s_['agl_after_boresight_m']['p95']:.2f} m",
                na(s_.get("laser_point_position_m", {}).get("p95"), "{:.2f} m"),
                f"{r.get('completeness', {}).get('pct', float('nan')):.1f}%"]
        for (name, x), v in zip(cols, vals):
            d.text((x, y), v, font=font(FONT, 17), fill=(235, 240, 245))
        y += 42
    d.text((60, y + 20), "True values: offset 0.62 m, bias +0.80 / -0.50°, boresight +1.00°.", font=font(FONT, 20),
           fill=(0, 230, 118))
    d.text((60, y + 54), "Logged: laser readings saved / clean frames sent while running. Laser pts: position error "
                         "of each laser reading matched on GNSS time.", font=font(FONT, 18), fill=MUTED)
    d.text((60, y + 84), "Saved virtual-flight results. Bench and flight tests are still needed.",
           font=font(FONT, 20), fill=MUTED)
    return im


# ---------------------------------------------------------------------------
# Recording the live display
# ---------------------------------------------------------------------------

def stop_process(proc):
    """Stop only a child we own, and reap it even when a scene fails."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def free_port():
    for port in range(8780, 8800):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free demo port in 8780-8799")


class Browser:
    def __init__(self, port: int, workdir: Path):
        self.proc = subprocess.Popen(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                                      f"--remote-debugging-port={port}", f"--user-data-dir={workdir}/prof", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                break
            except Exception:
                time.sleep(0.3)
        else:
            self.close()
            raise RuntimeError("Chromium did not start")
        self.ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        self.n = 0
        self.lock = asyncio.Lock()

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=50_000_000)

    async def cmd(self, method, **params):
        async with self.lock:
            self.n += 1
            my = self.n
            await self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
            while True:
                m = json.loads(await self.ws.recv())
                if m.get("id") == my:
                    if "error" in m:
                        raise RuntimeError(m["error"])
                    return m.get("result", m)

    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r.get("result", {}).get("value")

    async def grab(self) -> bytes | None:
        try:
            r = await self.cmd("Page.captureScreenshot", format="jpeg", quality=88)
        except RuntimeError as exc:
            if "Not attached to an active page" in str(exc):
                return None  # Chromium is navigating away from about:blank.
            raise
        return base64.b64decode(r["data"]) if "data" in r else None

    def close(self):
        stop_process(self.proc)


class Recorder:
    """Captures frames continuously while a scene plays."""

    def __init__(self, br: Browser, outdir: Path):
        self.br, self.out = br, outdir
        self.frames: list[tuple[str, float, str]] = []   # (scene, t, path)
        self.task = None
        self.scene = None
        self.t0 = 0.0
        self.k = 0

    async def _loop(self):
        while True:
            data = await self.br.grab()
            if data:
                p = self.out / f"f{self.k:06d}.jpg"
                p.write_bytes(data)
                self.frames.append((self.scene, time.time() - self.t0, str(p)))
                self.k += 1
            await asyncio.sleep(0.01)

    def start(self, scene: str):
        self.scene, self.t0 = scene, time.time()
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    def scene_frames(self, scene):
        return [(t, p) for s, t, p in self.frames if s == scene]


async def record_display(port: int, workdir: Path) -> tuple[Recorder, dict]:
    br = Browser(free_port(), workdir)
    try:
        return await record_scenes(br, port, workdir)
    finally:
        try:
            if hasattr(br, "ws"):
                await br.ws.close()
        finally:
            br.close()


async def record_scenes(br: Browser, port: int, workdir: Path) -> tuple[Recorder, dict]:
    await br.connect()
    rec = Recorder(br, workdir / "frames")
    (workdir / "frames").mkdir(exist_ok=True)
    marks: dict = {}
    url = f"http://127.0.0.1:{port}/"
    await br.cmd("Page.enable")
    # The boot graphic needs a taller CSS viewport than the instrument panels.
    await br.cmd("Emulation.setDeviceMetricsOverride", width=1536, height=756,
                 deviceScaleFactor=5 / 6, mobile=False)
    js = br.js

    async def click(sel):
        await js(f"document.querySelector({json.dumps(sel)}).click()")

    async def wait_for(expr, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await js(expr):
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"UI did not reach: {expr}")

    async def scroll(sel, block="center"):
        await js(f"document.querySelector({json.dumps(sel)}).scrollIntoView({{block:{json.dumps(block)}}})")
        await asyncio.sleep(0.3)

    async def hold(sel, seconds):
        await scroll(sel)
        await js(f"document.querySelector({json.dumps(sel)}).dispatchEvent(new PointerEvent('pointerdown', {{bubbles:true}}))")
        await asyncio.sleep(seconds + 0.2)
        await js(f"document.querySelector({json.dumps(sel)})?.dispatchEvent(new PointerEvent('pointerup', {{bubbles:true}}))")

    # Boot
    rec.start("boot")
    await br.cmd("Page.navigate", url=url)
    await wait_for("!!document.getElementById('p-survey')")
    for _ in range(80):
        if await js("!document.getElementById('boot')"):
            break
        await asyncio.sleep(0.25)
    await asyncio.sleep(1.5)
    await rec.stop()
    await br.cmd("Emulation.setDeviceMetricsOverride", width=int(W / VIEW_SCALE), height=int(VIEW_H / VIEW_SCALE),
                 deviceScaleFactor=VIEW_SCALE, mobile=False)

    # Calibration
    rec.start("cal")
    await asyncio.sleep(1.0)
    await click("#p-buddy")
    await asyncio.sleep(0.8)
    await click("[data-pane=pane-cal]")
    await asyncio.sleep(1.2)
    await js("document.getElementById('c-elev').value='120';document.getElementById('c-sec').value='10'")
    await asyncio.sleep(1.0)
    await click("#c-start")
    marks["cal_start"] = time.time() - rec.t0
    for _ in range(80):
        if await js("document.getElementById('c-status').textContent.startsWith('Result')"):
            break
        await asyncio.sleep(0.25)
    marks["cal_done"] = time.time() - rec.t0
    await asyncio.sleep(3.5)
    await js("document.getElementById('c-cmp').scrollIntoView({block:'center'})")
    await asyncio.sleep(2.5)
    await click("#c-apply")
    await asyncio.sleep(1.2)
    await js("document.getElementById('c-current').scrollIntoView({block:'center'})")
    await asyncio.sleep(3.0)
    await click("#b-close")
    await asyncio.sleep(1.0)
    await rec.stop()

    # Flight: display, tilt badge, callouts
    rec.start("flight")
    marks["toast"] = None
    t_end = time.time() + 70
    while time.time() < t_end:
        if marks["toast"] is None and time.time() - rec.t0 >= 14 and await js(
                "Array.from(document.querySelectorAll('.toast.warn')).some(el => el.textContent.includes('no ground return'))"):
            marks["toast"] = time.time() - rec.t0
            t_end = min(t_end, time.time() + 9)
        await asyncio.sleep(0.25)
    await rec.stop()

    # Checks
    rec.start("checks")
    await click("#p-buddy")
    await asyncio.sleep(0.6)
    await click("[data-pane=pane-check]")
    await asyncio.sleep(4.5)
    await js("document.getElementById('datastat').scrollIntoView({block:'center'})")
    await asyncio.sleep(4.0)
    await js("document.getElementById('bitlist').scrollIntoView({block:'start'})")
    await asyncio.sleep(3.5)
    await js("document.getElementById('healthrows').scrollIntoView({block:'center'})")
    await asyncio.sleep(3.5)
    await rec.stop()

    # Ask
    rec.start("ask")
    await click("[data-pane=pane-ask]")
    await asyncio.sleep(1.0)
    for q in ("What's wrong?", "Why did the laser drop out?", "Should I descend over the ice?"):
        await js(f"(()=>{{document.getElementById('q').value={json.dumps(q)};}})()")
        await asyncio.sleep(1.2)
        await js("document.getElementById('askform').requestSubmit()")
        await asyncio.sleep(3.5)
    await asyncio.sleep(1.0)
    await rec.stop()

    # Survey: the real readiness gate, recording confirmation and verified package.
    await click("#b-close")
    await br.cmd("Emulation.setDeviceMetricsOverride", width=W, height=VIEW_H,
                 deviceScaleFactor=1, mobile=False)
    await click("#p-survey")
    await wait_for("!!document.querySelector('#sv-start')")
    await js("document.querySelector('#sv-name').value='Demo survey'")
    # Wait for an actual simulated laser dropout to exercise the readiness
    # review, rather than changing findings or supplying fake browser state.
    rec.start("survey")
    await asyncio.sleep(2)
    await wait_for("(async () => (await (await fetch('/api/survey/readiness', {method:'POST'})).json()).verdict.level === 'red')()",
                   timeout=90)
    marks["readiness_start"] = time.time() - rec.t0
    await click("#sv-start")
    await wait_for("!!document.querySelector('#sv-force')")
    marks["readiness"] = time.time() - rec.t0
    await scroll("#sv-body", "start")
    await asyncio.sleep(4)
    await scroll("#sv-force")
    await asyncio.sleep(1)
    await click("#sv-force")
    await wait_for("!!document.querySelector('#sv-stop')")
    marks["survey_start"] = time.time() - rec.t0
    await scroll("#sv-msg", "start")
    await wait_for("document.querySelector('#sv-msg')?.textContent === 'Confirmed: everything is recording'")
    # Keep the actual 15-second window in the source recording. The edit cuts
    # the waiting, not the confirmation logic (which can finish early).
    await wait_for("Number(document.querySelector('#sv-el')?.textContent.split(':').pop()) >= 15")
    marks["confirmed"] = time.time() - rec.t0
    await asyncio.sleep(5)
    await hold("#sv-stop", 1.5)
    await wait_for("document.querySelector('#sv-body')?.textContent.includes('verified on 2 of 2 drive(s)')")
    await scroll("#sv-body > .sect", "start")
    marks["saved"] = time.time() - rec.t0
    await asyncio.sleep(4)
    await scroll("#sv-camcopy")
    await wait_for("document.querySelector('#cc-reason')?.textContent.includes('Unavailable: aircraft moving')")
    marks["camera_copy"] = time.time() - rec.t0
    await asyncio.sleep(5)
    await rec.stop()

    # Make only our disposable backup target unwritable. This exercises real
    # final_sync error handling, without fabricating UI state or touching disks.
    backup = workdir / "backup"
    backup.rename(workdir / "backup-saved")
    backup.write_text("Demo fault: backup target is a file, not a directory.\n")
    try:
        rec.start("shutdown")
        await hold("#sv-off", 2)
        await wait_for("!!document.querySelector('#sd-retry')")
        await scroll("#sv-body", "start")
        marks["review"] = time.time() - rec.t0
        await asyncio.sleep(5)
        await scroll("#sd-override")
        marks["review_controls"] = time.time() - rec.t0
        await asyncio.sleep(4)
        # Never press either power-off confirmation control.
        await click("#sd-cancel")
        await wait_for("!!document.querySelector('#sv-start')")
        await asyncio.sleep(0.8)
        await rec.stop()
    finally:
        backup.unlink()
        (workdir / "backup-saved").rename(backup)
    return rec, marks


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

class Cut:
    def __init__(self, path: Path):
        self.ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                    "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium",
                                    "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
                                   stdin=subprocess.PIPE)
        self.seconds = 0.0
        self.timeline = []

    def still(self, im: Image.Image, seconds: float, fade: bool = True):
        self.timeline.append({"kind": "still", "start": self.seconds, "end": self.seconds + seconds})
        data = im.convert("RGB").tobytes()
        n = int(seconds * FPS)
        for i in range(n):
            if fade and (i < 8 or i > n - 8):
                k = min(i, n - 1 - i) / 8
                self.ff.stdin.write(Image.blend(Image.new("RGB", (W, H), BG), im, max(0.0, min(1.0, k))).tobytes())
            else:
                self.ff.stdin.write(data)
        self.seconds += seconds

    def clip(self, frames: list[tuple[float, str]], start: float, end: float, captions: list[tuple[float, str]],
             chapter: str):
        """Play recorded frames from start to end (seconds into the scene), captions keyed by scene time."""
        frames = sorted(frames)
        if not frames:
            raise RuntimeError(f"No recorded frames for {chapter}")
        if end <= start:
            return
        self.timeline.append({"kind": "clip", "chapter": chapter, "start": self.seconds,
                              "end": self.seconds + end - start, "source_start": start})
        j, cache = 0, {}
        t = start
        while t < end:
            while j + 1 < len(frames) and frames[j + 1][0] <= t:
                j += 1
            cap = ""
            for ct, txt in captions:
                if t >= ct:
                    cap = txt
            key = (frames[j][1], cap)
            if key not in cache:
                cache.clear()
                cache[key] = caption(Image.open(frames[j][1]), cap, chapter, band=True).tobytes()
            self.ff.stdin.write(cache[key])
            t += 1.0 / FPS
        self.seconds += end - start

    def close(self):
        self.ff.stdin.close()
        if self.ff.wait() != 0:
            raise RuntimeError("ffmpeg failed")


def main():
    # Let our finally blocks reap Chromium/daemon/encoder on a tracked-PID kill.
    def terminate(_signum, _frame):
        raise SystemExit("Demo render terminated")

    signal.signal(signal.SIGTERM, terminate)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "docs" / "aerolab-demo.mp4"))
    ap.add_argument("--setup-dir", required=True)
    ap.add_argument("--accuracy", required=True)
    ap.add_argument("--work-dir", help="scratch directory for config, logs and recorded frames")
    ap.add_argument("--reuse-recording", action="store_true", help="edit saved frames without starting the daemon")
    a = ap.parse_args()
    if a.reuse_recording and not a.work_dir:
        ap.error("--reuse-recording requires --work-dir")
    setup = sorted(glob.glob(os.path.join(a.setup_dir, "setup_*.png")))
    if len(setup) < 32:
        ap.error("Regenerate setup PNGs with the current setup_screenshots.py (32 screens expected)")
    reports = json.loads(Path(a.accuracy).read_text())

    work = Path(a.work_dir).resolve() if a.work_dir else Path(tempfile.mkdtemp(prefix="aerolab-video-"))
    work.mkdir(parents=True, exist_ok=True)
    manifest = work / "recording.json"
    if a.reuse_recording:
        saved = json.loads(manifest.read_text())
        rec, marks = Recorder(None, work / "frames"), saved["marks"]
        rec.frames = saved["frames"]
    else:
        if manifest.exists():
            raise RuntimeError("Use a fresh work directory or --reuse-recording")
        cat = configure.load_catalog()
        base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
        prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
        cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "sim"), cat)
        cfg["sim"] = {"faults": True, "bank_deg": 4.0, "start_on_ground": True}
        cfg["buddy"]["ai"] = False
        cfg["logging"]["fallback_dir"] = str(work / "logs")
        # Relative to the child's scratch cwd, keeping the backup error readable.
        cfg["logging"]["mirror_dirs"] = ["backup"]
        (work / "backup").mkdir(exist_ok=True)
        cfg["camera"].update(status_url="http://127.0.0.1:8799/status", control_capture=True)
        cfg["timing"]["set_clock"] = True  # disabled by the daemon in simulation
        cfg["calibration_file"] = str(work / "cal.json")
        port = free_port()
        cfg["server"].update(host="127.0.0.1", port=port)
        (work / "cfg.yaml").write_text(yaml.safe_dump(cfg))
        with (work / "daemon.log").open("w") as log:
            dproc = subprocess.Popen([sys.executable, str(ROOT / "daemon" / "main.py"), "--sim",
                                      "--config", str(work / "cfg.yaml")], cwd=work,
                                     stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(60):
                    if dproc.poll() is not None:
                        raise RuntimeError(f"Daemon exited; see {work / 'daemon.log'}")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
                            state = json.load(response)
                        assert state["sim_mode"], "Demo requires simulation"
                        break
                    except OSError:
                        time.sleep(0.3)
                else:
                    raise RuntimeError("Simulation daemon did not become ready")
                rec, marks = asyncio.run(record_display(port, work))
            finally:
                stop_process(dproc)
        manifest.write_text(json.dumps({"frames": rec.frames, "marks": marks}, indent=2))
    print("recorded", len(rec.frames), "frames; marks", marks, flush=True)

    pick = lambda n: Image.open(setup[n - 1])

    cut = Cut(Path(a.out))
    try:
        cut.still(card("AeroLab-Pi", ["A Raspberry Pi logger and cockpit display", "for small-aircraft science payloads.",
                                      "", "Open source. Works offline."], "DEMO"), 3.0)
        cut.still(card("One Pi, every instrument", [
            "Laser height above ground, tilt-corrected when attitude is available",
            "GNSS or GNSS/INS → position, time, attitude",
            "Camera and radar trigger → every capture geotagged",
            "Sensor readings and raw data saved on disk",
            "A display any iPad in the cockpit can open"], "WHAT IT DOES"), 5.0)
        ch = "1  SETUP"
        for n, txt in ((3, "Setup starts from a profile. This snow survey uses Reach M2, AgLaser ILM-150-R, RedEdge-P and snow radar."),
                       (9, "Each device is a preset: driver, baud rate and settings, with notes on how to set the device up."),
                       (8, "Laser altimeters are listed with their rated range."),
                       (18, "Setup checks survey height against laser range. Here 2,250 ft is beyond the ILM-150-R's range."),
                       (19, "The camera captures on its own timer and the Pi monitors exposures. The radar records on its own GPS."),
                       (24, "GNSS and PPS provide time references for matching sensor readings."),
                       (25, "System clock from GNSS keeps UTC without internet. Clock sync is disabled in this simulation."),
                       (32, "A summary before anything is saved. Change it any time with ./configure.sh.")):
            cut.still(caption(pick(n), txt, ch), 3.4, fade=False)

        ch = "2  BOOT SELF-TEST"
        bf = rec.scene_frames("boot")
        cut.clip(bf, 0.3, bf[-1][0], [(0, "Every boot runs a self-test: config, a write-and-read test of the log disk, then each sensor's data rate, bad frames and plausibility."),
                                       (7.5, "On the ramp: the laser reads its own mounting height, attitude is left to post-processing, calibration is not done yet.")], ch)

        ch = "3  GROUND CALIBRATION"
        cf = rec.scene_frames("cal")
        cs, cd = marks["cal_start"], marks["cal_done"]
        cut.clip(cf, 0.0, cs + 2.0, [(0, "Before the first flight: Buddy → Calibrate. Aircraft parked, engine off, enter the field elevation."),
                                     (cs - 0.5, "The Pi records the laser, attitude and GNSS while the aircraft sits still.")], ch)
        cut.clip(cf, max(cs + 2.0, cd - 3.0), cf[-1][0],
                 [(0, "On the ground true AGL is zero, so the laser measures its own mounting height. The attitude sensor's tilt shows up as a bias."),
                  (cd + 3.5, "Measured values against current ones. Apply saves them, every flight record keeps a copy, and the aircraft departs.")], ch)

        ch = "4  IN FLIGHT"
        ff = rec.scene_frames("flight")
        tt = marks.get("toast") or ff[-1][0] - 6
        cut.clip(ff, 0.5, 10.5, [(0, "Laser AGL at 9 Hz, position at 10 Hz, every sensor's health along the top."),
                                 (7.0, "Attitude is applied in post-processing, so the display says the AGL is not tilt-corrected.")], ch)
        cut.clip(ff, max(14.0, tt - 2.0), min(ff[-1][0], tt + 7.0),
                 [(0, "Buddy calls out changes: a sensor lost or back, GNSS fix, the low-AGL line, logging, disk, heat, power."),
                  (tt + 1.0, "A simulated laser dropout: NO RETURN replaces the AGL reading. The amber callout reports it.")], ch)

        ch = "5  CHECKS"
        kf = rec.scene_frames("checks")
        cut.clip(kf, 0.0, kf[-1][0], [(0, "One verdict up top, then each problem with numbered fixes, most likely first."),
                                      (4.6, "Science data: readings saved, waiting and lost; raw bytes per sensor; mirror; PPS."),
                                      (8.6, "The boot self-test results stay available."),
                                      (12.2, "Live data rate, bad frames and age for every sensor. A wrong baud rate shows up here at once.")], ch)

        ch = "6  ASK BUDDY"
        af = rec.scene_frames("ask")
        cut.clip(af, 0.0, min(af[-1][0], 14.5), [(0, "Ask \"what's wrong?\" and Buddy answers with the same findings and the first thing to try."),
                                      (6.0, "A dropout answer gives when and how long. The data doesn't say why, so causes are only listed as possibilities."),
                                      (11.0, "Flight decisions stay with the pilot.")], ch)

        ch = "7  SURVEY"
        sf = rec.scene_frames("survey")
        start, confirmed = marks["survey_start"], marks["confirmed"]
        cut.clip(sf, 0, 2, [
            (0, "SURVEY checks readiness before starting. The Reach raw log status updates here.")], ch)
        cut.clip(sf, marks["readiness_start"], start + 1.5, [
            (0, "This demo overrides the laser readiness check. Review the findings before a real survey.")], ch)
        cut.clip(sf, confirmed, confirmed + 5, [
            (0, "Within 15 seconds, ticks confirm new readings, fixes, exposures and saved rows.")], ch)
        cut.clip(sf, marks["saved"] - 2, marks["saved"] + 4, [
            (0, "Hold STOP to save the survey package and verify every copy. These are scratch folders.")], ch)
        cut.clip(sf, marks["camera_copy"], marks["camera_copy"] + 5, [
            (0, "Copy camera images to the Pi after landing. Copying is blocked while the aircraft moves.")], "8  CAMERA IMAGES")

        sd = rec.scene_frames("shutdown")
        cut.clip(sd, 0, sd[-1][0], [
            (0, "Safe shutdown first prepares the data and camera, then opens a review."),
            (marks["review"], "Each step shows pass or fail. Here a scratch backup target was made unwritable."),
            (marks["review_controls"], "Retry, Cancel, or hold 5 seconds to override. This demo cancels without powering off.")], "9  SHUTDOWN REVIEW")

        cut.still(card("Data saved for processing", [
            "Every laser reading and GNSS fix logged with nanosecond timestamps",
            "Every raw byte from every sensor kept on disk, optionally mirrored",
            "Logging moves to another disk if the SSD goes; crashed tasks restart",
            "Laser points matched to GNSS position on GNSS time, refinable later",
            "", "tools/export_laser.py  ·  tools/raw_export.py"], "DATA"), 5.0)
        cut.still(accuracy_card(reports), 8.0)
        cut.still(card("Tested, then flown", [
            "Self-test every boot · ground calibration before each flight",
            "Integrity checks every second in flight · readiness report per flight",
            "System clock from GNSS via chrony, with PPS when wired",
            "Virtual flights for every hardware profile in the test suite",
            "Clock sync is disabled in simulation", "Not certified avionics: advisory, for the science payload."], "SUMMARY"), 6.0)
        cut.still(card("github.com/craigm26/aerolab-pi", ["./install.sh on a Raspberry Pi 5", "or try it on a laptop:",
                                                          "python daemon/main.py --sim",
                                                          "", "Simulation checks the software;",
                                                          "the bench and flight tests show the installation is."], "OPEN SOURCE"), 4.0)
    finally:
        cut.close()
    (work / "timeline.json").write_text(json.dumps(cut.timeline, indent=2))
    print(f"wrote {a.out}: {cut.seconds:.0f} s")


if __name__ == "__main__":
    main()
