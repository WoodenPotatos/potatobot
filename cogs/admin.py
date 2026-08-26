import discord
import asyncio
import os
import sys
import time
import logging

# Resolve the repository root when this cog is loaded outside the project directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
import permission_audit
from managed_messages import render_managed_message
from discord.ext import commands
from datetime import datetime, timedelta
from cogs.utils import (
    BoundedCooldownMap, can_self_assign_role, currency_emoji,
    guild_setting_sync, guild_settings_sync, is_staff, set_guild_setting, t,
    update_user_data,
)
from settings_registry import SETTING_DEFINITIONS
from feature_access import require_interaction_feature

onboarding_interaction_times = BoundedCooldownMap()
admin_logger = logging.getLogger("PotatoBot.Admin")


def permission_names(finding) -> str:
    """Localized, comma-joined Discord permission names for one finding."""
    return ", ".join(
        t(f"admin.permission_names.{name}") for name in finding.permissions
    )


def describe_permission_finding_title(finding) -> str:
    """The heading for one finding: the feature or setting it is about."""
    if finding.feature:
        return t(f"dashboard.features.{finding.feature}")
    if finding.subject:
        return t(f"dashboard.settings.{finding.subject}")
    return t("admin.permissions_title")


def describe_permission_finding(finding) -> str:
    """One human sentence per finding, from the locale catalog only."""
    severity = t(f"admin.permissions_severity_{finding.severity}")
    detail = t(
        f"admin.permissions_finding_{finding.code}",
        subject=finding.subject,
        identifier=finding.identifier,
        permissions=permission_names(finding),
    )
    return t("admin.permissions_finding_line", severity=severity, detail=detail)


async def check_onboarding_interaction(interaction):
    if not await require_interaction_feature(interaction, "onboarding"):
        return False
    action_id = (interaction.data or {}).get("custom_id", "onboarding")
    cooldown_key = (interaction.user.id, action_id)
    now = time.monotonic()
    retry_after = 5 - (now - onboarding_interaction_times.get(cooldown_key, 0))
    if retry_after > 0:
        await interaction.response.send_message(
            t("utils.command_cooldown", seconds=int(retry_after) + 1), ephemeral=True
        )
        return False
    onboarding_interaction_times[cooldown_key] = now
    return True

class EmbedSendModal(discord.ui.Modal, title=t("admin.embed_modal_title")):
    embed_title = discord.ui.TextInput(
        label=t("admin.embed_title_label"),
        placeholder=t("admin.embed_title_placeholder"),
        max_length=256,
    )
    embed_color = discord.ui.TextInput(
        label=t("admin.embed_color_label"),
        placeholder=t("admin.embed_color_placeholder"),
        default="#2f3136",
        max_length=20,
    )
    embed_desc = discord.ui.TextInput(
        label=t("admin.embed_message_label"),
        style=discord.TextStyle.paragraph,
        placeholder=t("admin.embed_message_placeholder"),
        max_length=4000,
    )

    def __init__(self, show_icon: bool):
        super().__init__()
        self.show_icon = show_icon

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "general"):
            return
        staff_role_ids = guild_setting_sync(interaction.guild.id, "admin_roles")
        is_current_staff = interaction.user.guild_permissions.administrator or any(
            role.id in staff_role_ids for role in interaction.user.roles
        )
        if not is_current_staff:
            return await interaction.response.send_message(
                t("utils.err_no_perms"), ephemeral=True
            )
        try:
            target_color = discord.Color.from_str(self.embed_color.value)
        except ValueError:
            target_color = discord.Color.blue()

        embed = discord.Embed(title=self.embed_title.value, description=self.embed_desc.value, color=target_color)
        if self.show_icon and interaction.guild and interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        await interaction.channel.send(embed=embed)
        await interaction.response.send_message(t("admin.embed_sent_success"), ephemeral=True)

# The one rules panel the commands address. A guild may have several through the
# dashboard; `/rules_group` has only ever meant "the" rules message, so it owns
# this key the way the default gacha banner owns its own.
RULES_PANEL_KEY = "rules"


