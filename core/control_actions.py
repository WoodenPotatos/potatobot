"""The durable Discord action outbox: the worker that drains `control_actions`.

The dashboard queues Discord work here — publishes, deletes, permission repairs,
fulfilment notices, entitlement teardowns and operator erasures — and only the
bot executes it, because only the bot holds a gateway connection. It lived in
`dashboard_api`, so `main.py` could start it only when it had imported the
dashboard; in the two-process split (`POTATOBOT_DASHBOARD_ENABLED=false`, the
dashboard as its own `python dashboard_api.py`) nothing drained the outbox and
every queued action waited forever. Importing `dashboard_api` in the bot was not
the fix: its import loads, and may create, the dashboard session secret.

So this module carries nothing of Flask, OAuth or the session, and `main.py`
starts `control_action_worker` in every topology. Authority is re-derived per
action: the actor's live `manage_guild`, the bot's own channel permission, and
for an erasure `ADMIN_DISCORD_ID`, read per call rather than from the queued row.

Rules that bind changes here: docs/subsystems/managed_messages.md
"""

import asyncio
import logging
import os
import time

import discord

from cogs.utils import t
from core import database
from core.managed_messages import render_managed_message

# The name the worker logged under while it lived in `dashboard_api`, kept so a
# journal search for an outbox failure still finds the old lines and the new.
logger = logging.getLogger("PotatoBot.Dashboard")

CONTROL_ACTION_RETENTION_DAYS = 30
# Pruning shares the worker's idle path rather than owning a timer of its own.
CONTROL_ACTION_PRUNE_INTERVAL = 3600


async def execute_member_erasure(bot, action) -> str | None:
    """Carry out an operator-requested erasure. Returns an error code or None.

    The dashboard process cannot revoke a Discord grant, so the route queues the
    request and the bot performs it here: grants first, then the database, so no
    premium role or rented asset outlives the record that would have expired it.
    """
    from cogs.privacy import revoke_all_entitlements

    # Re-derive authority here rather than trust the queued row: the action may
    # have been enqueued before ADMIN_DISCORD_ID last changed.
    host = (os.getenv("ADMIN_DISCORD_ID") or "").strip()
    if not host or str(action["actor_id"]) != host:
        return "permission_denied"
    subject_id = action["payload"].get("user_id")
    try:
        subject_id = int(subject_id)
    except (TypeError, ValueError):
        return "invalid_subject"
    if subject_id <= 0:
        return "invalid_subject"
    try:
        revoked = await revoke_all_entitlements(bot, subject_id)
        receipt = await database.run_write(
            database.anonymize_user, subject_id, action["actor_id"],
            action["guild_id"], "operator_request",
        )
    except database.ValidationError:
        return "invalid_subject"
    except Exception:
        # Broad on purpose: this settles one outbox action, and an escaping
        # exception would leave the operator with no error code at all.
        logger.exception("Operator-requested erasure failed.")
        return "internal_error"
    logger.info(
        "Operator-requested erasure complete (tombstone=%s, revoked=%s).",
        receipt["tombstone_id"], revoked,
    )
    return None


