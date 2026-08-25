import discord
import logging
import os
import re
import sys
import time
import unicodedata

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database

from bounded import BoundedValueMap
from discord.ext import commands
from datetime import datetime, timedelta
from feature_access import is_enabled, maintenance_blocks
from settings_registry import WARN_DEFAULT_TAG, WARN_TAGS
from cogs.utils import (is_staff, is_higher_than, role_autocomplete, t,
                        guild_setting_sync, guild_settings_many)

moderation_logger = logging.getLogger("PotatoBot.Moderation")

# Moderation commands enforce both Discord hierarchy and configured staff policy.

# Command metadata is built at import time, so the choice labels are too. The
# values are the stable English identifiers written into every warning row.
WARN_TAG_CHOICES = [
    discord.app_commands.Choice(name=t(f"moderation.warn_tags.{tag}"), value=tag)
    for tag in WARN_TAGS
]

# How long a guild's filter configuration is reused before being re-read. This
# runs on every message, so reading four settings per message is not an option;
# a minute of staleness on a word list is the accepted price.
FILTER_CACHE_SECONDS = 60

# Characters people substitute to slip a word past a literal comparison. Applied
# after accent stripping, so only the shapes that are not already decomposable
# need to be here.
FILTER_HOMOGLYPHS = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
    "@": "a", "$": "s", "!": "i", "|": "i", "\u0142": "l", "\u00f8": "o",
})

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_REPEATED = re.compile(r"(.)\1+")


def normalise_for_filter(text: str) -> str:
    """Fold text to the form a word filter should compare against.

    Casefold, decompose and drop combining marks so an accent cannot smuggle a
    word past, map the common digit-and-symbol substitutions, remove everything
    that is not a letter or digit so `b.a.d` and `b a d` do not evade, then
    collapse a run of one character to a single one so `baaad` does not either.

    Both the message and every listed word go through this, which is what keeps
    the comparison consistent — and is also the trade being made: removing the
    separators means a listed word matches inside a longer one, and collapsing
    repeats widens a word by one letter shape. A filter a full stop defeats is
    worth nothing, and a false positive is visible and fixable where an evasion
    is silent. Operators list distinctive words and exempt their staff.
    """
    folded = unicodedata.normalize("NFKD", str(text or "")).casefold()
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    mapped = stripped.translate(FILTER_HOMOGLYPHS)
    compact = _NON_ALPHANUMERIC.sub("", mapped)
    return _REPEATED.sub(r"\1", compact)

