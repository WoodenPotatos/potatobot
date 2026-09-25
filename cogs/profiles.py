import discord
import os
import sys


# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from core import database

from discord.ext import commands
from datetime import datetime
from core.clock import local_date, local_time, parse_stored, utc_now
from cogs.utils import (display_member_name, guild_member_ids, is_channel,
                        voice_reward_block, t)
from core.feature_access import is_enabled, require_interaction_feature

# These views are built per invocation and are not persistent, so a finite
# timeout is what lets discord.py drop them from its message view store.
PROFILE_VIEW_TIMEOUT = 15 * 60

LEADERBOARD_CHOICES = [
    discord.app_commands.Choice(name=t("profiles.leaderboard_choice_wealth"), value="wealth"),
    discord.app_commands.Choice(name=t("profiles.leaderboard_choice_levels"), value="levels"),
    discord.app_commands.Choice(name=t("profiles.leaderboard_choice_streaks"), value="streaks"),
    discord.app_commands.Choice(name=t("profiles.leaderboard_choice_pity"), value="pity"),
]

# A member needs this many 5-star pulls on the banner before the luck board
# will rank them -- one early lucky pull would otherwise top it on no
# evidence. Luck is the average pulls spent per 5-star, not the live pity
# counter `/pity` shows.
PITY_LEADERBOARD_MINIMUM_FIVE_STARS = 3