async def execute_managed_publish(guild, channel, action):
    """Post the managed message, or edit the one already posted.

    A recorded message that Discord no longer has is posted afresh rather than
    reported as an error: the operator deleted it by hand, and the alternative is
    a row that can never be published again.
    """
    payload = action["payload"]
    stored = await database.run_read(database.get_managed_message, guild.id,
                                     payload.get("kind"), payload.get("menu_key"))
    if not stored:
        return "document_unavailable"
    embeds, view = render_managed_message(guild, stored)
    if embeds is None:
        return view  # the error code

    if stored["message_id"]:
        # The message is wherever it was posted -- the recorded channel -- and
        # not necessarily the one the operator picked this time. Fetching it
        # from the target meant a publish into a second channel found nothing
        # there, posted a fresh copy, and left the first alive with working
        # buttons. A recorded message is edited where it is; moving one is a
        # delete and a re-post, which the delete route already does cleanly.
        recorded = (guild.get_channel(int(stored["channel_id"]))
                    if stored.get("channel_id") else None)
        source = recorded if isinstance(recorded, discord.TextChannel) else channel
        try:
            posted = await source.fetch_message(int(stored["message_id"]))
            await posted.edit(embeds=embeds, view=view,
                              allowed_mentions=discord.AllowedMentions.none())
            return None
        except discord.NotFound:
            logger.info(
                "Managed message %s/%s was gone; posting a new one "
                "(guild_id=%s)", stored["kind"], stored["menu_key"], guild.id)

    message = await channel.send(embeds=embeds, view=view,
                                 allowed_mentions=discord.AllowedMentions.none())
    await database.run_write(database.record_managed_post, guild.id,
                             stored["kind"], stored["menu_key"],
                             channel.id, message.id)
    return None


async def execute_managed_delete(channel, action):
    """Remove a message whose row is already gone.

    The ids travel in the payload because the row was deleted in the request that
    queued this — the row is the operator's decision and takes effect at once,
    while removing the message is Discord work that can be retried.
    """
    payload = action["payload"]
    try:
        message = await channel.fetch_message(int(payload["message_id"]))
    except discord.NotFound:
        return None  # Already gone, which is the outcome that was asked for.
    if not channel.permissions_for(channel.guild.me).manage_messages:
        return "bot_permission_denied"
    await message.delete()
    return None


async def execute_channel_permission_repair(channel, action):
    """Grant the bot's own missing permission(s) on one channel.

    `permissions` in the payload is the audit's own tuple, captured the
    moment the repair was queued and re-validated against `VALID_FLAGS`
    here -- never operator free text, and never trusted blindly even so.
    `set_permissions`'s keyword form merges into the bot's existing
    overwrite, leaving every other entry on the channel, and everyone else's
    permissions, untouched.
    """
    payload = action["payload"]
    names = payload.get("permissions") or []
    valid = discord.Permissions.VALID_FLAGS
    if not names or any(name not in valid for name in names):
        return "internal_error"
    await channel.set_permissions(
        channel.guild.me,
        reason=f"Dashboard permission repair (actor={action['actor_id']})",
        **{name: True for name in names},
    )
    await database.run_write(
        database.record_settings_audit, channel.guild.id, action["actor_id"],
        "permission.repair", payload.get("setting_key") or "",
        None, {"channel_id": channel.id, "permissions": names},
    )
    return None


async def execute_redeem_fulfilled_notice(channel, action) -> str | None:
    """Tell a redeem ticket its request was settled, from either side.

    Best-effort and channel-shaped like publish/delete: the database half has
    already committed by the time this runs, so a ticket that is already
    closed is not a failure to retry, just nothing left to notify.
    """
    payload = action["payload"]
    actor = channel.guild.get_member(action["actor_id"])
    staff_name = actor.mention if actor else t("gacha.redeem_fulfill_unknown_staff")
    if payload.get("outcome") == "cancelled":
        text = t("gacha.redeem_fulfill_notice_cancelled", staff=staff_name)
    else:
        text = t("gacha.redeem_fulfill_notice_fulfilled", staff=staff_name,
                 item_id=payload.get("discord_item_id", ""))
    await channel.send(text)
    return None


async def execute_entitlement_revocation(bot, action) -> str | None:
    """Tear down the Discord side of a revoked entitlement.

    Reuses `cogs.gacha.revoke_entitlement` unchanged -- already proven on the
    member-erasure path -- so a role is removed or an emoji/sticker/sound is
    deleted exactly as it would be on natural expiry. An entitlement kind it
    does not recognise settles as done with nothing left to do, the same
    "unrecognised key reports success" rule that path already follows.
    """
    from cogs.gacha import revoke_entitlement

    guild = bot.get_guild(action["guild_id"])
    if not guild:
        return None  # The guild is gone; nothing left to tear down.
    payload = action["payload"]
    entitlement = {
        "entitlement_id": payload.get("entitlement_id"),
        "user_id": payload.get("user_id"),
        "entitlement_key": payload.get("entitlement_key"),
        "discord_item_id": payload.get("discord_item_id"),
    }
    if not await revoke_entitlement(guild, entitlement):
        return "discord_http_error"
    return None


