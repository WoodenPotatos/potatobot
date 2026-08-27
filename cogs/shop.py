import discord
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
from feature_access import is_enabled, maintenance_blocks, require_interaction_feature
from support_tickets import open_ticket
import item_catalog
from item_catalog import SELECT_OPTION_LIMIT, SHOP_ITEMS, ItemEffect

from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
from cogs.tickets import TicketControl
from cogs.utils import (can_self_assign_role, currency_emoji,
                        currency_select_emoji, guild_setting_sync,
                        handle_loop_error, is_channel, t)

shop_logger = logging.getLogger("PotatoBot.Shop")

# A select option's label caps at 100 characters and discord.py does not check
# it — it coerces to `str` and lets Discord answer 400, which rejects the whole
# component and takes `/shop` down for that guild. A custom item's name is
# capped server-side too; this is the render-side half of the same guard, the
# shape `BUTTON_LABEL_LIMIT` already has.
SELECT_LABEL_LIMIT = 100

# `SELECT_OPTION_LIMIT` is imported from the catalog rather than written here:
# the per-section cap is computed in this process *and* in the dashboard's, and
# two literals cannot be kept equal by anything but a test.

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


def get_shop_items(prices, hidden=()):
    """Build the purchasable catalog with this guild's current prices.

    `hidden` is the guild's `shop_hidden_items` list. It filters the *shop* only:
    the gacha reads the catalog directly, so a hidden item is still drawable, and
    a member who already owns one can still spend it. Hiding decides what a guild
    sells, never what an existing grant is allowed to do.
    """
    items = {}
    for key, definition in item_catalog.visible_shop_items(hidden).items():
        item = {
            "price": prices.get(key, definition.shop_price),
            "type": _EFFECT_TYPES[definition.effect],
            "value": definition.value,
            "category": definition.category.value,
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
            # The row already carries the resolved shelf, but fall back through
            # the same resolver rather than guessing: a row read by an older
            # accessor would otherwise land nowhere and vanish from the menu.
            "category": definition.get("category") or
                        item_catalog.resolve_custom_category(
                            definition["template_type"], definition["config"],
                            definition.get("category_stored")),
        }
    return items


def shop_item_category(key, item):
    """Which shelf this item is on, never absent.

    An item with no resolvable section would simply not be rendered, so this
    fails to the first shelf rather than dropping it — a member who cannot see
    what a guild is selling has no way to report the problem.
    """
    category = item.get("category")
    if category in {member.value for member in item_catalog.ItemCategory}:
        return category
    shop_logger.error(
        "Shop item has no usable section, filing it under the first "
        "(item_key=%s, category=%r)", key, category)
    return item_catalog.SHOP_CATEGORY_ORDER[0].value


def build_category_index(items, guild_id=None):
    """Group the merged catalog into shelves, trimmed and never empty.

    Three properties, each load-bearing.

    **Built-ins come first within a shelf, explicitly.** That used to be a side
    effect of `add_custom_shop_items` appending after `get_shop_items`, and the
    trim below depends on it.

    **The trim is per shelf.** The dashboard refuses a create that would overfill
    a section, but that check counts one guild's rows at one instant and cannot
    know we shipped four consumables last month, so render defends itself too —
    the same refuse-at-write-*and*-trim-at-render posture `BUTTON_LABEL_LIMIT`
    has.

    **An empty shelf is dropped.** `discord.ui.Select(options=[])` does not raise
    locally; Discord answers 400 and the whole command dies. Since the only
    writer of the chosen section is a membership-checked read of this dict, a
    section that is offered always has something in it.
    """
    buckets = {}
    for key, item in items.items():
        buckets.setdefault(shop_item_category(key, item), []).append(key)

    index = {}
    for category in item_catalog.SHOP_CATEGORY_ORDER:
        keys = buckets.get(category.value)
        if not keys:
            continue
        builtin = [key for key in keys if key in database.BUILTIN_SHOP_KEYS]
        custom = [key for key in keys if key not in database.BUILTIN_SHOP_KEYS]
        ordered = builtin + custom
        if len(ordered) > SELECT_OPTION_LIMIT:
            dropped = ordered[SELECT_OPTION_LIMIT:]
            shop_logger.error(
                "Shop section exceeds the %s-option Discord limit; hiding %s "
                "item(s) (guild_id=%s, category=%s, hidden=%s)",
                SELECT_OPTION_LIMIT, len(dropped), guild_id, category.value,
                ",".join(dropped),
            )
            ordered = ordered[:SELECT_OPTION_LIMIT]
        index[category.value] = {key: items[key] for key in ordered}
    return index


