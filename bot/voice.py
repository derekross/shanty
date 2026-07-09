"""CORD-07 voice: derivations + the blind-broker client.

Port of Armada's client/src/concord-v2/lib/voice.ts (grant/token/rendezvous).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .cord import GroupKey, schnorr_sign, voice_group_key, voice_media_key, xonly_pubkey

KIND_HTTP_AUTH = 27235

MAX_VOICE_BROKERS = 3
VOICE_HEARTBEAT_S = 30.0
VOICE_STALE_S = 90.0


@dataclass(frozen=True)
class VoiceKeys:
    room: GroupKey       # pk = SFU room name; sk signs grants
    media_key: bytes     # raw 32-byte media root


def voice_keys(channel_secret: bytes, channel_id: bytes, epoch: int) -> VoiceKeys:
    return VoiceKeys(
        room=voice_group_key(channel_secret, channel_id, epoch),
        media_key=voice_media_key(channel_secret, channel_id, epoch),
    )


# ── Nostr event signing (kind 27235 grant) ───────────────────────────────────

def _event_id(pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str) -> str:
    payload = json.dumps([0, pubkey, created_at, kind, tags, content],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def finalize_event(sk: bytes, kind: int, tags: list[list[str]], content: str,
                   created_at: int | None = None) -> dict:
    """Build and schnorr-sign a Nostr event with the given secret key."""
    pubkey = xonly_pubkey(sk).hex()
    created_at = created_at if created_at is not None else int(time.time())
    eid = _event_id(pubkey, created_at, kind, tags, content)
    sig = schnorr_sign(bytes.fromhex(eid), sk)
    return {
        "id": eid,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig.hex(),
    }


# ── Broker client (CORD-07 §2) ───────────────────────────────────────────────

def av_token_url(origin: str, room_pk_hex: str) -> str:
    return f"{origin}/.well-known/concord/av/{room_pk_hex}"


def sign_av_grant(room: GroupKey, url: str, created_at: int | None = None) -> str:
    """kind-27235 grant signed by voice_key.sk, base64 of the JSON event.

    A random nonce tag keeps every grant's event id unique: the broker's
    anti-replay cache rejects duplicate ids, and two grants minted in the same
    second are otherwise identical. Brokers validate only u/method/sig/freshness
    (armada server/voice_token.go), so the extra tag is compatible.
    """
    nonce = os.urandom(8).hex()
    event = finalize_event(room.sk, KIND_HTTP_AUTH,
                           [["u", url], ["method", "GET"], ["nonce", nonce]],
                           "", created_at=created_at)
    return base64.b64encode(json.dumps(event, separators=(",", ":")).encode()).decode()


@dataclass(frozen=True)
class AvToken:
    token: str
    url: str        # wss:// SFU endpoint
    identity: str   # broker-assigned SFU identity (feeds sender-key derivation)


def probe_av_broker(origin: str, timeout: float = 5.0) -> bool:
    """Capability probe: GET /.well-known/concord/av → 204."""
    try:
        r = httpx.get(f"{origin}/.well-known/concord/av", timeout=timeout,
                      follow_redirects=False)
        return r.status_code == 204
    except httpx.HTTPError:
        return False


def fetch_av_token(origin: str, voice: VoiceKeys, timeout: float = 10.0) -> AvToken:
    url = av_token_url(origin, voice.room.pk)
    r = httpx.get(url, headers={"Authorization": f"Concord {sign_av_grant(voice.room, url)}"},
                  timeout=timeout)
    r.raise_for_status()
    body = r.json()
    sfu_url = body["url"]
    if urlparse(sfu_url).scheme != "wss":
        raise ValueError(f"broker returned non-wss SFU url: {sfu_url}")
    return AvToken(token=body["token"], url=sfu_url, identity=body["identity"])


# ── Rendezvous (CORD-07 §5) ──────────────────────────────────────────────────

def canonical_origin(raw: str) -> str | None:
    """RFC 6454-style ASCII origin: https only, lowercase host, drop :443/path."""
    try:
        u = urlparse(raw if "//" in raw else f"https://{raw}")
    except ValueError:
        return None
    if u.scheme != "https" or not u.hostname:
        return None
    port = f":{u.port}" if u.port and u.port != 443 else ""
    return f"https://{u.hostname.lower()}{port}"


def broker_rank(room_pk_hex: str, origin: str) -> str:
    """Tie-break rank: sha256(voice_room[32] || utf8(origin)), smallest hex wins."""
    return hashlib.sha256(bytes.fromhex(room_pk_hex) + origin.encode()).hexdigest()


def rendezvous_candidates(room_pk_hex: str, occupied: list[str], defaults: list[str]) -> list[str]:
    """Occupied brokers (canonicalized, tie-break ordered, capped) first, then defaults."""
    occ = []
    for o in occupied:
        c = canonical_origin(o)
        if c and c not in occ:
            occ.append(c)
    occ.sort(key=lambda o: broker_rank(room_pk_hex, o))
    out = occ[:MAX_VOICE_BROKERS]
    for d in defaults:
        c = canonical_origin(d)
        if c and c not in out:
            out.append(c)
    return out
