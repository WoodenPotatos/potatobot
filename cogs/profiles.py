import discord
import os
import sys

from cogs.utils import config

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database

from discord.ext import commands
from datetime import datetime
from cogs.utils import display_member_name, guild_member_ids, is_channel, t
from feature_access import require_interaction_feature

# These views are built per invocation and are not persistent, so a finite
# timeout is what lets discord.py drop them from its message view store.
PROFILE_VIEW_TIMEOUT = 15 * 60

class LvlsView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=PROFILE_VIEW_TIMEOUT)
        self.guild = guild
        
        # Construct the button at runtime so its label uses the active locale.
        btn_refresh = discord.ui.Button(label=t("profiles.refresh_btn"), style=discord.ButtonStyle.success, emoji="🔄")
        btn_refresh.callback = self.refresh_btn
        self.add_item(btn_refresh)

    async def generate_embed(self):
        results = await database.run(
            database.get_top_levels, guild_member_ids(self.guild), 10
        )

        if not results:
            return None

        description = ""
        for i, (user_id, level, xp) in enumerate(results, start=1):
            name = display_member_name(self.guild, user_id)
            description += t("profiles.lvl_row", index=i, name=name, level=level, xp=xp)

        return discord.Embed(title=t("profiles.lvls_title"), description=description, color=discord.Color.purple())

    async def refresh_btn(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "profiles"):
            return
        await interaction.response.defer()
        fresh_embed = await self.generate_embed()
        if fresh_embed:
            await interaction.edit_original_response(embed=fresh_embed, view=self)
        else:
            await interaction.followup.send(t("profiles.no_levels_stored"), ephemeral=True)

class RanksView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=PROFILE_VIEW_TIMEOUT)
        self.guild = guild

        btn_refresh = discord.ui.Button(label=t("profiles.refresh_btn"), style=discord.ButtonStyle.success, emoji="🔄")
        btn_refresh.callback = self.refresh_btn
        self.add_item(btn_refresh)

    async def generate_embed(self):
        results = await database.run(
            database.get_top_balances, guild_member_ids(self.guild), 10
        )

        if not results:
            return None

        leaderboard_str = ""
        for index, (user_id, balance, level) in enumerate(results, start=1):
            member = self.guild.get_member(user_id)
            
            booster_tag = " 💎" if (member and member.premium_since) else ""
            name = display_member_name(self.guild, user_id)
            
            medal = "🥇" if index == 1 else "🥈" if index == 2 else "🥉" if index == 3 else f"#{index}"
            leaderboard_str += t("profiles.rank_row", medal=medal, name=name, booster_tag=booster_tag, balance=balance, level=level)

        return discord.Embed(title=t("profiles.ranks_title"), description=leaderboard_str, color=discord.Color.gold())

    async def refresh_btn(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "profiles"):
            return
        await interaction.response.defer()
        fresh_embed = await self.generate_embed()
        if fresh_embed:
            await interaction.edit_original_response(embed=fresh_embed, view=self)
        else:
            await interaction.followup.send(t("profiles.leaderboard_empty"), ephemeral=True)