def shop_item_name(key, item):
    return item.get("name") or t(f"shop.items.{key}.name")


def shop_item_description(key, item):
    return item.get("description") or t(f"shop.items.{key}.desc")


@dataclass(frozen=True)
class PurchaseResult:
    """What a purchase did, and what to tell the member.

    `settled` means money moved: the caller must not offer this purchase again.
    `content` is already localized, so both callers only have to deliver it.
    """

    settled: bool
    content: str


async def purchase_item(guild, member, key, item) -> PurchaseResult:
    """Buy one item for one member.

    Takes a guild and a member and **no interaction or context**, which is what
    lets the menu and `/buy` share it — this lived inside `ShopView.buy_button`
    as a 160-line if/elif, so a second entry point would have meant a second
    copy of every refund path.

    Two things travel through unparameterised and must stay that way. The
    compensating rollback's arguments come from what
    `purchase_custom_shop_item` *returned* — the price the row actually charged —
    because a tidier signature taking a price would reintroduce exactly the bug
    the compensating-refund rule was written for, and `/buy` renders no menu so
    it would have to invent one. And the ticket capacity checks stay *before* the
    debit: checked, then charged, then the channel, and a channel Discord refused
    refunds.
    """
    item_type = item["type"]
    item_name = shop_item_name(key, item)
    user_id = member.id

    if item_type == "custom":
        if item["template_type"] == "fixed_role":
            role = guild.get_role(int(item["config"]["role_id"]))
            if role in member.roles:
                return PurchaseResult(False, t("shop.already_have_role"))
        purchase = await database.run_write(
            database.purchase_custom_shop_item, guild.id, user_id, key)
        if not purchase["purchased"]:
            # Three reasons, not two: `unavailable` means the row is missing or
            # the operator disabled it, and reporting that as "not enough money"
            # told a member they were broke when they were not. Invisible in the
            # menu, which never renders a disabled item, and reachable through
            # `/buy`, which names an item directly.
            reason = purchase.get("reason")
            if reason == "already_owned":
                return PurchaseResult(False, t("shop.already_have_vault"))
            if reason == "unavailable":
                return PurchaseResult(False, t("shop.item_unavailable"))
            return PurchaseResult(False, t("shop.not_enough_money"))
        if purchase["template_type"] in {"fixed_role", "timed_role"}:
            rollback_args = (
                guild.id, user_id, purchase["price"],
                purchase["template_type"],
                int(purchase["config"]["role_id"]),
                purchase["config"].get("duration_days"),
            )
            role = guild.get_role(int(purchase["config"]["role_id"]))
            if not role or not can_self_assign_role(guild, role):
                await database.run_write(
                    database.rollback_custom_role_purchase, *rollback_args)
                return PurchaseResult(False, t("shop.role_not_found"))
            try:
                await member.add_roles(role)
            except discord.HTTPException:
                await database.run_write(
                    database.rollback_custom_role_purchase, *rollback_args)
                return PurchaseResult(False, t("shop.bot_role_hierarchy_error"))
        suffix = (t("shop.custom_voucher_suffix", voucher_id=purchase["voucher_id"])
                  if purchase.get("voucher_id") else "")
        return PurchaseResult(
            True, t("shop.purchase_success", item_name=item_name) + suffix)

    if item_type == "ticket":
        # The feature gate lives here rather than at the call site, so both
        # callers get it from one place. `is_enabled` is synchronous and
        # cache-backed and needs no interaction; maintenance is already covered
        # by the view's own check and by the command tree's.
        if not is_enabled(guild.id, "rentals"):
            return PurchaseResult(False, t("utils.feature_disabled"))
        if item["limit_check"] == "static":
            if len([e for e in guild.emojis if not e.animated]) >= guild.emoji_limit:
                return PurchaseResult(False, t("shop.static_emoji_limit"))
        elif item["limit_check"] == "animated":
            if len([e for e in guild.emojis if e.animated]) >= guild.emoji_limit:
                return PurchaseResult(False, t("shop.anim_emoji_limit"))
        elif item["limit_check"] == "soundboard":
            sounds = await guild.fetch_soundboard_sounds()
            if len(sounds) >= 8:
                return PurchaseResult(False, t("shop.soundboard_limit"))

        purchase = await database.run(
            database.purchase_upgrade, user_id, item["price"], "debit")
        if not purchase["purchased"]:
            return PurchaseResult(False, t("shop.not_enough_money"))
        ticket_name = f"{item['ticket_prefix']}-{member.name.lower()}"
        channel = await open_ticket(guild, member, ticket_name, "rental")
        if channel is None:
            await database.run_write(
                database.refund_balance, user_id, item["price"])
            return PurchaseResult(False, t("shop.ticket_failed"))
        try:
            embed = discord.Embed(
                title=t("shop.ticket_title", item_name=item_name),
                description=t("shop.ticket_desc"), color=discord.Color.gold())
            await channel.send(embed=embed, view=TicketControl())
        except discord.HTTPException:
            # The channel is open and recorded, so the purchase stands; only its
            # opening message is missing.
            shop_logger.exception(
                "Could not post into a rental ticket (channel_id=%s)", channel.id)
        return PurchaseResult(
            True, t("shop.ticket_opened", channel=channel.mention))

    if item_type == "role":
        # A role item's catalog value is the setting key that names the role,
        # checked against the registry rather than trusted: an unregistered value
        # would otherwise raise inside the callback.
        from settings_registry import SETTING_DEFINITIONS
        setting_key = item["value"]
        role_id = (guild_setting_sync(guild.id, setting_key)
                   if setting_key in SETTING_DEFINITIONS else None)
        role = guild.get_role(role_id)
        if not role or not can_self_assign_role(guild, role):
            return PurchaseResult(False, t("shop.role_not_found"))
        if role in member.roles:
            return PurchaseResult(False, t("shop.already_have_role"))
        purchase = await database.run(
            database.purchase_upgrade, user_id, item["price"], "debit")
        if not purchase["purchased"]:
            return PurchaseResult(False, t("shop.not_enough_money"))
        try:
            await member.add_roles(role)
        except discord.Forbidden:
            await database.run(database.refund_balance, user_id, item["price"])
            return PurchaseResult(False, t("shop.bot_role_hierarchy_error"))
        except Exception:
            await database.run(database.refund_balance, user_id, item["price"])
            raise
    elif item_type == "vault":
        purchase = await database.run(
            database.purchase_upgrade, user_id, item["price"], "vault",
            item["value"])
    elif item_type == "inventory":
        # The same stackable, guild-local row Potato Gacha grants, so a bought
        # consumable and a pulled one are the same object.
        purchase = await database.run_write(
            database.purchase_inventory_item, guild.id, user_id,
            item["price"], key)
    elif item_type == "rent":
        expire_time = (datetime.now() + timedelta(hours=24)).isoformat()
        purchase = await database.run(
            database.purchase_upgrade, user_id, item["price"], "bodyguard",
            expire_time)
    else:
        shop_logger.error("Unknown shop item type (item_key=%s, type=%r)",
                          key, item_type)
        return PurchaseResult(False, t("shop.item_unavailable"))

    if not purchase["purchased"]:
        if purchase.get("reason") == "already_owned":
            return PurchaseResult(False, t("shop.already_have_vault"))
        return PurchaseResult(False, t("shop.not_enough_money"))
    return PurchaseResult(True, t("shop.purchase_success", item_name=item_name))