async def apply_warn_escalation(guild, member, tag: str, tag_count: int,
                                reason: str) -> str | None:
    """Alert and act when this tag's threshold has been reached.

    Alerting and acting are two flags on purpose, and they fail in opposite
    directions: a missed alert is an inconvenience, a wrong ban is not. Alerting
    therefore works with actions off, which is the whole point of the split.

    A threshold of 0 means never act. That is a decision, not an unset value —
    it is what every tag ships with, so upgrading an installation cannot start
    handing out consequences nobody configured.

    Returns the action actually applied, or None.
    """
    settings = await guild_settings_many(guild.id, (
        f"warn_threshold_{tag}", f"warn_action_{tag}",
        f"warn_timeout_minutes_{tag}", "moderation_log_channel",
    ))
    try:
        threshold = int(settings.get(f"warn_threshold_{tag}") or 0)
    except (TypeError, ValueError):
        threshold = 0
    if threshold <= 0 or tag_count < threshold:
        return None

    action = str(settings.get(f"warn_action_{tag}") or "none")
    acting = (is_enabled(guild.id, "moderation_warn_actions")
              and action != "none")

    # Never escalate against somebody the guild cannot afford to lose to a
    # miscounted threshold, and never against somebody the bot could not undo.
    blocked = None
    if acting:
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            blocked = "protected"
        elif guild.me.top_role <= member.top_role:
            blocked = "hierarchy"

    applied = None
    if acting and blocked is None:
        audit_reason = t("moderation.escalation_audit_reason",
                         tag=t(f"moderation.warn_tags.{tag}"), count=tag_count)
        try:
            if action == "timeout":
                minutes = int(settings.get(f"warn_timeout_minutes_{tag}") or 60)
                await member.timeout(timedelta(minutes=minutes),
                                     reason=audit_reason)
            elif action == "kick":
                await member.kick(reason=audit_reason)
            elif action == "ban":
                await member.ban(reason=audit_reason,
                                 delete_message_seconds=0)
            applied = action
        except discord.Forbidden:
            blocked = "forbidden"
        except discord.HTTPException:
            moderation_logger.exception(
                "Warn escalation failed (guild_id=%s, action=%s)",
                guild.id, action,
            )
            blocked = "failed"

    # Posted after the attempt so the record says what happened, not what was
    # intended, and posted even when actions are off — that is the split.
    if is_enabled(guild.id, "moderation_warn_alerts"):
        channel_id = settings.get("moderation_log_channel")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is not None:
            outcome = (t(f"moderation.escalation_applied_{applied}") if applied
                       else t(f"moderation.escalation_blocked_{blocked}") if blocked
                       else t("moderation.escalation_alert_only"))
            embed = discord.Embed(
                title=t("moderation.escalation_title"),
                description=t("moderation.escalation_body",
                              user=member.mention,
                              tag=t(f"moderation.warn_tags.{tag}"),
                              count=tag_count, threshold=threshold),
                color=discord.Color.red() if applied else discord.Color.orange(),
            )
            embed.add_field(name=t("moderation.escalation_outcome_label"),
                            value=outcome, inline=False)
            # The reason is member-supplied text on the filter path.
            embed.add_field(name=t("moderation.reason_label"),
                            value=discord.utils.escape_mentions(reason)[:1024],
                            inline=False)
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                moderation_logger.warning(
                    "Could not post a warn escalation alert (guild_id=%s, "
                    "channel_id=%s)", guild.id, channel_id,
                )
    return applied


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # One entry per guild holding the filter's compiled word list and the
        # three settings around it, because on_message must not read four
        # settings per message. Bounded, so a bot in many guilds cannot grow it
        # without limit.
        self._filter_cache = BoundedValueMap(max_entries=512)

    async def _filter_config(self, guild_id: int) -> dict:
        """This guild's filter configuration, re-read at most once a minute."""
        cached = self._filter_cache.get(guild_id)
        if cached is not None and time.monotonic() < cached["expires"]:
            return cached
        settings = await guild_settings_many(guild_id, (
            "word_filter_words", "word_filter_exempt_roles",
            "word_filter_tag", "word_filter_delete_message",
        ))
        # Normalised here, once, because the message side is normalised too and
        # the two are only comparable if folded the same way. An entry that
        # normalises to nothing — punctuation only — is dropped rather than
        # matching every message.
        needles = sorted({
            folded for folded in
            (normalise_for_filter(word) for word in
             (settings.get("word_filter_words") or []))
            if folded
        }, key=len)
        exempt = set()
        for role_id in settings.get("word_filter_exempt_roles") or ():
            try:
                exempt.add(int(role_id))
            except (TypeError, ValueError):
                continue
        tag = settings.get("word_filter_tag")
        entry = {
            "expires": time.monotonic() + FILTER_CACHE_SECONDS,
            "needles": needles,
            "exempt": exempt,
            "tag": tag if tag in WARN_TAGS else WARN_DEFAULT_TAG,
            "delete": bool(settings.get("word_filter_delete_message", True)),
        }
        self._filter_cache[guild_id] = entry
        return entry

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Delete a filtered message and warn under the configured tag.

        The filter deliberately does not decide a consequence. It files a
        warning and lets the tag's threshold decide, so a guild configures
        escalation once instead of the filter carrying a second copy of it.
        """
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if not is_enabled(message.guild.id, "moderation_word_filter"):
            return
        # Maintenance is the emergency stop and outranks the flag, including for
        # an automated action nobody asked for at that moment.
        if maintenance_blocks(message.guild, message.author):
            return

        member = message.author
        # Staff are exempt by permission as well as by configured role: a filter
        # that times out the moderators is worse than no filter at all.
        if (member.id == message.guild.owner_id
                or member.guild_permissions.manage_messages):
            return

        settings = await self._filter_config(message.guild.id)
        if not settings["needles"]:
            return
        if settings["exempt"] & {role.id for role in member.roles}:
            return
        folded = normalise_for_filter(message.content)
        if not folded:
            return
        matched = next((needle for needle in settings["needles"]
                        if needle in folded), None)
        if matched is None:
            return

        channel_name = getattr(message.channel, "name", "?")
        if settings["delete"]:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

        # The stored reason names no word. It is read back in /modlogs and in
        # the escalation alert, and a warning record is not the place for the
        # thing the guild is trying to stop repeating.
        tag = settings["tag"]
        record = await database.run(
            database.record_warning, member.id, self.bot.user.id,
            t("moderation.filter_warn_reason", channel=channel_name),
            datetime.now().isoformat(), message.guild.id, tag,
        )

        # Tell the member privately. There is no public notice at all: naming
        # the rule in the channel repeats what was just deleted.
        try:
            await member.send(t("moderation.filter_member_notice",
                                guild=message.guild.name))
        except discord.HTTPException:
            pass

        # The matched term goes to the moderation log and nowhere else, so staff
        # can see which entry fired without it being said out loud again.
        await self._report_filter_match(message.guild, member, matched,
                                        channel_name, record)
        await apply_warn_escalation(
            message.guild, member, tag, record["tag_count"],
            t("moderation.filter_warn_reason", channel=channel_name),
        )

    async def _report_filter_match(self, guild, member, matched, channel_name,
                                   record):
        settings = await guild_settings_many(guild.id, ("moderation_log_channel",))
        channel_id = settings.get("moderation_log_channel")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return
        embed = discord.Embed(title=t("moderation.filter_alert_title"),
                              color=discord.Color.orange())
        embed.add_field(name=t("moderation.user_label"), value=member.mention)
        embed.add_field(name=t("moderation.filter_channel_label"),
                        value=f"#{channel_name}")
        # Escaped: an operator authored the list, and it reaches message content.
        embed.add_field(name=t("moderation.filter_match_label"),
                        value=f"`{discord.utils.escape_mentions(matched)[:200]}`",
                        inline=False)
        embed.set_footer(text=t("moderation.warn_footer_tagged",
                                count=record["total"],
                                tag_count=record["tag_count"]))
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            moderation_logger.warning(
                "Could not post a word-filter alert (guild_id=%s)", guild.id
            )

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
        protected_category_id = guild_setting_sync(ctx.guild.id, "admin_category")
    
        if ctx.channel.category_id == protected_category_id:
            return await ctx.send(t("moderation.purge_protected_error"), ephemeral=True)

        if not 1 <= amount <= 100:
            return await ctx.send(t("moderation.purge_limit_error"), ephemeral=True)
        
        deleted = await ctx.channel.purge(limit=amount)
    
        await ctx.send(t("moderation.purge_success", count=len(deleted), channel=ctx.channel.name), ephemeral=True)

    @commands.hybrid_command(name="warn", description=t("general.cmd_warn"))
    @discord.app_commands.default_permissions(moderate_members=True)
    @discord.app_commands.choices(tag=WARN_TAG_CHOICES)
    @discord.app_commands.describe(tag=t("moderation.warn_tag_description"))
    @is_staff()
    async def warn(self, ctx, member: discord.Member, tag: str = None, *,
                   reason: str = None):
        if member.id == ctx.author.id:
            return await ctx.send(t("moderation.self_warn_error"), ephemeral=True)
    
        if not is_higher_than(ctx.author, member):
            return await ctx.send(t("moderation.hierarchy_error", user=member.mention), ephemeral=True)

        # The slash form always sends one of the choices, so this only ever
        # fires on the prefix form, which cannot tell a tag from the first word
        # of a reason. Give the token back to the reason rather than silently
        # eating it: `?warn @user being rude` must not file "rude" as the whole
        # reason. `reason` is therefore optional in the signature and required
        # here, which is also what makes a one-word reason still work.
        if tag is not None and tag not in WARN_TAGS:
            reason = f"{tag} {reason}".strip() if reason else tag
            tag = None
        tag = tag or WARN_DEFAULT_TAG
        if not reason or not reason.strip():
            return await ctx.send(t("moderation.warn_reason_required"),
                                  ephemeral=True)

        # Persist the warning and read back the counts it produced, in one
        # transaction, because the threshold below is compared against them.
        record = await database.run(
            database.record_warning, member.id, ctx.author.id, reason,
            datetime.now().isoformat(), ctx.guild.id, tag,
        )

        # Publish the moderation result to the invoking channel.
        embed = discord.Embed(title=t("moderation.warn_embed_title"), color=discord.Color.gold())
        embed.add_field(name=t("moderation.user_label"), value=member.mention)
        embed.add_field(name=t("moderation.tag_label"),
                        value=t(f"moderation.warn_tags.{tag}"))
        embed.add_field(name=t("moderation.reason_label"),
                        value=discord.utils.escape_mentions(reason), inline=False)
        embed.set_footer(text=t("moderation.warn_footer_tagged",
                                count=record["total"],
                                tag_count=record["tag_count"]))
    
        await ctx.send(embed=embed)

        # After the public result, so a member sees the warning even when the
        # consequence cannot be applied.
        await apply_warn_escalation(ctx.guild, member, tag,
                                    record["tag_count"], reason)

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
            for warning_id, reason, date, mod_id, tag in warnings:
                # A pre-schema-10 row has no tag and reads as the default.
                tag_label = t(f"moderation.warn_tags."
                              f"{tag if tag in WARN_TAGS else WARN_DEFAULT_TAG}")
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
                    value=t("moderation.warn_entry_body", tag=tag_label,
                            reason=reason),
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
        factions_config = guild_setting_sync(ctx.guild.id, "factions")

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
