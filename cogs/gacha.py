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
from cogs.utils import can_self_assign_role, config, is_channel, t
from feature_access import is_enabled, maintenance_blocks

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
    """
    kind = entitlement["entitlement_key"]
    try:
        if kind == "premium":
            member = guild.get_member(entitlement["user_id"])
            role_id = config.get("roles", {}).get("premium_role")
            role = guild.get_role(role_id) if role_id else None
            if member and role:
                await member.remove_roles(
                    role, reason=t("gacha.premium_expired_reason")
                )
        elif kind == "emoji":
            item = guild.get_emoji(int(entitlement["discord_item_id"]))
            if item:
                await item.delete(reason=t("gacha.asset_expired_reason"))
        elif kind == "sticker":
            item = await guild.fetch_sticker(int(entitlement["discord_item_id"]))
            if item:
                await item.delete(reason=t("gacha.asset_expired_reason"))
        else:
            item = await guild.fetch_soundboard_sound(
                int(entitlement["discord_item_id"])
            )
            if item:
                await item.delete(reason=t("gacha.asset_expired_reason"))
    except discord.HTTPException:
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
        embed.set_footer(
            text=t(
                "gacha.result_footer",
                pity=result["pity"],
                four_pity=result["four_star_counter"],
                four_interval=result["four_star_interval"],
                balance=result["balance"],
            )
        )
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
            role_id = config.get("roles", {}).get("premium_role")
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
            message = t(
                "gacha.redeem_fulfillment_success", request_id=result["request_id"]
            )
        await ctx.send(message, ephemeral=True)

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
            if not guild or not is_enabled(guild.id, "shop_gacha"):
                continue
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


async def setup(bot):
    await bot.add_cog(Gacha(bot))
