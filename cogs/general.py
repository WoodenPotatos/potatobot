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
from cogs.utils import BoundedCooldownMap, t, guild_setting_sync
from version import REPOSITORY_URL, release_channel, version_display
from feature_access import require_interaction_feature

general_logger = logging.getLogger("PotatoBot.General")

lfg_interaction_times = BoundedCooldownMap()


def get_help_data():
    return {
        "general": {
            "label": t("general.help_general_label"),
            "emoji": "👤",
            "description": t("general.help_general_desc"),
            "commands": {
                t("general.usage_profile"): t("general.cmd_profile"),
                t("general.usage_bal"): t("general.cmd_bal"),
                t("general.usage_ranks"): t("general.cmd_ranks"),
                t("general.usage_lvls"): t("general.cmd_lvls"),
                t("general.usage_topstreak"): t("general.cmd_topstreak"),
                t("general.usage_search"): t("general.cmd_search"),
                t("general.usage_version"): t("general.cmd_version"),
                t("general.usage_help"): t("general.cmd_help"),
                t("general.usage_mydata"): t("general.cmd_mydata"),
                t("general.usage_deletemydata"): t("general.cmd_deletemydata")
            }
        },
        "economy": {
            "label": t("general.help_economy_label"),
            "emoji": "💰",
            "description": t("general.help_economy_desc"),
            "commands": {
                t("general.usage_daily"): t("general.cmd_daily"),
                t("general.usage_work"): t("general.cmd_work"),
                t("general.usage_rob"): t("general.cmd_rob"),
                t("general.usage_pay"): t("general.cmd_pay"),
                t("general.usage_shop"): t("general.cmd_shop"),
                t("general.usage_gacha"): t("general.cmd_gacha"),
                t("general.usage_inventory"): t("general.cmd_inventory"),
                t("general.usage_pity"): t("general.cmd_pity"),
                t("general.usage_redeem"): t("general.cmd_redeem")
            }
        },
        "casino": {
            "label": t("general.help_casino_label"),
            "emoji": "🎰",
            "description": t("general.help_casino_desc"),
            "commands": {
                t("general.usage_bj"): t("general.cmd_bj"),
                t("general.usage_roulette"): t("general.cmd_roulette"),
                t("general.usage_slots"): t("general.cmd_slots"),
                t("general.usage_dice"): t("general.cmd_dice"),
                t("general.usage_mines"): t("general.cmd_mines"),
                t("general.usage_freemines"): t("general.cmd_freemines")
            }
        },
        "music": {
            "label": t("general.help_music_label"),
            "emoji": "🎵",
            "description": t("general.help_music_desc"),
            "commands": {
                t("general.usage_join"): t("general.cmd_join"),
                t("general.usage_play"): t("general.cmd_play"),
                t("general.usage_queue"): t("general.cmd_queue"),
                t("general.usage_skip"): t("general.cmd_skip"),
                t("general.usage_remove"): t("general.cmd_remove"),
                t("general.usage_shuffle"): t("general.cmd_shuffle"),
                t("general.usage_loop"): t("general.cmd_loop"),
                t("general.usage_np"): t("general.cmd_np"),
                t("general.usage_stop"): t("general.cmd_stop")
            }
        },
        "everydle": {
            "label": t("general.help_everydle_label"),
            "emoji": "🧠",
            "description": t("general.help_everydle_desc"),
            "commands": {
                t("general.usage_loldle"): t("general.cmd_loldle"),
                t("general.usage_valdle"): t("general.cmd_valdle"),
                t("general.usage_dbdle"): t("general.cmd_dbdle")
            }
        },
        "staff": {
            "label": t("general.help_staff_label"),
            "emoji": "🛡️",
            "description": t("general.help_staff_desc"),
            "hidden": True, 
            "commands": {
                t("general.usage_kick"): t("general.cmd_kick"),
                t("general.usage_ban"): t("general.cmd_ban"),
                t("general.usage_timeout"): t("general.cmd_timeout"),
                t("general.usage_untimeout"): t("general.cmd_untimeout"),
                t("general.usage_warn"): t("general.cmd_warn"),
                t("general.usage_unwarn"): t("general.cmd_unwarn"),
                t("general.usage_modlogs"): t("general.cmd_modlogs"),
                t("general.usage_msgdel"): t("general.cmd_msgdel")
            }
        },
        "leaders": {
            "label": t("general.help_leaders_label"),
            "emoji": "🎖️",
            "description": t("general.help_leaders_desc"),
            "hidden": True,
            "commands": {
                t("general.usage_manage"): t("general.cmd_manage")
            }
        },
        "admin": {
            "label": t("general.help_admin_label"),
            "emoji": "⚠️",
            "description": t("general.help_admin_desc"),
            "hidden": True,
            "commands": {
                t("general.usage_award"): t("general.cmd_award"),
                t("general.usage_awardall"): t("general.cmd_awardall"),
                t("general.usage_rent_start"): t("general.cmd_rent_start"),
                t("general.usage_maintenance"): t("general.cmd_maintenance"),
                t("general.usage_testreset"): t("general.cmd_testreset"),
                t("general.usage_sync_autoroles"): t("general.cmd_sync_autoroles"),
                t("general.usage_embedsend"): t("general.cmd_embedsend"),
                t("general.usage_testboost"): t("general.cmd_testboost"),
                t("general.usage_checkperms"): t("general.cmd_checkperms"),
                t("general.usage_setup_games"): t("general.cmd_setup_games"),
                t("general.usage_update_games"): t("general.cmd_update_games"),
                t("general.usage_setup_news"): t("general.cmd_setup_news"),
                t("general.usage_update_news"): t("general.cmd_update_news"),
                t("general.usage_setup_enter"): t("general.cmd_setup_enter"),
                t("general.usage_update_enter"): t("general.cmd_update_enter"),
                t("general.usage_rules_verify"): t("general.cmd_rules_verify"),
                t("general.usage_rules_group"): t("general.cmd_rules_group"),
                t("general.usage_update_rules_group"): t("general.cmd_update_rules_group"),
                t("general.usage_getraw"): t("general.cmd_getraw"),
                t("general.usage_setup_tickets"): t("general.cmd_setup_tickets"),
                t("general.usage_setup_themes"): t("general.cmd_setup_themes"),
                t("general.usage_update_themes"): t("general.cmd_update_themes")
            }
        }
    }

