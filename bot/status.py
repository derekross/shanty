"""Now-playing as a NIP-38 user status (kind 30315, d=music).

Shanty's profile shows "streaming: <track> 🎶" instead of spamming the
channel. Jukebox requests take priority over the lo-fi daemon's nowplaying
file; a NIP-40 expiration self-clears the status if the bot dies. Statuses
are addressable, so each publish replaces the last — one event, no timeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from nostr_sdk import Client, Event, Keys, NostrSigner, RelayUrl

from .config import Config
from .jukebox import Jukebox
from .voice import finalize_event

log = logging.getLogger("lowfi.status")

KIND_STATUS = 30315
POLL_S = 5.0
EXPIRY_S = 15 * 60
REFRESH_S = 10 * 60  # re-publish an unchanged status well before it expires


def read_nowplaying(path: str) -> str | None:
    """Current track name from the lofi daemon's file, or None."""
    try:
        with open(path) as f:
            name = json.load(f).get("name")
        return name if isinstance(name, str) and name else None
    except (OSError, ValueError):
        return None


def status_text(jukebox: Jukebox, nowplaying_path: str) -> str:
    """What the status should say right now ("" = nothing playing)."""
    track = jukebox.now_playing
    if track:
        return f"streaming: {track.pretty} (requested) 🎶"
    name = read_nowplaying(nowplaying_path)
    return f"streaming: {name} 🎶" if name else ""


def build_status_event(sk: bytes, text: str, now: int | None = None) -> dict:
    """kind-30315 d=music status; empty text clears it (no expiration needed)."""
    now = now if now is not None else int(time.time())
    tags = [["d", "music"]]
    if text:
        tags.append(["expiration", str(now + EXPIRY_S)])
    return finalize_event(sk, KIND_STATUS, tags, text, created_at=now)


class StatusPublisher:
    """Poll jukebox + nowplaying file; publish the status when it changes."""

    def __init__(self, cfg: Config, jukebox: Jukebox,
                 muted: Callable[[], bool] = lambda: False):
        self.cfg = cfg
        self.jukebox = jukebox
        self.muted = muted
        self._last = ""            # last successfully published text
        self._published_at = 0.0
        self._task: asyncio.Task | None = None

    def next_publish(self, now: float) -> str | None:
        """Text to publish this tick, or None to stay quiet (dedupe/refresh)."""
        text = "" if self.muted() else status_text(self.jukebox, self.cfg.nowplaying)
        if text != self._last:
            return text
        if text and now - self._published_at > REFRESH_S:
            return text
        return None

    async def _loop(self) -> None:
        while True:
            try:
                text = self.next_publish(time.time())
                if text is not None:
                    await self._publish(text)
            except Exception as e:
                # A missed status is cosmetic; retry on the next tick.
                log.warning("status publish failed: %s", e)
            await asyncio.sleep(POLL_S)

    async def _publish(self, text: str) -> None:
        event = build_status_event(bytes.fromhex(self.cfg.nsec_hex), text)
        client = Client(NostrSigner.keys(Keys.parse(self.cfg.nsec_hex)))
        for r in self.cfg.relays:
            await client.add_relay(RelayUrl.parse(r))
        await client.connect()
        await client.send_event(Event.from_json(json.dumps(event)))
        await client.disconnect()
        self._last = text
        self._published_at = time.time()
        log.info("status: %r", text)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="status")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._last:
            try:
                await self._publish("")  # clean shutdown clears the status
            except Exception as e:
                log.warning("status clear failed: %s", e)
