"""Control-plane reader: channel directory (and later, roles/grants).

Control editions (kind 3308) ride durable wraps at the Control plane address
(group_key("concord/control", community_root, community_id, root_epoch)) inside
PLAINTEXT seals. Each edition carries vsk (entity type) / eid (entity id) /
ev (version) tags. ChannelMetadata is vsk 2 with eid = channel_id.

This module does a latest-version fold — enough to resolve channel names and
flags. It does NOT validate role authority (that's the !music work); for
directory lookups at setup time, latest-wins is fine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .cord import control_group_key
from .relay import fetch_group_wraps_multi
from .stream import KIND_CONTROL, KIND_WRAP, open_control_wrap, tag_value

log = logging.getLogger("lowfi.control")

VSK_CHANNEL = "2"


@dataclass(frozen=True)
class Edition:
    author: str
    vsk: str
    eid: str
    version: int
    content: str


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    name: str
    private: bool
    deleted: bool


async def fetch_control_editions(community_root: bytes, community_id: bytes,
                                 epoch: int, relays: list[str],
                                 timeout_s: int = 15) -> list[Edition]:
    control = control_group_key(community_root, community_id, epoch)
    wraps = await fetch_group_wraps_multi(relays, control, [KIND_WRAP])

    editions: list[Edition] = []
    for wrap in wraps:
        opened = open_control_wrap(wrap, control)
        if opened is None or opened.kind != KIND_CONTROL:
            continue
        vsk = tag_value(opened.tags, "vsk")
        eid = tag_value(opened.tags, "eid")
        ev = tag_value(opened.tags, "ev")
        if vsk is None or eid is None or ev is None:
            continue
        editions.append(Edition(author=opened.author, vsk=vsk, eid=eid,
                                version=int(ev), content=opened.content))
    log.info("fetched %d control editions", len(editions))
    return editions


def fold_channels(editions: list[Edition]) -> list[ChannelInfo]:
    """Latest ChannelMetadata edition per channel id."""
    latest: dict[str, Edition] = {}
    for e in editions:
        if e.vsk != VSK_CHANNEL:
            continue
        cur = latest.get(e.eid)
        if cur is None or e.version > cur.version:
            latest[e.eid] = e
    out = []
    for eid, e in latest.items():
        try:
            meta = json.loads(e.content)
        except json.JSONDecodeError:
            continue
        out.append(ChannelInfo(
            channel_id=eid, name=str(meta.get("name", "")),
            private=bool(meta.get("private")),
            deleted=bool(meta.get("deleted"))))
    return [c for c in out if not c.deleted]


async def resolve_channel(community_root: bytes, community_id: bytes, epoch: int,
                          relays: list[str], name: str) -> ChannelInfo:
    channels = fold_channels(await fetch_control_editions(
        community_root, community_id, epoch, relays))
    if not channels:
        raise SystemExit("no channels found on the Control plane — wrong relays or keys?")
    matches = [c for c in channels if c.name.lower() == name.lower()]
    if not matches:
        listing = ", ".join(f"{c.name}{' 🔒' if c.private else ''}" for c in channels)
        raise SystemExit(f"no channel named {name!r}; community has: {listing}")
    if len(matches) > 1:
        raise SystemExit(f"{len(matches)} channels named {name!r} — use --channel-id: "
                         + ", ".join(c.channel_id for c in matches))
    return matches[0]
