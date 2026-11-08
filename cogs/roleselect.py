"""Game, news and theme role menus.

A menu's buttons are persistent, so the registered instance carries no role id
at all: the label is the `custom_id` and the role is resolved per interaction
from the guild the click came from. The rows the menus are built from live in
`managed_messages`.

Rules that bind changes here: docs/subsystems/managed_messages.md
"""

import asyncio
import discord
import logging
import os
import sys
import time

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from discord.ext import commands
from core import database
from cogs.utils import BoundedCooldownMap, can_self_assign_role, t
from core.bounded import BoundedValueMap
from core.feature_access import (is_enabled, maintenance_blocks,
                                 require_interaction_feature)
from core.managed_messages import emoji_key, role_menu_mode

role_logger = logging.getLogger("PotatoBot.RoleSelect")

ROLE_ACTION_COOLDOWN = 3
role_action_times = BoundedCooldownMap()
# One lock per (guild, member) for reaction menus. A reaction cannot be refused
# with a reply, so fast clicks are serialised rather than dropped: dropping one
# would leave a reaction on the message with no role behind it.
reaction_locks = BoundedValueMap()


def _reaction_lock(guild_id, user_id) -> asyncio.Lock:
    key = (guild_id, user_id)
    lock = reaction_locks.get(key)
    if lock is None:
        lock = reaction_locks[key] = asyncio.Lock()
    return lock

def menu_entries(guild_id, menu_key) -> dict:
    """One guild's menu, as {label: {"id": role_id, "emoji": str}}.

    Reads `managed_messages`, which is what the dashboard edits and what records
    where the menu was posted. It used to read a typed setting, which could
    describe the buttons but never the message they were on.
    """
    if not guild_id:
        return {}
    stored = database.get_managed_message(int(guild_id), "role_menu", menu_key)
    if not stored:
        return {}
    return {entry["label"]: {"id": entry["role_id"], "emoji": entry["emoji"]}
            for entry in stored["entries"]}


def guild_menu_keys(guild_id) -> list[str]:
    """Every role menu this guild has, in a stable order."""
    if not guild_id:
        return []
    return [menu["menu_key"] for menu in
            database.list_managed_messages(int(guild_id), "role_menu")]


def registered_menu_labels() -> dict:
    """Every label any guild uses for this menu, with no role ids attached.

    A persistent view routes a click by `custom_id`, and these custom_ids are
    derived from the operator's own labels — so the instance handed to
    `bot.add_view()` cannot enumerate them from one guild without being wrong for
    every other one. It takes the union instead, and carries no role id at all:
    the role is resolved per interaction from the guild the click came from,
    which is what keeps one shared instance correct everywhere and keeps
    per-message state off a persistent view.
    """
    labels = {}
    try:
        for guild in database.get_active_guilds():
            for menu in database.list_managed_messages(int(guild["id"]),
                                                       "role_menu"):
                for entry in menu["entries"]:
                    labels.setdefault(entry["label"],
                                      {"emoji": entry["emoji"]})
    except Exception:
        # Registration must not be what stops the bot starting. A label missing
        # here means that one button answers nothing until the next restart,
        # which is the same gap the config-based version had.
        role_logger.exception("Could not enumerate role menu labels")
    return labels


def resolve_menu_role(interaction, label) -> int | None:
    """The role this label names in the guild the click came from.

    Searched across every menu the guild has, because the `custom_id` shape
    already on posted messages does not say which menu a button belongs to.
    """
    for menu_key in guild_menu_keys(interaction.guild_id):
        entry = menu_entries(interaction.guild_id, menu_key).get(label)
        if entry is None:
            continue
        role_id = entry.get("id") if isinstance(entry, dict) else entry
        try:
            return int(role_id) if role_id else None
        except (TypeError, ValueError):
            return None
    return None


async def toggle_configured_role(interaction, role_id, display_name):
    if not await require_interaction_feature(interaction, "role_menus"):
        return
    now = time.monotonic()
    retry_after = ROLE_ACTION_COOLDOWN - (
        now - role_action_times.get(interaction.user.id, 0)
    )
    if retry_after > 0:
        return await interaction.response.send_message(
            t("roleselect.cooldown", seconds=int(retry_after) + 1), ephemeral=True
        )

    role = interaction.guild.get_role(role_id)
    if not role:
        return await interaction.response.send_message(
            t("roleselect.role_not_found"), ephemeral=True
        )

    if not can_self_assign_role(interaction.guild, role):
        return await interaction.response.send_message(
            t("roleselect.role_restricted"), ephemeral=True
        )

    role_action_times[interaction.user.id] = now
    try:
        if role in interaction.user.roles:
            await interaction.user.remove_roles(
                role, reason=t("roleselect.audit_reason")
            )
            message = t("roleselect.role_removed", name=display_name)
        else:
            await interaction.user.add_roles(
                role, reason=t("roleselect.audit_reason")
            )
            message = t("roleselect.role_added", name=display_name)
        await interaction.response.send_message(message, ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            t("roleselect.role_restricted"), ephemeral=True
        )

