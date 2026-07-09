"""CORD-01 stream wraps: rumor -> seal -> wrap, and the reverse.

Port of Armada's client/src/concord-v2/lib/stream.ts. Both layers encrypt with
NIP-44 under the stream's self-ECDH conversation key; the seal is signed by the
author's real key, the wrap by the derived stream key (reversed NIP-59).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass

from coincurve import PublicKeyXOnly
from nostr_sdk import Keys, PublicKey, SecretKey, nip44_encrypt, nip44_decrypt, Nip44Version

from .cord import GroupKey, xonly_pubkey
from .voice import finalize_event

KIND_WRAP = 1059
KIND_WRAP_EPHEMERAL = 21059
KIND_SEAL_ENCRYPTED = 20013
KIND_SEAL_PLAINTEXT = 20014   # Control plane only
KIND_VOICE_PRESENCE = 23313
KIND_CONTROL = 3308
KIND_CHAT = 9

TAG_MS = "ms"


def event_hash(pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str) -> str:
    payload = json.dumps([0, pubkey, created_at, kind, tags, content],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_event(event: dict) -> bool:
    try:
        eid = event_hash(event["pubkey"], event["created_at"], event["kind"],
                         event["tags"], event["content"])
        if eid != event.get("id"):
            return False
        return PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
            bytes.fromhex(event["sig"]), bytes.fromhex(eid))
    except Exception:
        return False


def _enc(stream: GroupKey, plaintext: str) -> str:
    return nip44_encrypt(SecretKey.parse(stream.sk.hex()), PublicKey.parse(stream.pk),
                         plaintext, Nip44Version.V2)


def _dec(stream: GroupKey, ciphertext: str) -> str:
    return nip44_decrypt(SecretKey.parse(stream.sk.hex()), PublicKey.parse(stream.pk), ciphertext)


# ── Building ─────────────────────────────────────────────────────────────────

def build_rumor(kind: int, content: str, tags: list[list[str]], pubkey: str,
                ms: int | None = None) -> dict:
    """Unsigned rumor; ms (epoch millis) split into created_at + ms tag (CORD-02 §4)."""
    tags = [list(t) for t in tags]
    if ms is None:
        created_at = int(time.time())
    else:
        created_at = ms // 1000
        tags.append([TAG_MS, str(ms % 1000)])
    rumor = {"kind": kind, "content": content, "tags": tags,
             "created_at": created_at, "pubkey": pubkey}
    rumor["id"] = event_hash(pubkey, created_at, kind, tags, content)
    return rumor


def seal_rumor(rumor: dict, stream: GroupKey, author_sk: bytes) -> dict:
    """Kind-20013 seal: rumor NIP-44'd under the stream conv key, signed by the
    author's REAL key, created_at mirroring the rumor's."""
    return finalize_event(author_sk, KIND_SEAL_ENCRYPTED, [],
                          _enc(stream, json.dumps(rumor, separators=(",", ":"))),
                          created_at=rumor["created_at"])


def wrap_seal(seal: dict, stream: GroupKey, ephemeral: bool = True) -> dict:
    """Outer wrap: seal NIP-44'd under the stream conv key, signed by the STREAM
    key, tagged with a random ephemeral p (NIP-59 reversed), created_at untweaked."""
    ephemeral_pk = xonly_pubkey(_random_valid_sk()).hex()
    return finalize_event(
        stream.sk,
        KIND_WRAP_EPHEMERAL if ephemeral else KIND_WRAP,
        [["p", ephemeral_pk]],
        _enc(stream, json.dumps(seal, separators=(",", ":"))),
    )


def _random_valid_sk() -> bytes:
    while True:
        sk = os.urandom(32)
        try:
            Keys.parse(sk.hex())
            return sk
        except Exception:
            continue


# ── Opening ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Opened:
    rumor_id: str
    author: str      # verified: the seal's signer, == rumor.pubkey
    kind: int
    content: str
    tags: list[list[str]]
    ms: int          # created_at*1000 + ms tag


def tag_value(tags: list[list[str]], name: str) -> str | None:
    for t in tags:
        if len(t) >= 2 and t[0] == name:
            return t[1]
    return None


def open_wrap(wrap: dict, stream: GroupKey, channel_id_hex: str, epoch: int) -> Opened | None:
    """Decrypt + verify a stream wrap; None for anything malformed or misbound."""
    opened = _open(wrap, stream, plaintext_seal=False)
    if opened is None:
        return None
    # Channel binding (CORD-03 §3): reject cross-channel/epoch replays.
    if tag_value(opened.tags, "channel") != channel_id_hex:
        return None
    if tag_value(opened.tags, "epoch") != str(epoch):
        return None
    return opened


def open_control_wrap(wrap: dict, control: GroupKey) -> Opened | None:
    """Control-plane wraps use PLAINTEXT seals (kind 20014) and edition tags
    instead of channel binding (CORD-02 §5)."""
    return _open(wrap, control, plaintext_seal=True)


def _open(wrap: dict, stream: GroupKey, plaintext_seal: bool) -> Opened | None:
    try:
        if wrap.get("kind") not in (KIND_WRAP, KIND_WRAP_EPHEMERAL):
            return None
        if wrap.get("pubkey") != stream.pk:
            return None
        seal = json.loads(_dec(stream, wrap["content"]))
        want_kind = KIND_SEAL_PLAINTEXT if plaintext_seal else KIND_SEAL_ENCRYPTED
        if seal.get("kind") != want_kind or not verify_event(seal):
            return None
        rumor = json.loads(seal["content"] if plaintext_seal else _dec(stream, seal["content"]))
        if rumor.get("pubkey") != seal["pubkey"]:  # anti-spoof: seal signer is author
            return None
        expected_id = event_hash(rumor["pubkey"], rumor["created_at"], rumor["kind"],
                                 rumor["tags"], rumor["content"])
        if rumor.get("id") != expected_id:
            return None
        ms_tag = tag_value(rumor["tags"], TAG_MS)
        ms = rumor["created_at"] * 1000 + (int(ms_tag) if ms_tag else 0)
        return Opened(rumor_id=rumor["id"], author=rumor["pubkey"], kind=rumor["kind"],
                      content=rumor["content"], tags=rumor["tags"], ms=ms)
    except Exception:
        return None