async def store_rules_panel(ctx, color_hex, add_button, arguments) -> dict:
    """Save what the command was asked to post, and hand back the stored row.

    The command posts from storage rather than from its own arguments, which is
    what stops the two paths diverging: whatever `/rules_group` sends is exactly
    what the dashboard then shows and can edit.
    """
    try:
        colour = discord.Color.from_str(color_hex).value
    except (ValueError, TypeError):
        colour = discord.Color.blue().value
    sections = []
    for index in range(1, 7):
        title = arguments.get(f"title{index}")
        body = arguments.get(f"msg{index}")
        if index == 1 or (title and body):
            sections.append({"title": title, "body": body or ""})
    # The seventh has never had a title; it is the closing paragraph.
    if arguments.get("msg7"):
        sections.append({"title": None, "body": arguments["msg7"]})

    existing = await database.run_read(database.get_managed_message,
                                       ctx.guild.id, "rules", RULES_PANEL_KEY)
    await database.run_write(
        database.save_managed_message, ctx.guild.id, ctx.author.id, "rules",
        RULES_PANEL_KEY, (existing or {}).get("display_name") or RULES_PANEL_KEY,
        (existing or {}).get("revision", 0), colour=colour,
        options={"sections": sections, "accept_button": bool(add_button),
                 "thumbnail": True})
    return await database.run_read(database.get_managed_message, ctx.guild.id,
                                   "rules", RULES_PANEL_KEY)


# The one entry gate the commands address, the way `RULES_PANEL_KEY` names the
# one rules panel. A guild may create others from the dashboard.
ENTRY_GATE_KEY = "airlock"


async def store_simple_panel(ctx, kind, menu_key, title, body, colour) -> dict:
    """Save a one-button panel and hand back the stored row.

    `/setup_enter` and `/setup_tickets` posted and forgot, so a panel put up from
    Discord was invisible to the dashboard and could never be edited. They write
    the row first and post *from* it, which is what stops the two paths
    describing different messages. An existing row keeps its operator-set text:
    re-running the command must not silently overwrite what somebody typed into
    the dashboard.
    """
    existing = await database.run_read(database.get_managed_message,
                                       ctx.guild.id, kind, menu_key)
    if existing:
        return existing
    await database.run_write(
        database.save_managed_message, ctx.guild.id, ctx.author.id, kind,
        menu_key, menu_key, 0, title=title, body=body, colour=colour)
    return await database.run_read(database.get_managed_message, ctx.guild.id,
                                   kind, menu_key)


class RuleAcceptView(discord.ui.View):
    """The rules panel's accept button, whose label an operator may set.

    The instance handed to `bot.add_view` takes no label and keeps the shipped
    one: a click routes by `custom_id`, which the label never touches, so the
    registered view is a routing table whose own labels are never drawn. Only
    the instance built for one posted message carries the operator's text.

    The button is constructed at runtime rather than by the decorator so the
    default label is localized when the view is built rather than frozen at
    import — the same shape `TicketLauncher` already uses.
    """

    def __init__(self, *, label: str = None):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=label or t("admin.accept_rules_button"),
            style=discord.ButtonStyle.green, custom_id="accept_rules_btn",
            emoji="✅")
        button.callback = self.accept
        self.add_item(button)

    async def interaction_check(self, interaction):
        return await check_onboarding_interaction(interaction)

    async def accept(self, interaction: discord.Interaction):
        onboarding_role = interaction.guild.get_role(
            guild_setting_sync(interaction.guild.id, "onboarding_role") or 0)
        member_role = interaction.guild.get_role(
            guild_setting_sync(interaction.guild.id, "member_role") or 0)

        if not onboarding_role or not member_role:
            return await interaction.response.send_message(t("admin.role_not_found"), ephemeral=True)
        if not can_self_assign_role(interaction.guild, onboarding_role):
            return await interaction.response.send_message(t("admin.role_not_found"), ephemeral=True)

        if member_role in interaction.user.roles:
            return await interaction.response.send_message(t("admin.already_member"), ephemeral=True)
        if onboarding_role in interaction.user.roles:
            return await interaction.response.send_message(t("admin.already_accepted_rules"), ephemeral=True)

        await interaction.user.add_roles(onboarding_role)
        await interaction.response.send_message(t("admin.rules_accepted_success"), ephemeral=True)

        # Store completion time for moderation analytics after the role change succeeds.
        time_taken = discord.utils.utcnow() - interaction.user.joined_at
        seconds_taken = int(time_taken.total_seconds())
        await database.run(database.set_rules_read_time, interaction.user.id, seconds_taken)
        
