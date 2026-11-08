"""Server event logging: messages, members, voice and server changes.

Decided 2026-11-06: one optional channel per family, ignore lists for channels
and roles, and bots' messages never logged. Everything is posted to Discord and
nothing is stored. A message's content is only known while discord.py holds the
message in its cache, so an edit or a deletion of an older message is logged
without its text rather than not at all.

The whole feature is off until a guild enables `event_logging`, because it
copies message content into another channel (Privacy Policy section 5).
Maintenance does not stop it: maintenance refuses what members ask of the bot,
and a log is an observation, which is most useful exactly then.
"""

import logging
import os
import sys

import discord
from discord.ext import commands

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from cogs.utils import guild_setting_sync, t
from core.feature_access import is_enabled

eventlog_logger = logging.getLogger("PotatoBot.EventLog")

# Literal keys, one per family, so every setting is read by name.
FAMILY_CHANNELS = {
    "messages": "event_log_messages_channel",
    "members": "event_log_members_channel",
    "voice": "event_log_voice_channel",
    "server": "event_log_server_channel",
}
# Discord's embed field limit; message text is cut to fit.
FIELD_LIMIT = 1024
COLOURS = {"messages": 0x5865F2, "members": 0x57F287, "voice": 0xFEE75C,
           "server": 0xEB459E}


