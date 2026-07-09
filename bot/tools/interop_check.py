"""Fully automated E2EE interop proof against armada.buzz's real SFU.

Publisher: headless Chromium page (the bot's actual media engine) pushing a
test tone with AES-256 sender-key encryption. Listener: a second headless page
using Armada's exact client settings, decoding and measuring RMS. Non-zero RMS
at the listener == the whole chain works: broker grant -> token -> TURN ->
E2EE frames a real Armada client can decrypt.

  .venv/bin/python -m bot.tools.interop_check [--broker https://armada.buzz] [--seconds 20]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import struct

from ..cord import voice_sender_key
from ..media.engine import FRAME_BYTES, MediaEngine, SR
from ..voice import fetch_av_token, probe_av_broker, voice_keys
from .scratch_publish import scratch_room

log = logging.getLogger("lowfi.interop")


def tone_chunk(t0: int) -> tuple[bytes, int]:
    """20ms of a gentle 220+330Hz chord, s16le stereo."""
    n = FRAME_BYTES // 4
    samples = []
    t = t0
    for _ in range(n):
        v = 0.2 * (math.sin(2 * math.pi * 220 * t / SR)
                   + 0.6 * math.sin(2 * math.pi * 330 * t / SR))
        s = int(v * 32767)
        samples += [s, s]
        t += 1
    return struct.pack(f"<{len(samples)}h", *samples), t


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="https://armada.buzz")
    ap.add_argument("--seconds", type=int, default=25)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    secret, channel = scratch_room()
    vk = voice_keys(secret, channel, 0)
    assert probe_av_broker(args.broker), f"broker {args.broker} failed probe"
    pub_tok = fetch_av_token(args.broker, vk)
    sub_tok = fetch_av_token(args.broker, vk)
    log.info("room %s…  publisher=%s listener=%s",
             vk.room.pk[:12], pub_tok.identity, sub_tok.identity)

    publisher = MediaEngine(http_port=8477, ws_port=8478)
    listener = MediaEngine(http_port=8487, ws_port=8488)
    await publisher.start("publisher.html")
    await listener.start("listener.html")

    try:
        await publisher.connect_publisher(
            pub_tok.url, pub_tok.token, pub_tok.identity,
            voice_sender_key(vk.media_key, pub_tok.identity))

        await listener.page.evaluate(
            "cfg => window.lofiListen(cfg)",
            {"url": sub_tok.url, "token": sub_tok.token, "mediaKeyHex": vk.media_key.hex()})
        await listener.wait_event("listening", timeout=60)

        async def pump():
            t = 0
            while True:
                chunk, t = tone_chunk(t)
                await publisher.send_pcm(chunk)

        pump_task = asyncio.create_task(pump())

        peak_rms = 0.0
        heard = False

        async def drain_publisher():
            while True:
                ev = await publisher.events.get()
                log.info("PUBLISHER event: %s", ev)

        drain_task = asyncio.create_task(drain_publisher())
        try:
            async with asyncio.timeout(args.seconds):
                while True:
                    ev = await listener.events.get()
                    if ev.get("type") == "rms":
                        peak_rms = max(peak_rms, ev["rms"])
                        if ev["rms"] > 0.02 and not heard:
                            heard = True
                            log.info("AUDIO DECODED: rms=%.4f from %s", ev["rms"], ev["from"])
                    else:
                        log.info("LISTENER event: %s", ev)
        except TimeoutError:
            pass
        finally:
            pump_task.cancel()
            drain_task.cancel()

        print(f"\npeak RMS at listener: {peak_rms:.4f}")
        if heard:
            print("INTEROP PROOF: PASS — Armada-compatible E2EE frames decoded end-to-end ✅")
        else:
            print("INTEROP PROOF: FAIL — no decodable audio at the listener ❌")
            raise SystemExit(1)
    finally:
        await publisher.stop()
        await listener.stop()


if __name__ == "__main__":
    asyncio.run(main())
