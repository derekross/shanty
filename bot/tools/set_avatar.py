"""Set Shanty's profile picture or banner: resize, upload to Blossom, update kind-0.

  .venv/bin/python -m bot.tools.set_avatar picture profile.png
  .venv/bin/python -m bot.tools.set_avatar banner banner.png

Both URLs persist in the bot config, so every kind-0 republish carries them.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import mimetypes
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from nostr_sdk import Client, EventBuilder, Keys, Metadata, NostrSigner, RelayUrl

from .. import config as cfg_mod
from ..invite import PROFILE
from ..voice import finalize_event

KIND_BLOSSOM_AUTH = 24242
DEFAULT_SERVERS = ["https://blossom.band", "https://blossom.primal.net"]

# field -> ffmpeg scale filter (avatar: exact square; banner: bound the width)
RESIZE = {
    "picture": "scale=512:512",
    "banner": "scale='min(1500,iw)':-1",
}


def resize(field: str, src: Path) -> Path:
    out = Path(tempfile.mkdtemp(prefix="shanty-img-")) / f"{field}{src.suffix}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-vf", RESIZE[field], str(out)], check=True)
    return out


def blossom_upload(server: str, blob: bytes, mime: str, sk: bytes) -> str:
    sha = hashlib.sha256(blob).hexdigest()
    auth = finalize_event(sk, KIND_BLOSSOM_AUTH,
                          [["t", "upload"], ["x", sha],
                           ["expiration", str(int(time.time()) + 600)]],
                          "Upload Shanty profile media")
    header = base64.b64encode(json.dumps(auth).encode()).decode()
    r = httpx.put(f"{server}/upload", content=blob,
                  headers={"Authorization": f"Nostr {header}", "Content-Type": mime},
                  timeout=60)
    r.raise_for_status()
    desc = r.json()
    if desc.get("sha256") not in (None, sha):
        raise ValueError(f"server returned mismatched sha256: {desc.get('sha256')}")
    return desc["url"]


async def publish_profile(cfg) -> None:
    keys = Keys.parse(cfg.nsec_hex)
    client = Client(NostrSigner.keys(keys))
    for r in cfg.relays:
        await client.add_relay(RelayUrl.parse(r))
    await client.connect()
    profile = dict(PROFILE)
    if cfg.picture:
        profile["picture"] = cfg.picture
    if cfg.banner:
        profile["banner"] = cfg.banner
    await client.send_event_builder(
        EventBuilder.metadata(Metadata.from_json(json.dumps(profile))))
    await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("field", choices=["picture", "banner"])
    ap.add_argument("image", help="source image path")
    ap.add_argument("--server", action="append", default=None)
    args = ap.parse_args()

    cfg = cfg_mod.load()
    sized = resize(args.field, Path(args.image))
    blob = sized.read_bytes()
    mime = mimetypes.guess_type(str(sized))[0] or "image/png"
    print(f"{args.field}: {Path(args.image).stat().st_size} -> {len(blob)} bytes")

    url = None
    for server in args.server or DEFAULT_SERVERS:
        try:
            url = blossom_upload(server, blob, mime, bytes.fromhex(cfg.nsec_hex))
            print(f"uploaded to {server}: {url}")
            break
        except Exception as e:
            print(f"{server}: {e}")
    if not url:
        raise SystemExit("all Blossom servers refused the upload")

    setattr(cfg, args.field, url)
    cfg_mod.save(cfg)
    asyncio.run(publish_profile(cfg))
    print(f"kind-0 updated: {args.field} = {url}")


if __name__ == "__main__":
    main()
