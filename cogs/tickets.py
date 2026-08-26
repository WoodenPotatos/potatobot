import discord
import asyncio
import os
import sys
import io
import time

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from discord.ext import commands
from datetime import datetime
from cogs.utils import BoundedCooldownMap, t, guild_setting_sync
import database
from feature_access import require_interaction_feature
from managed_messages import render_managed_message
from support_tickets import open_ticket

TICKET_OPEN_COOLDOWN = 300
TRANSCRIPT_MESSAGE_LIMIT = 50_000
TRANSCRIPT_PART_LIMIT = 5
TRANSCRIPT_PART_BYTES = 8 * 1024 * 1024 - 64 * 1024
ticket_open_times = BoundedCooldownMap()
ticket_control_times = BoundedCooldownMap()


def is_ticket_staff(member):
    staff_role_ids = guild_setting_sync(member.guild.id, "admin_roles")
    return member.guild_permissions.administrator or any(
        role.id in staff_role_ids for role in member.roles
    )


async def resolve_claimer_name(channel):
    """Resolve the stored claimer to a display name for the transcript header."""
    claimer_id = await database.run(database.get_ticket_claimer, channel.id)
    if not claimer_id:
        return t("tickets.nobody")
    member = channel.guild.get_member(claimer_id)
    return member.display_name if member else str(claimer_id)


async def get_ticket_opener(channel):
    opener_id = await database.run(database.get_ticket_opener, channel.id)
    if opener_id is not None:
        return opener_id

    # Backfill tickets created before the ownership migration from their
    # explicit member overwrite. New tickets always use the database record.
    candidates = [
        target.id
        for target, overwrite in channel.overwrites.items()
        if isinstance(target, discord.Member)
        and not target.bot
        and overwrite.view_channel is True
    ]
    if len(candidates) == 1:
        await database.run(database.add_ticket, channel.id, candidates[0], channel.guild.id)
        return candidates[0]
    return None

# Persistent ticket launcher and opener/staff-authorized controls.

# The one launcher `/setup_tickets` addresses; a guild may create others from
# the dashboard, for a second support channel.
TICKET_LAUNCHER_KEY = "ticket"


class TicketLauncher(discord.ui.View):
    """The launcher's button, whose label an operator may set.

    The instance registered with `bot.add_view` takes no label and keeps the
    shipped one — a click routes by `custom_id`, never by label — so only the
    instance built for one posted message carries the operator's text.
    """

    def __init__(self, *, label: str = None):
        super().__init__(timeout=None)

        # Construct the persistent button at runtime so its label is localized.
        btn_ticket = discord.ui.Button(label=label or t("tickets.open_btn"), style=discord.ButtonStyle.blurple, custom_id="ticket_button", emoji="📩")
        btn_ticket.callback = self.ticket
        self.add_item(btn_ticket)

    async def ticket(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "tickets"):
            return
        now = time.monotonic()
        retry_after = TICKET_OPEN_COOLDOWN - (
            now - ticket_open_times.get(interaction.user.id, 0)
        )
        if retry_after > 0:
            return await interaction.response.send_message(
                t("tickets.open_cooldown", seconds=int(retry_after) + 1),
                ephemeral=True,
            )

        existing_id = await database.run(
            database.get_open_support_ticket,
            interaction.guild.id, interaction.user.id,
        )
        if existing_id:
            existing_channel = interaction.guild.get_channel(existing_id)
            if existing_channel:
                return await interaction.response.send_message(
                    t("tickets.already_open", channel=existing_channel.mention),
                    ephemeral=True,
                )
            await database.run(database.remove_ticket, existing_id)

        ticket_name = f"ticket-{interaction.user.id}"

        ticket_open_times[interaction.user.id] = now
        await interaction.response.defer(ephemeral=True)

        channel = await open_ticket(
            interaction.guild, interaction.user, ticket_name, "support")
        if channel is None:
            # The cooldown is released: nothing was opened, so the member has
            # not used their attempt.
            ticket_open_times.pop(interaction.user.id, None)
            return await interaction.followup.send(
                t("tickets.open_failed"), ephemeral=True)

        embed = discord.Embed(
            title=t("tickets.created_title"), 
            description=t("tickets.created_desc"), 
            color=discord.Color.green()
        )
        
        await channel.send(embed=embed, view=TicketControl())
        await interaction.followup.send(t("tickets.opened_success", channel=channel.mention), ephemeral=True)

