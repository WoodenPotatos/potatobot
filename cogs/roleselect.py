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
import database
from cogs.utils import BoundedCooldownMap, can_self_assign_role, t
from feature_access import require_interaction_feature

role_logger = logging.getLogger("PotatoBot.RoleSelect")

ROLE_ACTION_COOLDOWN = 3
role_action_times = BoundedCooldownMap()

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

    async def post_menu(self, ctx, menu_key):
        if not menu_entries(ctx.guild.id, menu_key):
            await ctx.send(t("roleselect.menu_empty"), ephemeral=True)
            return
        view = RoleMenuView(ctx.guild.id, menu_key)
        # The routing instance registered at startup knows every label that
        # existed then. A label added since is not in it, so the freshly built
        # view has to be registered too or its button answers nothing after the
        # next restart-free edit.
        self.bot.add_view(view)
        message = await ctx.channel.send(
            embed=await self.menu_embed(ctx.guild.id, menu_key), view=view)
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
            view = RoleMenuView(ctx.guild.id, menu_key)
            self.bot.add_view(view)
            await message.edit(
                embed=await self.menu_embed(ctx.guild.id, menu_key), view=view)
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
