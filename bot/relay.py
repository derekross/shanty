"""Raw relay fetches for sealed planes.

Community relays (e.g. ditto) gate giftwrap queries: they serve kind-1059
events only to a session AUTHed (NIP-42) as the queried author. Plane wraps
are authored by derived group keys — which members hold — so we authenticate
as the group key itself. nostr-sdk's client auths as its own signer, hence
this small raw-websocket path.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from .cord import GroupKey
from .voice import finalize_event

log = logging.getLogger("lowfi.relay")

KIND_CLIENT_AUTH = 22242


async def fetch_group_wraps(relay_url: str, group: GroupKey, kinds: list[int],
                            limit: int = 500, timeout_s: float = 15.0) -> list[dict]:
    """Fetch events authored by `group.pk`, AUTHing as the group key if asked."""
    events: dict[str, dict] = {}
    flt = {"kinds": kinds, "authors": [group.pk], "limit": limit}
    try:
        async with websockets.connect(relay_url, open_timeout=timeout_s) as ws:
            await ws.send(json.dumps(["REQ", "plane", flt]))
            authed = False
            async with asyncio.timeout(timeout_s):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg[0] == "AUTH" and not authed:
                        auth = finalize_event(
                            group.sk, KIND_CLIENT_AUTH,
                            [["relay", relay_url], ["challenge", msg[1]]], "")
                        await ws.send(json.dumps(["AUTH", auth]))
                    elif msg[0] == "OK" and not authed:  # AUTH acknowledged
                        authed = True
                        await ws.send(json.dumps(["REQ", "plane2", flt]))
                    elif msg[0] == "EVENT":
                        events[msg[2]["id"]] = msg[2]
                    elif msg[0] == "EOSE" and (authed or msg[1] == "plane"):
                        break
                    elif msg[0] == "CLOSED" and (authed or msg[1] == "plane"):
                        if not authed:
                            continue  # initial REQ closed pending AUTH; wait for OK
                        log.debug("%s closed: %s", relay_url, msg[2])
                        break
    except (TimeoutError, OSError, websockets.WebSocketException) as e:
        log.warning("fetch from %s failed: %s", relay_url, e)
    return list(events.values())


async def fetch_group_wraps_multi(relay_urls: list[str], group: GroupKey,
                                  kinds: list[int], limit: int = 500) -> list[dict]:
    results = await asyncio.gather(
        *(fetch_group_wraps(u, group, kinds, limit) for u in relay_urls))
    merged: dict[str, dict] = {}
    for batch in results:
        for e in batch:
            merged[e["id"]] = e
    return list(merged.values())


async def subscribe_group_wraps(relay_url: str, group: GroupKey, kinds: list[int],
                                out: asyncio.Queue, since: int,
                                stop: asyncio.Event) -> None:
    """Live subscription to a group's wraps, AUTHing as the group key.
    Reconnects with backoff until `stop` is set; new events go to `out`."""
    backoff = 2
    while not stop.is_set():
        try:
            async with websockets.connect(relay_url, open_timeout=10) as ws:
                flt = {"kinds": kinds, "authors": [group.pk], "since": since}
                await ws.send(json.dumps(["REQ", "live", flt]))
                authed = False
                backoff = 2
                while not stop.is_set():
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    if msg[0] == "AUTH" and not authed:
                        auth = finalize_event(
                            group.sk, KIND_CLIENT_AUTH,
                            [["relay", relay_url], ["challenge", msg[1]]], "")
                        await ws.send(json.dumps(["AUTH", auth]))
                    elif msg[0] == "OK" and not authed:
                        authed = True
                        await ws.send(json.dumps(["REQ", "live2", flt]))
                    elif msg[0] == "EVENT":
                        event = msg[2]
                        since = max(since, event.get("created_at", since))
                        await out.put(event)
                    elif msg[0] == "CLOSED" and authed:
                        break  # relay ended us; reconnect
        except asyncio.TimeoutError:
            continue  # idle ping-out; reconnect quietly
        except (OSError, websockets.WebSocketException, json.JSONDecodeError) as e:
            log.warning("subscription to %s dropped: %s (retrying in %ds)",
                        relay_url, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
