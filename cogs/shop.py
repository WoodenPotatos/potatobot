import discord
import asyncio
import logging
import os
import sys
import time

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
from feature_access import is_enabled, require_interaction_feature
from item_catalog import SHOP_ITEMS, ItemEffect

from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
from cogs.tickets import TicketControl
from cogs.utils import (can_self_assign_role, currency_emoji,
                        currency_select_emoji, guild_setting_sync,
                        is_channel, t)

shop_logger = logging.getLogger("PotatoBot.Shop")

# A Discord select accepts at most 25 options, and the shop renders every
# built-in item plus every enabled custom one into a single menu. The dashboard
# derives its custom-item cap from the same ceiling, but that cap is only
# enforced when an item is created, so the menu still guards itself here.
SELECT_OPTION_LIMIT = 25

# Item names and descriptions are localized; this mapping carries the business
# behavior the purchase handler needs. Which items exist and what they do comes
# from the shared catalog, so the shop and Potato Gacha cannot disagree about an
# item they both hand out.
_EFFECT_TYPES = {
    ItemEffect.ROLE: "role",
    ItemEffect.INVENTORY: "inventory",
    ItemEffect.BODYGUARD: "rent",
    ItemEffect.VAULT: "vault",
    ItemEffect.RENT_TICKET: "ticket",
}


def get_shop_items(prices):
    """Build the purchasable catalog with this guild's current prices."""
    items = {}
    for key, definition in SHOP_ITEMS.items():
        item = {
            "price": prices.get(key, definition.shop_price),
            "type": _EFFECT_TYPES[definition.effect],
            "value": definition.value,
        }
        if definition.effect is ItemEffect.RENT_TICKET:
            item["ticket_prefix"] = definition.ticket_prefix
            item["limit_check"] = definition.limit_check
        items[key] = item
    return items


def add_custom_shop_items(items, definitions):
    for definition in definitions:
        if not definition["enabled"]:
            continue
        key = definition["item_key"]
        if key in database.BUILTIN_SHOP_KEYS:
            # Creation rejects these, so a row here predates the guard or was
            # written directly. Skip it rather than shadow the built-in.
            shop_logger.error(
                "Ignoring custom shop item that collides with a built-in key "
                "(guild_id=%s, item_key=%s)",
                definition.get("guild_id"), key,
            )
            continue
        items[key] = {
            "price": definition["price"], "type": "custom",
            "template_type": definition["template_type"], "config": definition["config"],
            "name": definition["name"] or definition["item_key"],
            "description": definition["description"] or "",
        }
    return items


def shop_item_name(key, item):
    return item.get("name") or t(f"shop.items.{key}.name")


def shop_item_description(key, item):
    return item.get("description") or t(f"shop.items.{key}.desc")


