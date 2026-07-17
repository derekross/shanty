"""The in-session Refounding watcher: a healthy session must still notice a
rotation (the old SFU room outlives it, so nothing else fails)."""

import asyncio
import os

import pytest

from bot import main as main_mod
from bot.config import Config
from bot.cord import xonly_pubkey


def make_cfg():
    me_sk = os.urandom(32)
    return Config(nsec_hex=me_sk.hex(), npub_hex=xonly_pubkey(me_sk).hex(),
                  community_id=os.urandom(32).hex(),
                  community_root=os.urandom(32).hex(), root_epoch=0,
                  owner=xonly_pubkey(os.urandom(32)).hex(),
                  channel_id=os.urandom(32).hex())


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(main_mod, "REKEY_CHECK_INTERVAL_S", 0)
    return main_mod.Shanty(make_cfg())


class TestWatchRefounding:
    @pytest.mark.asyncio
    async def test_raises_refounded_when_epoch_advances(self, bot, monkeypatch):
        results = iter([0, 0, 1])

        async def fake_follow_all(cfg):
            return next(results)

        monkeypatch.setattr(main_mod, "follow_all", fake_follow_all)
        with pytest.raises(main_mod.Refounded):
            await asyncio.wait_for(bot._watch_refounding(), timeout=5)

    @pytest.mark.asyncio
    async def test_survives_transient_check_failures(self, bot, monkeypatch):
        results = [RuntimeError("relay 429"), 0, 1]

        async def fake_follow_all(cfg):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(main_mod, "follow_all", fake_follow_all)
        with pytest.raises(main_mod.Refounded):
            await asyncio.wait_for(bot._watch_refounding(), timeout=5)

    @pytest.mark.asyncio
    async def test_quiet_watcher_never_returns(self, bot, monkeypatch):
        async def fake_follow_all(cfg):
            return 0

        monkeypatch.setattr(main_mod, "follow_all", fake_follow_all)
        task = asyncio.create_task(bot._watch_refounding())
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()
