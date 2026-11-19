"""Screenshots and GIFs of the running HUD, in real time, via Chrome DevTools Protocol.

Start the daemon first, ideally with sim.faults: true so callouts appear:
    python daemon/main.py --config <sim config> --port 8765
then:
    python tools/hud_screenshots.py http://127.0.0.1:8765/ <outdir>

Writes boot.gif, hud_live.gif, hud_ipad.png, hud_phone.png, hud_wide.png,
hud_wide_lowagl.png, hud_callout.png, buddy_callouts.png, buddy_check.png,
buddy_ask.png. Needs chromium, websockets and Pillow.
"""
import asyncio, base64, io, json, subprocess, sys, time, urllib.request
import websockets
from PIL import Image

URL, OUT = sys.argv[1], sys.argv[2]
ASK = ["Quick status", "How many triggers so far?", "Should I descend to 500 feet?", "Why did the laser drop out?"]
proc = subprocess.Popen(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                         "--remote-debugging-port=9333", "--user-data-dir=" + OUT + "/chrome-prof", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try:
        tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json")); break
    except Exception: time.sleep(0.3)
ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")


async def main():
    async with websockets.connect(ws_url, max_size=50_000_000) as ws:
        n = 0
        async def cmd(method, **params):
            nonlocal n; n += 1; my = n
            await ws.send(json.dumps({"id": my, "method": method, "params": params}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == my: return m.get("result", m)
        async def ev(expr):
            return (await cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True))["result"].get("value")
        async def grab(fmt="png"):
            for _ in range(20):
                r = await cmd("Page.captureScreenshot", format=fmt, **({"quality": 85} if fmt == "jpeg" else {}))
                if "data" in r: return Image.open(io.BytesIO(base64.b64decode(r["data"]))).convert("RGB")
                await asyncio.sleep(0.1)
        async def shot(name):
            (await grab()).save(f"{OUT}/{name}.png"); print("saved", name, flush=True)
        async def size(w, h, scale):
            await cmd("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=scale, mobile=w < 600)
        async def gif(name, seconds, fmt="jpeg"):
            frames, t0 = [], time.time()
            while time.time() - t0 < seconds:
                frames.append((time.time() - t0, await grab(fmt)))
            durs = [int((frames[i + 1][0] - frames[i][0]) * 1000) for i in range(len(frames) - 1)] + [1200]
            pal = [f.quantize(colors=96, method=Image.Quantize.MEDIANCUT) for _, f in frames]
            pal[0].save(f"{OUT}/{name}.gif", save_all=True, append_images=pal[1:], duration=durs, loop=0, optimize=True)
            print("saved", name, len(frames), "frames", flush=True)
        skip_boot = "document.getElementById('boot') && document.getElementById('boot').click()"

        await cmd("Page.enable")
        # 1. boot sequence
        await size(1200, 675, 1)
        await cmd("Page.navigate", url=URL)
        await gif("boot", 7.5)
        # 2. layouts
        for name, w, h, sc in (("hud_ipad", 1180, 820, 2), ("hud_phone", 390, 844, 3), ("hud_wide", 1200, 675, 2)):
            await size(w, h, sc)
            await cmd("Page.navigate", url=URL); await asyncio.sleep(1.0)
            await ev(skip_boot); await asyncio.sleep(2.0)
            await shot(name)
        # 3. low AGL
        for _ in range(240):
            if await ev("document.getElementById('agl-ft').classList.contains('agl-low')"): break
            await asyncio.sleep(0.25)
        await shot("hud_wide_lowagl")
        # 4. live GIF
        await size(1200, 675, 1)
        await gif("hud_live", 8)
        # 5. callout toast (sim faults: laser drops 4 s in every 45 s)
        await size(1200, 675, 2)
        for _ in range(240):
            if await ev("!!document.querySelector('.toast.alert')"): break
            await asyncio.sleep(0.25)
        await asyncio.sleep(0.4); await shot("hud_callout")
        await asyncio.sleep(5)
        # 6. buddy panel
        await ev("document.getElementById('p-buddy').click()"); await asyncio.sleep(0.6); await shot("buddy_callouts")
        await ev("document.querySelector('[data-pane=pane-check]').click()"); await asyncio.sleep(2.0); await shot("buddy_check")
        await ev("document.querySelector('[data-pane=pane-ask]').click()"); await asyncio.sleep(0.3)
        for q in ASK:
            await ev(f"(()=>{{document.getElementById('q').value={json.dumps(q)};document.getElementById('askform').requestSubmit()}})()")
            await asyncio.sleep(1.5)
        await asyncio.sleep(1.0)
        print("ask:", await ev("[...document.querySelectorAll('.msg')].map(m=>m.textContent).join(' || ')"))
        await shot("buddy_ask")

try:
    asyncio.run(main())
finally:
    proc.terminate()
