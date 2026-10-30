"""Member announcements: join, leave, kick, ban, boost and the boost reward.

What each message says is the guild's template, rendered by
`core/announcement_templates.py`. This cog decides *which* event happened and
where its message goes. Account provisioning and autoroles on join stay in
`cogs/serverevents.py`; this cog only posts.

Telling a leave from a kick from a ban is the part that is not obvious. Discord
sends a member removal for all three, and a ban also as its own event, in either
order. So the first of them opens a short settling window per member, and the
decision is made once, after it: a ban if a ban event arrived, a kick if the
audit log holds a kick of this member from the last minute, otherwise a leave.
One decision means one message, where a ban used to post both a farewell and a
ban notice.

Rules that bind changes here: docs/subsystems/announcements.md
"""

import asyncio
import io
import logging
import time

import discord
from discord.ext import commands

from cogs.utils import (BoundedCooldownMap, BoundedValueMap,
                        apply_database_result, currency_emoji,
                        guild_setting_sync, send_moderation_log, t)
from core import database
from core.announcement_templates import (EVENT_CHANNEL_SETTINGS, EVENT_FEATURES,
                                         TEMPLATE_FIELDS, Announcement,
                                         announcement_mentions,
                                         placeholder_values,
                                         render_announcement,
                                         take_moderator_action,
                                         template_setting_key)
from core.clock import utc_now
from core.feature_access import is_enabled

announcement_logger = logging.getLogger("PotatoBot.Announcements")

# Long enough for the removal, the ban and the audit entry to all arrive, short
# enough that the message still reads as the event.
REMOVAL_SETTLE_SECONDS = 2.0
# How old an audit entry may be and still describe this removal. A member
# kicked last week who rejoined and left today must read as a leave.
AUDIT_MATCH_SECONDS = 60
BOOST_DEDUP_SECONDS = 120

# The avatar is fetched into memory, attached, and dropped: nothing is written
# to this host. Bounded so a raid cannot open a download per joiner at once,
# and timed out so a slow CDN costs the attachment, never the message.
AVATAR_SIZE = 256
AVATAR_FETCH_SECONDS = 5
AVATAR_FILENAME = "avatar.png"
_avatar_downloads = asyncio.Semaphore(4)

BOOST_MESSAGE_TYPES = frozenset({
    discord.MessageType.premium_guild_subscription,
    discord.MessageType.premium_guild_tier_1,
    discord.MessageType.premium_guild_tier_2,
    discord.MessageType.premium_guild_tier_3,
})

_AUDIT_ACTIONS = {
    "kick": discord.AuditLogAction.kick,
    "ban": discord.AuditLogAction.ban,
}


def event_enabled(guild_id: int, event: str) -> bool:
    """The master, the event's own toggle, and the economy for the reward.

    Checked explicitly rather than trusting the stored child flag, because a
    child's stored value does not change when its master is switched off.
    """
    if not is_enabled(guild_id, "member_announcements"):
        return False
    if event == "boost_reward" and not is_enabled(guild_id, "economy"):
        return False
    return is_enabled(guild_id, EVENT_FEATURES[event])


def build_announcement(event: str, member, guild, *, footer: str | None = None,
                       **extra) -> Announcement:
    """Render one event for one member from the guild's stored template."""
    values = placeholder_values(member, guild, coin=currency_emoji(), **extra)
    stored = {field: guild_setting_sync(guild.id, template_setting_key(event, field))
              for field in TEMPLATE_FIELDS}
    announcement = render_announcement(
        event, stored, values,
        # The shipped text is trusted and goes through `t()`'s own `format`.
        shipped_title=t(f"announcements.{event}_title", **values),
        shipped_description=t(f"announcements.{event}_desc", **values),
        member=member, guild=guild,
    )
    if footer:
        announcement.embed.set_footer(text=footer)
    return announcement


def announcement_channel_id(guild_id: int, event: str):
    channel_id = guild_setting_sync(guild_id, EVENT_CHANNEL_SETTINGS[event])
    if not channel_id and event == "boost_reward":
        # Unset means "with the boost", which is where it was before it had
        # a channel of its own.
        channel_id = guild_setting_sync(guild_id, EVENT_CHANNEL_SETTINGS["boost"])
    return channel_id


