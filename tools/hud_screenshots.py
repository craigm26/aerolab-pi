"""Real-time screenshots + GIF of the running HUD via Chrome DevTools Protocol.

Usage: start `python daemon/main.py --sim --port 8765`, then
       python tools/hud_screenshots.py http://127.0.0.1:8765/ <outdir>   (needs chromium, websockets, Pillow)
"""
import asyncio, base64, io, json, subprocess, sys, time, urllib.request
import websockets
from PIL import Image

URL, OUT = sys.argv[1], sys.argv[2]
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
            return (await cmd("Runtime.evaluate", expression=expr, returnByValue=True))["result"].get("value")
        async def shot(name, w, h, scale):
            await cmd("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=scale, mobile=w < 600)
            await cmd("Page.navigate", url=URL); await asyncio.sleep(3.5)
            r = await cmd("Page.captureScreenshot", format="png")
            open(f"{OUT}/{name}.png", "wb").write(base64.b64decode(r["data"])); print("saved", name)
        await cmd("Page.enable")
        await shot("hud_ipad", 1180, 820, 2)          # iPad Air landscape
        await shot("hud_phone", 390, 844, 3)          # iPhone portrait
        await shot("hud_wide", 1200, 675, 2)          # 16:9 for X
        # Low-AGL (amber) frame: wait until the sim dips under 200 ft.
        for _ in range(240):
            if await ev("document.getElementById('agl-ft').classList.contains('agl-low')"): break
            await asyncio.sleep(0.25)
        r = await cmd("Page.captureScreenshot", format="png")
        open(f"{OUT}/hud_wide_lowagl.png", "wb").write(base64.b64decode(r["data"])); print("saved lowagl")
        # Animated GIF: 8 s at 5 fps, 16:9.
        await cmd("Emulation.setDeviceMetricsOverride", width=1200, height=675, deviceScaleFactor=1, mobile=False)
        frames = []
        for _ in range(40):
            r = await cmd("Page.captureScreenshot", format="png")
            frames.append(Image.open(io.BytesIO(base64.b64decode(r["data"]))).convert("RGB"))
            await asyncio.sleep(0.2)
        pal = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for f in frames]
        pal[0].save(f"{OUT}/hud_live.gif", save_all=True, append_images=pal[1:], duration=200, loop=0, optimize=True)
        print("saved gif", len(frames))
try:
    asyncio.run(main())
finally:
    proc.terminate()
