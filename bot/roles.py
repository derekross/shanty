"""CORD-04 roles — the conservative subset Shanty needs for !music gating.

Full clients validate an owner-rooted delegation fixpoint. Shanty applies a
stricter-but-safe rule: it honors role/grant editions authored by the OWNER,
plus one delegation hop (editions authored by someone the owner granted
MANAGE_ROLES). Forged grants by random members are never honored; the cost is
that deeply-delegated staff may be ignored (documented).

"Staff" = the owner, or any member holding a role with a management permission
(what Armada's built-in Admin and Moderator roles both carry).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .control import Edition
from .cord import grant_locator

log = logging.getLogger("lowfi.roles")

VSK_ROLE = "1"
VSK_GRANT = "3"

# Permission bits (CORD-04 §3, armada roles.ts)
MANAGE_ROLES = 1 << 0
MANAGE_CHANNELS = 1 << 1
MANAGE_METADATA = 1 << 2
KICK = 1 << 3
BAN = 1 << 4
MANAGE_MESSAGES = 1 << 5

# Any of these marks a role as "staff" for !music purposes.
STAFF_MASK = MANAGE_ROLES | MANAGE_CHANNELS | KICK | BAN | MANAGE_MESSAGES


@dataclass
class Roster:
    owner: str
    roles: dict[str, int] = field(default_factory=dict)      # role_id -> permissions
    grants: dict[str, list[str]] = field(default_factory=dict)  # member -> role_ids

    def is_staff(self, member_hex: str) -> bool:
        if member_hex == self.owner:
            return True
        perms = 0
        for role_id in self.grants.get(member_hex, []):
            perms |= self.roles.get(role_id, 0)
        return bool(perms & STAFF_MASK)


def _parse_role(content: str, eid: str) -> tuple[str, int] | None:
    try:
        w = json.loads(content)
        role_id = str(w["role_id"]).lower()
        if role_id != eid.lower():  # anti-spoof: coordinate must be the role's id
            return None
        perms = int(str(w["permissions"]))
        return role_id, perms
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _parse_grant(content: str) -> tuple[str, list[str]] | None:
    try:
        w = json.loads(content)
        member = str(w["member"]).lower()
        role_ids = [str(r).lower() for r in w.get("role_ids", [])]
        return member, role_ids
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def build_roster(editions: list[Edition], owner_hex: str, community_id: bytes) -> Roster:
    owner_hex = owner_hex.lower()

    def latest_by_eid(vsk: str, allowed_authors: set[str]) -> dict[str, Edition]:
        latest: dict[str, Edition] = {}
        for e in editions:
            if e.vsk != vsk or e.author.lower() not in allowed_authors:
                continue
            cur = latest.get(e.eid)
            if cur is None or e.version > cur.version:
                latest[e.eid] = e
        return latest

    # Pass 1: owner-authored roles and grants.
    roster = Roster(owner=owner_hex)
    for eid, e in latest_by_eid(VSK_ROLE, {owner_hex}).items():
        parsed = _parse_role(e.content, eid)
        if parsed:
            roster.roles[parsed[0]] = parsed[1]
    owner_grants = latest_by_eid(VSK_GRANT, {owner_hex})

    def apply_grants(grant_editions: dict[str, Edition]) -> None:
        for eid, e in grant_editions.items():
            parsed = _parse_grant(e.content)
            if not parsed:
                continue
            member, role_ids = parsed
            # anti-spoof: coordinate must be this member's grant locator
            if grant_locator(community_id, bytes.fromhex(member)).hex() != eid.lower():
                continue
            roster.grants[member] = role_ids

    apply_grants(owner_grants)

    # Pass 2 (one delegation hop): editions authored by MANAGE_ROLES holders.
    delegates = {m for m in roster.grants
                 if any(roster.roles.get(r, 0) & MANAGE_ROLES for r in roster.grants[m])}
    if delegates:
        for eid, e in latest_by_eid(VSK_ROLE, delegates).items():
            parsed = _parse_role(e.content, eid)
            if parsed and parsed[0] not in roster.roles:
                roster.roles[parsed[0]] = parsed[1]
        extra = latest_by_eid(VSK_GRANT, delegates)
        # owner-authored grants win over delegate-authored ones
        apply_grants({eid: e for eid, e in extra.items() if eid not in owner_grants})

    log.info("roster: %d roles, %d granted members (owner %s…)",
             len(roster.roles), len(roster.grants), owner_hex[:8])
    return roster
