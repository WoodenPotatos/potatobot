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
from cogs.utils import (BoundedCooldownMap, can_self_assign_role, t,
                        guild_setting_sync)
from feature_access import require_interaction_feature

role_logger = logging.getLogger("PotatoBot.RoleSelect")

ROLE_ACTION_COOLDOWN = 3
role_action_times = BoundedCooldownMap()

# Which typed setting backs each menu. The custom_id carries the entry's label,
# so this is also how a click finds the role it should toggle.
MENU_SETTINGS = ("game_roles", "news_roles", "theme_roles")


def menu_entries(guild_id, setting_key) -> dict:
    """One guild's menu, as {label: {"id": role_id, "emoji": str}}."""
    stored = guild_setting_sync(guild_id, setting_key) if guild_id else {}
    return stored if isinstance(stored, dict) else {}


def registered_menu_labels(setting_key) -> dict:
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
            for label, entry in menu_entries(int(guild["id"]), setting_key).items():
                labels.setdefault(label, entry if isinstance(entry, dict) else {})
    except Exception:
        # Registration must not be what stops the bot starting. A label missing
        # here means that one button answers nothing until the next restart,
        # which is the same gap the config-based version had.
        role_logger.exception("Could not enumerate role menu labels (%s)",
                              setting_key)
    return labels


def resolve_menu_role(interaction, label) -> int | None:
    """The role this label names in the guild the click came from.

    Searched across all three menus, because the `custom_id` shape already on
    posted messages does not say which menu a button belongs to.
    """
    for setting_key in MENU_SETTINGS:
        entry = menu_entries(interaction.guild_id, setting_key).get(label)
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

