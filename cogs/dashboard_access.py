"""Tell the dashboard the moment its idea of who may manage a guild goes stale.

The dashboard decides access from Discord's own answer to `/users/@me/guilds`
and caches it for `PERMISSION_CACHE_SECONDS`. This cog makes that cache expire
early: on each Discord event that can change who holds Administrator or Manage
Server, it writes a row to `dashboard_access_invalidations`. The dashboard,
which may be another process, reads the row and asks Discord again on that
session's next request. The row decides nothing itself, and the 30-second
refresh stays as the backstop for events the bot missed while it was offline.

**Not feature-gated, and not maintenance-gated**, by design: a flag decides
whether something may be acquired, never whether an authority may end. The
listeners make no Discord call and write one small row.
"""

import logging

from discord.ext import commands

from core import database

access_logger = logging.getLogger("PotatoBot.DashboardAccess")


def grants_dashboard_access(permissions) -> bool:
    """The bar the dashboard's login holds a member to, besides ownership."""
    return bool(permissions.administrator or permissions.manage_guild)


class DashboardAccess(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _record(self, rows):
        try:
            await database.run_write(database.record_access_invalidations, rows)
        except Exception:
            # Broad on purpose: the backstop refresh still runs, so a failed
            # write delays a revocation by seconds rather than losing it.
            access_logger.exception(
                "Dashboard access invalidation failed (rows=%s)", len(rows))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # `guild_permissions` is all permissions for the owner, so this also
        # covers ownership; a role or nickname change that moves neither bit
        # writes nothing.
        if (grants_dashboard_access(before.guild_permissions)
                != grants_dashboard_access(after.guild_permissions)):
            await self._record([(after.guild.id, after.id)])

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        # Fires for a leave, a kick and a ban alike.
        if grants_dashboard_access(member.guild_permissions):
            await self._record([(member.guild.id, member.id)])

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        # Every holder of the role is affected, so the row names the guild.
        if (grants_dashboard_access(before.permissions)
                != grants_dashboard_access(after.permissions)):
            await self._record([(after.guild.id, None)])

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        if grants_dashboard_access(role.permissions):
            await self._record([(role.guild.id, None)])

    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        if before.owner_id != after.owner_id:
            await self._record([(after.id, user_id)
                                for user_id in (before.owner_id, after.owner_id)
                                if user_id])


async def setup(bot):
    await bot.add_cog(DashboardAccess(bot))