class ProfileView(discord.ui.View):
    def __init__(self, member):
        super().__init__(timeout=PROFILE_VIEW_TIMEOUT)
        self.member = member

        btn_refresh = discord.ui.Button(label=t("profiles.refresh_btn"), style=discord.ButtonStyle.primary, emoji="🔄")
        btn_refresh.callback = self.refresh_btn
        self.add_item(btn_refresh)

    async def generate_embed(self):
        result = await database.run(database.get_user_profile, self.member.id)
        
        if not result:
            return None 
            
        lvl, xp, bal, wins, losses, streak_count, last_streak_update, rob_bonus = result
        rank = await database.run(
            database.get_user_rank, xp, guild_member_ids(self.member.guild)
        )

        prev_lvl_xp = 10 * ((lvl - 1) ** 2)
        next_lvl_xp = 10 * (lvl ** 2)
        xp_needed_total = max(1, next_lvl_xp - prev_lvl_xp)
        xp_in_this_level = xp - prev_lvl_xp
        percent = max(0, min(xp_in_this_level / xp_needed_total, 1.0))
        
        white_tiles = int(percent * 20)
        black_tiles = 20 - white_tiles
        bar = "⬜" * white_tiles + "⬛" * black_tiles

        streak_display = t("profiles.no_active_streak")
        streak_count = streak_count or 0

        if streak_count > 0 and last_streak_update:
            last_date = datetime.fromisoformat(last_streak_update).date()
            today = datetime.now().date()
            diff = (today - last_date).days

            if diff == 0 or diff == 1:
                streak_display = t("profiles.streak_days", count=streak_count)
            elif diff == 2:
                streak_display = t("profiles.streak_days_expiring", count=streak_count)
            elif diff >= 3:
                streak_display = t("profiles.streak_lost")

        embed = discord.Embed(title=t("profiles.profile_title", name=self.member.display_name), color=discord.Color.blue())
        pfp = self.member.avatar.url if self.member.avatar else self.member.default_avatar.url
        embed.set_thumbnail(url=pfp)
        
        embed.add_field(name=t("profiles.wallet_label"), value=t("profiles.wallet_value", balance=bal), inline=True)
        embed.add_field(name=t("profiles.global_rank_label"), value=t("profiles.global_rank_value", rank=rank), inline=True)
        embed.add_field(name=t("profiles.level_label"), value=t("profiles.level_value", level=lvl), inline=True)
        
        inventory = t("profiles.lockpick_active") if rob_bonus > 0.0 else t("profiles.empty_inventory")
        
        embed.add_field(name=t("profiles.daily_streak_label"), value=streak_display, inline=True)
        embed.add_field(name=t("profiles.inventory_label"), value=inventory, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True) 
        
        embed.add_field(name=t("profiles.progress_label", percent=int(percent*100)), 
                        value=t("profiles.progress_value", bar=bar, xp=xp, next_xp=next_lvl_xp), inline=False)
        
        total_games = wins + losses
        rate = (wins / total_games * 100) if total_games > 0 else 0
        embed.add_field(name=t("profiles.casino_stats_label"), 
                        value=t("profiles.casino_stats_value", wins=wins, losses=losses, rate=rate), inline=False)
        return embed

    async def refresh_btn(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "profiles"):
            return
        await interaction.response.defer()
        fresh_embed = await self.generate_embed()
        if fresh_embed:
            await interaction.edit_original_response(embed=fresh_embed, view=self)
        else:
            await interaction.followup.send(t("profiles.profile_not_found"), ephemeral=True)

class Profiles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="lvls", description=t("general.cmd_lvls"))
    @is_channel("levels_channels")
    async def toplvl(self, ctx):
        view = LvlsView(ctx.guild)
        embed = await view.generate_embed()
    
        if embed:
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(t("profiles.no_levels_stored"))

    @commands.hybrid_command(name="ranks", description=t("general.cmd_ranks"))
    @is_channel("levels_channels")
    async def top(self, ctx):
        view = RanksView(ctx.guild)
        embed = await view.generate_embed()
    
        if embed:
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(t("profiles.leaderboard_empty"))

    @commands.hybrid_command(name="topstreak", description=t("general.cmd_topstreak"))
    @is_channel("everydle_channel")
    async def topstreak(self, ctx):
        top_streakers = await database.run(
            database.get_top_streaks, guild_member_ids(ctx.guild), 10
        )

        if not top_streakers:
            return await ctx.send(t("profiles.no_streaks_yet"))
        
        embed = discord.Embed(
            title=t("profiles.topstreak_title"),
            description=t("profiles.topstreak_desc"),
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild.icon else None)

        board_text = ""
        for index, (user_id, streak) in enumerate(top_streakers, start=1):
            name = display_member_name(ctx.guild, user_id)
            
            if index == 1: medal = "🥇"
            elif index == 2: medal = "🥈"
            elif index == 3: medal = "🥉"
            else: medal = f"**{index}.**"

            board_text += t("profiles.streak_leaderboard_row", medal=medal, name=name, streak=streak)

        embed.description += board_text
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="profile", description=t("general.cmd_profile"))
    @is_channel("levels_channels")
    async def profile(self, ctx, member: discord.Member = None):
        member = member or ctx.author
    
        view = ProfileView(member)
        embed = await view.generate_embed()
    
        if embed:
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(t("profiles.profile_empty_chat_more"))
 
async def setup(bot):
    await bot.add_cog(Profiles(bot))
