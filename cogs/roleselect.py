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
from cogs.utils import BoundedCooldownMap, can_self_assign_role, t, config
from feature_access import require_interaction_feature

role_logger = logging.getLogger("PotatoBot.RoleSelect")

ROLE_ACTION_COOLDOWN = 3
role_action_times = BoundedCooldownMap()

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
    def __init__(self):
        super().__init__(timeout=None) 
        game_data = config.get("game_roles", {})
        for game_name, data in game_data.items():
            self.add_item(GameRoleButton(game_name, data))

class GameRoleButton(discord.ui.Button):
    def __init__(self, game_name, data):
        if isinstance(data, dict):
            self.role_id = data.get("id")
            button_emoji = data.get("emoji")
        else:
            self.role_id = data
            button_emoji = None

        super().__init__(
            label=game_name,
            style=discord.ButtonStyle.secondary,
            emoji=button_emoji,
            custom_id=f"role_{game_name.replace(' ', '_').lower()}"
        )
        self.game_name = game_name

    async def callback(self, interaction: discord.Interaction):
        await toggle_configured_role(interaction, self.role_id, self.game_name)

class NewsRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        news_data = config.get("news_roles", {})
        for news_name, data in news_data.items():
            self.add_item(NewsRoleButton(news_name, data))

class NewsRoleButton(discord.ui.Button):
    def __init__(self, news_name, data):
        if isinstance(data, dict):
            self.role_id = data.get("id")
            button_emoji = data.get("emoji")
        else:
            self.role_id = data
            button_emoji = None

        super().__init__(
            label=news_name,
            style=discord.ButtonStyle.secondary,
            emoji=button_emoji, 
            custom_id=f"role_{news_name.replace(' ', '_').lower()}"
        )
        self.news_name = news_name

    async def callback(self, interaction: discord.Interaction):
        await toggle_configured_role(interaction, self.role_id, self.news_name)

class ThemesRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        themes_data = config.get("themes_roles", {})
        for theme_name, data in themes_data.items():
            self.add_item(ThemesRoleButton(theme_name, data))

class ThemesRoleButton(discord.ui.Button):
    def __init__(self, theme_name, data):
        if isinstance(data, dict):
            self.role_id = data.get("id")
            button_emoji = data.get("emoji")
        else:
            self.role_id = data
            button_emoji = None

        super().__init__(
            label=theme_name,
            style=discord.ButtonStyle.secondary,
            emoji=button_emoji,
            custom_id=f"role_{theme_name.replace(' ', '_').lower()}"
        )
        self.theme_name = theme_name

    async def callback(self, interaction: discord.Interaction):
        await toggle_configured_role(interaction, self.role_id, self.theme_name)

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
        view = GameRoleView() 
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
        
            new_view = GameRoleView()
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
        view = NewsRoleView() 
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
        
            new_view = NewsRoleView()
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
        view = ThemesRoleView() 
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
        
            new_view = ThemesRoleView()
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
