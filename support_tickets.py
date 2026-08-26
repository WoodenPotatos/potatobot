"""Opening a support-style ticket channel, in one place.

There were two copies of this: `cogs/tickets.py` opened one from its launcher
button and `cogs/shop.py` opened one for a rented asset, each with its own
overwrite map assembled by hand. They had already drifted — the shop's omitted
`read_message_history` for the opener and recorded no `ticket_type` — and
neither could be called from anywhere else, because both needed an
`interaction`.

It is a root module rather than a cog for the reason `managed_messages.py` is
one: a cog cannot import another cog without dragging that cog's views and
command tree along with it, and three callers now need this.

The caller sends its own opening message. Building the channel is shared; what
goes in it, and which view it carries, is knowledge about the feature that
opened it.
"""

import logging

import discord

import database
from cogs.utils import guild_setting_sync, t

logger = logging.getLogger("PotatoBot.Tickets")


def ticket_overwrites(guild, member) -> dict:
    """Who can see a ticket: its opener, the bot, and every staff role.

    `@everyone` is denied explicitly rather than relying on the category, so a
    ticket opened outside one — because the category is unset or was deleted —
    is still private. That is the case worth being deliberate about: the failure
    is silent and the channel contains a member's private request.
    """
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True),
    }
    for role_id in guild_setting_sync(guild.id, "admin_roles"):
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True)
    return overwrites


async def open_ticket(guild, member, name: str, ticket_type: str):
    """Create a ticket channel and record who owns it, or return None.

    Returns None rather than raising when Discord refuses: every caller has
    already taken the member's money or spent their voucher by this point, and
    an exception there would leave them paid-for-nothing with a traceback. The
    caller decides what to do instead — refund, or fall back to telling them a
    request id.

    The row is written *after* the channel exists, and the channel is removed
    again if the row cannot be written, so a ticket without an owner cannot
    outlive the attempt: ownership is what the close and claim controls read.
    """
    category = guild.get_channel(guild_setting_sync(guild.id, "ticket_category"))
    channel = None
    try:
        channel = await guild.create_text_channel(
            name=name, category=category,
            overwrites=ticket_overwrites(guild, member),
        )
        await database.run_write(
            database.add_ticket, channel.id, member.id, guild.id, ticket_type)
        return channel
    except (discord.HTTPException, database.DatabaseOperationError):
        logger.exception(
            "Could not open a ticket (guild_id=%s, user_id=%s, kind=%s)",
            guild.id, member.id, ticket_type,
        )
        if channel is not None:
            try:
                await channel.delete(reason=t("tickets.audit_reason_setup_failed"))
            except discord.HTTPException:
                pass
        return None
