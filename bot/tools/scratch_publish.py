"""Scratch-room publisher: proves broker + E2EE publish without a real community.

Mints a throwaway channel secret (the broker is blind — any keypair names a
room), publishes a test tone or the phase-1 FIFO, and prints a browser URL for
interop.html (a second token is minted for the browser subscriber).

  .venv/bin/python -m bot.tools.scratch_publish [--fifo /tmp/lofi.pcm] [--broker https://armada.buzz]

Then in another terminal:
  cd bot/tools && ln -sfn ~/Projects/armada/client/node_modules/livekit-client/dist vendor
  python3 -m http.server 8099
and open the printed URL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import struct
from pathlib import Path
from urllib.parse import urlencode

from livekit import rtc

from ..publisher import (CHANNELS, SAMPLE_RATE, SAMPLES_PER_FRAME,
                         connect_and_publish, feed_fifo)
from ..cord import voice_sender_key
from ..voice import fetch_av_token, probe_av_broker, voice_keys

STATE = Path(__file__).parent / ".scratch-room.json"


def scratch_room() -> tuple[bytes, bytes]:
    """Stable scratch secret+channel across runs so browser links keep working."""
    if STATE.exists():
        d = json.loads(STATE.read_text())
        return bytes.fromhex(d["secret"]), bytes.fromhex(d["channel"])
    secret, channel = os.urandom(32), os.urandom(32)
    STATE.write_text(json.dumps({"secret": secret.hex(), "channel": channel.hex()}))
    return secret, channel


async def feed_tone(source: rtc.AudioSource, stop: asyncio.Event) -> None:
    """A gentle 220+330Hz test tone, in case the phase-1 daemon isn't running."""
    t = 0
    while not stop.is_set():
        samples = []
        for _ in range(SAMPLES_PER_FRAME):
            v = 0.15 * (math.sin(2 * math.pi * 220 * t / SAMPLE_RATE)
                        + 0.6 * math.sin(2 * math.pi * 330 * t / SAMPLE_RATE))
            s = int(v * 32767)
            samples += [s] * CHANNELS
            t += 1
        frame = rtc.AudioFrame(
            data=struct.pack(f"<{len(samples)}h", *samples),
            sample_rate=SAMPLE_RATE, num_channels=CHANNELS,
            samples_per_channel=SAMPLES_PER_FRAME,
        )
        await source.capture_frame(frame)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="https://armada.buzz")
    ap.add_argument("--fifo", default=None, help="publish this PCM FIFO instead of a test tone")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    secret, channel = scratch_room()
    vk = voice_keys(secret, channel, 0)
    print(f"room: {vk.room.pk}")

    assert probe_av_broker(args.broker), f"broker {args.broker} failed capability probe"
    pub_token = fetch_av_token(args.broker, vk)
    sub_token = fetch_av_token(args.broker, vk)  # separate identity for the browser
    print(f"publisher identity: {pub_token.identity}")

    url = "http://localhost:8099/interop.html?" + urlencode({
        "url": sub_token.url, "token": sub_token.token, "media": vk.media_key.hex(),
    })
    print(f"\nBROWSER INTEROP URL:\n{url}\n")

    sender_key = voice_sender_key(vk.media_key, pub_token.identity)
    handles = await connect_and_publish(pub_token, sender_key)
    stop = asyncio.Event()
    try:
        if args.fifo:
            await feed_fifo(handles.source, args.fifo, stop)
        else:
            await feed_tone(handles.source, stop)
    finally:
        await handles.room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
