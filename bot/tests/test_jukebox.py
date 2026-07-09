"""Jukebox: URL gating, queue semantics, and (network-marked) live resolvers."""

import asyncio

import pytest

from bot.jukebox import MAX_QUEUE, Jukebox, Track, resolve


def T(name: str) -> Track:
    return Track(stream_url=f"https://x/{name}.mp3", title=name, artist="a",
                 source="wavlake", requested_by="ab" * 32)


class TestQueue:
    def test_enqueue_positions_and_cap(self):
        jb = Jukebox()
        for i in range(MAX_QUEUE):
            assert jb.enqueue(T(f"t{i}")) == i + 1
        assert jb.enqueue(T("overflow")) is None
        assert len(jb.listing()) == MAX_QUEUE

    def test_pop_order_and_listing(self):
        jb = Jukebox()
        jb.enqueue(T("first")); jb.enqueue(T("second"))
        assert [t.title for t in jb.listing()] == ["first", "second"]
        assert jb.pop_next().title == "first"
        assert [t.title for t in jb.listing()] == ["second"]
        jb.pop_next()
        assert jb.pop_next() is None

    def test_skip_only_signals_when_playing(self):
        jb = Jukebox()
        assert jb.skip() is None
        assert not jb.skip_event.is_set()
        jb.now_playing = T("x")
        assert jb.skip().title == "x"
        assert jb.skip_event.is_set()


class TestUrlGating:
    def run(self, url):
        return asyncio.run(resolve(url, "ab" * 32))

    def test_rejects_non_https_and_foreign_hosts(self):
        assert self.run("http://wavlake.com/track/f272763b-8c11-445a-9473-995741fde794") is None
        assert self.run("https://evil.com/track/abc") is None
        assert self.run("https://wavlake.com.evil.com/track/abc") is None
        assert self.run("https://wavlake.com/album/whatever") is None
        assert self.run("not a url at all") is None


@pytest.mark.network
class TestLiveResolvers:
    def test_wavlake(self):
        t = asyncio.run(resolve(
            "https://wavlake.com/track/f272763b-8c11-445a-9473-995741fde794", "ab" * 32))
        assert t and t.source == "wavlake"
        assert t.stream_url.startswith("https://")
        assert t.title and t.artist
        assert t.duration_s and t.duration_s > 0

    def test_fountain(self):
        t = asyncio.run(resolve(
            "https://fountain.fm/track/BY8MzPtHsLio6Dq3ztoR", "ab" * 32))
        assert t and t.source == "fountain"
        assert t.stream_url.startswith("https://")
        assert t.title and t.artist
