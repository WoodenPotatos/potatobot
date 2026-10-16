"""`/manage`: a faction leader may hand out only roles the bot would self-assign.

A faction's `manageable_ids` may name any role, and the command granted
whatever the leader picked from that list — so a leader could hand a role
carrying administrator, manage_roles or ban to any member below them. The
leader's path is now screened by `can_self_assign_role`, like every other role
the bot hands out on somebody else's say-so. Administrators keep their bypass:
Discord already lets them grant any role directly, so screening them would take
away a convenience and protect nothing.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class Role:
    """Enough of `discord.Role` for `/manage` and `can_self_assign_role`."""

    def __init__(self, role_id, position, **permissions):
        self.id = role_id
        self.name = f"role-{role_id}"
        self.position = position
        self.managed = False
        self.permissions = SimpleNamespace(**permissions)

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.position >= other.position


LEADER_ROLE = 77
PLAIN = Role(301, position=10)
PRIVILEGED = Role(302, position=10, administrator=True)


class FactionManageScreeningTests(unittest.IsolatedAsyncioTestCase):

    async def run_manage(self, target, administrator=False):
        import cogs.moderation as moderation
        import cogs.utils as utils

        roles = {role.id: role for role in (PLAIN, PRIVILEGED)}
        guild = SimpleNamespace(
            id=123, owner_id=1, get_role=roles.get,
            me=SimpleNamespace(top_role=Role(1, position=100)))
        user = SimpleNamespace(
            id=5, roles=[Role(LEADER_ROLE, position=50)],
            top_role=Role(LEADER_ROLE, position=50),
            guild_permissions=SimpleNamespace(administrator=administrator))
        member = SimpleNamespace(
            id=9, roles=[], top_role=Role(2, position=5), display_name="member",
            add_roles=AsyncMock(), remove_roles=AsyncMock())
        interaction = SimpleNamespace(
            guild=guild, user=user,
            response=SimpleNamespace(send_message=AsyncMock()))
        factions = {"red": {"leader_role_id": LEADER_ROLE,
                            "manageable_ids": [PLAIN.id, PRIVILEGED.id]}}

        real_setting = utils.guild_setting_sync

        def setting(guild_id, key):
            # No staff roles configured; everything else (the language `t()`
            # reads) resolves as it would anywhere else.
            return [] if key == "admin_roles" else real_setting(guild_id, key)

        cog = moderation.Moderation.__new__(moderation.Moderation)
        with patch.object(moderation, "guild_setting_sync",
                          lambda guild_id, key: factions), \
                patch.object(utils, "guild_setting_sync", setting):
            await moderation.Moderation.manage.callback(
                cog, interaction, member, str(target.id))
        return member, interaction

    async def test_a_leader_cannot_grant_a_privileged_role(self):
        member, interaction = await self.run_manage(PRIVILEGED)
        member.add_roles.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()

    async def test_a_leader_can_still_grant_an_ordinary_faction_role(self):
        member, _ = await self.run_manage(PLAIN)
        member.add_roles.assert_awaited_once_with(PLAIN)

    async def test_an_administrator_keeps_the_bypass(self):
        member, _ = await self.run_manage(PRIVILEGED, administrator=True)
        member.add_roles.assert_awaited_once_with(PRIVILEGED)


if __name__ == "__main__":
    unittest.main()