async def avatar_file(avatar) -> discord.File | None:
    """The avatar as an attachment, or None to keep the plain link.

    An attached image belongs to the message, so it survives the member
    changing their avatar or deleting their account, which a link does not.
    Read into memory only; Discord stores the copy with the message.
    """
    if avatar is None:
        return None
    try:
        async with _avatar_downloads:
            data = await asyncio.wait_for(
                avatar.replace(size=AVATAR_SIZE, format="png").read(),
                AVATAR_FETCH_SECONDS)
    except (asyncio.TimeoutError, discord.DiscordException, ValueError) as exc:
        announcement_logger.warning(
            "Avatar fetch failed, linking instead (error=%s).", type(exc).__name__)
        return None
    return discord.File(io.BytesIO(data), filename=AVATAR_FILENAME)


async def post_announcement(guild, event: str, member,
                            announcement: Announcement) -> bool:
    """Send it where this event goes. Returns whether it was posted.

    Kick and ban with no channel of their own go to the moderation log, through
    the one function that reads that setting.
    """
    channel_id = announcement_channel_id(guild.id, event)
    if not channel_id and event not in ("kick", "ban"):
        announcement_logger.warning(
            "Announcement channel is unset (guild_id=%s, event=%s).", guild.id, event)
        return False
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if channel_id and channel is None:
        announcement_logger.warning(
            "Announcement channel is unavailable (guild_id=%s, event=%s, channel_id=%s).",
            guild.id, event, channel_id)
        return False
    # Fetched only once a destination is known, so an unset channel costs
    # no download.
    attachment = await avatar_file(announcement.avatar)
    extra = {}
    if attachment is not None:
        announcement.embed.set_thumbnail(url=f"attachment://{AVATAR_FILENAME}")
        extra["file"] = attachment
    mentions = announcement_mentions(member)
    if channel is None:
        return await send_moderation_log(
            guild, announcement.embed, f"a {event} announcement",
            content=announcement.content, allowed_mentions=mentions, **extra)
    try:
        await channel.send(content=announcement.content, embed=announcement.embed,
                           allowed_mentions=mentions, **extra)
    except (discord.Forbidden, discord.HTTPException) as exc:
        announcement_logger.error(
            "Announcement failed (guild_id=%s, event=%s, channel_id=%s, error=%s).",
            guild.id, event, channel.id, type(exc).__name__)
        return False
    return True


def sample_extras(event: str, guild, actor) -> dict:
    """What a test or preview fills the event-specific placeholders with."""
    if event in ("kick", "ban"):
        return {"moderator": actor, "reason": t("announcements.test_reason")}
    if event == "boost_reward":
        return {"amount": guild_setting_sync(guild.id, "boost_reward_amount") or 0}
    return {}


async def send_test_announcement(guild, actor, event: str) -> str | None:
    """The dashboard's "Send test": the saved template, with the operator as the member.

    Posted where the real one would go, marked as a test in the footer. Returns
    an outbox error code, or None on success.
    """
    announcement = build_announcement(
        event, actor, guild, footer=t("announcements.test_footer"),
        **sample_extras(event, guild, actor))
    if not await post_announcement(guild, event, actor, announcement):
        return "channel_unavailable"
    return None


