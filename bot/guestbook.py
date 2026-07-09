"""Guestbook plane (CORD-02 §5): the community membership directory.

Voice presence puts Shanty in the call roster; THIS puts it in the member
list — a one-time self-signed kind-3306 "join" at the guestbook address.
"""

from __future__ import annotations

import json
import logging
import time

from nostr_sdk import Client, Event, Keys, NostrSigner, RelayUrl

from .config import Config
from .cord import guestbook_group_key
from .stream import build_rumor, seal_rumor, wrap_seal

log = logging.getLogger("lowfi.guestbook")

KIND_JOIN_LEAVE = 3306


async def announce(cfg: Config, state: str = "join") -> None:
    """Publish a self-signed Guestbook join (or leave) for the bot."""
    guestbook = guestbook_group_key(bytes.fromhex(cfg.community_root),
                                    bytes.fromhex(cfg.community_id),
                                    cfg.root_epoch)
    rumor = build_rumor(KIND_JOIN_LEAVE, state, [], cfg.npub_hex,
                        ms=int(time.time() * 1000))
    wrap = wrap_seal(seal_rumor(rumor, guestbook, bytes.fromhex(cfg.nsec_hex)),
                     guestbook, ephemeral=False)

    client = Client(NostrSigner.keys(Keys.parse(cfg.nsec_hex)))
    for r in cfg.relays:
        await client.add_relay(RelayUrl.parse(r))
    await client.connect()
    out = await client.send_event(Event.from_json(json.dumps(wrap)))
    await client.disconnect()
    ok = [str(u) for u in out.success]
    log.info("guestbook %s published to %s", state, ok)
    print(f"guestbook {state} published to {len(ok)} relay(s) — "
          "Shanty should appear in the member directory")