class TicketCloseModal(discord.ui.Modal):
    def __init__(self, claimer_name):
        super().__init__(title=t("tickets.close_modal_title"))
        self.claimer_name = claimer_name
        
        self.reason = discord.ui.TextInput(
            label=t("tickets.close_reason_label"), 
            style=discord.TextStyle.paragraph, 
            min_length=5
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "tickets"):
            return
        await interaction.response.defer(ephemeral=True)
        opener_id = await get_ticket_opener(interaction.channel)
        if interaction.user.id != opener_id and not is_ticket_staff(interaction.user):
            return await interaction.followup.send(
                t("tickets.close_forbidden"), ephemeral=True
            )
        
        header = t(
            "tickets.log_header", channel=interaction.channel.name,
            mod=self.claimer_name, reason=self.reason.value,
        ).encode("utf-8")
        parts = [bytearray(header)]
        truncated = False
        message_count = 0
        async for m in interaction.channel.history(
            limit=TRANSCRIPT_MESSAGE_LIMIT + 1, oldest_first=True,
        ):
            if message_count >= TRANSCRIPT_MESSAGE_LIMIT:
                truncated = True
                break
            line = (
                f"[{m.created_at.strftime('%Y-%m-%d %H:%M')}] "
                f"{m.author.display_name}: {m.content}\n"
            ).encode("utf-8", errors="replace")
            if len(line) > TRANSCRIPT_PART_BYTES:
                line = line[:TRANSCRIPT_PART_BYTES] + b"\n"
                truncated = True
            if len(parts[-1]) + len(line) > TRANSCRIPT_PART_BYTES:
                if len(parts) >= TRANSCRIPT_PART_LIMIT:
                    truncated = True
                    break
                parts.append(bytearray())
            parts[-1].extend(line)
            message_count += 1

        if truncated:
            marker = ("\n" + t("tickets.log_truncated") + "\n").encode("utf-8")
            if len(parts[-1]) + len(marker) <= TRANSCRIPT_PART_BYTES:
                parts[-1].extend(marker)

        log_channel = interaction.guild.get_channel(
            guild_setting_sync(interaction.guild.id, "ticket_logs"))
        if log_channel:
            base_name = (
                f"log_{interaction.channel.name}_"
                f"{datetime.now().strftime('%Y-%m-%d')}"
            )
            files = [
                discord.File(
                    io.BytesIO(bytes(part)),
                    filename=f"{base_name}_part-{index}.txt",
                )
                for index, part in enumerate(parts, start=1)
            ]
            await log_channel.send(
                content=t("tickets.closed_log_msg", channel=interaction.channel.name),
                files=files,
            )

        await interaction.channel.send(t("tickets.logged_deleting"))
        await asyncio.sleep(3)
        await database.run(database.remove_ticket, interaction.channel.id)
        await interaction.channel.delete()

class TicketControl(discord.ui.View):
    """Persistent ticket controls.

    One instance serves every ticket channel after `bot.add_view`, so no
    per-ticket state may live on the view itself. The claimer is persisted on the
    ticket row, and the claimed button state is rendered into a fresh view for
    the message being edited.
    """

    def __init__(self, claimer_name: str | None = None):
        super().__init__(timeout=None)

        btn_claim = discord.ui.Button(
            label=(
                t("tickets.claimed_by", user=claimer_name)
                if claimer_name else t("tickets.claim_btn")
            ),
            style=discord.ButtonStyle.green,
            custom_id="claim_t",
            disabled=claimer_name is not None,
        )
        btn_claim.callback = self.claim
        self.add_item(btn_claim)

        btn_close = discord.ui.Button(label=t("tickets.close_btn"), style=discord.ButtonStyle.red, custom_id="close_t")
        btn_close.callback = self.close
        self.add_item(btn_close)

    async def interaction_check(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "tickets"):
            return False
        now = time.monotonic()
        retry_after = 3 - (now - ticket_control_times.get(interaction.user.id, 0))
        if retry_after > 0:
            await interaction.response.send_message(
                t("utils.command_cooldown", seconds=int(retry_after) + 1),
                ephemeral=True,
            )
            return False
        ticket_control_times[interaction.user.id] = now
        return True

    async def claim(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await interaction.response.send_message(t("tickets.staff_only"), ephemeral=True)
        await database.run(database.set_ticket_claimer,
                           interaction.channel.id, interaction.user.id)
        await interaction.response.edit_message(
            view=TicketControl(interaction.user.display_name)
        )

    async def close(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            TicketCloseModal(await resolve_claimer_name(interaction.channel))
        )

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(TicketControl())
        self.bot.add_view(TicketLauncher())
    
    @commands.hybrid_command(name="setup_tickets", description=t("general.cmd_setup_tickets"))
    @discord.app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_tickets(self, ctx):
        from cogs.admin import store_simple_panel
        stored = await store_simple_panel(
            ctx, "ticket", TICKET_LAUNCHER_KEY,
            t("tickets.setup_title"), t("tickets.setup_desc"),
            discord.Color.blue().value)
        embeds, view = render_managed_message(ctx.guild, stored)
        if embeds is None:
            return await ctx.send(t("tickets.operation_failed"), ephemeral=True)
        message = await ctx.channel.send(embeds=embeds, view=view)
        # Recorded, so the dashboard can edit the launcher this command posted
        # instead of being unable to see it.
        await database.run_write(database.record_managed_post, ctx.guild.id,
                                 "ticket", TICKET_LAUNCHER_KEY, ctx.channel.id,
                                 message.id)
        if ctx.interaction:
            await ctx.send(t("utils.command_completed"), ephemeral=True)

async def setup(bot):
    await bot.add_cog(Tickets(bot))