async def merged_shop_catalog(guild_id):
    """Every item this guild sells, built-in and custom, with live prices.

    One reader for `/shop`, `/buy` and `/buy`'s autocomplete, so the three
    cannot disagree about what is on sale.
    """
    prices, custom = await asyncio.gather(
        database.run_read(database.get_shop_prices, guild_id),
        # In the language the bot speaks, so a custom item follows the
        # `language` setting the way every built-in already does.
        database.run_read(database.get_shop_item_definitions, guild_id,
                          guild_setting_sync(None, "language")),
    )
    hidden = guild_setting_sync(guild_id, "shop_hidden_items") or []
    return add_custom_shop_items(get_shop_items(prices, hidden), custom)


def resolve_shop_item(items, query):
    """Which item a typed name means, or None.

    Discord does not constrain a submitted autocomplete value to the choices it
    offered, so `/buy` has to resolve whatever string arrives. Exact key first,
    then exact localized name, and no fuzzy fallback — guessing which item
    somebody meant to spend money on is not a kindness.
    """
    needle = (query or "").strip().casefold()
    if not needle:
        return None
    for key in items:
        if key.casefold() == needle:
            return key
    for key, item in items.items():
        if shop_item_name(key, item).casefold() == needle:
            return key
    return None


def shop_embed():
    """The menu's own embed, extracted so Back can restore it.

    `select_callback` overwrites it with the confirmation embed, so without this
    returning to the section list would leave "Confirm purchase" on screen.
    """
    return discord.Embed(title=t("shop.shop_title"),
                         description=t("shop.shop_desc"),
                         color=discord.Color.gold())