class LeaderboardView(discord.ui.View):
    """One view behind /leaderboard's three types.

    Replaces the former LvlsView, RanksView and /topstreak's ad hoc static
    embed. Each type keeps its own query, row format, color and empty-state
    message exactly as before -- this consolidates the refresh-button and
    command plumbing, not what any of the three leaderboards look like.
    """

    def empty_message(self) -> str:
        # Three literal t() calls, not a dict keyed by locale-key name, so
        # scripts/locale_audit.py's AST scan -- which only recognises a
        # literal string passed straight to t() -- can still see all three
        # keys as referenced.
        if self.leaderboard_type == "wealth":
            return t("profiles.leaderboard_empty")
        if self.leaderboard_type == "levels":
            return t("profiles.no_levels_stored")
        if self.leaderboard_type == "streaks":
            return t("profiles.no_streaks_yet")
        return t("profiles.no_pity_data")

    def __init__(self, guild, leaderboard_type: str):
        super().__init__(timeout=PROFILE_VIEW_TIMEOUT)
        self.guild = guild
        self.leaderboard_type = leaderboard_type

        # Construct the button at runtime so its label uses the active locale.
        btn_refresh = discord.ui.Button(label=t("profiles.refresh_btn"), style=discord.ButtonStyle.success, emoji="🔄")
        btn_refresh.callback = self.refresh_btn
        self.add_item(btn_refresh)

    async def generate_embed(self):
        if self.leaderboard_type == "wealth":
            return await self._wealth_embed()
        if self.leaderboard_type == "levels":
            return await self._levels_embed()
        if self.leaderboard_type == "streaks":
            return await self._streaks_embed()
        return await self._pity_embed()

    async def _wealth_embed(self):
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

    async def _levels_embed(self):
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

    async def _streaks_embed(self):
        results = await database.run(
            database.get_top_streaks, guild_member_ids(self.guild), 10
        )

        if not results:
            return None

        embed = discord.Embed(
            title=t("profiles.topstreak_title"),
            description=t("profiles.topstreak_desc"),
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url=self.guild.icon.url if self.guild.icon else None)

        board_text = ""
        for index, (user_id, streak) in enumerate(results, start=1):
            name = display_member_name(self.guild, user_id)

            if index == 1: medal = "🥇"
            elif index == 2: medal = "🥈"
            elif index == 3: medal = "🥉"
            else: medal = f"**{index}.**"

            board_text += t("profiles.streak_leaderboard_row", medal=medal, name=name, streak=streak)

        embed.description += board_text
        return embed

    async def _pity_embed(self):
        # The guild-wide board, so it stays on the default banner -- `/pity`
        # itself takes an optional `banner` for a member's own per-banner
        # view, which this is not.
        results = await database.run(
            database.get_top_gacha_luck, self.guild.id,
            guild_member_ids(self.guild), database.DEFAULT_GACHA_BANNER_KEY,
            10, PITY_LEADERBOARD_MINIMUM_FIVE_STARS,
        )

        if not results:
            return None

        embed = discord.Embed(
            title=t("profiles.pity_leaderboard_title"),
            description=t("profiles.pity_leaderboard_desc"),
            color=discord.Color.blue()
        )

        board_text = ""
        for index, (user_id, avg_pity, five_stars) in enumerate(results, start=1):
            name = display_member_name(self.guild, user_id)

            if index == 1: medal = "🥇"
            elif index == 2: medal = "🥈"
            elif index == 3: medal = "🥉"
            else: medal = f"**{index}.**"

            board_text += t("profiles.pity_leaderboard_row", medal=medal, name=name,
                            avg_pity=f"{avg_pity:.1f}", five_stars=five_stars)

        embed.description += board_text
        return embed

    async def refresh_btn(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "profiles"):
            return
        await interaction.response.defer()
        fresh_embed = await self.generate_embed()
        if fresh_embed:
            await interaction.edit_original_response(embed=fresh_embed, view=self)
        else:
            await interaction.followup.send(self.empty_message(), ephemeral=True)

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
            
        lvl, xp, bal, wins, losses, streak_count, last_streak_update = result
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
            last_date = local_date(parse_stored(last_streak_update))
            today = local_date(utc_now())
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
        
        embed.add_field(name=t("profiles.daily_streak_label"), value=streak_display, inline=True)
        
        embed.add_field(name=t("profiles.progress_label", percent=int(percent*100)), 
                        value=t("profiles.progress_value", bar=bar, xp=xp, next_xp=next_lvl_xp), inline=False)
        
        total_games = wins + losses
        rate = (wins / total_games * 100) if total_games > 0 else 0
        embed.add_field(name=t("profiles.casino_stats_label"), 
                        value=t("profiles.casino_stats_value", wins=wins, losses=losses, rate=rate), inline=False)

        # Pity, if this member has ever pulled. Everything above comes from the
        # global `users` row; pity is per guild *and* per banner, so this is
        # deliberately narrowed to this guild's standard banner and the field
        # name says so rather than implying it covers every banner.
        if is_enabled(self.member.guild.id, "shop_gacha"):
            pity = await database.run_read(
                database.get_gacha_pity, self.member.guild.id, self.member.id)
            if pity["total_pulls"]:
                embed.add_field(
                    name=t("profiles.pity_label"),
                    value=t("profiles.pity_value", pity=pity["pity"],
                            five_stars=pity["five_stars"],
                            total=pity["total_pulls"]),
                    inline=False,
                )

        # Why voice is paying nothing right now, when it is not. The rule itself
        # is `voice_reward_block`, shared with the loop that pays, so this can
        # never describe a rule the bot does not apply. Only the two states a
        # member can act on are named: `not_in_voice` is the absence of a
        # problem, and a member who *is* earning needs no notice at all.
        if is_enabled(self.member.guild.id, "voice_rewards"):
            blocked = voice_reward_block(self.member)
            if blocked in ("deafened", "afk_channel"):
                embed.add_field(
                    name=t("profiles.voice_blocked_label"),
                    value=t(f"profiles.voice_blocked_{blocked}"),
                    inline=False,
                )
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

    @commands.hybrid_command(name="leaderboard", description=t("general.cmd_leaderboard"))
    @is_channel("levels_channels")
    @discord.app_commands.choices(type=LEADERBOARD_CHOICES)
    async def leaderboard(self, ctx, type: str):
        view = LeaderboardView(ctx.guild, type)
        embed = await view.generate_embed()

        if embed:
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(view.empty_message())

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
