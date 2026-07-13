"""Endless stream daemon: compose ahead, crossfade, broadcast continuous PCM.

Output is s16le 48kHz stereo PCM on one or more FIFOs (default /tmp/lofi.pcm).
The daemon paces itself by wall clock — one 100ms chunk per 100ms — and fans
each chunk out to every FIFO through a small per-reader buffer. A missing,
slow, or paused reader (e.g. a bot instance airing a jukebox request) never
stalls the broadcast: its buffer drops the oldest audio and it rejoins live,
like tuning back into a radio station. If rendering ever falls behind (it
won't — a track renders ~15x faster than it plays), the last track loops.

Consume it like:
  ffmpeg -f s16le -ar 48000 -ac 2 -i /tmp/lofi.pcm -c:a libopus out.ogg
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
from collections import deque

import numpy as np

from . import SR
from .composer import TrackData, compose, render_track
from .theory import NOTE_NAMES

CROSSFADE_S = 3.0
CHUNK_FRAMES = 4800  # 100ms


class TrackQueue:
    """Background composer thread keeping N rendered tracks ahead."""

    def __init__(self, buffer_tracks: int, first_seed: int | None = None):
        self.q: queue.Queue[tuple[TrackData, np.ndarray]] = queue.Queue(maxsize=buffer_tracks)
        self.rng = np.random.default_rng(first_seed)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._work, daemon=True, name="composer")
        self.thread.start()

    def _work(self) -> None:
        while not self._stop.is_set():
            seed = int(self.rng.integers(2**31))
            track = compose(seed)
            audio = render_track(track)
            while not self._stop.is_set():
                try:
                    self.q.put((track, audio), timeout=1.0)
                    break
                except queue.Full:
                    continue

    def next_track(self, fallback: np.ndarray | None) -> tuple[TrackData | None, np.ndarray | None]:
        """Non-blocking-ish: wait briefly, then fall back to looping the last track."""
        try:
            return self.q.get(timeout=30.0)
        except queue.Empty:
            return None, fallback

    def stop(self) -> None:
        self._stop.set()


def write_nowplaying(path: str, track: TrackData) -> None:
    """Atomically publish the current track's metadata for the bot to pick up."""
    payload = {"name": track.name, "style": track.style, "seed": track.seed,
               "bpm": round(track.bpm), "key": NOTE_NAMES[track.key_pc],
               "started_at": int(time.time())}
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _to_s16le(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _crossfade(tail: np.ndarray, head: np.ndarray) -> np.ndarray:
    """Equal-power crossfade of tail (end of prev) into head (start of next)."""
    n = min(len(tail), len(head))
    t = np.linspace(0.0, np.pi / 2, n, dtype=np.float32)[:, None]
    return tail[-n:] * np.cos(t) + head[:n] * np.sin(t)


class FifoWriter:
    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            os.remove(path)
        os.mkfifo(path)
        self.fd: int | None = None

    @property
    def is_open(self) -> bool:
        return self.fd is not None

    def open_blocking(self) -> None:
        print(f"[lofi] waiting for a reader on {self.path} …", flush=True)
        self.fd = os.open(self.path, os.O_WRONLY)  # blocks until a reader appears
        print("[lofi] reader connected", flush=True)

    def try_write(self, data: bytes) -> bool:
        """Write, or drop the data and close on a gone reader (False)."""
        try:
            os.write(self.fd, data)
            return True
        except (BrokenPipeError, OSError):
            print("[lofi] reader disconnected; waiting for the next one", flush=True)
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            return False

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
        if os.path.exists(self.path):
            os.remove(self.path)


BUFFER_CHUNKS = 30  # ~3s per reader: the rejoin-live window for stalled consumers


class _FifoPump(threading.Thread):
    """One FIFO with today's blocking open/write semantics, fed from a bounded
    deque so a stalled reader only ever costs itself audio."""

    def __init__(self, path: str):
        super().__init__(daemon=True, name=f"fifo:{path}")
        self.fifo = FifoWriter(path)
        self.chunks: deque[bytes] = deque(maxlen=BUFFER_CHUNKS)
        self.ready = threading.Event()
        self._stop = threading.Event()
        self.start()

    def push(self, data: bytes) -> None:
        self.chunks.append(data)  # deque drops the oldest chunk when the reader lags
        self.ready.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.fifo.is_open:
                # Open BEFORE popping: audio buffered while we waited stays in
                # the deque, so a (re)joining reader starts near live — never
                # with one stale chunk from whenever it went away.
                self.fifo.open_blocking()
                continue
            try:
                chunk = self.chunks.popleft()
            except IndexError:
                self.ready.clear()
                self.ready.wait(timeout=1.0)
                continue
            self.fifo.try_write(chunk)  # EPIPE drops the chunk; reader rejoins live

    def stop(self) -> None:
        self._stop.set()
        self.ready.set()
        self.fifo.close()


class BroadcastWriter:
    """Fan one realtime PCM stream out to N FIFOs, each on its own thread."""

    def __init__(self, paths: list[str]):
        self.pumps = [_FifoPump(p) for p in paths]

    def write(self, data: bytes) -> None:
        for p in self.pumps:
            p.push(data)

    def close(self) -> None:
        for p in self.pumps:
            p.stop()


def run_stream(fifo_paths: list[str] | str = "/tmp/lofi.pcm", buffer_tracks: int = 3,
               nowplaying_path: str = "/tmp/lofi-nowplaying.json") -> None:
    if isinstance(fifo_paths, str):
        fifo_paths = [fifo_paths]
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)  # FifoWriter handles EPIPE
    tq = TrackQueue(buffer_tracks)
    out = BroadcastWriter(fifo_paths)

    def shutdown(*_):
        print("\n[lofi] shutting down", flush=True)
        tq.stop()
        out.close()
        try:
            os.remove(nowplaying_path)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    xfade = int(CROSSFADE_S * SR)
    track, audio = tq.next_track(None)
    while audio is None:
        track, audio = tq.next_track(None)

    next_write = time.monotonic()

    def write_paced(frames: np.ndarray) -> None:
        """Broadcast in 100ms chunks at wall-clock realtime — the station clock."""
        nonlocal next_write
        for i in range(0, len(frames), CHUNK_FRAMES):
            chunk = frames[i : i + CHUNK_FRAMES]
            out.write(_to_s16le(chunk))
            next_write += len(chunk) / SR
            delay = next_write - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    while True:
        if track is not None:
            print(f"[lofi] ♪ now playing: {track.name}  [{track.style}, seed {track.seed}]  "
                  f"{track.bpm:.0f}bpm in {NOTE_NAMES[track.key_pc]}  "
                  f"({len(audio)/SR/60:.1f} min)", flush=True)
            try:
                write_nowplaying(nowplaying_path, track)
            except OSError as e:
                print(f"[lofi] nowplaying write failed: {e}", flush=True)

        write_paced(audio[:-xfade])

        next_track, next_audio = tq.next_track(fallback=audio)
        if next_track is None:
            print("[lofi] render fell behind — looping last track", flush=True)
        write_paced(_crossfade(audio[-xfade:], next_audio[:xfade]))
        track, audio = next_track, np.ascontiguousarray(next_audio[xfade:])