# Self-service role menus validate every configured role again at click time.

class RoleMenuView(discord.ui.View):
    """One guild's role menu, or the instance that routes every guild's clicks.

    There used to be three near-identical classes, one per fixed menu. A guild
    may have any number of menus now, so binding a class to a menu stopped being
    possible — and it was never necessary: a click routes by `custom_id`, and the
    role behind a label is resolved per interaction from the guild the click came
    from.

    `menu_key=None` builds the routing instance handed to `bot.add_view()`, which
    takes the union of every label in every guild. It carries no role id, so one
    shared instance is correct everywhere and holds no per-message state.
    """

    def __init__(self, guild_id: int = None, menu_key: str = None):
        super().__init__(timeout=None)
        entries = (menu_entries(guild_id, menu_key) if guild_id and menu_key
                   else registered_menu_labels())
        for label, data in entries.items():
            self.add_item(RoleMenuButton(label, data))


class RoleMenuButton(discord.ui.Button):
    """One menu entry. Carries its label and emoji, never its role id.

    The role is looked up per interaction, so this instance is correct for every
    guild whose menu uses this label — a persistent view is shared by every
    message it serves and must hold no per-message state.
    """

    def __init__(self, label, data):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            emoji=(data.get("emoji") or None) if isinstance(data, dict) else None,
            custom_id=f"role_{label.replace(' ', '_').lower()}",
        )
        self.menu_label = label

    async def callback(self, interaction: discord.Interaction):
        await toggle_configured_role(
            interaction, resolve_menu_role(interaction, self.menu_label),
            self.menu_label)


