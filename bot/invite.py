"""Identity + invite intake for Shanty.

create-identity: mint the bot's nsec, publish its kind-0 profile plus NIP-65 /
kind-10050 relay lists (so direct invites have a deterministic delivery target),
print the npub for Derek to invite from Armada.

accept-invite: watch for a CORD-05 direct invite (kind-3313 rumor in a classic
NIP-59 giftwrap addressed to the bot), validate the bundle, store the community
secrets + chosen channel in the config.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import timedelta
from urllib.parse import unquote, urlparse

from nostr_sdk import (Client, EventBuilder, Filter, Keys, Kind, Metadata, RelayUrl,
                       Nip19Coordinate, NostrSigner, PublicKey, SingleLetterTag,
                       Tag, Alphabet, UnwrappedGift)

from . import config as cfg_mod, nip44raw
from .config import Config
from .cord import invite_bundle_key, verify_community_id

log = logging.getLogger("lowfi.invite")

KIND_GIFT_WRAP = 1059
KIND_DIRECT_INVITE_RUMOR = 3313
KIND_INVITE_BUNDLE = 33301
VSK_INVITE_LIVE = "6"
VSK_INVITE_REVOKED = "9"
FRAGMENT_VERSION = 4
TOKEN_BYTES = 16
FLAG_STOCK_SET = 0x01
# CORD-05 §3 fragment relay dictionary (frozen per version byte; armada invite.ts)
RELAY_DICTIONARY = {
    1: "wss://jskitty.com/nostr",
    2: "wss://asia.vectorapp.io/nostr",
    3: "wss://relay.ditto.pub",
    4: "wss://nos.lol",
}
STOCK_RELAYS = [RELAY_DICTIONARY[i] for i in (1, 2, 3, 4)]

PROFILE = {
    "name": "Shanty",
    "display_name": "Shanty ⚓🎶",
    "about": "24/7 lo-fi radio for your Concord live channel. "
             "Generative beats, zero repeats. A Soapbox bot — sibling of Flagship.",
    "bot": True,
}


async def create_identity(cfg_path=cfg_mod.DEFAULT_PATH) -> Config:
    try:
        cfg = cfg_mod.load(cfg_path)
        if cfg.nsec_hex:
            keys = Keys.parse(cfg.nsec_hex)
            print(f"identity already exists: {keys.public_key().to_bech32()}")
            return cfg
    except FileNotFoundError:
        cfg = Config()

    keys = Keys.generate()
    cfg.nsec_hex = keys.secret_key().to_hex()
    cfg.npub_hex = keys.public_key().to_hex()
    cfg_mod.save(cfg, cfg_path)

    client = Client(NostrSigner.keys(keys))
    for r in cfg.relays:
        await client.add_relay(RelayUrl.parse(r))
    await client.connect()

    metadata = Metadata.from_json(json.dumps(PROFILE))
    await client.send_event_builder(EventBuilder.metadata(metadata))
    # NIP-65 read relays + kind-10050 DM relays: where invites should land.
    relay_tags = [Tag.parse(["r", r]) for r in cfg.relays]
    await client.send_event_builder(EventBuilder(Kind(10002), "").tags(relay_tags))
    dm_tags = [Tag.parse(["relay", r]) for r in cfg.relays]
    await client.send_event_builder(EventBuilder(Kind(10050), "").tags(dm_tags))
    await client.disconnect()

    print("Shanty is born ⚓🎶")
    print(f"  npub:   {keys.public_key().to_bech32()}")
    print(f"  hex:    {cfg.npub_hex}")
    print(f"  config: {cfg_path}")
    print("\nInvite this npub to your community from Armada (direct invite), "
          "then run: python -m bot.cli accept-invite --channel-id <live channel id>")
    return cfg


# ── Public invite links (CORD-05 §2/§3) ──────────────────────────────────────

def decode_fragment(fragment: str) -> tuple[bytes, list[str]]:
    """base64url [version][flags][relays?][token16] -> (token, bootstrap relays)."""
    pad = "=" * (-len(fragment) % 4)
    data = base64.urlsafe_b64decode(fragment.strip() + pad)
    o = 0

    def need(n: int) -> None:
        if o + n > len(data):
            raise ValueError("fragment truncated")

    need(2)
    version, flags = data[0], data[1]
    o = 2
    if version != FRAGMENT_VERSION:
        raise ValueError(f"unsupported invite fragment version {version}")

    relays: list[str] = []
    if flags & FLAG_STOCK_SET:
        relays = list(STOCK_RELAYS)
    else:
        need(1)
        count = data[o]; o += 1
        if count > 3:
            raise ValueError("too many bootstrap relays")
        for _ in range(count):
            need(1)
            lead = data[o]; o += 1
            if 1 <= lead <= 254:
                url = RELAY_DICTIONARY.get(lead)
                if url:
                    relays.append(url)  # unknown ids skipped, not fatal
            else:
                need(1)
                ln = data[o]; o += 1
                need(ln)
                text = data[o:o + ln].decode(); o += ln
                relays.append(text if lead == 255 else f"wss://{text}")

    need(TOKEN_BYTES)
    token = data[o:o + TOKEN_BYTES]; o += TOKEN_BYTES
    if o != len(data):
        raise ValueError("trailing bytes in fragment")
    return token, relays


def parse_invite_link(link: str) -> tuple[str, bytes, list[str]]:
    """-> (link_signer pubkey hex, token, bootstrap relays)."""
    trimmed = link.strip()
    if trimmed.lower().startswith("naddr1") and "#" in trimmed:
        naddr, fragment = trimmed.split("#", 1)
    else:
        parsed = urlparse(trimmed)
        if "/invite/" not in parsed.path:
            raise ValueError("not an invite link (missing /invite/ path)")
        naddr = unquote(parsed.path.split("/invite/", 1)[1]).rstrip("/")
        fragment = parsed.fragment
    if not naddr or not fragment:
        raise ValueError("invite link needs both an naddr and a #fragment")
    coord = Nip19Coordinate.from_bech32(naddr).coordinate()
    if coord.kind().as_u16() != KIND_INVITE_BUNDLE or coord.identifier() != "":
        raise ValueError("naddr is not an invite bundle coordinate")
    token, relays = decode_fragment(fragment)
    return coord.public_key().to_hex(), token, relays


async def accept_invite_link(link: str, channel_id: str | None,
                             channel_name: str | None = None,
                             cfg_path=cfg_mod.DEFAULT_PATH) -> Config:
    cfg = cfg_mod.load(cfg_path)
    signer_pk, token, bootstrap = parse_invite_link(link)
    log.info("invite link parsed: signer %s…, %d bootstrap relays", signer_pk[:12], len(bootstrap))

    client = Client()
    for r in bootstrap or cfg.relays:
        await client.add_relay(RelayUrl.parse(r))
    await client.connect()
    flt = (Filter().kind(Kind(KIND_INVITE_BUNDLE))
           .author(PublicKey.parse(signer_pk)).identifier(""))
    events = await client.fetch_events(flt, timedelta(seconds=15))
    await client.disconnect()

    latest = None
    for event in events.to_vec():
        e = json.loads(event.as_json())
        if latest is None or e["created_at"] > latest["created_at"]:
            latest = e
    if latest is None:
        raise SystemExit("invite bundle not found on the bootstrap relays")

    vsk = next((t[1] for t in latest["tags"] if len(t) >= 2 and t[0] == "vsk"), None)
    if vsk == VSK_INVITE_REVOKED:
        raise SystemExit("this invite link has been revoked")
    if vsk != VSK_INVITE_LIVE:
        raise SystemExit(f"unexpected invite bundle state (vsk={vsk})")

    bundle = json.loads(nip44raw.decrypt(invite_bundle_key(token), latest["content"]))
    _validate_bundle(bundle)
    if bundle.get("expires_at") and time.time() * 1000 > bundle["expires_at"]:
        raise SystemExit("this invite link has expired")

    cfg = _store_bundle(cfg, bundle, channel_id, cfg_path)
    if channel_name and not cfg.channel_id:
        resolved = await _resolve_channel_name(cfg, channel_name)
        cfg = _store_bundle(cfg, bundle, resolved, cfg_path)
    await announce_membership(cfg)
    return cfg


def _validate_bundle(bundle: dict) -> None:
    for key in ("community_id", "owner", "owner_salt", "community_root", "root_epoch"):
        if key not in bundle:
            raise ValueError(f"invite bundle missing {key}")
    if not verify_community_id(bundle["community_id"], bundle["owner"], bundle["owner_salt"]):
        raise ValueError("invite bundle failed community_id verification")


async def accept_invite(channel_id: str | None, channel_name: str | None = None,
                        cfg_path=cfg_mod.DEFAULT_PATH, wait_s: int = 120) -> Config:
    cfg = cfg_mod.load(cfg_path)
    keys = Keys.parse(cfg.nsec_hex)
    signer = NostrSigner.keys(keys)

    client = Client(signer)
    for r in cfg.relays:
        await client.add_relay(RelayUrl.parse(r))
    await client.connect()

    # Direct invites are indexable: kinds 1059, #p me, #k 3313 (CORD-05 §6).
    flt = (Filter().kind(Kind(KIND_GIFT_WRAP))
           .pubkey(keys.public_key())
           .custom_tag(SingleLetterTag.lowercase(Alphabet.K), str(KIND_DIRECT_INVITE_RUMOR)))
    log.info("looking for a direct invite for %s…", keys.public_key().to_bech32())
    events = await client.fetch_events(flt, timedelta(seconds=15))

    bundle = None
    for event in events.to_vec():
        try:
            gift = await UnwrappedGift.from_gift_wrap(signer, event)
            rumor = json.loads(gift.rumor().as_json())
            if rumor.get("kind") != KIND_DIRECT_INVITE_RUMOR:
                continue
            candidate = json.loads(rumor["content"])
            _validate_bundle(candidate)
            bundle = candidate
            log.info("invite found: community %r from %s",
                     candidate.get("name"), gift.sender().to_bech32())
            break
        except Exception as e:
            log.debug("skipping non-invite wrap: %s", e)

    await client.disconnect()
    if bundle is None:
        raise SystemExit(
            f"no valid invite found on {len(cfg.relays)} relays — "
            "send a Direct Invite to the bot's npub from Armada first")

    cfg = _store_bundle(cfg, bundle, channel_id, cfg_path)
    if channel_name and not cfg.channel_id:
        resolved = await _resolve_channel_name(cfg, channel_name)
        cfg = _store_bundle(cfg, bundle, resolved, cfg_path)
    await announce_membership(cfg)
    return cfg


async def _resolve_channel_name(cfg: Config, name: str) -> str:
    from .control import resolve_channel
    ch = await resolve_channel(bytes.fromhex(cfg.community_root),
                               bytes.fromhex(cfg.community_id),
                               cfg.root_epoch, cfg.relays, name)
    print(f"resolved channel {name!r} -> {ch.channel_id[:16]}… "
          f"({'private' if ch.private else 'public'})")
    return ch.channel_id


def _store_bundle(cfg: Config, bundle: dict, channel_id: str | None, cfg_path) -> Config:
    cfg.community_id = bundle["community_id"]
    cfg.community_root = bundle["community_root"]
    cfg.root_epoch = int(bundle["root_epoch"])
    cfg.community_name = bundle.get("name", "")
    cfg.owner = str(bundle.get("owner", "")).lower()
    if bundle.get("relays"):
        cfg.relays = bundle["relays"]

    channels = bundle.get("channels") or []
    if channel_id:
        cfg.channel_id = channel_id
        match = next((c for c in channels if c.get("id") == channel_id), None)
        if match:  # private channel: bundle carries its key
            cfg.channel_key = match.get("key", "")
            cfg.channel_epoch = int(match.get("epoch", 0))
            cfg.channel_name = match.get("name", "")
    elif len(channels) == 1:
        c = channels[0]
        cfg.channel_id, cfg.channel_key = c["id"], c.get("key", "")
        cfg.channel_epoch, cfg.channel_name = int(c.get("epoch", 0)), c.get("name", "")
    else:
        names = ", ".join(f"{c.get('name')}={c.get('id', '')[:12]}…" for c in channels) or "none listed"
        print(f"bundle channels: {names}")
        print("re-run with --channel-id <hex> to pick the live channel "
              "(for a public channel, any channel id from Armada's channel settings)")

    cfg_mod.save(cfg, cfg_path)
    print(f"joined community {cfg.community_name!r} (root epoch {cfg.root_epoch})")
    if cfg.channel_id:
        kind = "private" if cfg.channel_key else "public"
        print(f"live channel: {cfg.channel_name or cfg.channel_id[:12]}… ({kind})")
    return cfg


async def announce_membership(cfg: Config) -> None:
    """Guestbook join — makes the bot show up in the member directory."""
    from .guestbook import announce
    await announce(cfg, "join")
