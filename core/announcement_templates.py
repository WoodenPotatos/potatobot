"""Member announcements: what each event says, and how a guild's template renders.

The join, leave, kick, ban, boost and boost-reward messages are each a set of
typed settings a guild may fill in. Every field left empty falls back to the
shipped locale text, so a guild that never opens the editor keeps exactly the
messages it had. This module holds the declaration both the registry and the
renderer read, and the rendering itself; `cogs/announcements.py` decides when an
event happened and where it is posted.

It imports nothing from `cogs` and nothing from `settings_registry`, because the
registry imports *this* to generate its settings, and the dashboard's test send
renders through it from the outbox worker.

Rules that bind changes here: docs/subsystems/announcements.md
"""

import random
import re
import time
from dataclasses import dataclass

import discord

from core.bounded import BoundedValueMap

ANNOUNCEMENT_EVENTS = ("join", "leave", "kick", "ban", "boost", "boost_reward")

# The sub-toggle that switches each event on. Each depends on the
# `member_announcements` master; the boost reward depends on `economy` too.
EVENT_FEATURES = {event: f"announce_{event}" for event in ANNOUNCEMENT_EVENTS}

# Literal keys, not derived, so the settings are findable by name.
EVENT_CHANNEL_SETTINGS = {
    "join": "join_channel",
    "leave": "leave_channel",
    "kick": "kick_channel",
    "ban": "ban_channel",
    "boost": "booster_channel",
    "boost_reward": "boost_reward_channel",
}

TEMPLATE_FIELDS = ("title", "texts", "content", "color", "image_url", "thumbnail")
THUMBNAIL_CHOICES = ("avatar", "server", "none")
# Each is its own box in the editor; more than this stops being a choice and
# becomes a page nobody scrolls (decided 2026-10-30).
MAX_TEXT_VARIANTS = 5

# Shipped colours, used while a guild has not chosen one.
DEFAULT_COLORS = {
    "join": 0x2ECC71,
    "leave": 0xE74C3C,
    "kick": 0xE67E22,
    "ban": 0x992D22,
    "boost": 0xFF73FA,
    "boost_reward": 0xFF73FA,
}

# Which placeholders each event can fill. The dashboard lists exactly these
# beside the editor, and a placeholder an event cannot fill is left as typed,
# so an operator sees the mistake instead of a silent blank.
# `user` is a mention, which Discord draws as a name only while the viewer's
# client knows the member, so after they leave it reads as a bare id. The
# other three are written in as text and survive that.
COMMON_PLACEHOLDERS = ("user", "user_name", "display_name", "user_id", "server",
                       "member_count", "account_age")
EVENT_PLACEHOLDERS = {
    "join": COMMON_PLACEHOLDERS,
    "leave": COMMON_PLACEHOLDERS + ("joined_ago",),
    "kick": COMMON_PLACEHOLDERS + ("joined_ago", "moderator", "reason"),
    "ban": COMMON_PLACEHOLDERS + ("moderator", "reason"),
    "boost": COMMON_PLACEHOLDERS + ("boost_count", "boost_tier"),
    "boost_reward": COMMON_PLACEHOLDERS + ("amount", "coin"),
}

# Discord's own embed limits. Checked again after substitution, because a
# placeholder can expand a short template past them.
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
CONTENT_LIMIT = 2000

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def template_setting_key(event: str, field: str) -> str:
    return f"{event}_message_{field}"


def substitute(template: str, values: dict[str, str]) -> str:
    """Fill `{name}` placeholders in one pass, from a whitelist of strings.

    Never `str.format`: an operator's text reaching `format` could walk object
    attributes (`{user.__class__}`), and a member named `{reason}` substituted
    first and re-expanded by a later replace would put the reason somewhere it
    was not written. One regex pass reads only the original text.

    `{newline}` is still a line break, though the editor no longer offers it:
    a variant was one line of a list box until each got its own multi-line
    box, and a variant saved then must keep rendering as written.
    """
    def fill(match):
        name = match.group(1)
        if name == "newline":
            return "\n"
        return values.get(name, match.group(0))
    return _PLACEHOLDER.sub(fill, template)


