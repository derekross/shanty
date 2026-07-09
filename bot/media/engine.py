"""The bot's media engine: headless Chromium publishing E2EE audio via livekit-client.

Why a browser: Armada's clients encrypt frames with AES-256-GCM (keySize: 256),
which LiveKit's native SDKs hardcode away (AES-128, no option), and the native
TLS stack can't reach armada.buzz's TURN server (stale root bundle). Chromium
runs Armada's exact crypto and trust store, so the bot interops by construction.

Python feeds s16le/48kHz/stereo PCM over a localhost WebSocket; the page's
AudioWorklet turns it into a published track. Flow control: the worklet reports
consumed frames, we keep ~0.5s in flight.
"""

from __future__ import annotations

import array
import asyncio
import functools
import http.server
import json
import logging
import os
import threading
from pathlib import Path

import websockets
from playwright.async_api import async_playwright

log = logging.getLogger("lowfi.media")

MEDIA_DIR = Path(__file__).resolve().parent
SR = 48000
FRAME_MS = 20
FRAME_BYTES = SR * FRAME_MS // 1000 * 2 * 2  # 20ms s16le stereo = 3840 bytes
TARGET_BUFFER_S = 0.5
STALL_TIMEOUT_S = 10.0


def _serve_dir(directory: Path, port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="media-http").start()
    return httpd


