"""Member-driven data export and erasure, plus the retention sweep.

``docs/privacy.md`` requires authenticated export and deletion workflows. Members
cannot sign in to the dashboard — only the host and guild administrators can — so
the member-facing half lives here as Discord commands and the operator sees the
outcome through the audit feed. Erasure is installation-wide by necessity: wallets
are keyed only by Discord user id, so there is no per-guild copy to erase.
"""

import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
from cogs.gacha import revoke_entitlement
from cogs.utils import t
from settings_registry import SETTING_DEFINITIONS

privacy_logger = logging.getLogger("PotatoBot.Privacy")

# A request is cheap for the member and expensive for the database, so both
# commands are rate limited per user. main.py already reports CommandOnCooldown.
REQUEST_COOLDOWN_SECONDS = 900
CONFIRM_TIMEOUT_SECONDS = 60
# One pass erases at most this many members, so a long-neglected installation
# drains gradually instead of issuing thousands of Discord calls at once.
RETENTION_BATCH = 25


async def _retention_days(guild_id: int) -> int:
    """This guild's configured retention window; 0 means retain indefinitely."""
    definition = SETTING_DEFINITIONS["data_retention_days"]
    try:
        stored = await database.run_read(database.get_guild_settings, guild_id)
    except database.DatabaseOperationError:
        privacy_logger.exception(
            "Could not read the retention window (guild_id=%s).", guild_id
        )
        return 0
    row = stored.get("data_retention_days")
    value = row["value"] if row else definition.default
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def revoke_all_entitlements(bot, user_id: int) -> int:
    """Withdraw every live Discord grant before its record is erased."""
    entitlements = await database.run_read(
        database.get_active_entitlements_for_user, user_id
    )
    revoked = 0
    for entitlement in entitlements:
        guild = bot.get_guild(entitlement["guild_id"])
        if guild is None:
            continue
        if await revoke_entitlement(guild, entitlement):
            revoked += 1
    return revoked


class ErasureConfirmView(discord.ui.View):
    """A single deliberate confirmation, locked to the member who asked."""

    def __init__(self, user_id: int):
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.user_id = user_id
        self.confirmed = False
        button = discord.ui.Button(
            label=t("privacy.confirm_btn"), style=discord.ButtonStyle.danger, emoji="🗑️"
        )
        button.callback = self.confirm
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                t("privacy.not_your_request"), ephemeral=True
            )
            return False
        return True

    async def confirm(self, interaction: discord.Interaction):
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=t("privacy.erasure_running"), view=self
        )
        self.stop()


