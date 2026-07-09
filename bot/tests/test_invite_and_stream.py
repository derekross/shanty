"""Tests: invite-link parsing/decrypt and the CORD-01 stream wrap roundtrip."""

import base64
import json
import os

from nostr_sdk import Keys, Kind, Coordinate, Nip19Coordinate

from bot import nip44raw
from bot.cord import channel_group_key, invite_bundle_key, xonly_pubkey
from bot.invite import KIND_INVITE_BUNDLE, decode_fragment, parse_invite_link
from bot.stream import build_rumor, open_wrap, seal_rumor, wrap_seal


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class TestFragment:
    def test_stock_set(self):
        token = bytes(range(16))
        frag = b64url(bytes([4, 0x01]) + token)
        got_token, relays = decode_fragment(frag)
        assert got_token == token
        assert relays == ["wss://jskitty.com/nostr", "wss://asia.vectorapp.io/nostr",
                          "wss://relay.ditto.pub", "wss://relay.dreamith.to"]

    def test_explicit_relays(self):
        token = bytes([9] * 16)
        host = b"custom.example.com/x"
        # count=2: dictionary id 3 + lead 0 (wss:// + text)
        body = bytes([4, 0x00, 2, 3, 0, len(host)]) + host + token
        got_token, relays = decode_fragment(b64url(body))
        assert got_token == token
        assert relays == ["wss://relay.ditto.pub", "wss://custom.example.com/x"]

    def test_rejects_wrong_version_and_trailing(self):
        token = bytes(16)
        for bad in (bytes([3, 1]) + token, bytes([4, 1]) + token + b"\x00"):
            try:
                decode_fragment(b64url(bad))
                assert False, "should have raised"
            except ValueError:
                pass


class TestInviteLink:
    def _link(self, signer_pk_hex: str, fragment: str) -> str:
        coord = Coordinate(Kind(KIND_INVITE_BUNDLE), __import__("nostr_sdk").PublicKey.parse(signer_pk_hex), "")
        naddr = Nip19Coordinate(coord, []).to_bech32()
        return f"https://armada.buzz/invite/{naddr}#{fragment}"

    def test_parse_and_decrypt_bundle(self):
        keys = Keys.generate()
        signer_pk = keys.public_key().to_hex()
        token = os.urandom(16)
        frag = b64url(bytes([4, 0x01]) + token)
        link = self._link(signer_pk, frag)

        got_signer, got_token, relays = parse_invite_link(link)
        assert got_signer == signer_pk
        assert got_token == token
        assert len(relays) == 4

        bundle = {"community_id": "a" * 64, "owner": "b" * 64, "owner_salt": "c" * 64,
                  "community_root": "d" * 64, "root_epoch": 0, "channels": [],
                  "relays": [], "name": "Shanty Test Cove"}
        ct = nip44raw.encrypt(invite_bundle_key(token), json.dumps(bundle))
        assert json.loads(nip44raw.decrypt(invite_bundle_key(token), ct)) == bundle

    def test_bare_naddr_form(self):
        keys = Keys.generate()
        frag = b64url(bytes([4, 0x01]) + bytes(16))
        coord = Coordinate(Kind(KIND_INVITE_BUNDLE), keys.public_key(), "")
        naddr = Nip19Coordinate(coord, []).to_bech32()
        signer, _, _ = parse_invite_link(f"{naddr}#{frag}")
        assert signer == keys.public_key().to_hex()


class TestStreamWraps:
    def test_roundtrip_tamper_binding(self):
        secret, chan = os.urandom(32), os.urandom(32)
        stream = channel_group_key(secret, chan, 0)
        bot_sk = os.urandom(32)
        rumor = build_rumor(23313, "joined",
                            [["channel", chan.hex()], ["epoch", "0"],
                             ["identity", "abc"], ["broker", "https://armada.buzz"]],
                            xonly_pubkey(bot_sk).hex(), ms=1783620000123)
        wrap = wrap_seal(seal_rumor(rumor, stream, bot_sk), stream)
        assert wrap["kind"] == 21059 and wrap["pubkey"] == stream.pk

        opened = open_wrap(wrap, stream, chan.hex(), 0)
        assert opened and opened.author == xonly_pubkey(bot_sk).hex()
        assert opened.ms == 1783620000123 and opened.content == "joined"

        bad = dict(wrap)
        bad["content"] = wrap["content"][:-4] + "AAAA"
        assert open_wrap(bad, stream, chan.hex(), 0) is None
        assert open_wrap(wrap, stream, chan.hex(), 1) is None       # wrong epoch
        assert open_wrap(wrap, stream, os.urandom(32).hex(), 0) is None  # wrong channel