class MediaEngine:
    """One headless Chromium page publishing one audio track."""

    def __init__(self, http_port: int = 8477, ws_port: int = 8478):
        self.http_port = http_port
        self.ws_port = ws_port
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.consumed_frames = 0
        self.sent_frames = 0
        self._pcm_ws = None
        self._httpd = None
        self._pw = None
        self._browser = None
        self.page = None

    # -- lifecycle -----------------------------------------------------------
    async def start(self, page_name: str = "publisher.html") -> None:
        self._httpd = _serve_dir(MEDIA_DIR, self.http_port)
        self._ws_server = await websockets.serve(self._on_pcm_ws, "127.0.0.1", self.ws_port)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            # Force the FULL Chromium build in new-headless mode: the default
            # "headless shell" build has no working WebAudio rendering, which
            # silently freezes the AudioWorklet (silent published track).
            channel="chromium",
            args=[
                "--autoplay-policy=no-user-gesture-required",
                # A 24/7 radio must never get background-throttled:
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        self.page = await self._browser.new_page()
        self.page.on("console", lambda m: log.info("[%s console] %s", page_name, m.text[:300]))
        self.page.on("pageerror", lambda e: log.error("[%s pageerror] %s", page_name, e))
        await self.page.expose_function("lofiEvent", self._on_page_event)
        await self.page.goto(f"http://127.0.0.1:{self.http_port}/{page_name}")
        await self.wait_event("page_ready", timeout=15)
        log.info("media page ready (%s)", page_name)

    async def stop(self) -> None:
        try:
            if self.page:
                await self.page.evaluate("window.lofiStop && window.lofiStop()")
        except Exception:
            pass
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._ws_server.close()
        if self._httpd:
            self._httpd.shutdown()

    # -- events ----------------------------------------------------------------
    def _on_page_event(self, payload: str) -> None:
        event = json.loads(payload)
        if event.get("type") == "error":
            log.error("page error: %s", event.get("message"))
        self.events.put_nowait(event)

    async def wait_event(self, etype: str, timeout: float = 30) -> dict:
        """Wait for a specific event type, surfacing page errors immediately."""
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if event.get("type") == etype:
                    return event
                if event.get("type") == "error":
                    raise RuntimeError(f"media page error: {event.get('message')}")

    # -- publishing ------------------------------------------------------------
    async def connect_publisher(self, url: str, token: str, identity: str,
                                sender_key: bytes, volume: float = 1.0) -> None:
        await self.page.evaluate(
            "cfg => window.lofiConnect(cfg)",
            {"url": url, "token": token, "identity": identity,
             "senderKeyHex": sender_key.hex(), "wsPort": self.ws_port,
             "volume": volume},
        )
        await self.wait_event("published", timeout=60)
        log.info("published to room as %s", identity)

    async def set_muted(self, muted: bool) -> None:
        await self.page.evaluate("m => window.lofiSetMuted(m)", muted)

    async def _on_pcm_ws(self, ws) -> None:
        log.info("PCM websocket connected")
        self._pcm_ws = ws
        try:
            async for message in ws:  # worklet feedback: {consumed, buffered}
                if isinstance(message, str):
                    data = json.loads(message)
                    self.consumed_frames = data.get("consumed", self.consumed_frames)
        finally:
            self._pcm_ws = None
            log.info("PCM websocket closed")

    async def send_pcm(self, chunk: bytes) -> None:
        """Send one PCM chunk, flow-controlled against the worklet's consumption.

        If the worklet stops consuming for STALL_TIMEOUT_S while we have audio
        to deliver, the page's audio graph is wedged — raise so the supervisor
        rebuilds the whole session rather than broadcasting silence forever.
        """
        while self._pcm_ws is None:
            await asyncio.sleep(0.1)
        stalled_since = None
        while (self.sent_frames - self.consumed_frames) / SR > TARGET_BUFFER_S:
            last = self.consumed_frames
            await asyncio.sleep(FRAME_MS / 1000)
            if self.consumed_frames != last:
                stalled_since = None
                continue
            now = asyncio.get_running_loop().time()
            stalled_since = stalled_since or now
            if now - stalled_since > STALL_TIMEOUT_S:
                raise RuntimeError("audio pipeline stalled: worklet stopped consuming")
        await self._pcm_ws.send(chunk)
        self.sent_frames += len(chunk) // 4  # frames = bytes / (2ch * s16)

    async def feed_fifo(self, fifo_path: str, stop: asyncio.Event) -> None:
        """Pump the phase-1 FIFO into the page; reopen if the writer goes away."""
        await self.play_mixed(None, fifo_path, stop)

    async def play_mixed(self, jukebox, fifo_path: str, stop: asyncio.Event,
                         announce=None) -> None:
        """The player: lo-fi from the FIFO by default; when the jukebox holds a
        request, pause the FIFO (the daemon blocks on backpressure) and pump the
        track through ffmpeg instead, then resume. `announce(text)` narrates."""
        loop = asyncio.get_running_loop()

        async def say(text: str) -> None:
            if announce:
                try:
                    await announce(text)
                except Exception:
                    pass

        while not stop.is_set():
            # 1. Drain any queued requests.
            track = jukebox.pop_next() if jukebox else None
            if track is not None:
                await self._play_track(jukebox, track, stop, say)
                continue

            # 2. Lo-fi from the FIFO, checking the queue between chunks.
            try:
                fd = await loop.run_in_executor(None, os.open, fifo_path, os.O_RDONLY)
            except FileNotFoundError:
                log.warning("FIFO %s missing; retrying in 2s", fifo_path)
                await asyncio.sleep(2)
                continue
            log.info("reading PCM from %s", fifo_path)
            try:
                buf = b""
                while not stop.is_set():
                    if jukebox and jukebox.listing():
                        # fade the lo-fi out over the buffered remainder and switch
                        await self._send_faded(buf[:FRAME_BYTES * 12], fade="out")
                        buf = b""
                        break
                    chunk = await loop.run_in_executor(None, os.read, fd, FRAME_BYTES * 4)
                    if not chunk:
                        log.warning("FIFO writer went away; reopening")
                        break
                    buf += chunk
                    while len(buf) >= FRAME_BYTES:
                        await self.send_pcm(buf[:FRAME_BYTES])
                        buf = buf[FRAME_BYTES:]
            finally:
                os.close(fd)

    async def _play_track(self, jukebox, track, stop: asyncio.Event, say) -> None:
        from ..jukebox import MAX_TRACK_S
        log.info("jukebox: playing %s (%s)", track.pretty, track.stream_url[:80])
        await say(f"▶️ now playing: {track.pretty} "
                  f"[{track.source}] — requested by {track.requested_by}")
        jukebox.now_playing = track
        jukebox.skip_event.clear()
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", track.stream_url,
            # Commercial masters run much hotter than the lo-fi; normalize to a
            # comparable integrated loudness so requests don't blast listeners.
            "-af", "loudnorm=I=-18:TP=-2",
            "-f", "s16le", "-ar", str(SR), "-ac", "2", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        started = asyncio.get_running_loop().time()
        first = True
        try:
            buf = b""
            while not stop.is_set() and not jukebox.skip_event.is_set():
                if asyncio.get_running_loop().time() - started > MAX_TRACK_S:
                    log.info("jukebox: track hit the %ds cap", MAX_TRACK_S)
                    break
                chunk = await proc.stdout.read(FRAME_BYTES * 4)
                if not chunk:
                    break  # track finished
                buf += chunk
                while len(buf) >= FRAME_BYTES:
                    frame = buf[:FRAME_BYTES]
                    buf = buf[FRAME_BYTES:]
                    if first:
                        await self._send_faded(frame + buf[:FRAME_BYTES * 11], fade="in")
                        buf = buf[FRAME_BYTES * 11:]
                        first = False
                    else:
                        await self.send_pcm(frame)
        finally:
            jukebox.now_playing = None
            skipped = jukebox.skip_event.is_set()
            jukebox.skip_event.clear()
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            nxt = jukebox.listing()
            if skipped:
                await say("⏭️ skipped")
            if not nxt:
                await say("📻 back to the lo-fi")

    async def _send_faded(self, pcm: bytes, fade: str) -> None:
        """Linear fade over an s16le stereo buffer, then send frame-sized chunks."""
        n = len(pcm) // 2
        if n == 0:
            return
        samples = array.array("h")
        samples.frombytes(pcm[: n * 2])
        total = len(samples)
        for i in range(total):
            g = i / total if fade == "in" else 1.0 - i / total
            samples[i] = int(samples[i] * g)
        data = samples.tobytes()
        for off in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES):
            await self.send_pcm(data[off:off + FRAME_BYTES])
