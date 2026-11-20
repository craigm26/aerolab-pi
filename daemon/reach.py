"""Read-only monitor for the Emlid Reach M2's own internal raw GNSS log.

Emlid has no official public API for checking whether raw GNSS logging is
running. The open-source Home Assistant integration (bscholer/home-assistant-emlid,
tested on firmware 35 on Reach RS3/RS4, same firmware family as the M2) showed
that Reach Panel serves Socket.IO over Engine.IO v3 on port 80 at /socket.io/
and emits a "broadcast" event with name "active_logs" every ~2 s containing
payload.raw = {is_writing, format, file_name, size, recording_time,
remaining_time, start_time}.

This client implements minimal Engine.IO v3 HTTP long-polling with httpx only
(no extra dependencies) and never invokes any Reach REST or Socket.IO command:
  * Handshake: GET <base>/socket.io/?EIO=3&transport=polling
  * Long-poll: GET <base>/socket.io/?EIO=3&transport=polling&sid=<sid>
  * Keepalive POSTs: Engine.IO v3 requires client-initiated ping packets ("1:2")
    every pingInterval ms so the server does not time out the polling session,
    and replies "1:3" if the server ever sends a ping ("2"). If the handshake
    does not include the Socket.IO default-namespace connect packet ("40"), one
    "2:40" connect packet is posted to /socket.io/. No other POST is ever sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import httpx


def detect_eio3_framing(raw: bytes) -> str:
    """Return 'binary', 'text', or 'unknown' for an Engine.IO v3 polling payload."""
    if not raw:
        return "unknown"
    if raw[0] in (0, 1):
        return "binary"
    if raw[0:1].isdigit():
        return "text"
    return "unknown"


def encode_eio3_text(packets: list[str]) -> bytes:
    """Encode Engine.IO v3 string packets in text '<len>:<packet>' framing."""
    return "".join(f"{len(p)}:{p}" for p in packets).encode("utf-8")


def encode_eio3_binary(packets: list[str]) -> bytes:
    """Encode Engine.IO v3 string packets in binary 0x00 <digits> 0xFF <utf8> framing."""
    out = bytearray()
    for p in packets:
        data = p.encode("utf-8")
        out.append(0)
        for ch in str(len(data)):
            out.append(int(ch))
        out.append(0xFF)
        out.extend(data)
    return bytes(out)


def parse_eio3_payload(raw: bytes | str) -> list[str]:
    """Decode Engine.IO v3 HTTP polling payload framing into string packets.

    Handles both Engine.IO v3 polling payload encodings:
      1. Binary-length framing (application/octet-stream):
         <0x00 for string | 0x01 for binary> <length digits as raw bytes 0..9> <0xFF> <payload>
      2. Text-length framing (text/plain, or when b64=1):
         <decimal length>:<packet>[<decimal length>:<packet>...]
    """
    if not raw:
        return []
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    else:
        data = raw

    packets: list[str] = []
    if data[0] in (0, 1):
        i = 0
        n = len(data)
        while i < n:
            pkt_type = data[i]
            if pkt_type not in (0, 1):
                break
            i += 1
            length = 0
            saw_digit = False
            while i < n and data[i] != 0xFF:
                d = data[i]
                if d > 9:
                    return packets
                length = length * 10 + d
                saw_digit = True
                i += 1
            if not saw_digit or i >= n or data[i] != 0xFF:
                break
            i += 1
            if i + length > n:
                break
            chunk = data[i : i + length]
            i += length
            if pkt_type == 0:
                packets.append(chunk.decode("utf-8", errors="replace"))
        return packets

    text = data.decode("utf-8", errors="replace")
    i = 0
    n = len(text)
    while i < n:
        colon = text.find(":", i)
        if colon == -1:
            break
        len_str = text[i:colon]
        if not len_str.isdigit():
            break
        length = int(len_str)
        start = colon + 1
        end = start + length
        if end > n:
            break
        packets.append(text[start:end])
        i = end
    return packets


def decode_sio_event(packet: str) -> tuple[str, Any] | None:
    """Decode a Socket.IO EVENT packet ('42[...]') into (event_name, payload).

    For Emlid Reach broadcasts of the form:
        42["broadcast", {"name": "active_logs", "payload": {...}}]
    this unwraps the outer "broadcast" envelope and returns ("active_logs", {...}).
    Direct events like 42["active_logs", {...}] are also decoded cleanly.
    """
    if not packet.startswith("42"):
        return None
    rest = packet[2:]
    bracket = rest.find("[")
    if bracket == -1:
        return None
    prefix = rest[:bracket]
    # Allow optional namespace (e.g. "/,") or numeric ack id before '['.
    if prefix and not prefix.isdigit() and "," not in prefix:
        return None
    try:
        arr = json.loads(rest[bracket:])
    except (ValueError, TypeError):
        return None
    if not isinstance(arr, list) or not arr:
        return None
    outer = str(arr[0])
    arg = arr[1] if len(arr) > 1 else None
    if outer == "broadcast" and isinstance(arg, dict) and "name" in arg:
        return str(arg["name"]), arg.get("payload")
    return outer, arg


class ReachLog:
    """Watches an Emlid Reach receiver's raw GNSS log status over local HTTP."""

    def __init__(
        self,
        url: str = "",
        sim: bool = False,
        grow_window_s: float = 15.0,
        poll_timeout_s: float = 15.0,
    ):
        self.url = (url or "").strip().rstrip("/")
        self.sim = bool(sim)
        self.enabled = bool(self.url) or self.sim
        self.grow_window_s = float(grow_window_s)
        self.poll_timeout_s = float(poll_timeout_s)

        self.reachable: bool = False
        self._writing: bool | None = None
        self._file_name: str | None = None
        self._size: float | None = None
        self._recording_time: float | None = None
        self._last_size: float | None = None
        self._last_file_name: str | None = None
        self._last_growth_mono: float = 0.0
        self._last_msg_mono: float = 0.0
        self.error: str = ""

    def handle_active_logs(self, payload: dict[str, Any], now: float | None = None) -> None:
        """Update cached raw-log state from one active_logs payload."""
        now = time.monotonic() if now is None else float(now)
        raw = payload.get("raw") if isinstance(payload, dict) else None
        self._last_msg_mono = now
        if not isinstance(raw, dict):
            self._writing = False
            self._file_name = None
            self._size = None
            self._recording_time = None
            self._last_size = None
            self._last_file_name = None
            self._last_growth_mono = 0.0
            return

        is_writing = bool(raw.get("is_writing", False))
        file_name = raw.get("file_name")
        size_val = raw.get("size")
        size = float(size_val) if isinstance(size_val, (int, float)) else None
        rec_val = raw.get("recording_time")
        rec_time = float(rec_val) if isinstance(rec_val, (int, float)) else None

        self._writing = is_writing
        self._file_name = str(file_name) if file_name is not None else None
        self._size = size
        self._recording_time = rec_time

        if is_writing and size is not None:
            if self._last_file_name is not None and file_name != self._last_file_name:
                self._last_growth_mono = 0.0
            elif self._last_size is not None and size > self._last_size:
                self._last_growth_mono = now
            self._last_size = size
        else:
            self._last_size = size
            self._last_growth_mono = 0.0
        self._last_file_name = self._file_name

    def status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the Reach raw log state."""
        now = time.monotonic()
        age_s = round(now - self._last_msg_mono, 1) if self._last_msg_mono else None
        growing = bool(
            self.reachable
            and self._writing
            and self._last_growth_mono
            and (now - self._last_growth_mono) <= self.grow_window_s
        )
        return {
            "enabled": self.enabled,
            "reachable": self.reachable,
            "writing": self._writing if self.reachable else None,
            "file_name": self._file_name,
            "size": self._size,
            "recording_time": self._recording_time,
            "growing": growing,
            "age_s": age_s,
            "error": self.error,
        }

    async def run(self) -> None:
        """Keep polling the Reach (or simulate a growing log in sim mode)."""
        if not self.enabled:
            return
        if self.sim:
            await self._sim_loop()
            return

        backoff = 1.0
        while True:
            try:
                got_data = await self._poll_session()
                if got_data:
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reachable = False
                self.error = (str(exc).strip() or type(exc).__name__)[:160]
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 15.0)

    async def _sim_loop(self) -> None:
        """Simulate a Reach M2 actively writing and growing a RINEX raw log."""
        self.reachable = True
        self.error = ""
        size = 1024.0
        rec_s = 0.0
        fname = "Reach_raw_sim_RINEX_3_03"
        self.handle_active_logs({"raw": {"is_writing": True, "file_name": fname,
                                         "size": size, "recording_time": rec_s}})
        await asyncio.sleep(0.5)
        while True:
            size += 16384.0
            rec_s += 1.0
            self.handle_active_logs({"raw": {"is_writing": True, "file_name": fname,
                                             "size": size, "recording_time": rec_s}})
            await asyncio.sleep(1.0)

    async def _post_eio(self, client: httpx.AsyncClient, sid: str, packet: str) -> None:
        """Send one Engine.IO v3 polling control packet (ping '2', pong '3', or connect '40')."""
        await client.post(
            f"{self.url}/socket.io/",
            params={"EIO": "3", "transport": "polling", "sid": sid},
            content=encode_eio3_text([packet]),
            headers={"content-type": "text/plain;charset=UTF-8"},
            timeout=5.0,
        )

    async def _poll_session(self) -> bool:
        """Run one Engine.IO v3 polling session until the server closes or errors."""
        endpoint = f"{self.url}/socket.io/"
        got_data = False
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                endpoint,
                params={"EIO": "3", "transport": "polling"},
                timeout=5.0,
            )
            resp.raise_for_status()
            pkts = parse_eio3_payload(resp.content)
            open_pkt = next((p for p in pkts if p.startswith("0")), None)
            if not open_pkt:
                raise ValueError("Engine.IO handshake did not contain an OPEN packet")
            open_info = json.loads(open_pkt[1:])
            sid = str(open_info["sid"])
            ping_interval_s = max(1.0, float(open_info.get("pingInterval", 25000)) / 1000.0)
            ping_timeout_s = max(1.0, float(open_info.get("pingTimeout", 5000)) / 1000.0)

            self.reachable = True
            self.error = ""
            got_data = True

            saw_connect = False
            for pkt in pkts:
                if pkt.startswith("40"):
                    saw_connect = True
                elif pkt.startswith("42"):
                    self._dispatch_event(pkt)

            if not saw_connect:
                # Some Socket.IO servers expect an explicit connect packet for '/'.
                with contextlib.suppress(httpx.HTTPError):
                    await self._post_eio(client, sid, "40")

            next_ping = time.monotonic() + max(1.0, ping_interval_s * 0.8)
            poll_timeout = min(self.poll_timeout_s, max(2.0, ping_interval_s * 0.8))
            quiet_since = time.monotonic()

            while True:
                now = time.monotonic()
                if now >= next_ping:
                    # Engine.IO v3 requires client-initiated ping ("2") to keep the session open.
                    await self._post_eio(client, sid, "2")
                    next_ping = time.monotonic() + max(1.0, ping_interval_s * 0.8)

                try:
                    poll_resp = await client.get(
                        endpoint,
                        params={"EIO": "3", "transport": "polling", "sid": sid},
                        timeout=poll_timeout,
                    )
                    poll_resp.raise_for_status()
                except httpx.ReadTimeout:
                    if time.monotonic() - quiet_since > ping_interval_s + ping_timeout_s + 5.0:
                        raise
                    continue

                quiet_since = time.monotonic()
                for pkt in parse_eio3_payload(poll_resp.content):
                    if pkt == "1" or pkt == "41":
                        return got_data
                    if pkt == "2":
                        await self._post_eio(client, sid, "3")
                    elif pkt.startswith("42"):
                        self._dispatch_event(pkt)
        return got_data

    def _dispatch_event(self, pkt: str) -> None:
        ev = decode_sio_event(pkt)
        if ev is None:
            return
        name, payload = ev
        if name == "active_logs" and isinstance(payload, dict):
            self.handle_active_logs(payload)
