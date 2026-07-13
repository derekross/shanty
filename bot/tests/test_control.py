"""Control-plane reader: plaintext-seal wraps and the channel-directory fold."""

import json
import os

from bot.control import Edition, fold_channels
from bot.cord import control_group_key, xonly_pubkey
from bot.stream import (KIND_CONTROL, build_rumor, open_control_wrap,
                        wrap_seal, KIND_SEAL_PLAINTEXT)
from bot.voice import finalize_event


def make_control_wrap(control, author_sk, eid_hex, version, meta):
    rumor = build_rumor(KIND_CONTROL, json.dumps(meta),
                        [["vsk", "2"], ["eid", eid_hex], ["ev", str(version)]],
                        xonly_pubkey(author_sk).hex(), ms=None)
    seal = finalize_event(author_sk, KIND_SEAL_PLAINTEXT, [],
                          json.dumps(rumor, separators=(",", ":")),
                          created_at=rumor["created_at"])
    return wrap_seal(seal, control, ephemeral=False)


class TestControlPlane:
    def test_open_and_fold_channels(self):
        root, cid = os.urandom(32), os.urandom(32)
        control = control_group_key(root, cid, 0)
        admin_sk = os.urandom(32)

        chan_a, chan_b = os.urandom(32).hex(), os.urandom(32).hex()
        wraps = [
            make_control_wrap(control, admin_sk, chan_a, 1,
                              {"name": "general", "private": False}),
            make_control_wrap(control, admin_sk, chan_b, 1,
                              {"name": "lofi lounge", "private": False}),
            # A later edition renames channel A — fold must pick version 2.
            make_control_wrap(control, admin_sk, chan_a, 2,
                              {"name": "general-chat", "private": False}),
        ]

        editions = []
        for w in wraps:
            opened = open_control_wrap(w, control)
            assert opened is not None and opened.kind == KIND_CONTROL
            from bot.stream import tag_value
            editions.append(Edition(
                author=opened.author, vsk=tag_value(opened.tags, "vsk"),
                eid=tag_value(opened.tags, "eid"),
                version=int(tag_value(opened.tags, "ev")), content=opened.content))

        channels = fold_channels(editions)
        by_name = {c.name: c for c in channels}
        assert set(by_name) == {"general-chat", "lofi lounge"}
        assert by_name["lofi lounge"].channel_id == chan_b
        assert by_name["general-chat"].channel_id == chan_a

    def test_control_wrap_rejects_encrypted_seal_mismatch(self):
        root, cid = os.urandom(32), os.urandom(32)
        control = control_group_key(root, cid, 0)
        other = control_group_key(os.urandom(32), cid, 0)
        wrap = make_control_wrap(control, os.urandom(32), os.urandom(32).hex(), 1,
                                 {"name": "x", "private": False})
        assert open_control_wrap(wrap, other) is None  # wrong plane key
