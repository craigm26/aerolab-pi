"""Build the AeroLab-Pi demo video from the real running code.

Starts the daemon in simulation mode (snow-survey profile, laser dropouts on,
uncalibrated), drives the cockpit display in headless Chromium over the
DevTools protocol, records each scene in real time, and cuts it together with
title cards and captions into an MP4. Setup-menu scenes come from PNGs made
by tools/setup_screenshots.py; the accuracy card from tools/virtual_flight.py --json.

    python tools/make_demo_video.py --out docs/aerolab-demo.mp4 \\
        --setup-dir <dir of setup PNGs> --accuracy <virtual_flight json>

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
        f = font(FONT_B, 27)
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
            y = VIEW_H + (H - VIEW_H - 38 * len(lines)) // 2
        else:
            bh = 26 + 38 * len(lines)
            d.rectangle([0, H - bh - 18, W, H], fill=(0, 0, 0, 200))
            y = H - bh - 4
        for ln in lines:
            d.text((80, y), ln, font=f, fill=(255, 255, 255, 255))
            y += 38
    if chapter:
        f2 = font(FONT_B, 18)
        tw = d.textlength(chapter, font=f2)
        if band:
            d.text((W - tw - 24, H - 30), chapter, font=f2, fill=ACCENT + (255,))
        else:
            d.rounded_rectangle([20, 18, 44 + tw, 52], radius=8, fill=(0, 0, 0, 190))
            d.text((32, 24), chapter, font=f2, fill=ACCENT + (255,))
    return Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")


def accuracy_card(reports: list[dict]) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((60, 40), "7  ACCURACY, MEASURED", font=font(FONT_B, 22), fill=ACCENT)
    d.text((60, 72), "Real daemon vs simulated sensors speaking each device's protocol, known truth",
           font=font(FONT, 24), fill=(220, 226, 232))
    d.text((60, 108), "Injected: IMU bias +0.8/-0.5°, laser offset 0.62 m (0.40 entered), 1.0° boresight, "
                      "2% corrupt frames, 1% laser spikes", font=font(FONT, 18), fill=MUTED)
    cols = [("Profile", 60), ("Height", 410), ("Offset", 500), ("IMU bias", 590),
            ("Boresight", 760), ("AGL p95", 880), ("Geotag p95", 1000)]
    y = 170
    for name, x in cols:
        d.text((x, y), name, font=font(FONT_B, 18), fill=MUTED)
    y += 36
    names = {"airborne-snow-survey": "Snow survey (Riegl, VN-200)", "survey-budget": "Budget (TF03, F9P, BNO085)",
             "midrange-aircraft": "Mid-range (GRF-500, VN-100)", "flight-controller": "Flight controller (MAVLink)",
             "bench-generic": "Generic serial"}
    for r in reports:
        s, c, b = r["score"], r["calibration"], r["boresight_fit"]
        vals = [names.get(r["profile"], r["profile"]), f"{s['survey_agl_m']:.0f} m", f"{c['laser_offset_m']:.2f} m",
                f"{c['imu_roll_bias_deg']:+.2f} / {c['imu_pitch_bias_deg']:+.2f}°",
                f"{b['boresight_roll_deg']:+.2f}°", f"{s['agl_after_boresight_m']['p95']:.2f} m",
                f"{s['geotag_last_fix_m']['p95']:.1f} → {s['geotag_extrapolated_m']['p95']:.1f} m"]
        for (name, x), v in zip(cols, vals):
            d.text((x, y), v, font=font(FONT, 18), fill=(235, 240, 245))
        y += 44
    d.text((60, y + 20), "True values: offset 0.62 m, bias +0.80 / -0.50°, boresight +1.00°.", font=font(FONT, 20),
           fill=(0, 230, 118))
    d.text((60, y + 54), "Geotag: last GNSS fix → position extrapolated to the trigger instant.", font=font(FONT, 20),
           fill=MUTED)
    d.text((60, y + 84), "Simulation shows the software is right; only flight tests show the installation is.",
           font=font(FONT, 20), fill=MUTED)
    return im


# ---------------------------------------------------------------------------
# Recording the live display
# ---------------------------------------------------------------------------

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
                    return m.get("result", m)

    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def grab(self) -> bytes | None:
        r = await self.cmd("Page.captureScreenshot", format="jpeg", quality=88)
        return base64.b64decode(r["data"]) if "data" in r else None

    def close(self):
        self.proc.terminate()


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
    br = Browser(9340, workdir)
    await br.connect()
    rec = Recorder(br, workdir / "frames")
    (workdir / "frames").mkdir(exist_ok=True)
    marks: dict = {}
    url = f"http://127.0.0.1:{port}/"
    await br.cmd("Page.enable")
    await br.cmd("Emulation.setDeviceMetricsOverride", width=int(W / VIEW_SCALE), height=int(VIEW_H / VIEW_SCALE),
                 deviceScaleFactor=VIEW_SCALE, mobile=False)
    js = br.js

    async def click(sel):
        await js(f"document.querySelector({json.dumps(sel)}).click()")

    # Boot
    rec.start("boot")
    await br.cmd("Page.navigate", url=url)
    for _ in range(80):
        if await js("!document.getElementById('boot')"):
            break
        await asyncio.sleep(0.25)
    await asyncio.sleep(1.5)
    await rec.stop()

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
        if marks["toast"] is None and await js("!!document.querySelector('.toast.alert')"):
            marks["toast"] = time.time() - rec.t0
            t_end = min(t_end, time.time() + 9)
        await asyncio.sleep(0.25)
    await rec.stop()

    # Checks
    rec.start("checks")
    await click("#p-buddy")
    await asyncio.sleep(0.6)
    await click("[data-pane=pane-check]")
    await asyncio.sleep(3.0)
    await js("document.getElementById('bitlist').scrollIntoView({block:'start'})")
    await asyncio.sleep(3.5)
    await js("document.getElementById('healthrows').scrollIntoView({block:'center'})")
    await asyncio.sleep(3.5)
    await rec.stop()

    # Ask
    rec.start("ask")
    await click("[data-pane=pane-ask]")
    await asyncio.sleep(1.0)
    for q in ("Quick status", "Why did the laser drop out?", "Should I descend over the ice?"):
        await js(f"(()=>{{document.getElementById('q').value={json.dumps(q)};}})()")
        await asyncio.sleep(1.2)
        await js("document.getElementById('askform').requestSubmit()")
        await asyncio.sleep(3.5)
    await asyncio.sleep(1.0)
    await rec.stop()
    br.close()
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

    def still(self, im: Image.Image, seconds: float, fade: bool = True):
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
            return
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
        self.ff.wait()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "docs" / "aerolab-demo.mp4"))
    ap.add_argument("--setup-dir", required=True)
    ap.add_argument("--accuracy", required=True)
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="aerolab-video-"))
    cat = configure.load_catalog()
    base = yaml.safe_load((ROOT / "config" / "sensors.yaml").read_text())
    prof = configure.by_id(cat["profiles"])["airborne-snow-survey"]
    cfg = configure.build_config(base, configure.profile_answers(prof, base, cat, "sim"), cat)
    cfg["sim"] = {"faults": True, "bank_deg": 24.0, "start_on_ground": True}
    cfg["buddy"]["ai"] = False
    cfg["logging"]["fallback_dir"] = str(work / "logs")
    cfg["calibration_file"] = str(work / "cal.json")
    with socket.socket() as s_:
        s_.bind(("127.0.0.1", 0))
        port = s_.getsockname()[1]
    cfg["server"]["port"] = port
    (work / "cfg.yaml").write_text(yaml.safe_dump(cfg))
    dproc = subprocess.Popen([sys.executable, str(ROOT / "daemon" / "main.py"), "--config", str(work / "cfg.yaml")],
                             stdout=open(work / "daemon.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    try:
        rec, marks = asyncio.run(record_display(port, work))
    finally:
        dproc.terminate()
    print("recorded", len(rec.frames), "frames; marks", marks)

    setup = sorted(glob.glob(os.path.join(a.setup_dir, "setup_*.png")))
    pick = lambda n: Image.open(setup[n - 1])
    reports = json.load(open(a.accuracy))

    cut = Cut(Path(a.out))
    cut.still(card("AeroLab-Pi", ["A Raspberry Pi logger and cockpit display", "for small-aircraft science payloads.",
                                  "", "Open source. Works offline."], "DEMO"), 4.5)
    cut.still(card("One Pi, every instrument", [
        "Laser altimeter → height above ground, corrected for bank and pitch",
        "GNSS or GNSS/INS → position, time, attitude",
        "Camera and radar trigger → every capture geotagged",
        "Everything logged so a power cut loses almost nothing",
        "A display any iPad in the cockpit can open"], "WHAT IT DOES"), 7.0)
    ch = "1  SETUP"
    for n, txt in ((3, "Setup starts from a profile. The default is an estimate for a light-aircraft ice survey."),
                   (5, "Each device is a preset: driver, baud rate and protocol quirks come with it."),
                   (7, "Laser altimeters are listed with their rated range."),
                   (16, "Setup checks the planned survey height against the laser. Most lasers can't reach 2,000 ft."),
                   (17, "Payloads: cameras bring their trigger settings; the snow radar records on its own GPS."),
                   (25, "A summary before anything is saved. Change it any time with ./configure.sh.")):
        cut.still(caption(pick(n), txt, ch), 4.2, fade=False)

    ch = "2  BOOT SELF-TEST"
    bf = rec.scene_frames("boot")
    cut.clip(bf, 0.3, bf[-1][0], [(0, "Every boot runs a self-test: config, a write-and-read test of the log disk, then each sensor's data rate, bad frames and plausibility."),
                                   (7.5, "The aircraft is on the ramp: the laser reads its own mounting height. Calibration is flagged as not done yet.")], ch)

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
    cut.clip(ff, 0.5, 14.0, [(0, "Height above ground, corrected for bank and pitch. Attitude, position and every sensor's health along the top."),
                             (7.0, "Past 20° of bank the tilt correction is less certain, and the display says so.")], ch)
    cut.clip(ff, max(14.0, tt - 2.0), min(ff[-1][0], tt + 7.0),
             [(0, "Buddy calls out changes: a sensor lost or back, GNSS fix, the low-AGL line, logging, disk, heat, power."),
              (4.5, "The laser drops out over open water or glassy ice; the display shows dashes, never a frozen number.")], ch)

    ch = "5  CHECKS"
    kf = rec.scene_frames("checks")
    cut.clip(kf, 0.0, kf[-1][0], [(0, "Ground check from live data: on the ramp the laser should read about 0 m."),
                                  (3.2, "The self-test results stay available."),
                                  (6.8, "Live data rate, bad frames and age for every sensor. A wrong baud rate shows up here at once.")], ch)

    ch = "6  ASK BUDDY"
    af = rec.scene_frames("ask")
    cut.clip(af, 0.0, af[-1][0], [(0, "Ask answers from the Pi's own data, offline."),
                                  (6.0, "A dropout answer gives when and how long. The data doesn't say why, so causes are only listed as possibilities."),
                                  (11.0, "Flight decisions stay with the pilot.")], ch)

    cut.still(accuracy_card(reports), 10.0)
    cut.still(card("Tested, then flown", [
        "Self-test every boot · ground calibration before each flight",
        "Integrity checks every second in flight",
        "Boresight fitted from banks over flat ground",
        "Virtual flights for every hardware profile in the test suite",
        "", "Not certified avionics: advisory, for the science payload."], "SUMMARY"), 7.0)
    cut.still(card("github.com/craigm26/aerolab-pi", ["./install.sh on a Raspberry Pi 5", "or try it on a laptop:",
                                                      "python daemon/main.py --sim"], "OPEN SOURCE"), 5.0)
    cut.close()
    print(f"wrote {a.out}: {cut.seconds:.0f} s")


if __name__ == "__main__":
    main()
