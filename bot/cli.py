"""Shanty CLI.

  python -m bot.cli create-identity
  python -m bot.cli accept-invite [--channel-id <hex>]
  python -m bot.cli run
"""

from __future__ import annotations

import argparse
import asyncio
import logging


def main() -> None:
    ap = argparse.ArgumentParser(prog="shanty", description="Concord lo-fi radio bot")
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

    sub.add_parser("run", help="run the 24/7 radio bot")
    sub.add_parser("announce-join", help="publish the Guestbook join (member directory)")

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    if args.cmd == "create-identity":
        from .invite import create_identity
        asyncio.run(create_identity())
    elif args.cmd == "accept-invite":
        if args.link:
            from .invite import accept_invite_link
            asyncio.run(accept_invite_link(args.link, args.channel_id, args.channel_name))
        else:
            from .invite import accept_invite
            asyncio.run(accept_invite(args.channel_id, args.channel_name))
    elif args.cmd == "run":
        from .main import run_bot
        asyncio.run(run_bot())
    elif args.cmd == "announce-join":
        from . import config as cfg_mod
        from .guestbook import announce
        asyncio.run(announce(cfg_mod.load()))


if __name__ == "__main__":
    main()