class Privacy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.retention_sweep.start()

    def cog_unload(self):
        self.retention_sweep.cancel()

    # -- export ------------------------------------------------------------

    @commands.hybrid_command(name="mydata", description=t("general.cmd_mydata"))
    @commands.cooldown(1, REQUEST_COOLDOWN_SECONDS, commands.BucketType.user)
    async def mydata(self, ctx):
        await ctx.defer(ephemeral=True)
        export = await database.run_read(database.export_user_data, ctx.author.id)
        payload = json.dumps(export, indent=2, ensure_ascii=False, sort_keys=True)
        attachment = discord.File(
            io.BytesIO(payload.encode("utf-8")),
            filename=f"potatobot-data-{ctx.author.id}.json",
        )
        try:
            # The export names the member and every guild they share with the bot,
            # so it goes to their DMs and never to a channel.
            await ctx.author.send(t("privacy.export_dm"), file=attachment)
        except discord.Forbidden:
            return await ctx.send(t("privacy.export_dm_closed"), ephemeral=True)
        except discord.HTTPException:
            privacy_logger.exception(
                "Could not deliver a data export (user_id=%s).", ctx.author.id
            )
            return await ctx.send(t("privacy.export_failed"), ephemeral=True)
        rows = sum(len(value) for value in export["tables"].values())
        await ctx.send(
            t("privacy.export_sent", rows=rows,
              tables=len([k for k, v in export["tables"].items() if v])),
            ephemeral=True,
        )

    # -- erasure -----------------------------------------------------------

    @commands.hybrid_command(name="deletemydata", description=t("general.cmd_deletemydata"))
    @commands.cooldown(1, REQUEST_COOLDOWN_SECONDS, commands.BucketType.user)
    async def deletemydata(self, ctx):
        view = ErasureConfirmView(ctx.author.id)
        message = await ctx.send(
            t("privacy.erasure_warning"), view=view, ephemeral=True
        )
        await view.wait()
        if not view.confirmed:
            for child in view.children:
                child.disabled = True
            try:
                await message.edit(content=t("privacy.erasure_cancelled"), view=view)
            except discord.HTTPException:
                pass
            return

        revoked = await revoke_all_entitlements(self.bot, ctx.author.id)
        receipt = await database.run_write(
            database.anonymize_user, ctx.author.id, ctx.author.id,
            ctx.guild.id if ctx.guild else 0, "member_request",
        )
        receipt["revoked_grants"] = revoked
        await self._send_receipt(ctx.author, receipt)
        await ctx.send(
            t("privacy.erasure_done", tombstone=receipt["tombstone_id"]),
            ephemeral=True,
        )

    async def _send_receipt(self, user, receipt):
        """The member is entitled to know what was kept as well as what went."""
        summary = json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True)
        attachment = discord.File(
            io.BytesIO(summary.encode("utf-8")),
            filename=f"potatobot-erasure-{receipt['tombstone_id']}.json",
        )
        try:
            await user.send(t("privacy.erasure_receipt"), file=attachment)
        except (discord.Forbidden, discord.HTTPException):
            # A closed DM must not roll back an erasure that already committed.
            privacy_logger.info(
                "Erasure receipt undeliverable (tombstone=%s).",
                receipt["tombstone_id"],
            )

    # -- retention ---------------------------------------------------------

    @tasks.loop(hours=24)
    async def retention_sweep(self):
        """Erase members who left long ago, if the operator configured a window.

        Two conditions must both hold: the recorded activity predates the window,
        and the member is absent from every guild the bot serves. Staleness alone
        would erase a lurker who simply never triggered an activity write.
        """
        windows = [
            days for days in
            [await _retention_days(guild.id) for guild in self.bot.guilds]
            if days > 0
        ]
        if not windows:
            return
        # The shortest configured window wins, because erasure is installation-wide
        # and a guild cannot consent to retaining data on another guild's behalf.
        cutoff = datetime.now(timezone.utc) - timedelta(days=min(windows))
        candidates = await database.run_read(
            database.get_retention_candidates, cutoff.isoformat(), RETENTION_BATCH
        )
        present = {
            member.id for guild in self.bot.guilds for member in guild.members
        }
        erased = 0
        for user_id in candidates:
            if user_id in present:
                continue
            try:
                revoked = await revoke_all_entitlements(self.bot, user_id)
                receipt = await database.run_write(
                    database.anonymize_user, user_id, self.bot.user.id, 0,
                    "retention_policy",
                )
            except database.DatabaseOperationError:
                privacy_logger.exception("Retention erasure failed.")
                continue
            erased += 1
            privacy_logger.info(
                "Retention erasure complete (tombstone=%s, revoked=%s).",
                receipt["tombstone_id"], revoked,
            )
        if erased:
            privacy_logger.info(
                "Retention sweep erased %s member(s) inactive since %s.",
                erased, cutoff.date().isoformat(),
            )

    @retention_sweep.before_loop
    async def before_retention_sweep(self):
        await self.bot.wait_until_ready()

    @retention_sweep.error
    async def retention_sweep_error(self, error):
        privacy_logger.exception(
            "Retention sweep stopped unexpectedly.", exc_info=error
        )


async def setup(bot):
    await bot.add_cog(Privacy(bot))