class GameRoleView(discord.ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        # A guild renders its own menu; no guild means this is the
        # instance registered for routing, which takes every label.
        game_data = (menu_entries(guild_id, "game_roles") if guild_id
                      else registered_menu_labels("game_roles"))
        for game_name, data in game_data.items():
            self.add_item(GameRoleButton(game_name, data))

class GameRoleButton(discord.ui.Button):
    """One menu entry. Carries its label and emoji, never its role id.

    The role is looked up per interaction, so this instance is correct for every
    guild whose menu uses this label — a persistent view is shared by every
    message it serves and must hold no per-message state.
    """

    def __init__(self, game_name, data):
        button_emoji = data.get("emoji") if isinstance(data, dict) else None

        super().__init__(
            label=game_name,
            style=discord.ButtonStyle.secondary,
            emoji=button_emoji,
            custom_id=f"role_{game_name.replace(' ', '_').lower()}"
        )
        self.game_name = game_name

    async def callback(self, interaction: discord.Interaction):
        label = self.game_name
        await toggle_configured_role(
            interaction, resolve_menu_role(interaction, label), label)

class NewsRoleView(discord.ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        # A guild renders its own menu; no guild means this is the
        # instance registered for routing, which takes every label.
        news_data = (menu_entries(guild_id, "news_roles") if guild_id
                      else registered_menu_labels("news_roles"))
        for news_name, data in news_data.items():
            self.add_item(NewsRoleButton(news_name, data))

class NewsRoleButton(discord.ui.Button):
    """One menu entry. Carries its label and emoji, never its role id.

    The role is looked up per interaction, so this instance is correct for every
    guild whose menu uses this label — a persistent view is shared by every
    message it serves and must hold no per-message state.
    """

    def __init__(self, news_name, data):
        button_emoji = data.get("emoji") if isinstance(data, dict) else None

        super().__init__(
            label=news_name,
            style=discord.ButtonStyle.secondary,
            emoji=button_emoji, 
            custom_id=f"role_{news_name.replace(' ', '_').lower()}"
        )
        self.news_name = news_name

    async def callback(self, interaction: discord.Interaction):
        label = self.news_name
        await toggle_configured_role(
            interaction, resolve_menu_role(interaction, label), label)

class ThemesRoleView(discord.ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None)
        # A guild renders its own menu; no guild means this is the
        # instance registered for routing, which takes every label.
        themes_data = (menu_entries(guild_id, "theme_roles") if guild_id
                      else registered_menu_labels("theme_roles"))
        for theme_name, data in themes_data.items():
            self.add_item(ThemesRoleButton(theme_name, data))

class ThemesRoleButton(discord.ui.Button):
    """One menu entry. Carries its label and emoji, never its role id.

    The role is looked up per interaction, so this instance is correct for every
    guild whose menu uses this label — a persistent view is shared by every
    message it serves and must hold no per-message state.
    """

    def __init__(self, theme_name, data):
        button_emoji = data.get("emoji") if isinstance(data, dict) else None

        super().__init__(
            label=theme_name,
            style=discord.ButtonStyle.secondary,
            emoji=button_emoji,
            custom_id=f"role_{theme_name.replace(' ', '_').lower()}"
        )
        self.theme_name = theme_name

    async def callback(self, interaction: discord.Interaction):
        label = self.theme_name
        await toggle_configured_role(
            interaction, resolve_menu_role(interaction, label), label)

class RoleSelect(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(GameRoleView())
        self.bot.add_view(NewsRoleView())
        self.bot.add_view(ThemesRoleView())

    @commands.hybrid_command(name="setup_games", description=t("general.cmd_setup_games"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_games(self, ctx):
        embed = discord.Embed(
            title=t("roleselect.games_title"),
            description=t("roleselect.games_desc"),
            color=discord.Color.green()
        )
        view = GameRoleView(ctx.guild.id)
        self.bot.add_view(view)
        await ctx.channel.send(embed=embed, view=view)
        if ctx.interaction:
            await ctx.send(t("utils.command_completed"), ephemeral=True)

    @commands.hybrid_command(name="update_games", description=t("general.cmd_update_games"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_games(self, ctx, message_id: str):
        try:
            msg_id_int = int(message_id.strip())
            message = await ctx.channel.fetch_message(msg_id_int)
        
            new_view = GameRoleView(ctx.guild.id)
            self.bot.add_view(new_view)
            new_embed = discord.Embed(
                title=t("roleselect.games_title"),
                description=t("roleselect.games_desc"),
                color=discord.Color.green()
            )
        
            await message.edit(embed=new_embed, view=new_view)
            await ctx.send(t("roleselect.games_updated"), ephemeral=True)
        
        except Exception:
            role_logger.exception("Failed to update the game role menu.")
            await ctx.send(t("roleselect.update_failed"), ephemeral=True)

    @commands.hybrid_command(name="setup_news", description=t("general.cmd_setup_news"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_news(self, ctx):
        embed = discord.Embed(
            title=t("roleselect.news_title"),
            description=t("roleselect.news_desc"),
            color=discord.Color.green()
        )
        view = NewsRoleView(ctx.guild.id)
        self.bot.add_view(view)
        await ctx.channel.send(embed=embed, view=view)
        if ctx.interaction:
            await ctx.send(t("utils.command_completed"), ephemeral=True)

    @commands.hybrid_command(name="update_news", description=t("general.cmd_update_news"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_news(self, ctx, message_id: str):
        try:
            msg_id_int = int(message_id.strip())
            message = await ctx.channel.fetch_message(msg_id_int)
        
            new_view = NewsRoleView(ctx.guild.id)
            self.bot.add_view(new_view)
            new_embed = discord.Embed(
                title=t("roleselect.news_title"),
                description=t("roleselect.news_desc"),
                color=discord.Color.green()
            )
        
            await message.edit(embed=new_embed, view=new_view)
            await ctx.send(t("roleselect.news_updated"), ephemeral=True)
        
        except Exception:
            role_logger.exception("Failed to update the news role menu.")
            await ctx.send(t("roleselect.update_failed"), ephemeral=True)

    @commands.hybrid_command(name="setup_themes", description=t("general.cmd_setup_themes"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_themes(self, ctx):
        embed = discord.Embed(
            title=t("roleselect.themes_title"),
            description=t("roleselect.themes_desc"),
            color=discord.Color.green()
        )
        view = ThemesRoleView(ctx.guild.id)
        self.bot.add_view(view)
        await ctx.channel.send(embed=embed, view=view)
        if ctx.interaction:
            await ctx.send(t("utils.command_completed"), ephemeral=True)

    @commands.hybrid_command(name="update_themes", description=t("general.cmd_update_themes"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_themes(self, ctx, message_id: str):
        try:
            msg_id_int = int(message_id.strip())
            message = await ctx.channel.fetch_message(msg_id_int)
        
            new_view = ThemesRoleView(ctx.guild.id)
            self.bot.add_view(new_view)
            new_embed = discord.Embed(
                title=t("roleselect.themes_title"),
                description=t("roleselect.themes_desc"),
                color=discord.Color.green()
            )
        
            await message.edit(embed=new_embed, view=new_view)
            await ctx.send(t("roleselect.themes_updated"), ephemeral=True)
        
        except Exception:
            role_logger.exception("Failed to update the theme role menu.")
            await ctx.send(t("roleselect.update_failed"), ephemeral=True)

async def setup(bot):
    await bot.add_cog(RoleSelect(bot))