async def control_action_worker(bot):
    """Execute queued Discord publishes after live permission and feature checks."""
    next_prune = 0.0
    while not bot.is_closed():
        if time.monotonic() >= next_prune:
            next_prune = time.monotonic() + CONTROL_ACTION_PRUNE_INTERVAL
            try:
                removed = await database.run_write(
                    database.prune_control_actions, CONTROL_ACTION_RETENTION_DAYS
                )
                if removed:
                    logger.info(
                        "Pruned settled control actions (removed=%s)", removed
                    )
            except database.DatabaseOperationError:
                logger.exception("Control action pruning failed.")

        action = await database.run_write(database.claim_control_action)
        if action is None:
            await asyncio.sleep(2)
            continue
        error_code = None
        try:
            if action["action_type"] == "erase_member":
                # Erasure needs neither a channel nor a document, and its authority
                # comes from ADMIN_DISCORD_ID rather than guild permissions, so it
                # is handled before the publish preamble below.
                error_code = await execute_member_erasure(bot, action)
                await database.run_write(
                    database.finish_control_action, action["action_id"],
                    error_code is None, error_code,
                )
                continue
            guild = bot.get_guild(action["guild_id"])
            actor = guild.get_member(action["actor_id"]) if guild else None
            channel = guild.get_channel(action["payload"].get("channel_id")) if guild else None
            if action["action_type"] == "repair_channel_permission":
                # Editing a channel's own overwrites needs `manage_roles`
                # there, not `send_messages`, and applies to any guild
                # channel kind -- not only a `TextChannel` -- so this cannot
                # share the preamble below.
                if not guild or not actor or not actor.guild_permissions.manage_guild:
                    error_code = "permission_denied"
                elif channel is None:
                    error_code = "channel_unavailable"
                elif not channel.permissions_for(guild.me).manage_roles:
                    error_code = "bot_permission_denied"
                else:
                    error_code = await execute_channel_permission_repair(channel, action)
                await database.run_write(
                    database.finish_control_action, action["action_id"],
                    error_code is None, error_code,
                )
                continue
            if action["action_type"] == "revoke_entitlement":
                # Acts on a member's grant, not a channel, so it cannot share
                # the channel preamble below either.
                if not guild or not actor or not actor.guild_permissions.manage_guild:
                    error_code = "permission_denied"
                else:
                    error_code = await execute_entitlement_revocation(bot, action)
                await database.run_write(
                    database.finish_control_action, action["action_id"],
                    error_code is None, error_code,
                )
                continue
            if not guild or not actor or not actor.guild_permissions.manage_guild:
                error_code = "permission_denied"
            elif not isinstance(channel, discord.TextChannel):
                error_code = "channel_unavailable"
            elif not channel.permissions_for(guild.me).send_messages:
                error_code = "bot_permission_denied"
            elif action["action_type"] == "publish_managed":
                error_code = await execute_managed_publish(guild, channel, action)
            elif action["action_type"] == "delete_managed":
                error_code = await execute_managed_delete(channel, action)
            elif action["action_type"] == "notify_redeem_fulfilled":
                error_code = await execute_redeem_fulfilled_notice(channel, action)
            else:
                error_code = "feature_disabled_or_unsupported"
        except discord.HTTPException:
            logger.exception("Dashboard control action failed (action_id=%s)", action["action_id"])
            error_code = "discord_http_error"
        except Exception:
            logger.exception("Dashboard control action crashed (action_id=%s)", action["action_id"])
            error_code = "internal_error"
        await database.run_write(
            database.finish_control_action, action["action_id"], error_code is None, error_code
        )
