"""Multi-instance plumbing: config path memory, status flag, init-instance."""

import json
from pathlib import Path

import pytest

from bot import config as cfg_mod
from bot.cli import init_instance


def write_cfg(path: Path, **overrides) -> Path:
    data = {"nsec_hex": "ab" * 32, "npub_hex": "cd" * 32,
            "community_id": "11" * 32, "community_root": "22" * 32,
            "channel_id": "33" * 32, "picture": "https://x/p.png"}
    data.update(overrides)
    path.write_text(json.dumps(data))
    return path


class TestConfigPath:
    def test_save_returns_to_loaded_path(self, tmp_path):
        p = write_cfg(tmp_path / "instance.json")
        cfg = cfg_mod.load(p)
        cfg.root_epoch = 7
        cfg_mod.save(cfg)  # no explicit path — the rekey.py call shape
        assert json.loads(p.read_text())["root_epoch"] == 7
        assert not (cfg_mod.DEFAULT_PATH.exists()
                    and json.loads(cfg_mod.DEFAULT_PATH.read_text()).get("root_epoch") == 7)

    def test_path_attr_not_serialized(self, tmp_path):
        p = write_cfg(tmp_path / "a.json")
        cfg = cfg_mod.load(p)
        cfg_mod.save(cfg)
        assert "path" not in json.loads(p.read_text())

    def test_old_config_defaults_publish_status_on(self, tmp_path):
        cfg = cfg_mod.load(write_cfg(tmp_path / "old.json"))
        assert cfg.publish_status is True


class TestInitInstance:
    def test_copies_identity_not_community(self, tmp_path):
        src = write_cfg(tmp_path / "shanty.json")
        new = tmp_path / "shanty-nest.json"
        init_instance(new, src, fifo=None)
        cfg = cfg_mod.load(new)
        assert cfg.nsec_hex == "ab" * 32 and cfg.npub_hex == "cd" * 32
        assert cfg.picture == "https://x/p.png"
        assert cfg.community_id == "" and cfg.channel_id == ""
        assert cfg.publish_status is False
        assert cfg.fifo == "/tmp/lofi-nest.pcm"

    def test_refuses_overwrite_and_missing_identity(self, tmp_path):
        src = write_cfg(tmp_path / "shanty.json")
        new = tmp_path / "shanty-nest.json"
        init_instance(new, src, fifo=None)
        with pytest.raises(SystemExit):
            init_instance(new, src, fifo=None)
        empty = write_cfg(tmp_path / "empty.json", nsec_hex="")
        with pytest.raises(SystemExit):
            init_instance(tmp_path / "x.json", empty, fifo=None)
