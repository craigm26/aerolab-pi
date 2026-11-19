"""Keep every part of the daemon running through a long flight.

supervise() runs a coroutine forever: if it crashes it is logged, reported to
Buddy and restarted after a short, growing delay. The watchdog tells systemd
the daemon is alive only while every supervised task is; if one is wedged
(not crashed, stuck), systemd restarts the whole service (WatchdogSec in the
unit file), and the restart continues logging into the same flight database.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import traceback


class Supervisor:
    def __init__(self, report=None):
        self.report = report or (lambda level, text: None)
        self.beats: dict[str, float] = {}
        self.restarts: dict[str, int] = {}
        self.stall_s: dict[str, float] = {}

    def beat(self, name: str):
        """Called by a task on every loop: proves it is not stuck."""
        self.beats[name] = time.monotonic()

    def run(self, name: str, factory, stall_s: float | None = None):
        """Start factory() under supervision; returns the asyncio task."""
        if stall_s:
            self.stall_s[name] = stall_s
        return asyncio.create_task(self._loop(name, factory))

    async def _loop(self, name, factory):
        delay = 1.0
        while True:
            started = time.monotonic()
            self.beat(name)
            try:
                await factory()
                return                                  # finished normally (e.g. trigger disabled)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.restarts[name] = self.restarts.get(name, 0) + 1
                print(f"[supervisor] {name} crashed: {exc!r}\n{traceback.format_exc()}", flush=True)
                self.report("alert", f"{name} crashed and is restarting ({type(exc).__name__}: {exc})")
                if time.monotonic() - started > 60:
                    delay = 1.0                         # it had been running fine: restart quickly
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def stalled(self) -> list[str]:
        now = time.monotonic()
        return [n for n, s in self.stall_s.items() if now - self.beats.get(n, now) > s]

    def status(self) -> dict:
        now = time.monotonic()
        return {n: {"restarts": self.restarts.get(n, 0),
                    "last_beat_s": round(now - self.beats.get(n, now), 1)} for n in set(self.beats) | set(self.restarts)}


def sd_notify(msg: str) -> bool:
    """Tell systemd something (READY=1, WATCHDOG=1, STATUS=...). No-op when not under systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False


async def watchdog(sup: Supervisor, interval: float = 5.0):
    sd_notify("READY=1")
    while True:
        stuck = sup.stalled()
        if stuck:
            sd_notify(f"STATUS=stalled: {', '.join(stuck)}")
            # no WATCHDOG=1: systemd restarts us after WatchdogSec
        else:
            sd_notify("WATCHDOG=1")
        await asyncio.sleep(interval)
