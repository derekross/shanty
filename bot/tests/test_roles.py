"""Roster building and staff gating for !music."""

import json
import os

from bot.control import Edition
from bot.cord import grant_locator
from bot.roles import MANAGE_MESSAGES, MANAGE_ROLES, build_roster

CID = bytes([5] * 32)
OWNER = "aa" * 32
ADMIN = "bb" * 32
MOD = "cc" * 32
RANDO = "dd" * 32
ROLE_ADMIN = "11" * 32
ROLE_MOD = "22" * 32
ROLE_COSMETIC = "33" * 32


def role_edition(author, role_id, perms, version=1):
    return Edition(author=author, vsk="1", eid=role_id, version=version,
                   content=json.dumps({"role_id": role_id, "name": "r",
                                       "position": 1, "permissions": str(perms),
                                       "scope": {"kind": "server"}, "color": 0}))


def grant_edition(author, member, role_ids, version=1):
    eid = grant_locator(CID, bytes.fromhex(member)).hex()
    return Edition(author=author, vsk="3", eid=eid, version=version,
                   content=json.dumps({"member": member, "role_ids": role_ids}))


class TestRoster:
    def test_owner_always_staff(self):
        roster = build_roster([], OWNER, CID)
        assert roster.is_staff(OWNER)
        assert not roster.is_staff(RANDO)

    def test_owner_granted_moderator(self):
        editions = [
            role_edition(OWNER, ROLE_MOD, MANAGE_MESSAGES),
            grant_edition(OWNER, MOD, [ROLE_MOD]),
        ]
        roster = build_roster(editions, OWNER, CID)
        assert roster.is_staff(MOD)
        assert not roster.is_staff(RANDO)

    def test_cosmetic_role_is_not_staff(self):
        editions = [
            role_edition(OWNER, ROLE_COSMETIC, 0),  # no management bits
            grant_edition(OWNER, RANDO, [ROLE_COSMETIC]),
        ]
        assert not build_roster(editions, OWNER, CID).is_staff(RANDO)

    def test_one_hop_delegation(self):
        editions = [
            role_edition(OWNER, ROLE_ADMIN, MANAGE_ROLES | MANAGE_MESSAGES),
            grant_edition(OWNER, ADMIN, [ROLE_ADMIN]),
            # the admin grants moderator to MOD
            role_edition(ADMIN, ROLE_MOD, MANAGE_MESSAGES),
            grant_edition(ADMIN, MOD, [ROLE_MOD]),
        ]
        roster = build_roster(editions, OWNER, CID)
        assert roster.is_staff(ADMIN)
        assert roster.is_staff(MOD)

    def test_forged_grant_by_rando_ignored(self):
        editions = [
            role_edition(OWNER, ROLE_ADMIN, MANAGE_ROLES),
            grant_edition(RANDO, RANDO, [ROLE_ADMIN]),  # self-grant forgery
        ]
        assert not build_roster(editions, OWNER, CID).is_staff(RANDO)

    def test_spoofed_grant_coordinate_ignored(self):
        # Grant content claims MOD but sits at RANDO's locator — must be dropped.
        eid = grant_locator(CID, bytes.fromhex(RANDO)).hex()
        bad = Edition(author=OWNER, vsk="3", eid=eid, version=1,
                      content=json.dumps({"member": MOD, "role_ids": [ROLE_ADMIN]}))
        editions = [role_edition(OWNER, ROLE_ADMIN, MANAGE_ROLES), bad]
        roster = build_roster(editions, OWNER, CID)
        assert not roster.is_staff(MOD)
