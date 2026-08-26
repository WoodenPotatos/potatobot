"""Potato Gacha commands, inventory, vouchers, and entitlement cleanup."""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
from cogs.utils import (can_self_assign_role, guild_setting_sync,
                        handle_loop_error, is_channel, t)
from feature_access import is_enabled, maintenance_blocks
from support_tickets import open_ticket

gacha_logger = logging.getLogger("PotatoBot.Gacha")


def gacha_reward_label(key, duration_days=None):
    if duration_days is not None and key.split("_", 1)[0] in {"emoji", "sticker", "sound"}:
        known = {
            "emoji_30d", "sticker_30d", "sound_30d",
            "emoji_180d", "sticker_180d", "sound_180d",
        }
        if key not in known:
            return t(
                "gacha.custom_asset_voucher",
                asset=t(f"gacha.asset_types.{key.split('_', 1)[0]}"),
                days=duration_days,
            )
    return t(f"gacha.rewards.{key}")


async def revoke_entitlement(guild, entitlement) -> bool:
    """Remove the Discord side of one gacha entitlement.

    Shared by the daily expiry loop and by data erasure: erasing the record without
    revoking the grant would leave a premium role or a rented asset in place with
    nothing left to attribute or expire it. Returns False when Discord rejected the
    call, so the caller can leave the record alone and retry on the next pass.

    Every branch is matched by name and the fall-through revokes nothing. It used
    to be a catch-all that read `discord_item_id`, which a custom-shop `role:<id>`
    entitlement leaves NULL — so `int(None)` raised a TypeError that the handler
    below did not catch, and erasure aborted before `anonymize_user` ran. A member
    asked to be erased, saw a command error, and was not erased. An unrecognised
    key therefore reports success: there is nothing here that knows how to reach
    it, and retrying forever would block the erasure rather than complete it.
    """
    kind = entitlement["entitlement_key"]
    try:
        if kind == "premium":
            member = guild.get_member(entitlement["user_id"])
            role_id = guild_setting_sync(guild.id, "premium_role")
            role = guild.get_role(role_id) if role_id else None
            if member and role:
                await member.remove_roles(
                    role, reason=t("gacha.premium_expired_reason")
                )
        elif kind.startswith("role:"):
            # A custom-shop timed role carries its role id in the key and stores
            # no `discord_item_id`, so it must be matched before any branch that
            # reads that column. Mirrors cogs/shop.py's own expiry path.
            member = guild.get_member(entitlement["user_id"])
            role = guild.get_role(int(kind.split(":", 1)[1]))
            if member and role:
                await member.remove_roles(
                    role, reason=t("shop.custom_role_expired_reason")
                )
        elif kind in {"emoji", "sticker", "sound"}:
            item_id = entitlement.get("discord_item_id")
            if item_id is None:
                gacha_logger.warning(
                    "Asset entitlement has no Discord id, nothing to revoke "
                    "(entitlement_id=%s, kind=%s).",
                    entitlement["entitlement_id"], kind,
                )
                return True
            if kind == "emoji":
                item = guild.get_emoji(int(item_id))
            elif kind == "sticker":
                item = await guild.fetch_sticker(int(item_id))
            else:
                item = await guild.fetch_soundboard_sound(int(item_id))
            if item:
                await item.delete(reason=t("gacha.asset_expired_reason"))
        else:
            gacha_logger.warning(
                "Unrecognised entitlement key, nothing revoked "
                "(entitlement_id=%s, kind=%s).",
                entitlement["entitlement_id"], kind,
            )
    except (discord.HTTPException, TypeError, ValueError):
        gacha_logger.exception(
            "Failed to expire gacha entitlement (entitlement_id=%s, kind=%s).",
            entitlement["entitlement_id"], kind,
        )
        return False
    return True


