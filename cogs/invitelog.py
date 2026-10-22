"""Invite logging: who invited a member who has just joined.

Discord does not say. The only way to tell is to hold each guild's invite use
counts and compare them when a member joins: the invite whose count went up is
the one they used. Listing a guild's invites needs Manage Server, which is why
`invite_logging` ships off and declares that permission.

Nothing is persisted. The snapshot lives in memory and is rebuilt from Discord
whenever it is missing, so a restart costs at most one join reported as
"could not be determined". Welcoming the member stays with
`cogs/serverevents.py`; this cog only writes to a log channel.
"""

import asyncio
import logging
from dataclasses import dataclass

import discord
from discord.ext import commands

from cogs.utils import BoundedValueMap, guild_setting_sync, t
from core.feature_access import is_enabled, maintenance_blocks

invite_logger = logging.getLogger("PotatoBot.InviteLog")

# The vanity link has no invite code of its own in the list `guild.invites()`
# returns, so it is held under a key no real code can take.
VANITY = "\0vanity"


@dataclass(frozen=True)
class InviteState:
    uses: int
    max_uses: int = 0
    inviter: object = None


@dataclass(frozen=True)
class Attribution:
    """`kind` is `invite`, `vanity` or `unknown`; `code` is set for `invite`."""
    kind: str
    code: str | None = None


UNKNOWN = Attribution("unknown")


def attribute_join(before: dict[str, InviteState] | None,
                   after: dict[str, InviteState] | None) -> Attribution:
    """Which invite a join used, from the use counts either side of it.

    Exactly one count that went up is an answer. None that went up, and exactly
    one invite gone that was one use from its limit, is also an answer: Discord
    deletes an invite the moment its last use is spent. Anything else — two
    joins racing between snapshots, a join through Discovery or an OAuth add,
    no snapshot to compare with — is `unknown`, never a guess, because naming
    the wrong inviter is worse than naming none.
    """
    if before is None or after is None:
        return UNKNOWN
    increased = [code for code, state in after.items()
                 if state.uses > before.get(code, InviteState(0)).uses]
    if len(increased) == 1:
        code = increased[0]
        return Attribution("vanity") if code == VANITY else Attribution("invite", code)
    if increased:
        return UNKNOWN
    spent = [code for code, state in before.items()
             if code not in after and code != VANITY
             and state.max_uses and state.uses + 1 >= state.max_uses]
    if len(spent) == 1:
        return Attribution("invite", spent[0])
    return UNKNOWN


def invite_log_channel_id(guild_id: int) -> int | None:
    """The separate channel when the box is ticked and one is set, else the bot log."""
    if guild_setting_sync(guild_id, "invite_log_use_separate_channel"):
        separate = guild_setting_sync(guild_id, "invite_log_channel")
        if separate:
            return separate
    return guild_setting_sync(guild_id, "bot_log_channel")


def _name_and_id(user) -> str:
    return f"{user.mention} {discord.utils.escape_markdown(user.name)} ({user.id})"


class InviteLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.snapshots = BoundedValueMap(max_entries=1024)
        self.locks = BoundedValueMap(max_entries=1024)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.locks.get(guild_id)
        if lock is None:
            lock = self.locks[guild_id] = asyncio.Lock()
        return lock

    async def _fetch(self, guild) -> dict[str, InviteState] | None:
        """The guild's invites now, or None when Discord will not list them."""
        try:
            invites = await guild.invites()
            snapshot = {
                invite.code: InviteState(invite.uses or 0, invite.max_uses or 0,
                                         invite.inviter)
                for invite in invites
            }
            if "VANITY_URL" in guild.features:
                vanity = await guild.vanity_invite()
                if vanity is not None:
                    snapshot[VANITY] = InviteState(vanity.uses or 0)
        except discord.Forbidden:
            invite_logger.warning(
                "Invite log cannot list invites: missing Manage Server (guild_id=%s).",
                guild.id)
            return None
        except discord.HTTPException as exc:
            invite_logger.warning(
                "Invite list failed (guild_id=%s, status=%s).",
                guild.id, getattr(exc, "status", None))
            return None
        return snapshot

    async def _prime(self, guild):
        if not is_enabled(guild.id, "invite_logging"):
            return
        async with self._lock(guild.id):
            snapshot = await self._fetch(guild)
            if snapshot is not None:
                self.snapshots[guild.id] = snapshot

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                await self._prime(guild)
            except Exception:
                invite_logger.exception("Invite snapshot failed (guild_id=%s).", guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        try:
            await self._prime(guild)
        except Exception:
            invite_logger.exception("Invite snapshot failed (guild_id=%s).", guild.id)

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        # Local bookkeeping only, no request. A deletion is deliberately not
        # mirrored: when a join spends an invite's last use, Discord's delete
        # event can arrive before the join, and the invite must still be in the
        # snapshot then or that join cannot be attributed.
        guild = invite.guild
        if guild is None or not is_enabled(guild.id, "invite_logging"):
            return
        snapshot = self.snapshots.get(guild.id)
        if snapshot is not None:
            snapshot[invite.code] = InviteState(invite.uses or 0, invite.max_uses or 0,
                                                invite.inviter)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        # Broad on purpose: an exception escaping a listener is logged by
        # discord.py and nothing else, but this one must never be the reason a
        # join goes unrecorded somewhere it should not.
        try:
            await self._log_join(member)
        except Exception:
            invite_logger.exception("Invite log failed (guild_id=%s).", member.guild.id)

    async def _log_join(self, member):
        guild = member.guild
        # Bots arrive through OAuth, never through an invite.
        if member.bot or not is_enabled(guild.id, "invite_logging"):
            return
        if maintenance_blocks(guild, member):
            return
        channel_id = invite_log_channel_id(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            invite_logger.warning(
                "Invite log channel is unavailable (guild_id=%s, channel_id=%s).",
                guild.id, channel_id)
            return

        async with self._lock(guild.id):
            before = self.snapshots.get(guild.id)
            after = await self._fetch(guild)
            if after is not None:
                self.snapshots[guild.id] = after
        attribution = attribute_join(before, after)

        embed = discord.Embed(title=t("invitelog.title"), color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name=t("invitelog.member_field"), value=_name_and_id(member),
                        inline=False)
        embed.add_field(name=t("invitelog.account_created_field"),
                        value=f"<t:{int(member.created_at.timestamp())}:R>", inline=False)
        if attribution.kind == "invite":
            # An invite whose last use this join spent is gone from `after`,
            # so its count is the one before plus this join.
            if attribution.code in after:
                state = after[attribution.code]
                uses = state.uses
            else:
                state = before[attribution.code]
                uses = state.uses + 1
            inviter = state.inviter
            embed.add_field(name=t("invitelog.inviter_field"),
                            value=_name_and_id(inviter) if inviter else t("invitelog.no_inviter"),
                            inline=False)
            embed.add_field(name=t("invitelog.invite_field"),
                            value=t("invitelog.invite_value", code=attribution.code, uses=uses),
                            inline=False)
        elif attribution.kind == "vanity":
            embed.add_field(name=t("invitelog.inviter_field"), value=t("invitelog.vanity"),
                            inline=False)
        else:
            embed.add_field(name=t("invitelog.inviter_field"), value=t("invitelog.unknown"),
                            inline=False)

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as exc:
            invite_logger.error(
                "Invite log post failed (guild_id=%s, channel_id=%s, error=%s).",
                guild.id, channel.id, type(exc).__name__)


async def setup(bot):
    await bot.add_cog(InviteLog(bot))