def _clean(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def _relative(moment) -> str:
    return f"<t:{int(moment.timestamp())}:R>" if moment else "?"


def placeholder_values(member, guild, *, moderator=None, reason=None,
                       amount=None, coin="") -> dict[str, str]:
    """Every value a template may name, as display-ready text.

    A name is the one thing in here somebody else chose, so it is escaped for
    markdown and for mentions; the mention itself is built by Discord from the id.
    """
    values = {
        "user": member.mention,
        "user_name": _clean(member.name),
        "display_name": _clean(getattr(member, "display_name", member.name)),
        "user_id": str(member.id),
        "server": _clean(guild.name),
        "member_count": str(guild.member_count or 0),
        "account_age": _relative(getattr(member, "created_at", None)),
        "joined_ago": _relative(getattr(member, "joined_at", None)),
        "boost_count": str(getattr(guild, "premium_subscription_count", 0) or 0),
        "boost_tier": str(getattr(guild, "premium_tier", 0) or 0),
        "coin": coin,
    }
    if moderator is not None:
        values["moderator"] = (moderator.mention if hasattr(moderator, "mention")
                               else _clean(str(moderator)))
    if reason is not None:
        values["reason"] = _clean(reason)[:1024]
    if amount is not None:
        values["amount"] = f"{amount:,}".replace(",", " ")
    return values


@dataclass(frozen=True)
class Announcement:
    content: str | None
    embed: discord.Embed
    # The member's avatar, when the thumbnail shows it. The poster attaches the
    # image to the message rather than linking it, because a linked avatar
    # disappears once the member changes it or deletes their account.
    avatar: object = None


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def render_announcement(event: str, stored: dict, values: dict[str, str], *,
                        shipped_title: str, shipped_description: str,
                        member=None, guild=None,
                        choose=random.choice) -> Announcement:
    """Turn one event's stored fields into the message Discord is sent.

    `stored` maps each of `TEMPLATE_FIELDS` to the guild's value. Absent, empty
    and set are three different states and only the third overrides: an empty
    title is not a request for a blank title, it is "use the shipped one".
    """
    title = _text(stored.get("title"))
    title = substitute(title, values) if title else shipped_title

    variants = [line for line in (stored.get("texts") or [])
                if isinstance(line, str) and line.strip()]
    description = (substitute(choose(variants), values) if variants
                   else shipped_description)

    color = _text(stored.get("color"))
    colour = (int(color[1:], 16) if re.fullmatch(r"#[0-9a-fA-F]{6}", color)
              else DEFAULT_COLORS[event])

    embed = discord.Embed(title=title[:TITLE_LIMIT],
                          description=description[:DESCRIPTION_LIMIT],
                          color=colour)
    image_url = _text(stored.get("image_url"))
    if image_url.startswith("https://"):
        embed.set_image(url=image_url)

    thumbnail = stored.get("thumbnail") or "avatar"
    avatar = None
    if thumbnail == "avatar" and member is not None:
        avatar = member.display_avatar
        # The link stays as the fallback if the attachment cannot be made.
        embed.set_thumbnail(url=avatar.url)
    elif thumbnail == "server" and guild is not None and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    raw_content = _text(stored.get("content"))
    content = (substitute(raw_content, values)[:CONTENT_LIMIT] if raw_content
               else None)
    return Announcement(content=content, embed=embed, avatar=avatar)


def announcement_mentions(member) -> discord.AllowedMentions:
    """Only the member the message is about may be pinged.

    An operator's template can contain `@everyone` or a role mention; Discord
    renders it and pings nobody. This is what makes free text safe to post.
    """
    return discord.AllowedMentions(everyone=False, roles=False,
                                   users=[member], replied_user=False)


# --- moderator hand-off -----------------------------------------------------
#
# When `/kick` or `/ban` acts, Discord records the *bot* as the moderator, so
# the announcement would credit the bot. The command records who really asked
# just before it calls Discord, and the announcement consumes it. Module-level
# so it survives either cog being reloaded between the two.

MODERATOR_HANDOFF_SECONDS = 60
_moderator_handoffs: BoundedValueMap = BoundedValueMap(max_entries=1024)


def record_moderator_action(guild_id: int, target_id: int, kind: str,
                            moderator_id: int) -> None:
    _moderator_handoffs[(int(guild_id), int(target_id), kind)] = (
        int(moderator_id), time.monotonic())


def take_moderator_action(guild_id: int, target_id: int, kind: str) -> int | None:
    entry = _moderator_handoffs.pop((int(guild_id), int(target_id), kind), None)
    if entry is None:
        return None
    moderator_id, recorded = entry
    if time.monotonic() - recorded > MODERATOR_HANDOFF_SECONDS:
        return None
    return moderator_id

