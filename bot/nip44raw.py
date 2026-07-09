"""NIP-44 v2 encrypt/decrypt with a RAW conversation key.

nostr-sdk only exposes NIP-44 keyed by (sk, pk) pairs; Concord invite bundles
are encrypted under a derived 32-byte key (CORD-05 §2), so this implements the
v2 payload format directly. Vector-tested against nostr-tools (bot/tests/).
"""

from __future__ import annotations

import base64
import hmac as hmac_mod
import hashlib
import math
import os

from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import ChaCha20

VERSION = 2


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac_mod.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def _message_keys(conv_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    keys = _hkdf_expand(conv_key, nonce, 76)
    return keys[:32], keys[32:44], keys[44:76]  # chacha_key, chacha_nonce, hmac_key


def _calc_padded_len(n: int) -> int:
    if n <= 32:
        return 32
    next_pow = 1 << (math.floor(math.log2(n - 1)) + 1)
    chunk = 32 if next_pow <= 256 else next_pow // 8
    return chunk * math.ceil(n / chunk)


def _pad(plaintext: bytes) -> bytes:
    n = len(plaintext)
    if not 1 <= n <= 65535:
        raise ValueError("invalid plaintext length")
    return n.to_bytes(2, "big") + plaintext + bytes(_calc_padded_len(n) - n)


def _unpad(padded: bytes) -> bytes:
    n = int.from_bytes(padded[:2], "big")
    if n == 0 or len(padded) != 2 + _calc_padded_len(n):
        raise ValueError("invalid padding")
    return padded[2:2 + n]


def _chacha(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    # cryptography's ChaCha20 wants a 16-byte nonce: 4-byte LE counter (0) + 12-byte nonce
    cipher = Cipher(ChaCha20(key, b"\x00" * 4 + nonce12), mode=None)
    return cipher.encryptor().update(data)


def encrypt(conv_key: bytes, plaintext: str, nonce: bytes | None = None) -> str:
    nonce = nonce or os.urandom(32)
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    ciphertext = _chacha(chacha_key, chacha_nonce, _pad(plaintext.encode()))
    mac = hmac_mod.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(bytes([VERSION]) + nonce + ciphertext + mac).decode()


def decrypt(conv_key: bytes, payload: str) -> str:
    data = base64.b64decode(payload, validate=True)
    if len(data) < 99 or data[0] != VERSION:
        raise ValueError("invalid nip44 payload")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    expected = hmac_mod.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac_mod.compare_digest(mac, expected):
        raise ValueError("nip44 mac mismatch")
    return _unpad(_chacha(chacha_key, chacha_nonce, ciphertext)).decode()
