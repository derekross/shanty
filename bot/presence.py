"""CORD-07 §4 presence: heartbeats out, fold of everyone else's presence in.

Presence rides ephemeral kind-21059 wraps at the channel's stream address —
relays don't store them, so state is built by listening, and a joiner waits a
full 30s heartbeat interval before calling the room empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from nostr_sdk import (Client, Event, Filter, HandleNotification, Keys, Kind,
                       NostrSigner, PublicKey, RelayMessage, RelayUrl)

from .cord import GroupKey
from .stream import KIND_VOICE_PRESENCE, KIND_WRAP_EPHEMERAL, Opened, build_rumor, open_wrap, seal_rumor, tag_value, wrap_seal
from .voice import VOICE_HEARTBEAT_S, VOICE_STALE_S

log = logging.getLogger("lowfi.presence")


@dataclass
class PresenceEntry:
    author: str
    ms: int
    rumor_id: str
    status: str            # "joined" | "left"
    identity: str | None
    broker: str | None


class PresenceFold:
    """Latest-per-author presence, with staleness and identity verification."""

    def __init__(self):
        self.by_author: dict[str, PresenceEntry] = {}

    def ingest(self, opened: Opened) -> None:
        if opened.kind != KIND_VOICE_PRESENCE:
            return
        entry = PresenceEntry(
            author=opened.author, ms=opened.ms, rumor_id=opened.rumor_id,
            status=opened.content,
            identity=tag_value(opened.tags, "identity"),
            broker=tag_value(opened.tags, "broker"),
        )
        cur = self.by_author.get(opened.author)
        if cur is None or (entry.ms, entry.rumor_id) > (cur.ms, cur.rumor_id):
            self.by_author[opened.author] = entry

    def present(self, now_ms: int | None = None) -> list[PresenceEntry]:
        now_ms = now_ms or int(time.time() * 1000)
        return [e for e in self.by_author.values()
                if e.status == "joined" and now_ms - e.ms < VOICE_STALE_S * 1000]

    def verified_author_of(self, identity: str) -> str | None:
        """The author whose fresh presence claims `identity` — None unless exactly one."""
        claimants = [e.author for e in self.present() if e.identity == identity]
        return claimants[0] if len(claimants) == 1 else None

    def occupied_brokers(self) -> list[str]:
        return [e.broker for e in self.present() if e.broker]


class PresenceService:
    """Publishes Shanty's heartbeat and folds everyone else's presence."""

    def __init__(self, stream: GroupKey, bot_sk: bytes, bot_pubkey: str,
                 channel_id_hex: str, epoch: int, relays: list[str]):
        self.stream = stream
        self.bot_sk = bot_sk
        self.bot_pubkey = bot_pubkey
        self.channel_id_hex = channel_id_hex
        self.epoch = epoch
        self.relays = relays
        self.fold = PresenceFold()
        self.client: Client | None = None
        self._hb_task: asyncio.Task | None = None
        self._identity: str | None = None
        self._broker: str | None = None

    # -- wire ------------------------------------------------------------------
    def _presence_rumor(self, status: str) -> dict:
        tags = [["channel", self.channel_id_hex], ["epoch", str(self.epoch)]]
        if status == "joined":
            tags += [["identity", self._identity or ""], ["broker", self._broker or ""]]
        return build_rumor(KIND_VOICE_PRESENCE, status, tags, self.bot_pubkey,
                           ms=int(time.time() * 1000))

    async def _publish(self, status: str) -> None:
        wrap = wrap_seal(seal_rumor(self._presence_rumor(status), self.stream, self.bot_sk),
                         self.stream, ephemeral=True)
        event = Event.from_json(json.dumps(wrap))
        try:
            await self.client.send_event(event)
        except Exception as e:
            log.warning("presence publish failed: %s", e)

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        self.client = Client(NostrSigner.keys(Keys.parse(self.bot_sk.hex())))
        for r in self.relays:
            await self.client.add_relay(RelayUrl.parse(r))
        await self.client.connect()

        flt = Filter().kind(Kind(KIND_WRAP_EPHEMERAL)).author(PublicKey.parse(self.stream.pk))
        await self.client.subscribe(flt)

        service = self

        class Handler(HandleNotification):
            async def handle(self, relay_url: str, subscription_id: str, event: Event):
                try:
                    wrap = json.loads(event.as_json())
                    opened = open_wrap(wrap, service.stream, service.channel_id_hex, service.epoch)
                    if opened:
                        service.fold.ingest(opened)
                except Exception as e:
                    log.debug("presence ingest error: %s", e)

            async def handle_msg(self, relay_url: str, msg: RelayMessage):
                pass

        self._notif_task = asyncio.create_task(self.client.handle_notifications(Handler()))
        log.info("presence subscribed at stream %s… on %d relays",
                 self.stream.pk[:12], len(self.relays))

    async def start_heartbeat(self, identity: str, broker: str) -> None:
        self._identity, self._broker = identity, broker
        if self._hb_task:
            self._hb_task.cancel()

        async def beat():
            while True:
                await self._publish("joined")
                await asyncio.sleep(VOICE_HEARTBEAT_S)

        self._hb_task = asyncio.create_task(beat())
        log.info("heartbeat started (identity %s, broker %s)", identity, broker)

    async def stop_heartbeat_and_leave(self) -> None:
        if self._hb_task:
            self._hb_task.cancel()
            self._hb_task = None
        await self._publish("left")

    async def stop(self) -> None:
        await self.stop_heartbeat_and_leave()
        if self.client:
            await self.client.disconnect()