class HelpSelect(discord.ui.Select):
    def __init__(self, user_roles, is_admin, guild_id=None):
        options = []
        help_data = get_help_data()
        
        for key, data in help_data.items():
            if data.get("hidden", False):
                staff_roles = guild_setting_sync(guild_id, "admin_roles") if guild_id else []
                if key == "staff" and not (is_admin or any(r.id in staff_roles for r in user_roles)):
                    continue
                if key == "admin" and not is_admin:
                    continue
                
                # Read faction leadership live so permission changes apply after reload.
                factions = (guild_setting_sync(guild_id, "factions") or {}) if guild_id else {}
                leader_roles = [f_data.get("leader_role_id")
                                for f_data in factions.values()
                                if isinstance(f_data, dict) and f_data.get("leader_role_id")]
                if key == "leaders" and not (is_admin or any(r.id in leader_roles for r in user_roles)):
                    continue

            options.append(discord.SelectOption(
                label=data["label"], 
                description=data["description"], 
                emoji=data["emoji"], 
                value=key
            ))

        super().__init__(placeholder=t("general.help_placeholder"), min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "general"):
            return
        choice = self.values[0]
        data = get_help_data()[choice]
        
        embed = discord.Embed(
            title=f"{data['emoji']} {data['label']}", 
            description=data["description"], 
            color=discord.Color.blue()
        )
        
        for cmd_name, cmd_desc in data["commands"].items():
            embed.add_field(name=cmd_name, value=cmd_desc, inline=False)
            
        await interaction.response.edit_message(embed=embed)

class HelpView(discord.ui.View):
    def __init__(self, user_roles, is_admin, guild_id=None):
        super().__init__()
        self.add_item(HelpSelect(user_roles, is_admin, guild_id))

class LFGView(discord.ui.View):
    def __init__(self, host: discord.Member, game_info, needed: int):
        super().__init__(timeout=2 * 60 * 60)
        self.host = host
        self.game_info = game_info 
        self.needed = needed
        self.joined = []

        btn_join = discord.ui.Button(label=t("general.lfg_btn_join"), style=discord.ButtonStyle.success, emoji="✅")
        btn_join.callback = self.join_btn
        self.add_item(btn_join)

        btn_leave = discord.ui.Button(label=t("general.lfg_btn_leave"), style=discord.ButtonStyle.danger, emoji="❌")
        btn_leave.callback = self.leave_btn
        self.add_item(btn_leave)

        btn_delete = discord.ui.Button(label=t("general.lfg_btn_delete"), style=discord.ButtonStyle.secondary, emoji="🗑️")
        btn_delete.callback = self.delete_btn
        self.add_item(btn_delete)

    async def interaction_check(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "lfg"):
            return False
        now = time.monotonic()
        retry_after = 2 - (now - lfg_interaction_times.get(interaction.user.id, 0))
        if retry_after > 0:
            await interaction.response.send_message(
                t("utils.command_cooldown", seconds=int(retry_after) + 1),
                ephemeral=True,
            )
            return False
        lfg_interaction_times[interaction.user.id] = now
        return True

    def build_embed(self):
        if isinstance(self.game_info, discord.Role):
            game_name = self.game_info.mention
            embed_color = self.game_info.color
        else:
            game_name = f"**{self.game_info}**"
            embed_color = discord.Color.teal() 

        embed = discord.Embed(
            title=t("general.lfg_title"),
            color=embed_color
        )
        
        desc = t("general.lfg_host_game", host=self.host.mention, game=game_name)
        
        if self.needed > 0:
            desc += t("general.lfg_needed", joined=len(self.joined), needed=self.needed)
        else:
            desc += t("general.lfg_any")

        if self.joined:
            players_str = "\n".join([t("general.lfg_joined_format", uid=uid) for uid in self.joined])
            embed.add_field(name=t("general.lfg_joined_title"), value=players_str, inline=False)
        else:
            embed.add_field(name=t("general.lfg_joined_title"), value=t("general.lfg_nobody_yet"), inline=False)
            
        embed.description = desc
        return embed

    async def join_btn(self, interaction: discord.Interaction):
        if interaction.user.id in self.joined:
            return await interaction.response.send_message(t("general.lfg_err_already_joined"), ephemeral=True)
        if interaction.user.id == self.host.id:
            return await interaction.response.send_message(t("general.lfg_err_host_auto_in"), ephemeral=True)
        if self.needed > 0 and len(self.joined) >= self.needed:
            return await interaction.response.send_message(t("general.lfg_err_full"), ephemeral=True)

        self.joined.append(interaction.user.id)

        full = self.needed > 0 and len(self.joined) == self.needed
        if full:
            self.children[0].disabled = True

        # Acknowledge first. The "party is full" announcement used to be sent
        # before this, so a refused channel send left the member joined, the
        # interaction unacknowledged, and "This interaction failed" on screen.
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

        if full:
            game_name = self.game_info.name if isinstance(self.game_info, discord.Role) else self.game_info
            try:
                await interaction.channel.send(
                    t("general.lfg_success_full", host=self.host.mention,
                      game=game_name),
                    delete_after=60,
                )
            except discord.HTTPException:
                general_logger.exception(
                    "Could not announce a full LFG party (guild_id=%s)",
                    interaction.guild.id,
                )

    async def leave_btn(self, interaction: discord.Interaction):
        if interaction.user.id not in self.joined:
            return await interaction.response.send_message(t("general.lfg_err_not_in"), ephemeral=True)

        self.joined.remove(interaction.user.id)
        self.children[0].disabled = False 
        
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def delete_btn(self, interaction: discord.Interaction):
        if interaction.user.id != self.host.id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(t("general.lfg_err_not_host"), ephemeral=True)
        await interaction.message.delete()