class Announcements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # (guild_id, user_id) -> {"user": ..., "banned": bool}
        self.pending_removals = BoundedValueMap(max_entries=1024)
        # (guild_id, target_id, kind) -> (moderator_id, reason)
        self.audit_entries = BoundedValueMap(max_entries=1024)
        self.recent_boosts = BoundedCooldownMap(max_age=300, max_entries=4096)
        self._tasks = set()

    def cog_unload(self):
        for task in self._tasks:
            task.cancel()

    # --- join -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not event_enabled(member.guild.id, "join"):
            return
        try:
            await post_announcement(member.guild, "join", member,
                                    build_announcement("join", member, member.guild))
        except Exception:
            # A broken template must never be why the next join goes unseen.
            announcement_logger.exception(
                "Join announcement crashed (guild_id=%s).", member.guild.id)

    # --- leave, kick, ban ----------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        self._removal_seen(member.guild, member, banned=False)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        self._removal_seen(guild, user, banned=True)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        # Arrives only with `view_audit_log`; `_audit_entry` falls back to
        # reading the log when this event was missed or came late.
        for kind, action in _AUDIT_ACTIONS.items():
            if entry.action is action:
                target_id = getattr(entry.target, "id", None)
                if target_id is not None:
                    self.audit_entries[(entry.guild.id, target_id, kind)] = (
                        entry.user_id, entry.reason)

    def _removal_seen(self, guild, user, *, banned: bool):
        if not is_enabled(guild.id, "member_announcements"):
            return
        key = (guild.id, user.id)
        state = self.pending_removals.get(key)
        if state is not None:
            state["banned"] = state["banned"] or banned
            if isinstance(user, discord.Member):
                # The removal's member object knows when they joined; the ban
                # event's user does not.
                state["user"] = user
            return
        self.pending_removals[key] = {"user": user, "banned": banned}
        task = asyncio.create_task(self._settle_removal(guild, key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _settle_removal(self, guild, key):
        await asyncio.sleep(REMOVAL_SETTLE_SECONDS)
        state = self.pending_removals.pop(key, None)
        if state is None:
            return
        try:
            await self._announce_removal(guild, state["user"], state["banned"])
        except Exception:
            announcement_logger.exception(
                "Removal announcement crashed (guild_id=%s).", guild.id)

    async def _announce_removal(self, guild, user, banned: bool):
        if banned:
            event, entry = "ban", await self._audit_entry(guild, user.id, "ban")
        else:
            entry = await self._audit_entry(guild, user.id, "kick")
            event = "kick" if entry else "leave"
        if not event_enabled(guild.id, event):
            return
        extras = {}
        if event in ("kick", "ban"):
            moderator_id, reason = entry or (None, None)
            bot_id = getattr(self.bot.user, "id", None)
            if moderator_id is None or moderator_id == bot_id:
                # Our own `/kick` or `/ban`: Discord credits the bot, the
                # command recorded who asked.
                moderator_id = take_moderator_action(
                    guild.id, user.id, event) or moderator_id
            moderator = guild.get_member(moderator_id) if moderator_id else None
            extras = {
                "moderator": moderator or t("announcements.unknown_moderator"),
                "reason": reason or t("announcements.no_reason"),
            }
        await post_announcement(guild, event, user,
                                build_announcement(event, user, guild, **extras))

    async def _audit_entry(self, guild, target_id: int, kind: str):
        """(moderator_id, reason) of a recent kick or ban of this member, or None."""
        cached = self.audit_entries.pop((guild.id, target_id, kind), None)
        if cached is not None:
            return cached
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(action=_AUDIT_ACTIONS[kind], limit=5):
                if getattr(entry.target, "id", None) != target_id:
                    continue
                if (utc_now() - entry.created_at).total_seconds() > AUDIT_MATCH_SECONDS:
                    continue
                return entry.user_id, entry.reason
        except (discord.Forbidden, discord.HTTPException) as exc:
            announcement_logger.warning(
                "Audit log read failed (guild_id=%s, error=%s).",
                guild.id, type(exc).__name__)
        return None

    # --- boosts ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message):
        # The hottest listener in the bot: one set membership test first.
        if message.type not in BOOST_MESSAGE_TYPES or message.guild is None:
            return
        if isinstance(message.author, discord.Member):
            await self._boosted(message.author)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # Fires for a member's first boost only. A repeat boost changes nothing
        # on the member and arrives only as the system message above.
        if before.premium_since is None and after.premium_since is not None:
            await self._boosted(after)

    async def _boosted(self, member):
        guild = member.guild
        key = (guild.id, member.id)
        now = time.monotonic()
        if now - self.recent_boosts.get(key, -BOOST_DEDUP_SECONDS) < BOOST_DEDUP_SECONDS:
            return
        self.recent_boosts[key] = now
        try:
            if event_enabled(guild.id, "boost"):
                await post_announcement(guild, "boost", member,
                                        build_announcement("boost", member, guild))
            await self._pay_boost_reward(member)
        except Exception:
            announcement_logger.exception(
                "Boost handling crashed (guild_id=%s).", guild.id)

    async def _pay_boost_reward(self, member):
        """Economy's half: paid whether or not anybody announces it."""
        guild = member.guild
        if not is_enabled(guild.id, "economy"):
            return
        amount = guild_setting_sync(guild.id, "boost_reward_amount") or 0
        if amount <= 0:
            return
        result = await database.run_write(
            database.claim_periodic_reward,
            guild.id, member.id, "server_boost", amount, 30,
        )
        if result is None:
            # Already claimed within 30 days.
            return
        await apply_database_result(member, result)
        if event_enabled(guild.id, "boost_reward"):
            await post_announcement(
                guild, "boost_reward", member,
                build_announcement("boost_reward", member, guild, amount=amount))


async def setup(bot):
    await bot.add_cog(Announcements(bot))
