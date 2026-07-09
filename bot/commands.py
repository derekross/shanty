"""!music on/off — staff-gated chat commands.

Listens to the channel's Chat plane (kind-9 rumors in durable wraps), checks
the sender against the CORD-04 roster (owner/admin/mod — see roles.py), flips
the media engine's mute, and acks in chat as Shanty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from nostr_sdk import Client, Event, Keys, NostrSigner, PublicKey, RelayUrl

from .config import Config
from .control import fetch_control_editions
from .cord import GroupKey
from .jukebox import Jukebox, resolve
from .relay import subscribe_group_wraps
from .roles import Roster, build_roster
from .stream import KIND_CHAT, KIND_WRAP, build_rumor, open_wrap, seal_rumor, wrap_seal

log = logging.getLogger("lowfi.commands")

ROSTER_REFRESH_S = 300


class ChatCommands:
    def __init__(self, cfg: Config, stream: GroupKey,
                 on_music: Callable[[bool], Awaitable[None]],
                 jukebox: Jukebox | None = None):
        self.cfg = cfg
        self.stream = stream
        self.on_music = on_music
        self.jukebox = jukebox
        self.bot_sk = bytes.fromhex(cfg.nsec_hex)
        self.roster: Roster | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._publisher: Client | None = None
        self._seen: set[str] = set()

    async def start(self) -> None:
        self._publisher = Client(NostrSigner.keys(Keys.parse(self.cfg.nsec_hex)))
        for r in self.cfg.relays:
            await self._publisher.add_relay(RelayUrl.parse(r))
        await self._publisher.connect()

        await self._refresh_roster()
        self._tasks.append(asyncio.create_task(self._roster_loop()))

        queue: asyncio.Queue[dict] = asyncio.Queue()
        since = int(time.time())
        for relay in self.cfg.relays:
            self._tasks.append(asyncio.create_task(
                subscribe_group_wraps(relay, self.stream, [KIND_WRAP],
                                      queue, since, self._stop)))
        self._tasks.append(asyncio.create_task(self._command_loop(queue)))
        log.info("chat commands live (!music on/off; staff only)")

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._publisher:
            await self._publisher.disconnect()

    # -- roster -----------------------------------------------------------------
    async def _refresh_roster(self) -> None:
        try:
            editions = await fetch_control_editions(
                bytes.fromhex(self.cfg.community_root),
                bytes.fromhex(self.cfg.community_id),
                self.cfg.root_epoch, self.cfg.relays)
            self.roster = build_roster(editions, self.cfg.owner,
                                       bytes.fromhex(self.cfg.community_id))
        except Exception as e:
            log.warning("roster refresh failed: %s", e)

    async def _roster_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(ROSTER_REFRESH_S)
            await self._refresh_roster()

    # -- commands ---------------------------------------------------------------
    async def _command_loop(self, queue: asyncio.Queue[dict]) -> None:
        epoch = self.cfg.channel_epoch_effective
        while not self._stop.is_set():
            wrap = await queue.get()
            opened = open_wrap(wrap, self.stream, self.cfg.channel_id, epoch)
            if opened is None or opened.kind != KIND_CHAT:
                continue
            if opened.rumor_id in self._seen:
                continue  # same wrap via multiple relays
            self._seen.add(opened.rumor_id)
            if len(self._seen) > 4096:
                self._seen.clear()

            raw = opened.content.strip()
            if not raw.lower().startswith("!music"):
                continue
            arg = raw[len("!music"):].strip()
            author = opened.author
            staff = self.roster is not None and self.roster.is_staff(author)

            if arg.lower() in ("on", "off"):
                if not staff:
                    await self._say("sorry, only the crew can work the deck 🫡")
                    continue
                on = arg.lower() == "on"
                log.info("!music %s by staff %s…", arg.lower(), author[:8])
                await self.on_music(on)
                await self._say("⚓🎶 back on the airwaves" if on
                                else "🔇 going quiet — !music on when you want me back")

            elif arg.lower() == "skip":
                if not staff:
                    await self._say("sorry, only the crew can skip tracks 🫡")
                    continue
                playing = self.jukebox.skip() if self.jukebox else None
                if playing is None:
                    await self._say("nothing to skip — the lo-fi never stops 📻")

            elif arg.lower() == "queue":
                if not self.jukebox:
                    continue
                lines = []
                if self.jukebox.now_playing:
                    lines.append(f"▶️ {self.jukebox.now_playing.pretty}")
                lines += [f"{i}. {t.pretty}" for i, t in
                          enumerate(self.jukebox.listing(), start=1)]
                await self._say("\n".join(lines) if lines
                                else "queue's empty — drop a wavlake or fountain track link after !music 🎵")

            elif arg.startswith("https://"):
                if not self.jukebox:
                    continue
                try:
                    mention = f"nostr:{PublicKey.parse(author).to_bech32()}"
                except Exception:
                    mention = author[:8] + "…"
                track = await resolve(arg, mention)
                if track is None:
                    await self._say("couldn't read that link — I take "
                                    "wavlake.com/track/… or fountain.fm/track/… URLs")
                    continue
                pos = self.jukebox.enqueue(track)
                if pos is None:
                    await self._say("queue's full (10) — try again in a few songs 🎶")
                elif pos == 1 and self.jukebox.now_playing is None:
                    log.info("jukebox request %s by %s…", track.pretty, author[:8])
                    # player announces "now playing" when it picks it up
                else:
                    await self._say(f"🎵 queued #{pos}: {track.pretty}")

            elif arg:
                await self._say("I know: !music on · off · skip · queue · <wavlake/fountain track url>")

    async def _say(self, text: str) -> None:
        try:
            rumor = build_rumor(
                KIND_CHAT, text,
                [["channel", self.cfg.channel_id],
                 ["epoch", str(self.cfg.channel_epoch_effective)]],
                self.cfg.npub_hex, ms=int(time.time() * 1000))
            wrap = wrap_seal(seal_rumor(rumor, self.stream, self.bot_sk),
                             self.stream, ephemeral=False)
            await self._publisher.send_event(Event.from_json(json.dumps(wrap)))
        except Exception as e:
            log.warning("chat ack failed: %s", e)