class ShopView(discord.ui.View):
    """Sections first, then that section's items.

    One Discord select holds 25 options, which used to be the whole shop. Two
    steps make it 25 per section.

    The four components are built **once** in `__init__` and only re-attached by
    `_render()`. Rebuilding them per render would reset the per-`custom_id`
    cooldown in `interaction_check` silently, and they are held as named
    attributes because indexing `self.children[0]` — which this did — is wrong
    the moment there is a second select.
    """

    CATEGORY_ID = "shop:category"
    ITEM_ID = "shop:item"
    BUY_ID = "shop:buy"
    BACK_ID = "shop:back"

    def __init__(self, user_id, items, guild_id=None):
        # Longer than the old 60s: section, then item, then confirm, then buy.
        super().__init__(timeout=120)
        self.user_id = user_id
        self.guild_id = guild_id
        self.categories = build_category_index(items, guild_id=guild_id)
        self.trimmed = len(items) > sum(len(b) for b in self.categories.values())
        self.category = None
        self.selected_item = None
        self.purchase_complete = False
        self.purchase_lock = asyncio.Lock()
        self.last_interactions = {}

        self.category_select = discord.ui.Select(
            custom_id=self.CATEGORY_ID, placeholder=t("shop.category_placeholder"))
        self.category_select.callback = self.category_callback

        self.item_select = discord.ui.Select(custom_id=self.ITEM_ID)
        self.item_select.callback = self.select_callback

        self.buy_btn = discord.ui.Button(
            custom_id=self.BUY_ID, label=t("shop.buy_btn"),
            style=discord.ButtonStyle.green, emoji="💰")
        self.buy_btn.callback = self.buy_button

        self.back_btn = discord.ui.Button(
            custom_id=self.BACK_ID, label=t("shop.back_btn"),
            style=discord.ButtonStyle.secondary)
        self.back_btn.callback = self.back_button

        self._render()

    @property
    def items(self):
        """The items on the shelf being viewed, which is what `buy_button` reads."""
        return self.categories.get(self.category, {})

    def _item_options(self, category):
        options = []
        for key, item in self.categories[category].items():
            name = shop_item_name(key, item)
            desc = shop_item_description(key, item)
            # A select option's label is plain text, so the currency cannot be
            # written into it: a custom emoji renders as its raw `<:name:id>`
            # there. `emoji=` is the supported route, and it is dropped rather
            # than guessed when the configured symbol is not resolvable.
            label = t("shop.select_option_label", name=name, price=item["price"])
            options.append(discord.SelectOption(
                # Truncated because discord.py does not check and Discord
                # answers 400 for the whole component — a >100-character label
                # would take the command down for that guild.
                label=label[:SELECT_LABEL_LIMIT],
                description=desc[:100], value=key,
                emoji=currency_select_emoji(),
            ))
        return options

    def _render(self):
        self.clear_items()
        if self.category is None:
            self.category_select.options = [
                discord.SelectOption(
                    label=t(f"shop.categories.{category}.name")[:SELECT_LABEL_LIMIT],
                    description=t(f"shop.categories.{category}.desc")[:100],
                    value=category)
                for category in self.categories
            ]
            self.add_item(self.category_select)
            return
        self.item_select.options = self._item_options(self.category)
        self.item_select.placeholder = t(
            "shop.item_placeholder",
            category=t(f"shop.categories.{self.category}.name"))
        self.add_item(self.item_select)
        self.add_item(self.buy_btn)
        self.add_item(self.back_btn)

    async def category_callback(self, interaction: discord.Interaction):
        if self.purchase_complete or self.purchase_lock.locked():
            return await interaction.response.defer()
        # A submitted value comes from the client and discord.py does not check
        # it against the options, so this is a membership test rather than a
        # lookup. It is also the invariant that keeps a section non-empty: the
        # only writer of `self.category` reads the dict the options came from.
        chosen = self.category_select.values[0]
        if chosen not in self.categories:
            return await interaction.response.send_message(
                t("shop.category_unavailable"), ephemeral=True)
        self.category = chosen
        # Mandatory, not cosmetic: pick an item under one section, press Back,
        # open another, press Buy — without this you buy the first item.
        self.selected_item = None
        self._render()
        await interaction.response.edit_message(embed=shop_embed(), view=self)

    async def back_button(self, interaction: discord.Interaction):
        if self.purchase_complete or self.purchase_lock.locked():
            return await interaction.response.defer()
        self.category = None
        self.selected_item = None
        self._render()
        await interaction.response.edit_message(embed=shop_embed(), view=self)

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
        if self.purchase_complete or self.purchase_lock.locked():
            return await interaction.response.defer()
        # The bound component, not `self.children[0]`: that index was the item
        # select only while there was one, and discord.py sets `values` on the
        # object it dispatches for, so the attribute is always the right one.
        chosen = self.item_select.values[0]
        if chosen not in self.items:
            return await interaction.response.send_message(
                t("shop.item_unavailable"), ephemeral=True)
        self.selected_item = chosen
        item = self.items[chosen]
        name = shop_item_name(chosen, item)
        desc = shop_item_description(chosen, item)

        embed = discord.Embed(title=t("shop.confirm_title"),
                              color=discord.Color.blue())
        embed.add_field(name=t("shop.item_label"), value=name)
        embed.add_field(name=t("shop.price_label"),
                        value=f"{item['price']}{currency_emoji()}")
        embed.description = t("shop.effect_desc", desc=desc)

        await interaction.response.edit_message(embed=embed, view=self)

    async def buy_button(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.purchase_lock:
            if self.purchase_complete:
                return await interaction.edit_original_response(
                    content=t("shop.purchase_already_processed"),
                    embed=None, view=None)
            if not self.selected_item:
                return await interaction.edit_original_response(
                    content=t("shop.no_item_selected"), embed=None, view=self)
            # Bound once inside the lock: a concurrent select is a separate
            # interaction, so the per-component cooldown does not cover it and
            # the choice could otherwise change mid-purchase.
            key = self.selected_item
            result = await purchase_item(
                interaction.guild, interaction.user, key, self.items[key])
            if result.settled:
                self.purchase_complete = True
            return await interaction.edit_original_response(
                content=result.content, embed=None,
                view=None if result.settled else self)


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rental_cleanup.start() 

    def cog_unload(self):
        self.rental_cleanup.cancel() 

    @commands.hybrid_command(name="shop", description=t("general.cmd_shop"))
    @is_channel("economy_channels")
    async def shop(self, ctx):
        items = await merged_shop_catalog(ctx.guild.id)
        view = ShopView(ctx.author.id, items, guild_id=ctx.guild.id)
        if not view.categories:
            # A view carrying a select with zero options is a Discord 400 that
            # takes the command down, and discord.py does not catch it locally.
            return await ctx.send(t("shop.empty"), ephemeral=True)
        embed = shop_embed()
        if view.trimmed:
            # An ERROR in the journal is not something the operator reads, so
            # say the menu is incomplete where somebody will see it. `/buy`
            # still reaches whatever was hidden.
            embed.set_footer(text=t("shop.items_hidden"))
        await ctx.send(embed=embed, view=view, ephemeral=True)

    async def item_autocomplete(self, interaction: discord.Interaction, current: str):
        """Offer this guild's items, matched on name or key.

        Autocomplete is its own interaction, so it carries the same gates the
        command does. No `is_channel` check, deliberately: `is_channel` is a
        `commands.check` and does not run for an autocomplete anyway, and an
        item name is guild-visible content rather than configuration — this is
        not the disclosure the banner autocomplete's docstring is about.

        Matching is substring here and exact in the command, both through
        `resolve_shop_item`'s catalog, so the list and the purchase cannot
        disagree about what an item is called.
        """
        if maintenance_blocks(interaction.guild, interaction.user) or not is_enabled(
            interaction.guild_id, "shop"
        ):
            return []
        try:
            items = await merged_shop_catalog(interaction.guild_id)
        except database.DatabaseOperationError:
            shop_logger.exception("Shop item autocomplete lookup failed.")
            return []
        needle = (current or "").casefold()
        choices = []
        for key, item in items.items():
            name = shop_item_name(key, item)
            if needle and needle not in key.casefold() and needle not in name.casefold():
                continue
            choices.append(discord.app_commands.Choice(
                # A Choice name caps at 100 characters, and discord.py does not
                # check it either.
                name=t("shop.select_option_label",
                       name=name, price=item["price"])[:SELECT_LABEL_LIMIT],
                value=key))
        return choices[:SELECT_OPTION_LIMIT]

    @commands.hybrid_command(name="buy", description=t("general.cmd_buy"))
    @discord.app_commands.describe(item=t("general.buy_item_description"))
    @discord.app_commands.autocomplete(item=item_autocomplete)
    @is_channel("economy_channels")
    async def buy(self, ctx, *, item: str):
        """Buy one item by name, without walking the menu.

        Resolved against the **untrimmed** catalog, so this reaches an item a
        full section had to hide from `/shop` — which is what makes the render
        trim acceptable rather than a silent loss.

        `PRIVATE`, with a literal `ephemeral=True` on every branch: the tree has
        already deferred ephemerally, so nothing takes `PotatoContext.send`'s
        visibility swap and no deferred original is deleted. Most invocations in
        a mature guild are refusals — not enough money, already own the role,
        vault already larger, emoji limit full — and under a public policy every
        one would flash a placeholder in the economy channel.
        """
        items = await merged_shop_catalog(ctx.guild.id)
        key = resolve_shop_item(items, item)
        if key is None:
            return await ctx.send(t("shop.item_unknown"), ephemeral=True)
        result = await purchase_item(ctx.guild, ctx.author, key, items[key])
        await ctx.send(result.content, ephemeral=True)

    @tasks.loop(hours=24)
    async def rental_cleanup(self):
        # Read per guild rather than installation-wide, so no guild's cleanup pass
        # can ever see, let alone delete, another guild's rented asset.
        for guild in list(self.bot.guilds):
            # Not gated on `rentals`, same rule as the entitlement pass below: a
            # flag decides whether a member may rent something, never whether a
            # rental that has already run out is allowed to end.
            rentals = await database.run(database.get_all_rentals, guild.id)
            await self._expire_guild_rentals(guild, rentals)
        # Entitlements already carry their own guild, so this pass stays global.
        await self._expire_shop_entitlements()

    async def _expire_guild_rentals(self, guild, rentals):
        for r_id, item_type, discord_item_id, expires_at, guild_id in rentals:
            expire_date = datetime.fromisoformat(expires_at)
            now = datetime.now(expire_date.tzinfo) if expire_date.tzinfo else datetime.now()
            if now >= expire_date:

                # Whether the asset was actually found *in this guild*. A
                # rental with no provenance is handed to every guild's pass, so
                # without this the first guild to run deleted the row while the
                # asset lived on in whichever guild owned it — one silently, two
                # after logging a misleading "failed to delete".
                found = False
                try:
                    if item_type == "emoji":
                        emoji = guild.get_emoji(int(discord_item_id))
                        found = emoji is not None
                        if emoji:
                            await emoji.delete(reason=t("shop.rent_expired_reason"))
                    elif item_type == "sound":
                        sound = await guild.fetch_soundboard_sound(int(discord_item_id))
                        found = sound is not None
                        if sound:
                            await sound.delete(reason=t("shop.rent_expired_reason"))
                    elif item_type == "sticker":
                        sticker = await guild.fetch_sticker(int(discord_item_id))
                        found = sticker is not None
                        if sticker:
                            await sticker.delete(reason=t("shop.rent_expired_reason"))
                except discord.NotFound:
                    # Gone from Discord: the row is stale and may be cleared.
                    found = False
                except Exception:
                    # An unknown failure — a transient HTTP error, a permission
                    # problem — is not evidence the asset is absent, so keep the
                    # row and retry on the next pass rather than losing the
                    # record of a rental that is still live.
                    shop_logger.exception(
                        "Failed to delete expired %s with Discord ID %s.",
                        item_type,
                        discord_item_id,
                    )
                    continue

                if guild_id is None and not found:
                    # An unattributed row needs positive evidence that the asset
                    # is ours before its record is destroyed. A row that names
                    # this guild is cleared either way: a missing asset there is
                    # the ordinary "already deleted by hand" case.
                    continue

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
            if not guild:
                continue
            # Not gated on `shop`, for the reason the gacha loop records: expiry
            # of an obligation already made must not depend on the flag that
            # created it, or turning the shop off makes every timed role
            # permanent.
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

    @rental_cleanup.error
    async def rental_cleanup_error(self, error):
        # Started in __init__, so there is no reconnect that brings it back.
        await handle_loop_error(
            self.bot, self.rental_cleanup, "rental_cleanup", error, shop_logger,
        )

async def setup(bot):
    await bot.add_cog(Shop(bot))
