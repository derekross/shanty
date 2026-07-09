"""Endless stream daemon: compose ahead, crossfade, write continuous PCM.

Output is s16le 48kHz stereo PCM on a FIFO (default /tmp/lofi.pcm). Pacing
comes from the consumer: writes block until the reader (the phase-2 Concord
bot, or ffmpeg for testing) pulls data. If the reader disconnects, the daemon
reopens the FIFO and keeps going. If rendering ever falls behind (it won't —
a track renders ~15x faster than it plays), the last track loops.

Consume it like:
  ffmpeg -f s16le -ar 48000 -ac 2 -i /tmp/lofi.pcm -c:a libopus out.ogg
"""

from __future__ import annotations

import os
import queue
import signal
import sys
import threading

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

    def _open(self) -> None:
        print(f"[lofi] waiting for a reader on {self.path} …", flush=True)
        self.fd = os.open(self.path, os.O_WRONLY)  # blocks until a reader appears
        print("[lofi] reader connected", flush=True)

    def write(self, data: bytes) -> None:
        while True:
            if self.fd is None:
                self._open()
            try:
                os.write(self.fd, data)
                return
            except BrokenPipeError:
                print("[lofi] reader disconnected; waiting for the next one", flush=True)
                os.close(self.fd)
                self.fd = None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
        if os.path.exists(self.path):
            os.remove(self.path)


def run_stream(fifo_path: str = "/tmp/lofi.pcm", buffer_tracks: int = 3) -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)  # handle EPIPE ourselves
    tq = TrackQueue(buffer_tracks)
    out = FifoWriter(fifo_path)

    def shutdown(*_):
        print("\n[lofi] shutting down", flush=True)
        tq.stop()
        out.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    xfade = int(CROSSFADE_S * SR)
    track, audio = tq.next_track(None)
    while audio is None:
        track, audio = tq.next_track(None)

    while True:
        if track is not None:
            print(f"[lofi] ♪ now playing: {track.name}  [{track.style}, seed {track.seed}]  "
                  f"{track.bpm:.0f}bpm in {NOTE_NAMES[track.key_pc]}  "
                  f"({len(audio)/SR/60:.1f} min)", flush=True)

        body = audio[:-xfade]
        for i in range(0, len(body), CHUNK_FRAMES):
            out.write(_to_s16le(body[i : i + CHUNK_FRAMES]))

        next_track, next_audio = tq.next_track(fallback=audio)
        if next_track is None:
            print("[lofi] render fell behind — looping last track", flush=True)
        out.write(_to_s16le(_crossfade(audio[-xfade:], next_audio[:xfade])))
        track, audio = next_track, np.ascontiguousarray(next_audio[xfade:])
