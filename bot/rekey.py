"""CORD-06 rekey-following: pick up a Refounding and adopt the new root.

When a community re-founds (ban, key rotation), every plane and voice room
re-derives at the new epoch. The refounder publishes kind-3303 rumors at a
precomputed address (base_rekey_group_key(prior_root, community_id, epoch+1)),
carrying each surviving member a NIP-44-wrapped 72-byte payload:
scope_id[32] ‖ epoch_be[8] ‖ new_key[32], located by recipient_locator.

Validation before adopting (CORD-06 §3): the INNER scope/epoch must match the
event tags, the prevcommit must equal our held key's epoch-key commitment
(continuity — a forger without the current root can't fake it), and the
rotator must be community staff per the roster (owner-rooted, so a random
member can't rotate us onto their key even from inside).
"""

from __future__ import annotations

import base64
import json
import logging

from nostr_sdk import PublicKey, SecretKey, nip44_decrypt

from . import config as cfg_mod
from .config import Config
from .control import fetch_control_editions
from .cord import (base_rekey_group_key, epoch_key_commitment,
                   recipient_locator, xonly_pubkey)
from .relay import fetch_group_wraps_multi
from .roles import BAN, build_roster
from .stream import KIND_WRAP, Opened, _open, tag_value

log = logging.getLogger("lowfi.rekey")

KIND_REKEY = 3303
ZERO32 = bytes(32)


def _open_rekey_wrap(wrap: dict, group) -> Opened | None:
    # Rekey wraps carry encrypted seals like chat; accept plaintext too.
    return (_open(wrap, group, plaintext_seal=False)
            or _open(wrap, group, plaintext_seal=True))


def _parse(opened: Opened) -> dict | None:
    if opened.kind != KIND_REKEY:
        return None
    try:
        scope = tag_value(opened.tags, "scope")
        newepoch = int(tag_value(opened.tags, "newepoch"))
        prevepoch = int(tag_value(opened.tags, "prevepoch"))
        prevcommit = tag_value(opened.tags, "prevcommit").lower()
        chunk = next((t for t in opened.tags if t[0] == "chunk"), ["chunk", "1", "1"])
        blobs = [b for b in json.loads(opened.content)
                 if isinstance(b, dict) and "locator" in b and "wrapped" in b]
        return {"rotator": opened.author, "scope": scope.lower(),
                "newepoch": newepoch, "prevepoch": prevepoch,
                "prevcommit": prevcommit, "chunk_i": int(chunk[1]),
                "chunk_n": int(chunk[2]), "blobs": blobs}
    except (TypeError, ValueError, AttributeError, json.JSONDecodeError, StopIteration):
        return None


async def _rotator_is_staff(cfg: Config, rotator: str) -> bool:
    """Validate authority against the OLD epoch's roster (what we can verify)."""
    try:
        editions = await fetch_control_editions(
            bytes.fromhex(cfg.community_root), bytes.fromhex(cfg.community_id),
            cfg.root_epoch, cfg.relays)
        roster = build_roster(editions, cfg.owner, bytes.fromhex(cfg.community_id))
        if rotator.lower() == cfg.owner.lower():
            return True
        perms = 0
        for role_id in roster.grants.get(rotator.lower(), []):
            perms |= roster.roles.get(role_id, 0)
        return bool(perms & BAN)
    except Exception as e:
        log.warning("roster check for rotator failed: %s", e)
        return False


async def follow_refounding(cfg: Config, save: bool = True) -> bool:
    """Check for a Refounding at root_epoch+1; adopt it if valid. Returns True
    if the config moved to a new epoch (repeat until False to catch chains)."""
    prior_root = bytes.fromhex(cfg.community_root)
    cid = bytes.fromhex(cfg.community_id)
    new_epoch = cfg.root_epoch + 1
    me_sk = bytes.fromhex(cfg.nsec_hex)
    me_xonly = xonly_pubkey(me_sk)

    rekey_group = base_rekey_group_key(prior_root, cid, new_epoch)
    wraps = await fetch_group_wraps_multi(cfg.relays, rekey_group, [KIND_WRAP])
    if not wraps:
        return False
    log.info("refounding candidates at epoch %d: %d wraps", new_epoch, len(wraps))

    parsed = [p for w in wraps
              if (o := _open_rekey_wrap(w, rekey_group)) and (p := _parse(o))]
    # Group chunks by rotation identity; a rotation is usable once we hold all n
    # chunks OR we find our locator in any chunk (a missing chunk is never a
    # removal — CORD-06 §3).
    for p in parsed:
        if p["scope"] != ZERO32.hex() or p["newepoch"] != new_epoch:
            continue
        if p["prevepoch"] != cfg.root_epoch:
            continue
        # Continuity: commitment over the key we hold must match.
        want = epoch_key_commitment(cfg.root_epoch, prior_root).hex()
        if p["prevcommit"] != want:
            log.warning("refounding by %s… fails continuity — ignoring", p["rotator"][:8])
            continue
        rotator_xonly = bytes.fromhex(p["rotator"])
        locator = recipient_locator(rotator_xonly, me_xonly, ZERO32, new_epoch).hex()
        blob = next((b for b in p["blobs"] if b["locator"].lower() == locator), None)
        if blob is None:
            continue
        if not await _rotator_is_staff(cfg, p["rotator"]):
            log.warning("refounding by %s… fails authority check — ignoring", p["rotator"][:8])
            continue
        plain = base64.b64decode(nip44_decrypt(
            SecretKey.parse(me_sk.hex()), PublicKey.parse(p["rotator"]), blob["wrapped"]))
        if len(plain) != 72 or plain[:32] != ZERO32:
            log.warning("wrapped key malformed/mis-scoped — ignoring")
            continue
        if int.from_bytes(plain[32:40], "big") != new_epoch:
            log.warning("wrapped key epoch mismatch — ignoring")
            continue
        new_key = plain[40:72]

        log.info("REFOUNDING ADOPTED: epoch %d -> %d (rotator %s…)",
                 cfg.root_epoch, new_epoch, p["rotator"][:8])
        cfg.community_root = new_key.hex()
        cfg.root_epoch = new_epoch
        if save:
            cfg_mod.save(cfg)
        return True

    log.info("refounding events present but no valid blob for us "
             "(removed from the community, or partial chunks) — staying at epoch %d",
             cfg.root_epoch)
    return False


async def follow_all(cfg: Config) -> int:
    """Follow chained refoundings; returns how many epochs we advanced."""
    hops = 0
    while await follow_refounding(cfg):
        hops += 1
        if hops > 32:
            raise RuntimeError("absurd refounding chain — refusing to continue")
    return hops
