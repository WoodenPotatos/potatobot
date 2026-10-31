"""Invite logging: who invited a member who has just joined.

Discord does not say. The only way to tell is to hold each guild's invite use
counts and compare them when a member joins: the invite whose count went up is
the one they used. Listing a guild's invites needs Manage Server, which is why
`invite_logging` ships off and declares that permission.

A spent invite is credited only when Discord's delete for it arrives within
`DELETE_WINDOW_SECONDS` of the join, either side. Without that, an invite a
moderator deleted with one use left at any time since the last snapshot was
credited with the next join that no invite explains — a Discovery or OAuth
join — naming the wrong inviter.

Nothing is persisted. The snapshot lives in memory and is rebuilt from Discord
whenever it is missing: at `on_ready` if the feature cache is loaded by then,
and otherwise by `prime_missing` within a minute. Without that loop the first
join after every restart was "could not be determined", because this cog's
`on_ready` runs alongside `main.py`'s, before the flags it reads have loaded,
and an unloaded flag reads as off (found live 2026-10-22). Welcoming the
member stays with
`cogs/serverevents.py`; this cog only writes to a log channel.
"""

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

import discord
from discord.ext import commands, tasks

from cogs.utils import BoundedValueMap, guild_setting_sync, handle_loop_error, t
from core.feature_access import is_enabled, maintenance_blocks

invite_logger = logging.getLogger("PotatoBot.InviteLog")

# The vanity link has no invite code of its own in the list `guild.invites()`
# returns, so it is held under a key no real code can take.
VANITY = "\0vanity"

# How far apart an invite's delete and a join may be for the join to have spent
# that invite. Discord sends the two events close together but in either order.
DELETE_WINDOW_SECONDS = 2.0


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


def spent_codes(before: dict[str, InviteState],
                after: dict[str, InviteState]) -> list[str]:
    """Invites gone since `before` that were one use from their limit."""
    return [code for code, state in before.items()
            if code not in after and code != VANITY
            and state.max_uses and state.uses + 1 >= state.max_uses]


def attribute_join(before: dict[str, InviteState] | None,
                   after: dict[str, InviteState] | None,
                   *, deleted: frozenset[str] | set[str]) -> Attribution:
    """Which invite a join used, from the use counts either side of it.

    Exactly one count that went up is an answer. None that went up, and exactly
    one invite gone that was one use from its limit *and deleted around the
    join* (`deleted`), is also an answer: Discord deletes an invite the moment
    its last use is spent. One gone without such a delete was removed by
    someone, not spent. Anything else — two
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
    spent = spent_codes(before, after)
    if len(spent) == 1 and spent[0] in deleted:
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
        # (guild_id, code) -> monotonic time of the invite's delete event.
        self.deletions = BoundedValueMap(max_entries=1024)

    async def cog_load(self):
        # Started here rather than in `__init__`, which has no running loop
        # when a test builds the cog directly.
        self.prime_missing.start()

    async def cog_unload(self):
        self.prime_missing.cancel()

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

    @tasks.loop(seconds=60)
    async def prime_missing(self):
        # Only a guild with the feature on and no snapshot costs a request, so
        # once every guild is primed this sends nothing.
        for guild in self.bot.guilds:
            if guild.id in self.snapshots:
                continue
            try:
                await self._prime(guild)
            except Exception:
                invite_logger.exception("Invite snapshot failed (guild_id=%s).", guild.id)

    @prime_missing.before_loop
    async def before_prime_missing(self):
        await self.bot.wait_until_ready()

    @prime_missing.error
    async def prime_missing_error(self, error):
        await handle_loop_error(
            self.bot, self.prime_missing, "invite_prime_missing", error, invite_logger,
        )

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
    async def on_invite_delete(self, invite):
        # Recorded, never applied to the snapshot (see `on_invite_create`): the
        # time is what tells a spent invite from one a moderator removed.
        guild = invite.guild
        if guild is None or not is_enabled(guild.id, "invite_logging"):
            return
        self.deletions[(guild.id, invite.code)] = monotonic()

    def _deleted_near(self, guild_id: int, codes, joined_at: float) -> set[str]:
        return {code for code in codes
                if (deleted_at := self.deletions.get((guild_id, code))) is not None
                and abs(deleted_at - joined_at) <= DELETE_WINDOW_SECONDS}

    async def _wait(self, seconds: float):
        await asyncio.sleep(seconds)

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
        joined_at = monotonic()
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
        spent = spent_codes(before, after) if before and after else []
        deleted = self._deleted_near(guild.id, spent, joined_at)
        if len(spent) == 1 and not deleted:
            # The delete for an invite this join spent may still be on its way.
            # Only this rare path waits, and only out the rest of the window.
            remaining = DELETE_WINDOW_SECONDS - (monotonic() - joined_at)
            if remaining > 0:
                await self._wait(remaining)
            deleted = self._deleted_near(guild.id, spent, joined_at)
        attribution = attribute_join(before, after, deleted=deleted)

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
