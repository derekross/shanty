"""NIP-38 now-playing status: text priority, event shape, dedupe/refresh."""

import json

from bot.config import Config
from bot.jukebox import Jukebox, Track
from bot.status import (EXPIRY_S, KIND_STATUS, REFRESH_S, StatusPublisher,
                        build_status_event, read_nowplaying, status_text)

SK = bytes.fromhex("ab" * 32)


def write_np(path, name="misty raincoat"):
    path.write_text(json.dumps({"name": name, "style": "synthwave",
                                "seed": 321346528, "bpm": 80, "key": "A",
                                "started_at": 1783900000}))


def jukebox_track(title="Neon Nights", artist="Kai"):
    return Track(stream_url="https://x/t.mp3", title=title, artist=artist,
                 source="wavlake", requested_by="ab" * 32)


class TestNowplayingFile:
    def test_reads_name(self, tmp_path):
        p = tmp_path / "np.json"
        write_np(p)
        assert read_nowplaying(str(p)) == "misty raincoat"

    def test_missing_file_and_garbage_are_none(self, tmp_path):
        assert read_nowplaying(str(tmp_path / "absent.json")) is None
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert read_nowplaying(str(bad)) is None
        empty = tmp_path / "empty.json"
        empty.write_text('{"name": ""}')
        assert read_nowplaying(str(empty)) is None


class TestStatusText:
    def test_priority_jukebox_over_file_over_nothing(self, tmp_path):
        p = tmp_path / "np.json"
        jb = Jukebox()
        assert status_text(jb, str(p)) == ""
        write_np(p)
        assert status_text(jb, str(p)) == "streaming: misty raincoat 🎶"
        jb.now_playing = jukebox_track()
        assert status_text(jb, str(p)) == "streaming: Neon Nights — Kai (requested) 🎶"


class TestEventShape:
    def test_playing_status(self):
        ev = build_status_event(SK, "streaming: misty raincoat 🎶", now=1783900000)
        assert ev["kind"] == KIND_STATUS
        assert ev["content"] == "streaming: misty raincoat 🎶"
        assert ["d", "music"] in ev["tags"]
        assert ["expiration", str(1783900000 + EXPIRY_S)] in ev["tags"]
        assert len(ev["sig"]) == 128 and len(ev["id"]) == 64

    def test_clear_status_has_no_expiration(self):
        ev = build_status_event(SK, "", now=1783900000)
        assert ev["content"] == ""
        assert ev["tags"] == [["d", "music"]]


class TestPublishDecision:
    def make(self, tmp_path, muted=lambda: False):
        cfg = Config(nowplaying=str(tmp_path / "np.json"))
        jb = Jukebox()
        return StatusPublisher(cfg, jb, muted=muted), jb, tmp_path / "np.json"

    def test_nothing_playing_at_startup_stays_quiet(self, tmp_path):
        sp, _, _ = self.make(tmp_path)
        assert sp.next_publish(now=1000.0) is None

    def test_publishes_on_change_then_dedupes(self, tmp_path):
        sp, _, np = self.make(tmp_path)
        write_np(np)
        assert sp.next_publish(1000.0) == "streaming: misty raincoat 🎶"
        sp._last, sp._published_at = "streaming: misty raincoat 🎶", 1000.0
        assert sp.next_publish(1005.0) is None
        write_np(np, name="velvet dusk")
        assert sp.next_publish(1010.0) == "streaming: velvet dusk 🎶"

    def test_refreshes_before_expiry(self, tmp_path):
        sp, _, np = self.make(tmp_path)
        write_np(np)
        sp._last, sp._published_at = "streaming: misty raincoat 🎶", 1000.0
        assert sp.next_publish(1000.0 + REFRESH_S - 1) is None
        assert sp.next_publish(1000.0 + REFRESH_S + 1) == "streaming: misty raincoat 🎶"

    def test_mute_clears_and_unmute_restores(self, tmp_path):
        muted = {"on": False}
        sp, _, np = self.make(tmp_path, muted=lambda: muted["on"])
        write_np(np)
        sp._last, sp._published_at = "streaming: misty raincoat 🎶", 1000.0
        muted["on"] = True
        assert sp.next_publish(1005.0) == ""      # publish the clear
        sp._last = ""
        assert sp.next_publish(1010.0) is None    # cleared stays quiet, no refresh
        muted["on"] = False
        assert sp.next_publish(1015.0) == "streaming: misty raincoat 🎶"

    def test_jukebox_wins_over_file(self, tmp_path):
        sp, jb, np = self.make(tmp_path)
        write_np(np)
        jb.now_playing = jukebox_track()
        assert sp.next_publish(1000.0) == "streaming: Neon Nights — Kai (requested) 🎶"
