"""Drive scripts/configure.py in a virtual terminal and save each screen as PNG.

Presses Enter on every screen (accepting defaults) except where a rule in
KEYS says otherwise, so the run follows the default profile. Writes and
leaves config/sensors.local.yaml.

Usage: python tools/setup_screenshots.py <repo> <outdir> [laser-hotkey]
Needs pyte and Pillow.
"""
import fcntl, os, pty, re, select, struct, sys, termios, time
import pyte
from PIL import Image, ImageDraw, ImageFont

repo, outdir = sys.argv[1], sys.argv[2]
laser_key = sys.argv[3].encode() if len(sys.argv) > 3 and sys.argv[3] != "-" else b""
survey = sys.argv[4].encode() if len(sys.argv) > 4 else b""
KEYS = [("Laser altimeter:", laser_key)]   # screen text -> keys before Enter
if survey:
    KEYS.append(("Planned survey height", b"\x7f" * 8 + survey))
# Never touch real serial ports from a screenshot run: answer No to "Listen on <port>".
STICKY = [("Listen on ", b"\t")]
COLS, ROWS = 100, 30
pid, fd = pty.fork()
if pid == 0:
    os.environ.update(TERM="xterm", COLUMNS=str(COLS), LINES=str(ROWS))
    os.chdir(repo)
    os.execv(f"{repo}/.venv/bin/python", [f"{repo}/.venv/bin/python", "scripts/configure.py"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
screen = pyte.Screen(COLS, ROWS); stream = pyte.ByteStream(screen)
PAL = {"black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121), "brown": (229, 229, 16),
       "yellow": (229, 229, 16), "blue": (36, 114, 200), "magenta": (188, 63, 188), "cyan": (17, 168, 205),
       "white": (229, 229, 229), "lightgray": (229, 229, 229)}
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 18)
cw, ch = 11, 23

def pump(t):
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try: stream.feed(os.read(fd, 65536))
            except OSError: return False
    return True

def snap(name):
    img = Image.new("RGB", (COLS * cw, ROWS * ch), (20, 20, 20)); d = ImageDraw.Draw(img)
    for y in range(ROWS):
        for x in range(COLS):
            c = screen.buffer[y][x]
            fg = PAL.get(c.fg) or (229, 229, 229); bg = PAL.get(c.bg) or (20, 20, 20)
            if c.reverse: fg, bg = bg, fg
            d.rectangle([x * cw, y * ch, (x + 1) * cw, (y + 1) * ch], fill=bg)
            if c.data.strip(): d.text((x * cw, y * ch + 1), c.data, font=font, fill=fg)
    img.save(f"{outdir}/{name}.png")

pump(2.0)
n, last = 0, ""
while True:
    text = "\n".join(screen.display)
    if text != last:
        n += 1
        first = next((l.strip(" │") for l in screen.display[1:] if l.strip(" │┌┐─┤├")), "")
        slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")[:40]
        snap(f"setup_{n:02d}_{slug}")
        last = text
    for k, keys in KEYS:
        if k in text and keys:
            os.write(fd, keys); pump(0.4); KEYS.remove((k, keys)); break
    for k, keys in STICKY:
        if k in text:
            os.write(fd, keys); pump(0.3)
    os.write(fd, b"\r")
    if not pump(1.0):
        break
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:
            break
    except ChildProcessError:
        break
print(n, "screens")
