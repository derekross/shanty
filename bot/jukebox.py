"""The jukebox: resolve Wavlake/Fountain track URLs and queue them for airplay.

Any member can request; the player (media engine) drains the queue between
lo-fi passages. Resolution is unauthenticated HTTPS:
  - wavlake.com/track/<uuid>  -> wavlake API json (mediaUrl, title, artist, duration)
  - fountain.fm/track/<id>    -> track page og:audio / og:title meta tags
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger("lowfi.jukebox")

MAX_QUEUE = 10
MAX_TRACK_S = 15 * 60
HTTP_TIMEOUT = 20.0

ALLOWED_HOSTS = {"wavlake.com", "www.wavlake.com", "fountain.fm", "www.fountain.fm"}

_WAVLAKE_TRACK = re.compile(r"^/track/([0-9a-f-]{36})$", re.I)
_FOUNTAIN_TRACK = re.compile(r"^/track/([A-Za-z0-9]+)$")
_OG_META = re.compile(
    r'<meta[^>]+property="og:(audio|title)"[^>]+content="([^"]*)"', re.I)


@dataclass
class Track:
    stream_url: str
    title: str
    artist: str
    source: str          # "wavlake" | "fountain"
    requested_by: str    # display mention ("nostr:npub1…"), rendered in announcements
    duration_s: int | None = None

    @property
    def pretty(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title


async def resolve(url: str, requested_by: str) -> Track | None:
    """Turn a supported track URL into a playable Track; None if unsupported/broken."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            return None
        host = parsed.hostname.removeprefix("www.")
        if host == "wavlake.com":
            m = _WAVLAKE_TRACK.match(parsed.path)
            return await _resolve_wavlake(m.group(1), requested_by) if m else None
        if host == "fountain.fm":
            m = _FOUNTAIN_TRACK.match(parsed.path)
            return await _resolve_fountain(url, requested_by) if m else None
        return None
    except Exception as e:
        log.warning("resolve failed for %s: %s", url, e)
        return None


async def _resolve_wavlake(track_id: str, requested_by: str) -> Track | None:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(f"https://wavlake.com/api/v1/content/track/{track_id}")
        r.raise_for_status()
        data = r.json()
    entry = data[0] if isinstance(data, list) and data else None
    if not entry or not entry.get("mediaUrl"):
        return None
    return Track(stream_url=entry["mediaUrl"], title=entry.get("title", "untitled"),
                 artist=entry.get("artist", ""), source="wavlake",
                 requested_by=requested_by, duration_s=entry.get("duration"))


async def _resolve_fountain(page_url: str, requested_by: str) -> Track | None:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(page_url)
        r.raise_for_status()
        page = r.text
    metas = {kind.lower(): html.unescape(content)
             for kind, content in _OG_META.findall(page)}
    audio = metas.get("audio")
    if not audio:
        return None
    title, artist = metas.get("title", "untitled"), ""
    # og:title shape: "Artist • Track • Listen on Fountain"
    parts = [p.strip() for p in title.split("•")]
    if len(parts) >= 2:
        artist, title = parts[0], parts[1]
    return Track(stream_url=audio, title=title, artist=artist,
                 source="fountain", requested_by=requested_by)


class Jukebox:
    """Bounded, listable request queue + skip signal, shared by commands and the player."""

    def __init__(self):
        self._items: list[Track] = []
        self.skip_event = asyncio.Event()
        self.now_playing: Track | None = None

    def enqueue(self, track: Track) -> int | None:
        """Queue a track; returns 1-based position, or None if full."""
        if len(self._items) >= MAX_QUEUE:
            return None
        self._items.append(track)
        return len(self._items)

    def pop_next(self) -> Track | None:
        """Player-side: take the next request (None if the queue is empty)."""
        return self._items.pop(0) if self._items else None

    def listing(self) -> list[Track]:
        return list(self._items)

    def skip(self) -> Track | None:
        playing = self.now_playing
        if playing:
            self.skip_event.set()
        return playing
