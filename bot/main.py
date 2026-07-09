"""Shanty's supervisor: the 24/7 loop.

config -> derive keys -> presence -> rendezvous -> broker token -> media engine
-> heartbeat + FIFO pump. Any failure tears the session down, backs off, and
rejoins from rendezvous (fresh SFU identity, fresh sender key, re-announced
presence).
"""

from __future__ import annotations

import asyncio
import logging

from . import config as cfg_mod
from .commands import ChatCommands
from .cord import channel_group_key, voice_sender_key
from .jukebox import Jukebox
from .media.engine import MediaEngine
from .presence import PresenceService
from .voice import (fetch_av_token, probe_av_broker, rendezvous_candidates,
                    voice_keys)

log = logging.getLogger("lowfi.shanty")

REJOIN_BACKOFF_S = [5, 15, 60, 120, 300]


class Shanty:
    def __init__(self, cfg: cfg_mod.Config):
        self.cfg = cfg
        secret = cfg.channel_secret
        epoch = cfg.channel_epoch_effective
        self.stream = channel_group_key(secret, cfg.channel_id_bytes, epoch)
        self.voice = voice_keys(secret, cfg.channel_id_bytes, epoch)
        self.presence = PresenceService(
            stream=self.stream, bot_sk=bytes.fromhex(cfg.nsec_hex),
            bot_pubkey=cfg.npub_hex, channel_id_hex=cfg.channel_id,
            epoch=epoch, relays=cfg.relays)
        self.engine: MediaEngine | None = None
        self.stop_event = asyncio.Event()
        self.muted = False  # the !music switch flips this
        self.jukebox = Jukebox()
        self.commands: ChatCommands | None = None

    # -- rendezvous (CORD-07 §5) ------------------------------------------------
    async def pick_broker(self) -> str:
        occupied = self.presence.fold.occupied_brokers()
        if not occupied:
            # Ephemeral presence: listen one heartbeat interval before deciding.
            log.info("no presence seen yet; listening 30s for the room's broker…")
            await asyncio.sleep(30)
            occupied = self.presence.fold.occupied_brokers()
        for origin in rendezvous_candidates(self.voice.room.pk, occupied, self.cfg.brokers):
            if probe_av_broker(origin):
                return origin
        raise RuntimeError("no reachable AV broker")

    # -- one session --------------------------------------------------------------
    async def run_session(self) -> None:
        broker = await self.pick_broker()
        token = fetch_av_token(broker, self.voice)
        log.info("session: broker=%s identity=%s", broker, token.identity)

        self.engine = MediaEngine()
        await self.engine.start("publisher.html")
        try:
            sender_key = voice_sender_key(self.voice.media_key, token.identity)
            await self.engine.connect_publisher(token.url, token.token,
                                                token.identity, sender_key,
                                                volume=self.cfg.volume)
            if self.muted:
                await self.engine.set_muted(True)
            await self.presence.start_heartbeat(token.identity, broker)

            feed = asyncio.create_task(
                self.engine.play_mixed(self.jukebox, self.cfg.fifo, self.stop_event,
                                       announce=self._announce))
            disconnect = asyncio.create_task(
                self.engine.wait_event("disconnected", timeout=None))
            # Session ends when the page disconnects OR the audio pipeline dies.
            done, pending = await asyncio.wait(
                {feed, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    log.warning("session ending: %s", exc)
                elif t is disconnect:
                    log.warning("media engine disconnected: %s",
                                t.result().get("reason"))
        finally:
            await self.presence.stop_heartbeat_and_leave()
            await self.engine.stop()
            self.engine = None

    # -- forever --------------------------------------------------------------------
    async def _announce(self, text: str) -> None:
        if self.commands:
            await self.commands._say(text)

    async def run(self) -> None:
        await self.presence.start()
        self.commands = ChatCommands(self.cfg, self.stream, self.set_music,
                                     jukebox=self.jukebox)
        await self.commands.start()
        attempt = 0
        while not self.stop_event.is_set():
            try:
                await self.run_session()
                attempt = 0
            except Exception as e:
                log.error("session failed: %s", e, exc_info=True)
            if self.stop_event.is_set():
                break
            delay = REJOIN_BACKOFF_S[min(attempt, len(REJOIN_BACKOFF_S) - 1)]
            attempt += 1
            log.info("rejoining in %ds", delay)
            await asyncio.sleep(delay)
        await self.commands.stop()
        await self.presence.stop()

    async def set_music(self, on: bool) -> None:
        """The !music switch: (un)mute the published track without leaving."""
        self.muted = not on
        if self.engine:
            await self.engine.set_muted(self.muted)
        log.info("music %s", "ON" if on else "OFF")


async def run_bot() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = cfg_mod.load()
    if not (cfg.nsec_hex and cfg.community_root and cfg.channel_id):
        raise SystemExit("config incomplete — run create-identity and accept-invite first")
    bot = Shanty(cfg)
    await bot.run()
