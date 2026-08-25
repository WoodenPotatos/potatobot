import discord
import asyncio
import logging
import os
import sys

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
from feature_access import is_enabled

from discord.ext import commands, tasks
from datetime import datetime, timedelta
from cogs.utils import (apply_database_result, guild_setting_sync,
                        mark_top_ranker_dirty, update_user_data, t)

event_logger = logging.getLogger("PotatoBot.ServerEvents")

class ServerEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.message_cooldowns = {}
        self.daily_activity_cache = {}
        self._daily_activity_date = None

    @commands.Cog.listener()
    async def on_ready(self):
        event_logger.info("Server event handlers are ready.")
        if not self.voice_xp_paycheck.is_running():
            self.voice_xp_paycheck.start()
            event_logger.info("Voice reward loop started.")
            
        if not self.inactivity_scanner.is_running():
            self.inactivity_scanner.start()
            event_logger.info("Inactivity scanner started.")

    def cog_unload(self):
        if self.voice_xp_paycheck.is_running():
            self.voice_xp_paycheck.cancel()
        if self.inactivity_scanner.is_running():
            self.inactivity_scanner.cancel()

    def _restart_loop(self, loop, name):
        if self.bot.is_closed() or loop.is_running():
            return
        try:
            loop.restart()
            event_logger.warning("Background loop restarted (loop=%s)", name)
        except RuntimeError:
            event_logger.exception("Background loop restart failed (loop=%s)", name)

    async def _handle_loop_error(self, loop, name, error):
        event_logger.exception(
            "Background loop failed; restart scheduled (loop=%s, error=%s)",
            name, type(error).__name__, exc_info=error,
        )
        self.bot.loop.call_later(30, self._restart_loop, loop, name)

    def _daily_activity_pending(self, activity_key, today_str) -> bool:
        """Report whether today's activity write is still outstanding.

        The cache only deduplicates within a single day, so it is dropped on the
        first observation of a new date instead of retaining one entry for every
        member seen since startup.
        """
        if self._daily_activity_date != today_str:
            self.daily_activity_cache.clear()
            self._daily_activity_date = today_str
        return self.daily_activity_cache.get(activity_key) != today_str

    # Activity writes are limited to once per day, while chat rewards have a shorter limit.
    
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        user_id = message.author.id
        activity_key = (message.guild.id, user_id)
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        if is_enabled(message.guild.id, "inactivity") and self._daily_activity_pending(activity_key, today_str):
            await database.run(database.update_last_active, user_id, now.isoformat())
            self.daily_activity_cache[activity_key] = today_str

        if not is_enabled(message.guild.id, "chat_rewards"):
            return

        if activity_key in self.message_cooldowns:
            last_msg_time = self.message_cooldowns[activity_key]
            if now < last_msg_time + timedelta(seconds=60):
                return

        if len(self.message_cooldowns) >= 4096:
            cutoff = now - timedelta(minutes=5)
            self.message_cooldowns = {
                key: value for key, value in self.message_cooldowns.items()
                if value >= cutoff
            }
        self.message_cooldowns[activity_key] = now
        # Resolve rewards dynamically so administrative changes do not require restart.
        coin_rw, xp_rw = await database.run(
            database.get_reward, message.guild.id, "chat_message", 5, 2
        )
        await update_user_data(message.author, balance_change=coin_rw, xp_change=xp_rw)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot: return
        
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        
        activity_key = (member.guild.id, member.id)
        if is_enabled(member.guild.id, "inactivity") and self._daily_activity_pending(activity_key, today_str):
            await database.run(database.update_last_active, member.id, now.isoformat())
            self.daily_activity_cache[activity_key] = today_str

    @tasks.loop(hours=24)
    async def inactivity_scanner(self):
        now = datetime.now()

        for guild in self.bot.guilds:
            if not is_enabled(guild.id, "inactivity"):
                continue
            # The log channel is a per-guild setting, so it is resolved per
            # guild. It used to be read once outside the loop, which meant one
            # guild's channel received every guild's report.
            admin_log_channel = guild_setting_sync(guild.id, "bot_log_channel")
            admin_channel = (guild.get_channel(admin_log_channel)
                             if admin_log_channel else None)
            if admin_channel is None:
                continue
            for member in guild.members:
                if member.bot: continue
                # A member id, compared as an id whichever shape it is
                # stored in. `ignored_users` is a STRING_LIST because a
                # snowflake cannot cross to a browser as a number, while
                # `config.json` holds it as integers — so comparing
                # `member.id in ignored_users` directly was False for anything
                # ever saved from the dashboard, and the list silently stopped
                # ignoring anyone.
                ignored_users = {
                    int(entry) for entry in
                    (guild_setting_sync(guild.id, "ignored_users") or ())
                    if str(entry).isdigit()
                }
                if member.id in ignored_users: continue

                result = await database.run(database.get_inactivity_data, member.id)

                if result:
                    last_active_str, warned = result
                    if not last_active_str:
                        await database.run(database.update_last_active, member.id, now.isoformat())
                    else:
                        last_active = datetime.fromisoformat(last_active_str)
                        diff = now - last_active

                        if diff.days >= 14 and warned == 0:
                            embed = discord.Embed(
                                title=t("serverevents.inactive_title"),
                                description=t("serverevents.inactive_desc", user=member.display_name, mention=member.mention, days=14, date=last_active.strftime('%Y-%m-%d %H:%M')),
                                color=discord.Color.orange()
                            )
                            embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
                            await admin_channel.send(embed=embed)

                            await database.run(database.set_inactive_warned, member.id)
                else:
                    await database.run(database.create_new_user, member.id, now.isoformat())

    @inactivity_scanner.before_loop
    async def before_inactivity_scanner(self):
        await self.bot.wait_until_ready()

    @inactivity_scanner.error
    async def inactivity_scanner_error(self, error):
        await self._handle_loop_error(
            self.inactivity_scanner, "inactivity_scanner", error
        )

    # Voice rewards and member lifecycle events.

    @tasks.loop(minutes=1)
    async def voice_xp_paycheck(self):
        enabled_guilds = [
            guild for guild in self.bot.guilds
            if is_enabled(guild.id, "voice_rewards")
        ]
        if not enabled_guilds:
            return
        levels_enabled_by_guild = {
            guild.id: is_enabled(guild.id, "levels") for guild in enabled_guilds
        }
        for guild in enabled_guilds:
            # Rewards are per guild since schema 8, so this read belongs inside the
            # loop rather than once for the whole installation.
            normal_reward = await database.run_read(
                database.get_reward, guild.id, "voice_minute_normal", 5, 5
            )
            premium_reward = await database.run_read(
                database.get_reward, guild.id, "voice_minute_premium", 10, 10
            )
            members = {
                member.id: member
                for channel in guild.voice_channels
                for member in channel.members
                if not member.bot
                and channel != guild.afk_channel
                and member.voice is not None
                and not member.voice.self_deaf
                and not member.voice.deaf
            }
            deltas = []
            for member in members.values():
                coin_rw, xp_rw = (
                    premium_reward if member.premium_since else normal_reward
                )
                if not levels_enabled_by_guild[guild.id]:
                    xp_rw = 0
                deltas.append((member.id, coin_rw, xp_rw))
            if not deltas:
                continue
            results = await database.run_write(
                database.apply_batch_user_deltas, deltas
            )
            for user_id, result in results.items():
                member = members.get(user_id)
                if member is not None and result["stats"][2] > result["old_level"]:
                    await apply_database_result(member, result)
            if any(result.get("xp_changed") for result in results.values()):
                mark_top_ranker_dirty(guild)

    @voice_xp_paycheck.error
    async def voice_xp_paycheck_error(self, error):
        await self._handle_loop_error(
            self.voice_xp_paycheck, "voice_xp_paycheck", error
        )

    @voice_xp_paycheck.before_loop
    async def before_voice_xp_paycheck(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if is_enabled(member.guild.id, "member_announcements"):
            channel_id = guild_setting_sync(member.guild.id, "join_channel")
            channel = member.guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                event_logger.warning(
                    "Join announcement channel is unavailable (guild_id=%s, channel_id=%s).",
                    member.guild.id, channel_id,
                )
            else:
                embed = discord.Embed(
                    title=t("serverevents.join_title"),
                    description=t("serverevents.join_desc", mention=member.mention),
                    color=discord.Color.green(),
                )
                embed.set_thumbnail(
                    url=member.avatar.url if member.avatar else member.default_avatar.url
                )
                try:
                    await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    event_logger.error(
                        "Join announcement failed (guild_id=%s, channel_id=%s, error=%s).",
                        member.guild.id, channel.id, type(exc).__name__,
                    )

        try:
            if not await database.run(database.user_exists, member.id):
                await database.run(
                    database.create_new_user, member.id, datetime.now().isoformat()
                )
                event_logger.info("Registered new user ID %s.", member.id)
            else:
                event_logger.info("Returning user ID %s already has an account.", member.id)
        except database.DatabaseOperationError:
            event_logger.exception(
                "Member account provisioning failed (guild_id=%s, user_id=%s).",
                member.guild.id, member.id,
            )

        autorole_ids = (
            guild_setting_sync(member.guild.id, "autoroles")
            if is_enabled(member.guild.id, "onboarding") else []
        )
        roles_to_add = []

        for role_id in autorole_ids:
            role = member.guild.get_role(role_id)
            if role:
                roles_to_add.append(role)

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add)
                event_logger.info("Assigned configured autoroles to user ID %s.", member.id)
            except discord.Forbidden:
                event_logger.error(
                    "Could not assign autoroles to user ID %s: missing Manage Roles "
                    "permission or sufficient role hierarchy.",
                    member.id,
                )

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if not is_enabled(member.guild.id, "member_announcements"):
            return
        channel_id = guild_setting_sync(member.guild.id, "leave_channel")
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            event_logger.warning(
                "Leave announcement channel is unavailable (guild_id=%s, channel_id=%s).",
                member.guild.id, channel_id,
            )
            return
        embed = discord.Embed(
            title=t("serverevents.leave_title"),
            description=t("serverevents.leave_desc", mention=member.mention),
            color=discord.Color.red(),
        )
        embed.set_thumbnail(
            url=member.avatar.url if member.avatar else member.default_avatar.url
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            event_logger.error(
                "Leave announcement failed (guild_id=%s, channel_id=%s, error=%s).",
                member.guild.id, channel.id, type(exc).__name__,
            )

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        if not is_enabled(guild.id, "member_announcements"):
            return
        channel = self.bot.get_channel(
            guild_setting_sync(guild.id, "leave_channel"))
        if channel:
            await asyncio.sleep(1)
        
            async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=1):
                reason = entry.reason if entry.reason else t("serverevents.no_reason")
                mod = entry.user
            
                embed = discord.Embed(title=t("serverevents.ban_title"), color=discord.Color.red())
                embed.add_field(name=t("serverevents.ban_user"), value=user.name, inline=True)
                embed.add_field(name=t("serverevents.ban_mod"), value=mod.name, inline=True)
                embed.add_field(name=t("serverevents.ban_reason"), value=reason, inline=False)
                await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.premium_since is None and after.premium_since is not None:
            bonus_amount = 50000
            reward_result = None
            if is_enabled(after.guild.id, "economy"):
                reward_result = await database.run_write(
                    database.claim_periodic_reward,
                    after.guild.id, after.id, "server_boost", bonus_amount, 30,
                )
                if reward_result is not None:
                    await apply_database_result(after, reward_result)
            if reward_result is None:
                return
        
            channel = (
                after.guild.get_channel(
                    guild_setting_sync(after.guild.id, "booster_channel"))
                if is_enabled(after.guild.id, "member_announcements") else None
            )
            if channel:
                embed = discord.Embed(
                    title=t("serverevents.booster_title"),
                    description=t("serverevents.booster_desc", mention=after.mention, amount=bonus_amount),
                    color=0xff73fa
                )
                await channel.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ServerEvents(bot))