def clip(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        return t("eventlog.no_text")
    return text if len(text) <= FIELD_LIMIT else text[:FIELD_LIMIT - 1] + "…"


def ignored(guild_id: int, *, channel=None, member=None) -> bool:
    """Whether an ignore list covers this channel (or its parent thread
    channel / category) or one of this member's roles."""
    channels = set(guild_setting_sync(guild_id, "event_log_ignored_channels") or [])
    if channel is not None:
        candidates = {getattr(channel, "id", None), getattr(channel, "parent_id", None),
                      getattr(channel, "category_id", None)}
        if channels & {c for c in candidates if c}:
            return True
    roles = set(guild_setting_sync(guild_id, "event_log_ignored_roles") or [])
    if member is not None and roles & {role.id for role in getattr(member, "roles", ())}:
        return True
    return False


class EventLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def target(self, guild, family: str):
        """The channel a family posts to, or None when it is not logged."""
        if guild is None or not is_enabled(guild.id, "event_logging"):
            return None
        channel_id = guild_setting_sync(guild.id, FAMILY_CHANNELS[family])
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def post(self, guild, family: str, embed: discord.Embed):
        channel = self.target(guild, family)
        if channel is None:
            return
        embed.colour = discord.Colour(COLOURS[family])
        embed.timestamp = discord.utils.utcnow()
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            eventlog_logger.warning(
                "Event log post failed (guild_id=%s, family=%s, error=%s)",
                guild.id, family, type(error).__name__)

    # ------------------------------------------------------------ messages

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        guild = after.guild
        if (guild is None or after.author.bot or before.content == after.content
                or self.target(guild, "messages") is None
                or ignored(guild.id, channel=after.channel, member=after.author)):
            return
        embed = discord.Embed(
            title=t("eventlog.message_edited"),
            description=t("eventlog.message_where", user=after.author.mention,
                          channel=after.channel.mention, link=after.jump_url))
        embed.add_field(name=t("eventlog.before"), value=clip(before.content), inline=False)
        embed.add_field(name=t("eventlog.after"), value=clip(after.content), inline=False)
        await self.post(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        # Only an uncached message: a cached one reaches on_message_edit with
        # its old text. Discord sends no old text for this one.
        if payload.cached_message is not None or payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        author = (payload.data or {}).get("author") or {}
        if (guild is None or author.get("bot") or "content" not in (payload.data or {})
                or self.target(guild, "messages") is None):
            return
        channel = guild.get_channel_or_thread(payload.channel_id)
        member = guild.get_member(int(author["id"])) if author.get("id") else None
        if ignored(guild.id, channel=channel, member=member):
            return
        embed = discord.Embed(
            title=t("eventlog.message_edited"),
            description=t("eventlog.message_uncached",
                          user=f"<@{author.get('id')}>", channel=f"<#{payload.channel_id}>"))
        embed.add_field(name=t("eventlog.after"), value=clip(payload.data.get("content")),
                        inline=False)
        await self.post(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or self.target(guild, "messages") is None:
            return
        message = payload.cached_message
        channel = guild.get_channel_or_thread(payload.channel_id)
        if message is not None:
            if message.author.bot or ignored(guild.id, channel=channel, member=message.author):
                return
            embed = discord.Embed(
                title=t("eventlog.message_deleted"),
                description=t("eventlog.message_where_deleted",
                              user=message.author.mention, channel=f"<#{payload.channel_id}>"))
            embed.add_field(name=t("eventlog.content"), value=clip(message.content), inline=False)
            if message.attachments:
                embed.add_field(name=t("eventlog.attachments"),
                                value=str(len(message.attachments)), inline=True)
        else:
            if ignored(guild.id, channel=channel):
                return
            embed = discord.Embed(
                title=t("eventlog.message_deleted"),
                description=t("eventlog.message_deleted_uncached",
                              channel=f"<#{payload.channel_id}>"))
        await self.post(guild, "messages", embed)

    # ------------------------------------------------------------- members

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        guild = after.guild
        if after.bot or self.target(guild, "members") is None or ignored(guild.id, member=after):
            return
        lines = []
        if before.nick != after.nick:
            lines.append(t("eventlog.nickname", before=discord.utils.escape_markdown(before.nick or "—"),
                           after=discord.utils.escape_markdown(after.nick or "—")))
        added = [role for role in after.roles if role not in before.roles]
        removed = [role for role in before.roles if role not in after.roles]
        if added:
            lines.append(t("eventlog.roles_added", roles=", ".join(role.mention for role in added)))
        if removed:
            lines.append(t("eventlog.roles_removed", roles=", ".join(role.mention for role in removed)))
        if before.timed_out_until != after.timed_out_until:
            lines.append(t("eventlog.timeout_until",
                           until=discord.utils.format_dt(after.timed_out_until, "f"))
                         if after.timed_out_until else t("eventlog.timeout_ended"))
        if not lines:
            return
        embed = discord.Embed(title=t("eventlog.member_updated"),
                              description=after.mention + "\n" + "\n".join(lines))
        await self.post(guild, "members", embed)

    # --------------------------------------------------------------- voice

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        guild = member.guild
        if self.target(guild, "voice") is None or ignored(guild.id, member=member):
            return
        channels = [channel for channel in (before.channel, after.channel) if channel]
        if any(ignored(guild.id, channel=channel) for channel in channels):
            return
        if before.channel is None:
            text = t("eventlog.voice_joined", user=member.mention, channel=after.channel.mention)
        elif after.channel is None:
            text = t("eventlog.voice_left", user=member.mention, channel=before.channel.mention)
        else:
            text = t("eventlog.voice_moved", user=member.mention,
                     before=before.channel.mention, after=after.channel.mention)
        await self.post(guild, "voice", discord.Embed(description=text))

    # -------------------------------------------------------------- server

    async def server_event(self, guild, key: str, **values):
        if self.target(guild, "server") is None:
            return
        await self.post(guild, "server", discord.Embed(description=t(f"eventlog.{key}", **values)))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await self.server_event(channel.guild, "channel_created", channel=channel.mention)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        await self.server_event(channel.guild, "channel_deleted",
                                name=discord.utils.escape_markdown(channel.name))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if before.name != after.name:
            await self.server_event(after.guild, "channel_renamed", channel=after.mention,
                                    before=discord.utils.escape_markdown(before.name))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await self.server_event(role.guild, "role_created", role=role.mention)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self.server_event(role.guild, "role_deleted",
                                name=discord.utils.escape_markdown(role.name))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        if before.name != after.name:
            await self.server_event(after.guild, "role_renamed", role=after.mention,
                                    before=discord.utils.escape_markdown(before.name))
        if before.permissions != after.permissions:
            await self.server_event(after.guild, "role_permissions", role=after.mention)

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        """Who made the invite and how long it lasts, as the maintainer asked
        (2026-11-06): the invite log only learns of an invite when someone
        joins through it, so this is where a new one is first seen."""
        if invite.guild is None:
            return
        await self.server_event(
            self.bot.get_guild(invite.guild.id) or invite.guild, "invite_created",
            code=invite.code,
            channel=f"<#{invite.channel.id}>" if invite.channel else "—",
            inviter=invite.inviter.mention if invite.inviter else "—",
            uses=str(invite.max_uses) if invite.max_uses else t("eventlog.invite_unlimited"),
            expires=(discord.utils.format_dt(invite.expires_at, "R") if invite.expires_at
                     else t("eventlog.invite_never")))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        if invite.guild is not None:
            await self.server_event(self.bot.get_guild(invite.guild.id) or invite.guild,
                                    "invite_deleted", code=invite.code)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after):
        added = [emoji for emoji in after if emoji not in before]
        removed = [emoji for emoji in before if emoji not in after]
        for emoji in added:
            await self.server_event(guild, "emoji_added", emoji=str(emoji), name=emoji.name)
        for emoji in removed:
            await self.server_event(guild, "emoji_removed", name=emoji.name)


async def setup(bot):
    await bot.add_cog(EventLog(bot))