class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="version", description=t("general.cmd_version"))
    async def version(self, ctx):
        # The version comes from the packaging metadata, never from config: it
        # used to be an operator-editable setting and it drifted from the real
        # one. The channel is derived from the version, so a build cannot claim
        # to be stable while carrying a prerelease suffix.
        is_maintenance = guild_setting_sync(None, "maintenance")
        maint_text = t("general.version_maint_true") if is_maintenance else t("general.version_maint_false")

        channel = release_channel()
        embed = discord.Embed(title=t("general.version_title"), color=discord.Color.blue())
        embed.add_field(name=t("general.version_bot_ver"),
                        value=version_display(), inline=True)
        embed.add_field(name=t("general.version_channel"),
                        value=t(f"general.version_channel_{channel}"), inline=True)
        embed.add_field(name=t("general.version_maint_status"),
                        value=f"**{maint_text}**", inline=True)
        embed.add_field(name=t("general.version_source"),
                        value=REPOSITORY_URL, inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="search", description=t("general.cmd_search"))
    @commands.cooldown(1, 120, commands.BucketType.user)
    async def search(self, ctx, needed: int = 0, *, game: str = None):
        lfg_channels = guild_setting_sync(ctx.guild.id, "lfg_channels") or {}
        default_lfg = guild_setting_sync(ctx.guild.id, "lfg_default_channel")
        default_lfg_channel_id = str(default_lfg or "")
        channel_id_str = str(ctx.channel.id)

        if channel_id_str in lfg_channels:
            role_id = lfg_channels[channel_id_str]
            role = ctx.guild.get_role(role_id)

            if not role:
                return await ctx.send(t("general.search_err_role_not_found"), ephemeral=True)

            view = LFGView(ctx.author, role, needed)
            embed = view.build_embed()
            # A nickname is attacker-controlled, so it must never survive as a
            # mention in the one message where role pings are permitted.
            ping_msg = t(
                "general.search_ping_msg",
                role=role.mention,
                user=discord.utils.escape_mentions(ctx.author.display_name),
            )
            await ctx.send(
                content=ping_msg,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=[role], users=True, replied_user=False
                ),
            )

        elif channel_id_str == default_lfg_channel_id:
            if not game:
                return await ctx.send(t("general.search_err_no_game"), ephemeral=True)

            view = LFGView(ctx.author, game, needed)
            embed = view.build_embed()
            custom_msg = t(
                "general.search_custom_game_msg",
                user=discord.utils.escape_mentions(ctx.author.display_name),
                game=discord.utils.escape_mentions(game),
            )
            await ctx.send(content=custom_msg, embed=embed, view=view)

        else:
            await ctx.send(t("general.search_err_wrong_channel"), ephemeral=True)

    @commands.hybrid_command(name="help", description=t("general.cmd_help"))
    async def help(self, ctx):
        roles = ctx.author.roles
        is_admin = ctx.author.guild_permissions.administrator
    
        view = HelpView(roles, is_admin, ctx.guild.id)
    
        embed = discord.Embed(
            title=t("general.help_title"),
            description=t("general.help_desc"),
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=self.bot.user.avatar.url)
    
        await ctx.send(embed=embed, view=view, ephemeral=True)

async def setup(bot):
    await bot.add_cog(General(bot))
