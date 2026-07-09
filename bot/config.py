"""Shanty's config: identity + community/channel secrets + relays.

JSON at ~/.config/lowfi/shanty.json, chmod 0600 (it holds key material).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "lowfi" / "shanty.json"
DEFAULT_RELAYS = ["wss://relay.ditto.pub", "wss://relay.dreamith.to"]
DEFAULT_BROKERS = ["https://armada.buzz"]


@dataclass
class Config:
    nsec_hex: str = ""
    npub_hex: str = ""
    # Community (from the invite bundle)
    community_id: str = ""
    community_root: str = ""      # hex; secret for public channels
    root_epoch: int = 0
    community_name: str = ""
    owner: str = ""               # owner pubkey hex — roots the role system
    # The live channel Shanty plays in
    channel_id: str = ""
    channel_key: str = ""         # hex; empty = public channel (use community_root)
    channel_epoch: int = 0
    channel_name: str = ""
    # Infra
    relays: list[str] = field(default_factory=lambda: list(DEFAULT_RELAYS))
    brokers: list[str] = field(default_factory=lambda: list(DEFAULT_BROKERS))
    fifo: str = "/tmp/lofi.pcm"
    volume: float = 0.5           # master output gain (1.0 = full; 0.5 ≈ -6 dB)
    # Published profile media (kept here so kind-0 republishes never drop them)
    picture: str = ""
    banner: str = ""

    @property
    def channel_secret(self) -> bytes:
        return bytes.fromhex(self.channel_key or self.community_root)

    @property
    def channel_epoch_effective(self) -> int:
        return self.channel_epoch if self.channel_key else self.root_epoch

    @property
    def channel_id_bytes(self) -> bytes:
        return bytes.fromhex(self.channel_id)


def load(path: Path = DEFAULT_PATH) -> Config:
    data = json.loads(path.read_text())
    return Config(**data)


def save(cfg: Config, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2))
    os.chmod(path, 0o600)