class ShopView(discord.ui.View):
    def __init__(self, user_id, items, guild_id=None):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.selected_item = None
        self.purchase_complete = False
        self.purchase_lock = asyncio.Lock()
        self.last_interactions = {}

        # Built-in items are ordered first so a guild that somehow exceeded the
        # custom-item cap loses only its own extra definitions. Discord rejects
        # the whole menu past 25 options, which would take the command down.
        if len(items) > SELECT_OPTION_LIMIT:
            dropped = list(items)[SELECT_OPTION_LIMIT:]
            shop_logger.error(
                "Shop menu exceeds the %s-option Discord limit; hiding %s item(s) "
                "(guild_id=%s, hidden=%s)",
                SELECT_OPTION_LIMIT, len(dropped), guild_id, ",".join(dropped),
            )
            items = {key: items[key] for key in list(items)[:SELECT_OPTION_LIMIT]}
        self.items = items

        # Build options at view creation so current prices and locale text are displayed.
        options = []
        for key, item in self.items.items():
            name = shop_item_name(key, item)
            desc = shop_item_description(key, item)
            # A select option's label is plain text, so the currency cannot be
            # written into it: a custom emoji renders as its raw `<:name:id>`
            # there, which is exactly what this menu was showing. `emoji=` is the
            # supported route, and it is dropped rather than guessed when the
            # configured symbol is not something Discord can resolve.
            label = t("shop.select_option_label", name=name, price=item['price'])
            options.append(discord.SelectOption(
                label=label, description=desc[:100], value=key,
                emoji=currency_select_emoji(),
            ))

        select_menu = discord.ui.Select(placeholder=t("shop.placeholder"), options=options)
        select_menu.callback = self.select_callback
        self.add_item(select_menu)

        buy_btn = discord.ui.Button(label=t("shop.buy_btn"), style=discord.ButtonStyle.green, emoji="💰")
        buy_btn.callback = self.buy_button
        self.add_item(buy_btn)

    async def interaction_check(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "shop"):
            return False
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(t("shop.not_your_menu"), ephemeral=True)
            return False
        action_id = (interaction.data or {}).get("custom_id", "shop")
        now = time.monotonic()
        retry_after = 2 - (now - self.last_interactions.get(action_id, 0))
        if retry_after > 0:
            await interaction.response.send_message(
                t("utils.command_cooldown", seconds=int(retry_after) + 1),
                ephemeral=True,
            )
            return False
        self.last_interactions[action_id] = now
        return True

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_item = self.children[0].values[0]
        item = self.items[self.selected_item]
        name = shop_item_name(self.selected_item, item)
        desc = shop_item_description(self.selected_item, item)
        
        embed = discord.Embed(title=t("shop.confirm_title"), color=discord.Color.blue())
        embed.add_field(name=t("shop.item_label"), value=name)
        embed.add_field(name=t("shop.price_label"), value=f"{item['price']}{currency_emoji()}")
        embed.description = t("shop.effect_desc", desc=desc)
        
        await interaction.response.edit_message(embed=embed, view=self)

    async def buy_button(self, interaction: discord.Interaction):
        if self.selected_item and self.items[self.selected_item]["type"] == "ticket":
            if not await require_interaction_feature(interaction, "rentals"):
                return
        await interaction.response.defer(ephemeral=True)
        async with self.purchase_lock:
            if self.purchase_complete:
                return await interaction.edit_original_response(content=t("shop.purchase_already_processed"), embed=None, view=None)
            if not self.selected_item:
                return await interaction.edit_original_response(content=t("shop.no_item_selected"), embed=None, view=self)

            item_data = self.items[self.selected_item]
            item_name = shop_item_name(self.selected_item, item_data)
            user_id = interaction.user.id

            if item_data["type"] == "custom":
                if item_data["template_type"] == "fixed_role":
                    role = interaction.guild.get_role(int(item_data["config"]["role_id"]))
                    if role in interaction.user.roles:
                        return await interaction.edit_original_response(
                            content=t("shop.already_have_role"), embed=None, view=self
                        )
                purchase = await database.run_write(
                    database.purchase_custom_shop_item,
                    interaction.guild.id, user_id, self.selected_item,
                )
                if not purchase["purchased"]:
                    key = "already_have_vault" if purchase["reason"] == "already_owned" else "not_enough_money"
                    return await interaction.edit_original_response(
                        content=t(f"shop.{key}"), embed=None, view=self
                    )
                if purchase["template_type"] in {"fixed_role", "timed_role"}:
                    # Compensation always mirrors the debit this purchase made,
                    # never the price the menu was rendered with.
                    rollback_args = (
                        interaction.guild.id, user_id, purchase["price"],
                        purchase["template_type"],
                        int(purchase["config"]["role_id"]),
                        purchase["config"].get("duration_days"),
                    )
                    role = interaction.guild.get_role(int(purchase["config"]["role_id"]))
                    if not role or not can_self_assign_role(interaction.guild, role):
                        await database.run_write(
                            database.rollback_custom_role_purchase, *rollback_args
                        )
                        return await interaction.edit_original_response(
                            content=t("shop.role_not_found"), embed=None, view=self
                        )
                    try:
                        await interaction.user.add_roles(role)
                    except discord.HTTPException:
                        await database.run_write(
                            database.rollback_custom_role_purchase, *rollback_args
                        )
                        return await interaction.edit_original_response(
                            content=t("shop.bot_role_hierarchy_error"), embed=None, view=self
                        )
                self.purchase_complete = True
                suffix = t("shop.custom_voucher_suffix", voucher_id=purchase["voucher_id"]) \
                    if purchase.get("voucher_id") else ""
                return await interaction.edit_original_response(
                    content=t("shop.purchase_success", item_name=item_name) + suffix,
                    embed=None, view=None,
                )

            if item_data["type"] == "ticket":
                guild = interaction.guild
                if item_data["limit_check"] == "static":
                    current_static = len([e for e in guild.emojis if not e.animated])
                    if current_static >= guild.emoji_limit:
                        return await interaction.edit_original_response(content=t("shop.static_emoji_limit"), embed=None, view=self)
                elif item_data["limit_check"] == "animated":
                    current_anim = len([e for e in guild.emojis if e.animated])
                    if current_anim >= guild.emoji_limit:
                        return await interaction.edit_original_response(content=t("shop.anim_emoji_limit"), embed=None, view=self)
                elif item_data["limit_check"] == "soundboard":
                    sounds = await guild.fetch_soundboard_sounds()
                    if len(sounds) >= 8:
                        return await interaction.edit_original_response(content=t("shop.soundboard_limit"), embed=None, view=self)

                purchase = await database.run(
                    database.purchase_upgrade, user_id, item_data["price"], "debit"
                )
                if not purchase["purchased"]:
                    return await interaction.edit_original_response(content=t("shop.not_enough_money"), embed=None, view=self)
                try:
                    ticket_name = f"{item_data['ticket_prefix']}-{interaction.user.name.lower()}"
                    category = guild.get_channel(
                        guild_setting_sync(guild.id, "ticket_category"))
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                    }
                    for r_id in guild_setting_sync(guild.id, "admin_roles"):
                        role = guild.get_role(r_id)
                        if role:
                            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                    channel = await guild.create_text_channel(name=ticket_name, category=category, overwrites=overwrites)
                    await database.run(
                        database.add_ticket, channel.id, interaction.user.id,
                        interaction.guild.id,
                    )
                    embed = discord.Embed(title=t("shop.ticket_title", item_name=item_name), description=t("shop.ticket_desc"), color=discord.Color.gold())
                    await channel.send(embed=embed, view=TicketControl())
                except Exception:
                    await database.run(database.refund_balance, user_id, item_data["price"])
                    if "channel" in locals() and channel is not None:
                        try:
                            await channel.delete(reason=t("shop.audit_reason_setup_failed"))
                        except discord.HTTPException:
                            pass
                    raise
                self.purchase_complete = True
                return await interaction.edit_original_response(content=t("shop.ticket_opened", channel=channel.mention), embed=None, view=None)

            if item_data["type"] == "role":
                # A role item's catalog value is the setting key that names the
                # role, checked against the registry rather than trusted: an
                # unregistered value would otherwise raise inside the callback.
                from settings_registry import SETTING_DEFINITIONS
                setting_key = item_data["value"]
                role_id = (guild_setting_sync(interaction.guild.id, setting_key)
                           if setting_key in SETTING_DEFINITIONS else None)
                role = interaction.guild.get_role(role_id)
                if not role or not can_self_assign_role(interaction.guild, role):
                    return await interaction.edit_original_response(content=t("shop.role_not_found"), embed=None, view=self)
                if role in interaction.user.roles:
                    return await interaction.edit_original_response(content=t("shop.already_have_role"), embed=None, view=self)
                purchase = await database.run(
                    database.purchase_upgrade, user_id, item_data["price"], "debit"
                )
                if not purchase["purchased"]:
                    return await interaction.edit_original_response(content=t("shop.not_enough_money"), embed=None, view=self)
                try:
                    await interaction.user.add_roles(role)
                except discord.Forbidden:
                    await database.run(database.refund_balance, user_id, item_data["price"])
                    return await interaction.edit_original_response(content=t("shop.bot_role_hierarchy_error"), embed=None, view=self)
                except Exception:
                    await database.run(database.refund_balance, user_id, item_data["price"])
                    raise
            elif item_data["type"] == "vault":
                purchase = await database.run(
                    database.purchase_upgrade, user_id, item_data["price"],
                    "vault", item_data["value"],
                )
            elif item_data["type"] == "inventory":
                # The same stackable, guild-local row Potato Gacha grants, so a
                # bought consumable and a pulled one are the same object.
                purchase = await database.run_write(
                    database.purchase_inventory_item, interaction.guild.id,
                    user_id, item_data["price"], self.selected_item,
                )
            elif item_data["type"] == "rent":
                expire_time = (datetime.now() + timedelta(hours=24)).isoformat()
                purchase = await database.run(
                    database.purchase_upgrade, user_id, item_data["price"],
                    "bodyguard", expire_time,
                )

            if not purchase["purchased"]:
                if purchase.get("reason") == "already_owned":
                    return await interaction.edit_original_response(
                        content=t("shop.already_have_vault"), embed=None, view=self
                    )
                return await interaction.edit_original_response(content=t("shop.not_enough_money"), embed=None, view=self)
            self.purchase_complete = True
            await interaction.edit_original_response(content=t("shop.purchase_success", item_name=item_name), embed=None, view=None)