class Gacha(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.entitlement_cleanup.start()

    def cog_unload(self):
        self.entitlement_cleanup.cancel()

    async def banner_autocomplete(self, interaction: discord.Interaction, current: str):
        """Offer the guild's enabled banners.

        Autocomplete is its own interaction, so it carries the same gates as the
        command: a disabled feature or maintenance mode must not disclose which
        banners a guild has configured.
        """
        if maintenance_blocks(interaction.guild, interaction.user) or not is_enabled(
            interaction.guild_id, "shop_gacha"
        ):
            return []
        try:
            banners = await database.run_read(
                database.list_gacha_banners, interaction.guild_id
            )
        except database.DatabaseOperationError:
            gacha_logger.exception("Banner autocomplete lookup failed.")
            return []
        needle = (current or "").casefold()
        return [
            app_commands.Choice(
                name=banner["display_name"], value=banner["banner_key"]
            )
            for banner in banners
            if banner["enabled"]
            and (needle in banner["display_name"].casefold()
                 or needle in banner["banner_key"].casefold())
        ][:25]

    @commands.hybrid_command(name="gacha", description=t("general.cmd_gacha"))
    @app_commands.describe(
        rolls=t("general.gacha_rolls_description"),
        banner=t("general.gacha_banner_description"),
    )
    @app_commands.choices(rolls=[
        app_commands.Choice(name=t("gacha.choice_one"), value=1),
        app_commands.Choice(name=t("gacha.choice_ten"), value=10),
    ])
    @app_commands.autocomplete(banner=banner_autocomplete)
    @is_channel("economy_channels")
    async def gacha(self, ctx, rolls: int = 1, banner: str = None):
        """Buy one or ten atomic pulls from one of the guild's banners."""
        if rolls not in (1, 10):
            return await ctx.send(t("gacha.invalid_roll_count"), ephemeral=True)
        banner_key = (banner or database.DEFAULT_GACHA_BANNER_KEY).strip()
        try:
            result = await database.run_write(
                database.perform_gacha_pulls, ctx.guild.id, ctx.author.id, rolls,
                banner_key,
            )
        except database.ValidationError:
            # A malformed banner argument is a user mistake, not a failure.
            return await ctx.send(t("gacha.banner_unknown"), ephemeral=True)
        if not result["purchased"]:
            key = {
                "banner_disabled": "banner_disabled",
                "banner_unknown": "banner_unknown",
            }.get(result["reason"], "not_enough_money")
            return await ctx.send(t(f"gacha.{key}"), ephemeral=True)

        lines = []
        for reward in result["results"]:
            suffix = ""
            if reward.get("duplicate_compensation") is not None:
                suffix = t(
                    "gacha.duplicate_suffix",
                    amount=reward["duplicate_compensation"],
                )
            if reward.get("voucher_id"):
                suffix += t("gacha.voucher_suffix", voucher_id=reward["voucher_id"])
            if reward["four_star_guarantee"]:
                suffix += t("gacha.four_star_guarantee_suffix")
            if reward.get("featured_guaranteed"):
                suffix += t("gacha.featured_guaranteed_suffix")
            elif reward.get("featured"):
                suffix += t("gacha.featured_suffix")
            lines.append(
                t(
                    "gacha.result_line",
                    stars="⭐" * reward["rarity"],
                    reward=gacha_reward_label(reward["key"]),
                    suffix=suffix,
                )
            )
        embed = discord.Embed(
            title=t("gacha.result_title", rolls=rolls),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_author(name=result["banner_name"])
        footer = t(
            "gacha.result_footer",
            pity=result["pity"],
            four_pity=result["four_star_counter"],
            four_interval=result["four_star_interval"],
            balance=result["balance"],
        )
        # A held guarantee is state the member paid for and cannot see anywhere
        # else, so it is reported the way pity is. Only tiers this banner
        # actually splits on are mentioned.
        held = result.get("guaranteed_featured") or {}
        for tier in ("5", "4"):
            if held.get(tier):
                footer += t("gacha.featured_guarantee_footer", stars=f"{tier}⭐")
        embed.set_footer(text=footer)
        await ctx.send(embed=embed, ephemeral=False)

    @commands.hybrid_command(name="inventory", description=t("general.cmd_inventory"))
    @is_channel("economy_channels")
    async def inventory(self, ctx):
        items, vouchers = await asyncio.gather(
            database.run_read(database.get_user_inventory, ctx.guild.id, ctx.author.id),
            database.run_read(database.get_user_vouchers, ctx.guild.id, ctx.author.id),
        )
        item_lines = [
            t("gacha.inventory_item", item=gacha_reward_label(key), amount=amount)
            for key, amount in items.items()
        ] or [t("gacha.inventory_empty")]
        voucher_lines = [
            t(
                "gacha.inventory_voucher",
                reward=gacha_reward_label(voucher["reward_key"], voucher["duration_days"]),
                voucher_id=voucher["voucher_id"],
                status=t(f"gacha.voucher_status.{voucher['status']}"),
            )
            for voucher in vouchers
        ] or [t("gacha.inventory_empty")]
        embed = discord.Embed(title=t("gacha.inventory_title"), color=discord.Color.gold())
        embed.add_field(
            name=t("gacha.inventory_items"), value="\n".join(item_lines), inline=False
        )
        embed.add_field(
            name=t("gacha.inventory_vouchers"),
            value="\n".join(voucher_lines), inline=False,
        )
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="pity", description=t("general.cmd_pity"))
    @is_channel("economy_channels")
    @discord.app_commands.autocomplete(banner=banner_autocomplete)
    @discord.app_commands.describe(banner=t("general.gacha_banner_description"))
    async def pity(self, ctx, banner: str = None):
        """Live pity plus the last five 5-stars, with the pity each landed at."""
        banner_key = banner or database.DEFAULT_GACHA_BANNER_KEY
        try:
            stored = await database.run_read(
                database.get_gacha_banner, ctx.guild.id, banner_key)
        except database.ValidationError:
            return await ctx.send(t("gacha.banner_unknown"), ephemeral=True)

        pity = await database.run_read(
            database.get_gacha_pity, ctx.guild.id, ctx.author.id, banner_key)
        history = await database.run_read(
            database.get_five_star_history, ctx.guild.id, ctx.author.id, 5)
        # Not named `config`: that is the legacy module-level dictionary's name,
        # and the test that keeps cogs off it matches the subscript by name.
        banner_config = stored["config"]

        embed = discord.Embed(
            title=t("gacha.pity_title", user=ctx.author.display_name),
            color=discord.Color.gold(),
        )
        embed.set_author(name=stored["display_name"])
        embed.add_field(
            name=t("gacha.pity_current"),
            value=t("gacha.pity_current_value",
                    pity=pity["pity"], hard=banner_config["hard_pity"],
                    four=pity["four_star_counter"],
                    interval=banner_config["four_star_guarantee_interval"]),
            inline=False,
        )
        # Only mention a guarantee the member actually holds; a line saying "no
        # guarantee" on every banner without a rate-up would be noise.
        held = [t(f"gacha.pity_guarantee_{tier}")
                for tier in ("five", "four")
                if pity[f"guaranteed_featured_{tier}"]]
        if held:
            embed.add_field(name=t("gacha.pity_guarantee"),
                            value="\n".join(held), inline=False)

        lines = [
            t("gacha.pity_history_line",
              reward=gacha_reward_label(entry["reward_key"]),
              pity=entry["pity"],
              marker=(t("gacha.pity_marker_hard") if entry["hard_pity"]
                      else t("gacha.pity_marker_featured") if entry["featured"]
                      else ""),
              banner=entry["banner_key"])
            for entry in history
        ] or [t("gacha.pity_history_empty")]
        embed.add_field(name=t("gacha.pity_history"), value="\n".join(lines),
                        inline=False)
        embed.set_footer(text=t("gacha.pity_footer",
                                total=pity["total_pulls"],
                                five_stars=pity["five_stars"]))
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="redeem", description=t("general.cmd_redeem"))
    @is_channel("economy_channels")
    async def redeem(self, ctx, voucher_id: str):
        result = await database.run_write(
            database.redeem_voucher, ctx.guild.id, ctx.author.id, voucher_id
        )
        if not result["redeemed"]:
            return await ctx.send(
                t(f"gacha.redeem_{result['reason']}"), ephemeral=True
            )
        if result["kind"] == "premium":
            role_id = guild_setting_sync(ctx.guild.id, "premium_role")
            role = ctx.guild.get_role(role_id) if role_id else None
            if not role or not can_self_assign_role(ctx.guild, role):
                await database.run_write(
                    database.rollback_premium_redemption,
                    ctx.guild.id, ctx.author.id, voucher_id,
                )
                return await ctx.send(t("shop.role_not_found"), ephemeral=True)
            try:
                await ctx.author.add_roles(role, reason=t("gacha.premium_role_reason"))
            except discord.HTTPException:
                gacha_logger.exception("Failed to assign a redeemed premium role.")
                await database.run_write(
                    database.rollback_premium_redemption,
                    ctx.guild.id, ctx.author.id, voucher_id,
                )
                return await ctx.send(t("shop.bot_role_hierarchy_error"), ephemeral=True)
            message = t(
                "gacha.redeem_premium_success", expires_at=result["expires_at"]
            )
        else:
            # An asset voucher needs a conversation: staff have to know which
            # emoji, sticker or sound to make. Before this, redeeming recorded a
            # request id and said so, and the two of you agreed on the asset
            # somewhere the bot could not see — or did not, and it sat in the
            # queue.
            message = await self._open_redeem_ticket(ctx, result)
        await ctx.send(message, ephemeral=True)

    async def _open_redeem_ticket(self, ctx, result) -> str:
        """Open the ticket an asset redemption needs, and say where it is.

        Falls back to the request id whenever a ticket cannot be opened — the
        feature is off, or Discord refused. The voucher is already spent by this
        point, so this must never fail the redemption: the request is in the
        queue either way and an operator can still fulfil it.
        """
        request_id = result["request_id"]
        if not is_enabled(ctx.guild.id, "tickets"):
            return t("gacha.redeem_fulfillment_success", request_id=request_id)

        channel = await open_ticket(
            ctx.guild, ctx.author,
            f"{result['asset_type']}-{ctx.author.name.lower()}"[:100],
            "redeem",
        )
        if channel is None:
            return t("gacha.redeem_fulfillment_success", request_id=request_id)

        embed = discord.Embed(
            title=t("gacha.redeem_ticket_title"),
            description=t("gacha.redeem_ticket_desc",
                          asset=t(f"gacha.asset_types.{result['asset_type']}")),
            color=discord.Color.gold(),
        )
        embed.add_field(name=t("gacha.redeem_ticket_request"),
                        value=f"#{request_id}", inline=True)
        embed.add_field(name=t("gacha.redeem_ticket_member"),
                        value=ctx.author.mention, inline=True)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            # The channel exists and is recorded, so the request is reachable;
            # only the opening message is missing.
            gacha_logger.exception(
                "Could not post into a redemption ticket (channel_id=%s)",
                channel.id)
        return t("gacha.redeem_ticket_opened", channel=channel.mention,
                 request_id=request_id)

    @tasks.loop(hours=24)
    async def entitlement_cleanup(self):
        entitlements = await database.run_read(
            database.get_expired_entitlements, datetime.now(timezone.utc).isoformat()
        )
        for entitlement in entitlements:
            kind = entitlement["entitlement_key"]
            if (
                kind not in {"premium", "emoji", "sticker", "sound"}
                or entitlement["source_type"] != "gacha"
            ):
                continue
            guild = self.bot.get_guild(entitlement["guild_id"])
            if not guild:
                continue
            # Deliberately NOT gated on `shop_gacha`. A feature flag decides
            # whether a member may *acquire* something; it cannot decide whether
            # something already granted is allowed to expire. Gating this froze
            # revocation, so switching the gacha off left members holding premium
            # roles and rented assets past their expiry with nothing left to
            # withdraw them — the grant outlived the record that measured it.
            if not await revoke_entitlement(guild, entitlement):
                continue
            await database.run_write(
                database.expire_entitlement, entitlement["entitlement_id"]
            )
            if entitlement["discord_item_id"]:
                await database.run_write(
                    database.delete_rental_for_item,
                    entitlement["guild_id"], kind, entitlement["discord_item_id"],
                )

    @entitlement_cleanup.before_loop
    async def before_entitlement_cleanup(self):
        await self.bot.wait_until_ready()

    @entitlement_cleanup.error
    async def entitlement_cleanup_error(self, error):
        # This loop starts in __init__ rather than on_ready, so a reconnect
        # would not revive it: without this handler one transient database
        # error stops premium and asset revocation until the next restart.
        await handle_loop_error(
            self.bot, self.entitlement_cleanup, "entitlement_cleanup", error,
            gacha_logger,
        )


async def setup(bot):
    await bot.add_cog(Gacha(bot))