class RoleSelect(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # One routing instance for every menu in every guild: a click is routed
        # by custom_id and the role resolved per interaction, so there is nothing
        # per-menu for a registered view to hold.
        self.bot.add_view(RoleMenuView())

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self.apply_reaction(payload, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self.apply_reaction(payload, added=False)

    async def apply_reaction(self, payload, *, added: bool):
        """A reaction on a reaction-mode role menu grants or removes its role.

        Routed by (message, emoji) to the stored menu, never by anything the
        message carries, so nothing per-message lives in memory. Every refusal a
        button gives applies here too, but silently: a reaction has no reply.
        Raw events, so a menu posted before the last restart still works
        without its message being cached.
        """
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", None):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or not is_enabled(guild.id, "role_menus"):
            return
        member = payload.member if added else guild.get_member(payload.user_id)
        if member is None or member.bot or maintenance_blocks(guild, member):
            return
        menu = await database.run_read(database.get_role_menu_by_message,
                                       guild.id, payload.message_id)
        if not menu or role_menu_mode(menu.get("options")) != "reactions":
            return
        key = emoji_key(str(payload.emoji))
        entry = next((item for item in menu["entries"]
                      if emoji_key(item["emoji"]) == key), None)
        role = guild.get_role(int(entry["role_id"])) if entry else None
        if role is None or not can_self_assign_role(guild, role):
            return
        reason = t("roleselect.audit_reason")
        async with _reaction_lock(guild.id, member.id):
            try:
                if not added:
                    if role in member.roles:
                        await member.remove_roles(role, reason=reason)
                    return
                if role not in member.roles:
                    await member.add_roles(role, reason=reason)
                if (menu.get("options") or {}).get("single_choice"):
                    await self._keep_single_choice(guild, member, menu, entry, payload,
                                                   reason)
            except discord.HTTPException as error:
                role_logger.warning(
                    "Reaction role change failed (guild_id=%s, menu=%s, error=%s)",
                    guild.id, menu["menu_key"], type(error).__name__)

    async def _keep_single_choice(self, guild, member, menu, chosen, payload, reason):
        """Pick-one: the member's other roles from this menu go, and so do their
        other reactions on it, so the message shows what they hold. Removing a
        reaction needs Manage Messages; without it the roles are still right."""
        others = [item for item in menu["entries"] if item is not chosen]
        drop = [role for role in (guild.get_role(int(item["role_id"])) for item in others)
                if role is not None and role in member.roles
                and can_self_assign_role(guild, role)]
        if drop:
            await member.remove_roles(*drop, reason=reason)
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            return
        message = channel.get_partial_message(payload.message_id)
        for item in others:
            try:
                await message.remove_reaction(item["emoji"], member)
            except discord.Forbidden:
                return
            except discord.HTTPException:
                continue

    async def menu_embed(self, guild_id, menu_key):
        """The embed for one menu, operator text first.

        A menu carries its own title and body since schema 12, so the dashboard
        can name it. The seeded menus have neither, and a menu created from a
        command has neither either, so the shipped locale text is what they fall
        back to — which keeps `/setup_games` producing exactly what it produced
        before anyone opened the dashboard.
        """
        stored = await database.run_read(database.get_managed_message,
                                         guild_id, "role_menu", menu_key)
        title = (stored or {}).get("title") or t(f"roleselect.{menu_key}_title")
        body = (stored or {}).get("body") or t(f"roleselect.{menu_key}_desc")
        colour = (stored or {}).get("colour")
        return discord.Embed(
            title=title, description=body,
            color=discord.Color(colour) if colour is not None
            else discord.Color.green())

    async def menu_parts(self, guild_id, menu_key):
        """The embed and view for one menu, honouring how it is offered.

        A reaction menu has no view and lists its emoji under the body, as the
        dashboard's renderer does; the commands must not post a button menu the
        operator set to reactions.
        """
        from core.managed_messages import reaction_legend
        stored = await database.run_read(database.get_managed_message,
                                         guild_id, "role_menu", menu_key)
        embed = await self.menu_embed(guild_id, menu_key)
        if stored and role_menu_mode(stored.get("options")) == "reactions":
            embed.description = "\n\n".join(
                part for part in (embed.description,
                                  reaction_legend(stored["entries"])) if part)
            return stored, embed, None
        view = RoleMenuView(guild_id, menu_key)
        # The routing instance registered at startup knows every label that
        # existed then. A label added since is not in it, so the freshly built
        # view has to be registered too or its button answers nothing after the
        # next restart-free edit.
        self.bot.add_view(view)
        return stored, embed, view

    async def post_menu(self, ctx, menu_key):
        from core.control_actions import sync_menu_reactions
        if not menu_entries(ctx.guild.id, menu_key):
            await ctx.send(t("roleselect.menu_empty"), ephemeral=True)
            return
        stored, embed, view = await self.menu_parts(ctx.guild.id, menu_key)
        message = await ctx.channel.send(embed=embed, view=view)
        if stored:
            await sync_menu_reactions(message, stored)
        # Remember it, so `/update_*` and the dashboard both know which message
        # is the live one without being told.
        await database.run_write(database.record_managed_post, ctx.guild.id,
                                 "role_menu", menu_key, ctx.channel.id,
                                 message.id)
        if ctx.interaction:
            await ctx.send(t("utils.command_completed"), ephemeral=True)

    async def refresh_menu(self, ctx, menu_key):
        """Edit the posted menu in place.

        The message id used to be an argument. It is stored now, so the operator
        is no longer asked to copy a snowflake out of Discord — and the failure
        mode when nothing is stored is a specific "post it first" rather than a
        generic failure.
        """
        stored = await database.run_read(database.get_managed_message,
                                         ctx.guild.id, "role_menu", menu_key)
        if not stored or not stored.get("message_id"):
            await ctx.send(t("roleselect.menu_not_posted"), ephemeral=True)
            return
        try:
            channel = (self.bot.get_channel(int(stored["channel_id"]))
                       if stored.get("channel_id") else ctx.channel)
            message = await (channel or ctx.channel).fetch_message(
                int(stored["message_id"]))
            from core.control_actions import sync_menu_reactions
            stored, embed, view = await self.menu_parts(ctx.guild.id, menu_key)
            await message.edit(embed=embed, view=view)
            await sync_menu_reactions(message, stored)
            await ctx.send(t(f"roleselect.{menu_key}_updated"), ephemeral=True)
        except Exception:
            role_logger.exception("Failed to update the %s role menu.", menu_key)
            await ctx.send(t("roleselect.update_failed"), ephemeral=True)

    @commands.hybrid_command(name="setup_games", description=t("general.cmd_setup_games"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_games(self, ctx):
        await self.post_menu(ctx, "games")

    @commands.hybrid_command(name="update_games", description=t("general.cmd_update_games"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_games(self, ctx):
        await self.refresh_menu(ctx, "games")

    @commands.hybrid_command(name="setup_news", description=t("general.cmd_setup_news"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_news(self, ctx):
        await self.post_menu(ctx, "news")

    @commands.hybrid_command(name="update_news", description=t("general.cmd_update_news"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_news(self, ctx):
        await self.refresh_menu(ctx, "news")

    @commands.hybrid_command(name="setup_themes", description=t("general.cmd_setup_themes"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_themes(self, ctx):
        await self.post_menu(ctx, "themes")

    @commands.hybrid_command(name="update_themes", description=t("general.cmd_update_themes"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_themes(self, ctx):
        await self.refresh_menu(ctx, "themes")

async def setup(bot):
    await bot.add_cog(RoleSelect(bot))
