"""Render the setup menus to PNG by driving scripts/configure.py in a virtual terminal.

Usage: python tools/setup_screenshots.py <repo> <outdir>   (needs pyte and Pillow; writes and leaves config/sensors.local.yaml)
"""
import os, pty, sys, time, select, fcntl, termios, struct, pyte
from PIL import Image, ImageDraw, ImageFont
repo, outdir = sys.argv[1], sys.argv[2]
COLS, ROWS = 92, 26
pid, fd = pty.fork()
if pid == 0:
    os.environ.update(TERM="xterm", COLUMNS=str(COLS), LINES=str(ROWS))
    os.chdir(repo)
    os.execv(f"{repo}/.venv/bin/python", [f"{repo}/.venv/bin/python", "scripts/configure.py"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
screen = pyte.Screen(COLS, ROWS); stream = pyte.ByteStream(screen)
def pump(t):
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try: stream.feed(os.read(fd, 65536))
            except OSError: return
PAL = {"default": None, "black": (0,0,0), "red": (205,49,49), "green": (13,188,121), "brown": (229,229,16),
       "yellow": (229,229,16), "blue": (36,114,200), "magenta": (188,63,188), "cyan": (17,168,205),
       "white": (229,229,229), "lightgray": (229,229,229)}
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 18)
cw, ch = 11, 23
def snap(name):
    img = Image.new("RGB", (COLS*cw, ROWS*ch), (20,20,20)); d = ImageDraw.Draw(img)
    for y in range(ROWS):
        for x in range(COLS):
            c = screen.buffer[y][x]
            fg = PAL.get(c.fg) or (229,229,229); bg = PAL.get(c.bg) or (20,20,20)
            if c.reverse: fg, bg = bg, fg
            d.rectangle([x*cw, y*ch, (x+1)*cw, (y+1)*ch], fill=bg)
            if c.data.strip(): d.text((x*cw, y*ch+1), c.data, font=font, fill=fg)
    img.save(f"{outdir}/{name}.png")
    open(f"{outdir}/{name}.txt","w").write("\n".join(screen.display))
pump(2.0)
names = {0:"tui_welcome",1:"tui_mode",2:"tui_gnss_port",18:"tui_summary"}
for step in range(19):
    if step in names: snap(names[step])
    os.write(fd, b"\r"); pump(0.9)
pump(1.0)
snap("tui_done")
_, status = os.waitpid(pid, 0)
print("exit", os.waitstatus_to_exitcode(status))