class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rental_cleanup.start() 

    def cog_unload(self):
        self.rental_cleanup.cancel() 

    @commands.hybrid_command(name="shop", description=t("general.cmd_shop"))
    @is_channel("economy_channels")
    async def shop(self, ctx):
        embed = discord.Embed(
            title=t("shop.shop_title"),
            description=t("shop.shop_desc"),
            color=discord.Color.gold()
        )
        prices, custom = await asyncio.gather(
            database.run_read(database.get_shop_prices, ctx.guild.id),
            database.run_read(database.get_shop_item_definitions, ctx.guild.id),
        )
        view = ShopView(
            ctx.author.id,
            add_custom_shop_items(get_shop_items(prices), custom),
            guild_id=ctx.guild.id,
        )
        await ctx.send(embed=embed, view=view, ephemeral=True)

    @tasks.loop(hours=24)
    async def rental_cleanup(self):
        # Read per guild rather than installation-wide, so no guild's cleanup pass
        # can ever see, let alone delete, another guild's rented asset.
        for guild in list(self.bot.guilds):
            if not is_enabled(guild.id, "rentals"):
                continue
            rentals = await database.run(database.get_all_rentals, guild.id)
            await self._expire_guild_rentals(guild, rentals)
        # Entitlements already carry their own guild, so this pass stays global.
        await self._expire_shop_entitlements()

    async def _expire_guild_rentals(self, guild, rentals):
        for r_id, item_type, discord_item_id, expires_at, guild_id in rentals:
            expire_date = datetime.fromisoformat(expires_at)
            now = datetime.now(expire_date.tzinfo) if expire_date.tzinfo else datetime.now()
            if now >= expire_date:

                try:
                    if item_type == "emoji":
                        emoji = guild.get_emoji(int(discord_item_id))
                        if emoji:
                            await emoji.delete(reason=t("shop.rent_expired_reason"))
                    
                    elif item_type == "sound":
                        sound = await guild.fetch_soundboard_sound(int(discord_item_id))
                        if sound:
                            await sound.delete(reason=t("shop.rent_expired_reason"))
                    elif item_type == "sticker":
                        sticker = await guild.fetch_sticker(int(discord_item_id))
                        if sticker:
                            await sticker.delete(reason=t("shop.rent_expired_reason"))
                except Exception:
                    shop_logger.exception(
                        "Failed to delete expired %s with Discord ID %s.",
                        item_type,
                        discord_item_id,
                    )

                await database.run(database.delete_rental, r_id)
                shop_logger.info(
                    "Removed expired rental %s with Discord ID %s.",
                    item_type,
                    discord_item_id,
                )

    async def _expire_shop_entitlements(self):
        entitlements = await database.run_read(
            database.get_expired_entitlements, datetime.now(timezone.utc).isoformat()
        )
        for entitlement in entitlements:
            guild = self.bot.get_guild(entitlement["guild_id"])
            if not guild or not is_enabled(guild.id, "shop"):
                continue
            kind = entitlement["entitlement_key"]
            if kind.startswith("role:"):
                member = guild.get_member(entitlement["user_id"])
                role = guild.get_role(int(kind.split(":", 1)[1]))
                if member and role:
                    try:
                        await member.remove_roles(role, reason=t("shop.custom_role_expired_reason"))
                    except discord.HTTPException:
                        shop_logger.exception("Failed to remove an expired custom shop role.")
                        continue
                await database.run_write(
                    database.expire_entitlement, entitlement["entitlement_id"]
                )
            elif (
                kind in {"emoji", "sticker", "sound"}
                and entitlement["source_type"] == "shop"
            ):
                try:
                    if kind == "emoji":
                        item = guild.get_emoji(int(entitlement["discord_item_id"]))
                        if item:
                            await item.delete(reason=t("shop.rent_expired_reason"))
                    elif kind == "sticker":
                        item = await guild.fetch_sticker(int(entitlement["discord_item_id"]))
                        if item:
                            await item.delete(reason=t("shop.rent_expired_reason"))
                    else:
                        item = await guild.fetch_soundboard_sound(
                            int(entitlement["discord_item_id"])
                        )
                        if item:
                            await item.delete(reason=t("shop.rent_expired_reason"))
                except discord.HTTPException:
                    shop_logger.exception(
                        "Failed to expire custom shop asset (entitlement_id=%s, kind=%s).",
                        entitlement["entitlement_id"], kind,
                    )
                    continue
                await database.run_write(
                    database.expire_entitlement, entitlement["entitlement_id"]
                )
                await database.run_write(
                    database.delete_rental_for_item,
                    entitlement["guild_id"], kind, entitlement["discord_item_id"],
                )
    
    @rental_cleanup.before_loop
    async def before_rental_cleanup(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(Shop(bot))