class EnterServerView(discord.ui.View):
    """The entry gate's button. Same label contract as `RuleAcceptView`.

    It grants the *member* role and takes the onboarding role away, which is the
    opposite end of the funnel from the rules button — the two are easy to
    confuse and do different things.
    """

    def __init__(self, *, label: str = None):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=label or t("admin.enter_server_button"),
            style=discord.ButtonStyle.success, custom_id="enter_server_btn",
            emoji="🚀")
        button.callback = self.enter_server
        self.add_item(button)

    async def interaction_check(self, interaction):
        return await check_onboarding_interaction(interaction)

    async def enter_server(self, interaction: discord.Interaction):
        onboarding_role = interaction.guild.get_role(
            guild_setting_sync(interaction.guild.id, "onboarding_role") or 0)
        member_role = interaction.guild.get_role(
            guild_setting_sync(interaction.guild.id, "member_role") or 0)

        if not onboarding_role or not member_role:
            return await interaction.response.send_message(t("admin.role_not_found"), ephemeral=True)
        if not can_self_assign_role(interaction.guild, member_role):
            return await interaction.response.send_message(t("admin.role_not_found"), ephemeral=True)

        if member_role in interaction.user.roles:
            return await interaction.response.send_message(t("admin.already_member"), ephemeral=True)

        if onboarding_role in interaction.user.roles:
            await interaction.user.remove_roles(onboarding_role)
        
        await interaction.user.add_roles(member_role)
        await interaction.response.send_message(t("admin.welcome_to_server"), ephemeral=True)

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(RuleAcceptView())
        self.bot.add_view(EnterServerView())
    
    @commands.hybrid_command(name="sync_autoroles", description=t("general.cmd_sync_autoroles"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def sync_autoroles(self, ctx):
        autorole_ids = guild_setting_sync(ctx.guild.id, "autoroles")
        if not autorole_ids:
            return await ctx.send(t("admin.no_autoroles_config"), ephemeral=True)

        # Bulk assignment screens roles exactly like every other role path, so a
        # privileged or unmanageable id in configuration cannot be handed out.
        roles_to_add = [
            role
            for role in (ctx.guild.get_role(r_id) for r_id in autorole_ids)
            if role is not None and can_self_assign_role(ctx.guild, role)
        ]
        if not roles_to_add:
            return await ctx.send(t("admin.invalid_autorole_ids"), ephemeral=True)

        updated_count = 0
        error_count = 0

        for member in ctx.guild.members:
            if member.bot: continue
            missing_roles = [role for role in roles_to_add if role not in member.roles]

            if missing_roles:
                try:
                    await member.add_roles(*missing_roles)
                    updated_count += 1
                    await asyncio.sleep(0.5)
                except discord.Forbidden:
                    error_count += 1
                except Exception as e:
                    error_count += 1

        embed = discord.Embed(
            title=t("admin.autorole_sync_title"),
            description=t("admin.autorole_sync_desc", count=updated_count),
            color=discord.Color.green()
        )
        if error_count > 0:
            embed.set_footer(text=t("admin.autorole_sync_warn", count=error_count))

        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="testreset", description=t("general.cmd_testreset"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def testreset(self, ctx, member: discord.Member = None):
        target = member or ctx.author
    
        if not await database.run(database.user_exists, target.id):
            return await ctx.send(t("admin.user_not_in_db", user=target.display_name), ephemeral=True)

        await database.run(database.reset_user_cooldowns, target.id)

        embed = discord.Embed(
            title=t("admin.timers_reset_title"),
            description=t("admin.timers_reset_desc", user=target.display_name),
            color=discord.Color.green()
        )
        embed.add_field(name=t("admin.affected_systems"), value=t("admin.affected_systems_value"))
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="checkperms", description=t("general.cmd_checkperms"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def checkperms(self, ctx):
        """Report whether this guild's enabled features can actually run.

        The report is built by `permission_audit`, which the dashboard's
        diagnostics endpoint also uses, so the two can never disagree about what
        this installation needs.
        """
        feature_states = await database.run_read(
            database.get_feature_states, ctx.guild.id)
        report = permission_audit.build_report(
            ctx.guild, feature_states,
            # Resolved through the settings cache, which owns the same fallback
            # chain `permission_audit.resolved_settings` reproduces by hand —
            # stored row, then `config.json`, then the registry default. That
            # fallback is not optional: `guild_settings` is sparse, so without it
            # every channel and role resolves to nothing and the report comes
            # back clean because it checked nothing. This command used to pass
            # `config` directly and the name stopped being imported, so it raised
            # instead.
            guild_settings_sync(ctx.guild.id, SETTING_DEFINITIONS),
        )

        blocking = report.blocking
        colour = discord.Color.red() if blocking else discord.Color.green()
        embed = discord.Embed(
            title=t("admin.permissions_title"),
            description=(
                t("admin.permissions_blocking_summary", count=len(blocking))
                if blocking else t("admin.permissions_all_clear")
            ),
            color=colour,
        )

        for finding in report.findings[:20]:
            embed.add_field(
                name=describe_permission_finding_title(finding),
                value=describe_permission_finding(finding),
                inline=False,
            )
        if len(report.findings) > 20:
            embed.add_field(
                name="\u200b",
                value=t("admin.permissions_more_findings",
                        count=len(report.findings) - 20),
                inline=False,
            )

        enabled = [entry for entry in report.features if entry["enabled"]]
        embed.set_footer(text=t(
            "admin.permissions_footer",
            enabled=len(enabled), total=len(report.features),
            healthy=len([entry for entry in enabled if not entry["missing"]]),
        ))
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="maintenance", description=t("general.cmd_maintenance"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def maintenance(self, ctx, status: bool):
        # The break-glass path for when the dashboard is the broken thing, and
        # it writes the same row the dashboard writes — through the one write
        # path, so the value is validated, the revision bumps and the audit row
        # commits with it. It used to rewrite config.json instead.
        await set_guild_setting(ctx.guild.id, ctx.author.id, "maintenance", status)

        state = t("admin.maintenance_enabled") if status else t("admin.maintenance_disabled")
        await ctx.send(t("admin.maintenance_status", state=state))

    @commands.hybrid_command(name="testboost", description=t("general.cmd_testboost"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def testboost(self, ctx):
        bonus_amount = 2000
        await update_user_data(ctx.author, balance_change=bonus_amount)
    
        target_id = guild_setting_sync(ctx.guild.id, "booster_channel")
        channel = self.bot.get_channel(target_id)
    
        if channel:
            embed = discord.Embed(
                title=t("admin.testboost_title"),
                description=t("admin.testboost_desc", user=ctx.author.mention, amount=bonus_amount),
                color=0xff73fa
            )
            await channel.send(embed=embed)
            await ctx.send(t("admin.testboost_success", channel=channel.mention), ephemeral=True)
        else:
            await ctx.send(t("admin.testboost_channel_missing", channel_id=target_id), ephemeral=True)

    @commands.hybrid_command(name="rent_start", description=t("general.cmd_rent_start"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def rent_start(self, ctx, item_type: str, item_id: str):
        if item_type not in ["emoji", "sound"]:
            return await ctx.send(t("admin.invalid_rent_type"), ephemeral=True)

        expires = (datetime.now() + timedelta(days=30)).isoformat()
        await database.run(database.add_rented_item, item_type, item_id, expires, ctx.guild.id)

        await ctx.send(t("admin.rent_registered", type=item_type, id=item_id), ephemeral=True)

    @commands.hybrid_command(name="award", description=t("general.cmd_award"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def award(self, ctx, member: discord.Member, amount: int):
        new_bal, _, _, _, _ = await update_user_data(member, balance_change=amount, xp_change=0)

        title = t("admin.award_add_title") if amount > 0 else t("admin.award_remove_title")
        color = discord.Color.green() if amount > 0 else discord.Color.red()
    
        embed = discord.Embed(title=title, color=color)
        embed.description = t("admin.award_desc", admin=ctx.author.display_name, user=member.mention)
        embed.add_field(name=t("admin.award_change"), value=f"{amount}{currency_emoji()}")
        embed.add_field(name=t("admin.award_new_bal"), value=f"{new_bal}{currency_emoji()}")
    
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="awardall", description=t("general.cmd_awardall"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def awardall(self, ctx, amount: int):
        if amount <= 0:
            return await ctx.send(t("admin.awardall_negative_error"), ephemeral=True)

        member_ids = [member.id for member in ctx.guild.members if not member.bot]
        count = await database.run(database.apply_batch_balance, member_ids, amount)
            
        embed = discord.Embed(
            title=t("admin.awardall_title"), 
            description=t("admin.awardall_desc", admin=ctx.author.display_name, amount=amount), 
            color=discord.Color.gold()
        )
        embed.set_footer(text=t("admin.awardall_footer", count=count))
        await ctx.send(embed=embed)

    @award.error
    async def award_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(t("admin.admin_required"), ephemeral=True)

    @commands.hybrid_command(name="embedsend", description=t("general.cmd_embedsend"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def embedsend(self, ctx, icons: bool = True):
        if ctx.interaction:
            await ctx.interaction.response.send_modal(EmbedSendModal(show_icon=icons))
        else:
            await ctx.send(t("admin.use_slash_command", cmd="embedsend"), ephemeral=True)

    @commands.hybrid_command(name="setup_enter", description=t("general.cmd_setup_enter"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_enter(self, ctx):
        stored = await store_simple_panel(
            ctx, "airlock", ENTRY_GATE_KEY,
            t("admin.airlock_title"), t("admin.airlock_desc"),
            discord.Color.green().value)
        embeds, view = render_managed_message(ctx.guild, stored)
        if embeds is None:
            return await ctx.send(t("admin.operation_failed"), ephemeral=True)
        message = await ctx.channel.send(embeds=embeds, view=view)
        await database.run_write(database.record_managed_post, ctx.guild.id,
                                 "airlock", ENTRY_GATE_KEY, ctx.channel.id,
                                 message.id)
        if ctx.interaction:
            await ctx.send(t("utils.command_completed"), ephemeral=True)

    @commands.hybrid_command(name="update_enter", description=t("general.cmd_update_enter"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_enter(self, ctx):
        """Re-render the entry gate this guild already posted.

        The message id was an argument until the panel started recording where
        it went. Nothing stored is a specific answer rather than the generic
        failure a mistyped id produced.
        """
        stored = await database.run_read(database.get_managed_message,
                                         ctx.guild.id, "airlock", ENTRY_GATE_KEY)
        if not stored or not stored.get("message_id"):
            return await ctx.send(t("admin.airlock_not_posted"), ephemeral=True)
        try:
            channel = (self.bot.get_channel(int(stored["channel_id"]))
                       if stored.get("channel_id") else ctx.channel)
            message = await (channel or ctx.channel).fetch_message(
                int(stored["message_id"]))
            embeds, view = render_managed_message(ctx.guild, stored)
            if embeds is None:
                return await ctx.send(t("admin.operation_failed"), ephemeral=True)
            await message.edit(embeds=embeds, view=view)
            await ctx.send(t("admin.airlock_updated"), ephemeral=True)
        except (discord.HTTPException, ValueError, TypeError):
            # Narrow, and `.exception` rather than `.warning`: the old handler
            # logged only the exception's class name, which is how a NameError in
            # here would have been reported as five characters and nothing else.
            admin_logger.exception(
                "Airlock message update failed (guild_id=%s)", ctx.guild.id)
            await ctx.send(t("admin.operation_failed"), ephemeral=True)

    @commands.hybrid_command(name="rules_group", description=t("general.cmd_rules_group"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def rules_group(self, ctx, color_hex: str,
                            title1: str, msg1: str,
                            title2: str = None, msg2: str = None,
                            title3: str = None, msg3: str = None,
                            title4: str = None, msg4: str = None,
                            title5: str = None, msg5: str = None,
                            title6: str = None, msg6: str = None,
                            msg7: str = None, add_button: bool = False):
        stored = await store_rules_panel(ctx, color_hex, add_button, locals())
        embeds, view = render_managed_message(ctx.guild, stored)
        if embeds is None:
            return await ctx.send(t("admin.operation_failed"), ephemeral=True)
        message = await ctx.channel.send(embeds=embeds, view=view)
        # Remembered, so `/update_rules_group` and the dashboard both know which
        # message this is without being handed its id.
        await database.run_write(database.record_managed_post, ctx.guild.id,
                                 "rules", RULES_PANEL_KEY, ctx.channel.id,
                                 message.id)
        await ctx.send(t("admin.rules_posted"), ephemeral=True)

    @commands.hybrid_command(name="getraw", description=t("general.cmd_getraw"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @is_staff()
    async def getraw(self, ctx, message_id: str):
        try:
            msg = await ctx.channel.fetch_message(int(message_id.strip()))
        except (discord.HTTPException, ValueError):
            return await ctx.send(t("admin.raw_message_not_found"), ephemeral=True)
        # A code block preserves raw markup instead of rendering Discord entities.
        await ctx.send(f"```text\n{msg.content}\n```", ephemeral=True)

    @commands.hybrid_command(name="update_rules_group", description=t("general.cmd_update_rules_group"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_rules_group(self, ctx, color_hex: str,
                            title1: str, msg1: str,
                            title2: str = None, msg2: str = None,
                            title3: str = None, msg3: str = None,
                            title4: str = None, msg4: str = None,
                            title5: str = None, msg5: str = None,
                            title6: str = None, msg6: str = None,
                            msg7: str = None, add_button: bool = False):
        """Edit the rules panel this guild already posted.

        The message id was an argument until schema 12 recorded where the panel
        went. Nothing stored means nothing to edit, which is a specific answer
        rather than the generic failure a mistyped id used to produce.
        """
        existing = await database.run_read(database.get_managed_message,
                                           ctx.guild.id, "rules", RULES_PANEL_KEY)
        if not existing or not existing.get("message_id"):
            return await ctx.send(t("admin.rules_not_posted"), ephemeral=True)
        try:
            channel = (self.bot.get_channel(int(existing["channel_id"]))
                       if existing.get("channel_id") else ctx.channel)
            message = await (channel or ctx.channel).fetch_message(
                int(existing["message_id"]))
        except (discord.HTTPException, ValueError, TypeError):
            admin_logger.exception(
                "Rules message could not be fetched (guild_id=%s)", ctx.guild.id)
            return await ctx.send(t("admin.rules_not_posted"), ephemeral=True)
        # The row is written outside the edit's handler on purpose. It used to sit
        # inside one `except Exception`, so an edit that failed reported
        # "operation failed" for a write that had in fact landed.
        stored = await store_rules_panel(ctx, color_hex, add_button, locals())
        embeds, view = render_managed_message(ctx.guild, stored)
        if embeds is None:
            return await ctx.send(t("admin.operation_failed"), ephemeral=True)
        try:
            await message.edit(embeds=embeds, view=view)
        except discord.HTTPException:
            admin_logger.exception(
                "Rules message edit failed (guild_id=%s)", ctx.guild.id)
            return await ctx.send(t("admin.rules_saved_edit_failed"), ephemeral=True)
        await ctx.send(t("admin.rules_updated"), ephemeral=True)

    @commands.hybrid_command(name="rules_verify", description=t("general.cmd_rules_verify"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def rules_verify(self, ctx, color_hex: str, title: str, message: str, banner_url: str = None):
        try:
            target_color = discord.Color.from_str(color_hex)
        except:
            target_color = discord.Color.blue()

        embed = discord.Embed(title=title, description=message.replace("\\n", "\n"), color=target_color)
        if banner_url: embed.set_image(url=banner_url)
        if ctx.guild.icon: embed.set_thumbnail(url=ctx.guild.icon.url)

        await ctx.channel.send(embed=embed, view=RuleAcceptView())
        if ctx.interaction:
            await ctx.send(t("utils.command_completed"), ephemeral=True)

async def setup(bot):
    await bot.add_cog(Admin(bot))
