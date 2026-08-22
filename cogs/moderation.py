import discord
import os
import sys

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database

from discord.ext import commands
from datetime import datetime, timedelta
from cogs.utils import is_staff, is_higher_than, role_autocomplete, t, config

# Moderation commands enforce both Discord hierarchy and configured staff policy.

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="kick", description=t("general.cmd_kick"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def kick(self, ctx, member: discord.Member, *, reason: str = None):
        if reason is None: 
            reason = t("moderation.no_reason_provided")

        if member.id == ctx.author.id:
            return await ctx.send(t("moderation.self_kick_error"), ephemeral=True)
    
        # Discord rejects actions against peers or higher-ranked members; fail clearly first.
        if not is_higher_than(ctx.author, member):
            return await ctx.send(t("moderation.hierarchy_error", user=member.mention), ephemeral=True)

        # Notify before removal because the member may become unreachable afterward.
        try:
            await member.send(t("moderation.kick_dm", guild=ctx.guild.name, reason=reason))
        except:
            pass # If their DMs are closed, ignore it

        await member.kick(reason=reason)
    
        embed = discord.Embed(title=t("moderation.kick_embed_title"), color=discord.Color.orange())
        embed.add_field(name=t("moderation.user_label"), value=member.mention, inline=True)
        embed.add_field(name=t("moderation.mod_label"), value=ctx.author.mention, inline=True)
        embed.add_field(name=t("moderation.reason_label"), value=reason, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="ban", description=t("general.cmd_ban"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def ban(self, ctx, member: discord.Member, *, reason: str = None):
        if reason is None: 
            reason = t("moderation.no_reason_provided")
    
        if member.id == ctx.author.id:
            return await ctx.send(t("moderation.self_ban_error"), ephemeral=True)
    
        if not is_higher_than(ctx.author, member):
            return await ctx.send(t("moderation.hierarchy_error", user=member.mention), ephemeral=True)

        try:
            await member.send(t("moderation.ban_dm", guild=ctx.guild.name, reason=reason))
        except:
            pass

        await member.ban(reason=reason)
    
        embed = discord.Embed(title=t("moderation.ban_embed_title"), color=discord.Color.red())
        embed.add_field(name=t("moderation.user_label"), value=member.mention, inline=True)
        embed.add_field(name=t("moderation.reason_label"), value=reason, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="timeout", description=t("general.cmd_timeout"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def timeout(self, ctx, member: discord.Member, minutes: int, *, reason: str = None):
        if reason is None: 
            reason = t("moderation.default_timeout_reason")
    
        if member.id == ctx.author.id:
            return await ctx.send(t("moderation.self_timeout_error"), ephemeral=True)
    
        if not is_higher_than(ctx.author, member):
            return await ctx.send(t("moderation.hierarchy_error", user=member.mention), ephemeral=True)
    
        if not 1 <= minutes <= 40320: # Discord limit is 28 days
            return await ctx.send(t("moderation.timeout_limit_error"), ephemeral=True)

        duration = timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
    
        embed = discord.Embed(title=t("moderation.timeout_embed_title"), color=discord.Color.yellow())
        embed.description = t("moderation.timeout_desc", user=member.mention, minutes=minutes)
        embed.add_field(name=t("moderation.reason_label"), value=reason)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="untimeout", description=t("general.cmd_untimeout"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def untimeout(self, ctx, member: discord.Member):
        if member.id == ctx.author.id or not is_higher_than(ctx.author, member):
            return await ctx.send(
                t("moderation.hierarchy_error", user=member.mention), ephemeral=True
            )
        await member.timeout(None, reason=t("moderation.untimeout_reason"))
        await ctx.send(t("moderation.untimeout_success", user=member.display_name))

    @commands.hybrid_command(name="msgdel", description=t("general.cmd_msgdel"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def msgdel(self, ctx, amount: int):
        protected_category_id = config["channels"].get("admin_category")
    
        if ctx.channel.category_id == protected_category_id:
            return await ctx.send(t("moderation.purge_protected_error"), ephemeral=True)

        if not 1 <= amount <= 100:
            return await ctx.send(t("moderation.purge_limit_error"), ephemeral=True)
        
        deleted = await ctx.channel.purge(limit=amount)
    
        await ctx.send(t("moderation.purge_success", count=len(deleted), channel=ctx.channel.name), ephemeral=True)

    @commands.hybrid_command(name="warn", description=t("general.cmd_warn"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def warn(self, ctx, member: discord.Member, *, reason: str):
        if member.id == ctx.author.id:
            return await ctx.send(t("moderation.self_warn_error"), ephemeral=True)
    
        if not is_higher_than(ctx.author, member):
            return await ctx.send(t("moderation.hierarchy_error", user=member.mention), ephemeral=True)

        # Persist the warning before reporting success.
        await database.run(
            database.add_warning, member.id, ctx.author.id, reason,
            datetime.now().isoformat(), ctx.guild.id,
        )
    
        # Read the committed count for the public warning footer.
        count = await database.run(database.get_warning_count, member.id, ctx.guild.id)
    
        # Publish the moderation result to the invoking channel.
        embed = discord.Embed(title=t("moderation.warn_embed_title"), color=discord.Color.gold())
        embed.add_field(name=t("moderation.user_label"), value=member.mention)
        embed.add_field(name=t("moderation.reason_label"), value=reason)
        embed.set_footer(text=t("moderation.warn_footer", count=count))
    
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="unwarn", description=t("general.cmd_unwarn"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def unwarn(self, ctx, member: discord.Member, warning_id: int):
        """Remove one warning by its guild-visible identifier."""
        removed = await database.run(
            database.remove_warning, warning_id, member.id, ctx.guild.id, ctx.author.id
        )
        if removed is None:
            return await ctx.send(
                t("moderation.unwarn_not_found", warning_id=warning_id), ephemeral=True
            )
        remaining = await database.run(database.get_warning_count, member.id, ctx.guild.id)
        await ctx.send(
            t(
                "moderation.unwarn_success",
                warning_id=warning_id,
                user=member.mention,
                count=remaining,
            ),
            ephemeral=True,
        )

    @commands.hybrid_command(name="modlogs", description=t("general.cmd_modlogs"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def modlogs(self, ctx, member: discord.Member):
        warnings = await database.run(database.get_warnings, member.id, ctx.guild.id)
        user_intel = await database.run(database.get_user_intel, member.id)
    
        embed = discord.Embed(title=t("moderation.modlogs_title", user=member.display_name), color=discord.Color.blue())
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)

        # Include account activity metadata only for staff viewers.
        if user_intel:
            read_time, last_active_str = user_intel
            
            if read_time is not None:
                if read_time < 10: read_msg = t("moderation.read_too_fast", time=read_time)
                elif read_time < 30: read_msg = t("moderation.read_fast", time=read_time)
                else: read_msg = t("moderation.read_normal", time=read_time)
            else:
                read_msg = t("moderation.no_data_old_member")

            if last_active_str:
                last_active = datetime.fromisoformat(last_active_str)
                now = datetime.now()
                diff = now - last_active
                
                if diff.days >= 14: active_msg = t("moderation.inactive_red", days=diff.days)
                elif diff.days >= 7: active_msg = t("moderation.inactive_yellow", days=diff.days)
                else: active_msg = t("moderation.active_green", days=diff.days)
            else:
                active_msg = t("moderation.no_data")

            embed.add_field(name=t("moderation.rules_read_label"), value=read_msg, inline=True)
            embed.add_field(name=t("moderation.activity_status_label"), value=active_msg, inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=False) 

        # Render the chronological warning history.
        if not warnings:
            embed.add_field(name=t("moderation.warnings_label"), value=t("moderation.clean_record"), inline=False)
            embed.color = discord.Color.green() 
        else:
            embed.color = discord.Color.orange() 
            for warning_id, reason, date, mod_id in warnings:
                date_obj = datetime.fromisoformat(date)
                date_str = date_obj.strftime("%Y-%m-%d %H:%M")
                mod = ctx.guild.get_member(mod_id)
                mod_name = mod.display_name if mod else t("moderation.unknown_mod")
            
                embed.add_field(
                    name=t(
                        "moderation.warn_entry_title_with_id",
                        warning_id=warning_id,
                        date=date_str,
                        mod=mod_name,
                    ),
                    value=reason,
                    inline=False,
                )
        
        await ctx.send(embed=embed)

    @discord.app_commands.command(name="manage", description=t("general.cmd_manage"))
    @discord.app_commands.checks.cooldown(1, 3, key=lambda interaction: interaction.user.id)
    @discord.app_commands.autocomplete(role_id=role_autocomplete)
    @discord.app_commands.describe(
        role_id=t("moderation.manage_role_description"),
        member=t("moderation.manage_member_description"),
    )
    async def manage(self, interaction: discord.Interaction, member: discord.Member, role_id: str):
    
        try:
            target_role = interaction.guild.get_role(int(role_id))
        except ValueError:
            return await interaction.response.send_message(t("moderation.invalid_role_id"), ephemeral=True)

        if not target_role:
            return await interaction.response.send_message(t("moderation.role_not_found"), ephemeral=True)

        is_authorized = False
        factions_config = config.get("factions", {})

        if interaction.user.guild_permissions.administrator:
            is_authorized = True
        else:
            user_role_ids = [r.id for r in interaction.user.roles]
        
            for data in factions_config.values():
                leader_id = data.get("leader_role_id")
                manageable_ids = data.get("manageable_ids", [])
            
                if leader_id in user_role_ids and target_role.id in manageable_ids:
                    is_authorized = True
                    break
    
        if not is_authorized:
            return await interaction.response.send_message(t("moderation.no_role_manage_perms"), ephemeral=True)

        if target_role.is_default() or target_role.managed:
            return await interaction.response.send_message(t("moderation.hierarchy_manage_error"), ephemeral=True)

        if interaction.guild.me is None or target_role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(t("moderation.bot_hierarchy_error"), ephemeral=True)

        if (
            interaction.user.id != interaction.guild.owner_id
            and target_role >= interaction.user.top_role
        ):
            return await interaction.response.send_message(t("moderation.hierarchy_manage_error"), ephemeral=True)

        if (
            member.id != interaction.guild.owner_id
            and member.top_role >= interaction.user.top_role
            and interaction.user.id != interaction.guild.owner_id
        ):
            return await interaction.response.send_message(t("moderation.hierarchy_manage_error"), ephemeral=True)

        try:
            if target_role in member.roles:
                await member.remove_roles(target_role)
                await interaction.response.send_message(t("moderation.faction_kick_success", user=member.display_name, role=target_role.name), ephemeral=False)
            else:
                await member.add_roles(target_role)
                await interaction.response.send_message(t("moderation.faction_add_success", user=member.display_name, role=target_role.name), ephemeral=False)
            
        except discord.Forbidden:
            await interaction.response.send_message(t("moderation.bot_hierarchy_error"), ephemeral=True)

    @manage.error
    async def manage_error(self, interaction, error):
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                t("utils.command_cooldown", seconds=max(1, int(error.retry_after) + 1)),
                ephemeral=True,
            )
            return
        raise error

async def setup(bot):
    await bot.add_cog(Moderation(bot))
