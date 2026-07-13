"""Shanty CLI.

  python -m bot.cli [--config PATH] create-identity
  python -m bot.cli [--config PATH] accept-invite [--channel-id <hex>]
  python -m bot.cli [--config PATH] run

Multi-community: each community runs its own bot instance on its own config
file (same npub). Create an extra instance with:

  python -m bot.cli --config ~/.config/lowfi/shanty-nest.json init-instance
  python -m bot.cli --config ~/.config/lowfi/shanty-nest.json accept-invite --link … --channel-name …
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path


def init_instance(cfg_path: Path, source_path: Path, fifo: str | None) -> None:
    """Create a config for an additional community instance: same identity,
    no community yet, status publishing off (one publisher per npub)."""
    from . import config as cfg_mod
    if cfg_path.exists():
        raise SystemExit(f"{cfg_path} already exists — refusing to overwrite")
    src = cfg_mod.load(source_path)
    if not src.nsec_hex:
        raise SystemExit(f"{source_path} has no identity — run create-identity first")
    name = cfg_path.stem.removeprefix("shanty-") or cfg_path.stem
    cfg = cfg_mod.Config(
        nsec_hex=src.nsec_hex, npub_hex=src.npub_hex,
        picture=src.picture, banner=src.banner,
        fifo=fifo or f"/tmp/lofi-{name}.pcm",
        nowplaying=src.nowplaying,      # shared: same broadcast, same track names
        publish_status=False,
    )
    cfg_mod.save(cfg, cfg_path)
    print(f"instance config written to {cfg_path} (fifo {cfg.fifo}, status publishing OFF)")
    print("next steps:")
    print(f"  1. python -m bot.cli --config {cfg_path} accept-invite --link <url> --channel-name <name>")
    print(f"  2. add --fifo {cfg.fifo} to lofi-stream.service and restart it")
    print(f"  3. systemctl --user enable --now shanty@{name}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="shanty", description="Concord lo-fi radio bot")
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="config file for this instance (default ~/.config/lowfi/shanty.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("create-identity", help="mint Shanty's npub and publish its profile")

    acc = sub.add_parser("accept-invite", help="join via direct invite or invite link")
    acc.add_argument("--channel-id", default=None, help="live channel id (64-char hex)")
    acc.add_argument("--channel-name", default=None,
                     help="live channel NAME — resolved to its id via the community's "
                          "channel directory (easier than hex)")
    acc.add_argument("--link", default=None,
                     help="public invite link (https://…/invite/naddr1…#… ) — "
                          "otherwise waits for a direct invite to the bot's npub")

    ini = sub.add_parser("init-instance",
                         help="create a config for an additional community (same npub); "
                              "pass the new file via --config")
    ini.add_argument("--from", dest="source", default=None, metavar="PATH",
                     help="config to copy the identity from (default the main config)")
    ini.add_argument("--fifo", default=None,
                     help="this instance's FIFO (default /tmp/lofi-<name>.pcm)")

    sub.add_parser("run", help="run the 24/7 radio bot")
    sub.add_parser("announce-join", help="publish the Guestbook join (member directory)")
    sub.add_parser("follow-rekey", help="check for community Refoundings and adopt the new keys")

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    from . import config as cfg_mod
    cfg_path = Path(args.config) if args.config else cfg_mod.DEFAULT_PATH

    if args.cmd == "create-identity":
        from .invite import create_identity
        asyncio.run(create_identity(cfg_path=cfg_path))
    elif args.cmd == "accept-invite":
        if args.link:
            from .invite import accept_invite_link
            asyncio.run(accept_invite_link(args.link, args.channel_id, args.channel_name,
                                           cfg_path=cfg_path))
        else:
            from .invite import accept_invite
            asyncio.run(accept_invite(args.channel_id, args.channel_name,
                                      cfg_path=cfg_path))
    elif args.cmd == "init-instance":
        if not args.config:
            raise SystemExit("init-instance needs --config <new instance file>")
        init_instance(cfg_path, Path(args.source) if args.source else cfg_mod.DEFAULT_PATH,
                      args.fifo)
    elif args.cmd == "run":
        from .main import run_bot
        asyncio.run(run_bot(cfg_path))
    elif args.cmd == "announce-join":
        from .guestbook import announce
        asyncio.run(announce(cfg_mod.load(cfg_path)))
    elif args.cmd == "follow-rekey":
        from .rekey import follow_all
        cfg = cfg_mod.load(cfg_path)
        hops = asyncio.run(follow_all(cfg))
        print(f"advanced {hops} epoch(s); root epoch is now {cfg.root_epoch}")


if __name__ == "__main__":
    main()
