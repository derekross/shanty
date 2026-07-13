"""CORD-06 follower: adopt a valid Refounding, reject forgeries."""

import base64
import json
import os

import pytest
from nostr_sdk import PublicKey, SecretKey, nip44_encrypt, Nip44Version

from bot import rekey as rekey_mod
from bot.config import Config
from bot.cord import (base_rekey_group_key, epoch_key_commitment,
                      recipient_locator, xonly_pubkey)
from bot.stream import build_rumor, seal_rumor, wrap_seal

ZERO32 = bytes(32)


def make_refounding(prior_root, cid, rotator_sk, me_pk_hex, new_key,
                    new_epoch=1, prev_commit=None):
    """Build the refounding wrap exactly as armada's buildRekeyRumors does."""
    rotator_pk = xonly_pubkey(rotator_sk)
    payload = ZERO32 + new_epoch.to_bytes(8, "big") + new_key
    wrapped = nip44_encrypt(SecretKey.parse(rotator_sk.hex()),
                            PublicKey.parse(me_pk_hex),
                            base64.b64encode(payload).decode(), Nip44Version.V2)
    locator = recipient_locator(rotator_pk, bytes.fromhex(me_pk_hex),
                                ZERO32, new_epoch).hex()
    commit = (prev_commit or epoch_key_commitment(0, prior_root).hex())
    rumor = build_rumor(
        3303, json.dumps([{"locator": locator, "wrapped": wrapped}]),
        [["scope", ZERO32.hex()], ["newepoch", str(new_epoch)],
         ["prevepoch", "0"], ["prevcommit", commit], ["chunk", "1", "1"]],
        rotator_pk.hex(), ms=1783885062000)
    group = base_rekey_group_key(prior_root, cid, new_epoch)
    return wrap_seal(seal_rumor(rumor, group, rotator_sk), group, ephemeral=False)


@pytest.fixture
def world(monkeypatch):
    prior_root, cid = os.urandom(32), os.urandom(32)
    rotator_sk, me_sk = os.urandom(32), os.urandom(32)
    new_key = os.urandom(32)
    cfg = Config(nsec_hex=me_sk.hex(), npub_hex=xonly_pubkey(me_sk).hex(),
                 community_id=cid.hex(), community_root=prior_root.hex(),
                 root_epoch=0, owner=xonly_pubkey(rotator_sk).hex())
    monkeypatch.setattr(rekey_mod, "_rotator_is_staff",
                        staticmethod_async(True))
    served = {}

    async def fake_fetch(relays, group, kinds, limit=500):
        return served.get(group.pk, [])

    monkeypatch.setattr(rekey_mod, "fetch_group_wraps_multi", fake_fetch)
    return dict(prior_root=prior_root, cid=cid, rotator_sk=rotator_sk,
                me_sk=me_sk, new_key=new_key, cfg=cfg, served=served)


def staticmethod_async(result):
    async def f(*a, **k):
        return result
    return f


class TestFollower:
    @pytest.mark.asyncio
    async def test_adopts_valid_refounding(self, world):
        w = world
        wrap = make_refounding(w["prior_root"], w["cid"], w["rotator_sk"],
                               w["cfg"].npub_hex, w["new_key"])
        group = base_rekey_group_key(w["prior_root"], w["cid"], 1)
        w["served"][group.pk] = [wrap]

        moved = await rekey_mod.follow_refounding(w["cfg"], save=False)
        assert moved
        assert w["cfg"].root_epoch == 1
        assert w["cfg"].community_root == w["new_key"].hex()

    @pytest.mark.asyncio
    async def test_rejects_bad_continuity(self, world):
        w = world
        wrap = make_refounding(w["prior_root"], w["cid"], w["rotator_sk"],
                               w["cfg"].npub_hex, w["new_key"],
                               prev_commit="ab" * 32)  # commitment over wrong key
        group = base_rekey_group_key(w["prior_root"], w["cid"], 1)
        w["served"][group.pk] = [wrap]

        assert not await rekey_mod.follow_refounding(w["cfg"], save=False)
        assert w["cfg"].root_epoch == 0

    @pytest.mark.asyncio
    async def test_not_a_recipient_stays_put(self, world):
        w = world
        other_pk = xonly_pubkey(os.urandom(32)).hex()
        wrap = make_refounding(w["prior_root"], w["cid"], w["rotator_sk"],
                               other_pk, w["new_key"])  # blob for someone else
        group = base_rekey_group_key(w["prior_root"], w["cid"], 1)
        w["served"][group.pk] = [wrap]

        assert not await rekey_mod.follow_refounding(w["cfg"], save=False)
        assert w["cfg"].root_epoch == 0
