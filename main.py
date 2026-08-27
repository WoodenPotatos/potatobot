import discord
import os
import sys
import logging
import logging.handlers
import asyncio
import time

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH)

import database as database_layer
from deployment import settings as deployment_settings
from feature_access import (
    PotatoBot,
    PotatoCommandTree,
    feature_for_command,
    is_enabled,
    maintenance_blocks,
    refresh_feature_cache_async,
)
import settings_cache

if deployment_settings.dashboard_enabled:
    import dashboard_api

import logging_setup
from cogs.utils import config, reload_config, t
from discord.ext import commands

# Configure logging before the bot starts so startup and migration failures are
# captured. The handlers live in `logging_setup` so that `python dashboard_api.py`
# — the two-process split — gets the same configuration without importing main.
LOG_DIR = logging_setup.LOG_DIR
configure_logger = logging_setup.configure_logger

logger = configure_logger('discord')
bot_logger = configure_logger('PotatoBot')
# Waitress logs its own queue-depth warnings, and they are worth reading in the
# same format as everything else rather than in whatever basicConfig picks.
configure_logger('waitress')

# The database module is the single schema owner. Migrations are applied before
# cogs or the dashboard can perform reads and writes.
database_layer.initialize_database()
recovery = database_layer.refund_pending_wagers()
if recovery["count"]:
    bot_logger.warning(
        "Recovered orphaned interactive wagers (count=%s, refunded_amount=%s)",
        recovery["count"], recovery["amount"],
    )

# Apply a lightweight cross-command rate limit while exempting game creation.
ANTI_SPAM_EXEMPT_COMMANDS = {
    "bj", "dice", "roulette", "slots", "mines", "freemines",
    "hilo", "crash", "wheel", "russian",
    "loldle", "valdle", "dbdle",
}
command_rate_limits = commands.CooldownMapping.from_cooldown(
    1, 3, commands.BucketType.user
)

# Request only the gateway intents used by the loaded cogs.
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True
intents.bans = True

# The prefix is a typed instance setting whose apply behaviour is `restart`,
# because discord.py binds it when the bot object is constructed. Loaded
# synchronously here, before there is a loop to await on; if the database cannot
# be read the cache stays cold and the fallback chain yields `?`, which matters
# because a database problem must not lock the operator out of `?reload`.
settings_cache.load_instance_sync()
COMMAND_PREFIX = str(settings_cache.setting(None, "command_prefix") or "?")

bot = PotatoBot(
    command_prefix=COMMAND_PREFIX,
    intents=intents,
    tree_cls=PotatoCommandTree,
    allowed_mentions=discord.AllowedMentions(
        everyone=False, roles=False, users=True, replied_user=False
    ),
)
tree = bot.tree
feature_refresh_task = None
event_loop_watchdog_task = None
dashboard_action_task = None
legacy_adoption_checked = False
commands_synchronized = False

bot.remove_command("help")


@bot.check
async def guild_only_check(ctx):
    if ctx.guild is not None:
        return True
    await ctx.send(t("utils.guild_only"))
    return False


@bot.check
async def anti_spam_check(ctx):
    """Applies a small global cooldown except when starting a game."""
    if ctx.command and ctx.command.name in ANTI_SPAM_EXEMPT_COMMANDS:
        return True
    bucket = command_rate_limits.get_bucket(ctx.message)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        raise commands.CommandOnCooldown(bucket, retry_after, commands.BucketType.user)
    return True


@bot.check
async def feature_check(ctx):
    """Block disabled prefix/hybrid commands before they can mutate state."""
    feature_key = feature_for_command(ctx.command.qualified_name if ctx.command else "")
    guild_id = ctx.guild.id if ctx.guild else None
    if feature_key is None or is_enabled(guild_id, feature_key):
        return True
    await ctx.send(t("utils.feature_disabled"), ephemeral=True)
    return False

async def load_cogs():
    for filename in os.listdir(os.path.join(BASE_DIR, "cogs")):
        if filename.endswith('.py'):
            # Utility modules are imported by cogs and are not extensions themselves.
            if filename in ['utils.py', '__init__.py', 'database.py']:
                continue

            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                bot_logger.info("Loaded extension: %s", filename)
            except Exception as e:
                bot_logger.exception("Failed to load extension: %s", filename)

async def refresh_feature_caches():
    """Notice feature and settings writes made by a separate dashboard process."""
    while not bot.is_closed():
        await asyncio.sleep(2)
        for guild in bot.guilds:
            try:
                await refresh_feature_cache_async(guild.id)
            except Exception:
                bot_logger.exception(
                    "Feature cache refresh failed (guild_id=%s)", guild.id
                )
        try:
            # One probe for every guild, because the settings revision is one
            # installation-wide number rather than one per guild.
            await settings_cache.refresh([guild.id for guild in bot.guilds])
        except Exception:
            # The last known-good cache stays, and every reader keeps resolving
            # through the legacy fallback, so this is a warning and not an
            # outage.
            bot_logger.exception("Settings cache refresh failed")

async def monitor_event_loop_lag():
    """Report blocking work before it grows into expired Discord interactions."""
    interval = 1.0
    expected = time.monotonic() + interval
    while not bot.is_closed():
        await asyncio.sleep(interval)
        now = time.monotonic()
        lag = max(0.0, now - expected)
        if lag >= 0.25:
            bot_logger.warning("Event loop lag detected (lag_ms=%s)", round(lag * 1000))
        expected = now + interval

# Load extensions before the gateway connection is established.
bot.setup_hook = load_cogs

@bot.event
async def on_ready():
    global feature_refresh_task, event_loop_watchdog_task
    global dashboard_action_task
    global legacy_adoption_checked, commands_synchronized
    bot_logger.info("Connected as %s (ID: %s)", bot.user, bot.user.id)

    for guild in bot.guilds:
        # Guarded per guild. `refresh_feature_cache_async` re-raises on a read
        # failure, and this loop runs before the background tasks are created —
        # so one guild's transient error used to abort the rest of on_ready and
        # leave the feature poller, the lag monitor and the control-action
        # worker unstarted, which stops the outbox draining. The guild is left
        # in its FAILED state, which fails closed, and the poller reconciles it.
        try:
            await database_layer.run_write(
                database_layer.register_guild, guild.id, guild.name)
            await refresh_feature_cache_async(guild.id, force=True)
        except Exception:
            bot_logger.exception(
                "Initial feature cache load failed (guild_id=%s)", guild.id)

    try:
        await settings_cache.refresh([guild.id for guild in bot.guilds], force=True)
    except Exception:
        bot_logger.exception("Initial settings cache load failed")

    if feature_refresh_task is None or feature_refresh_task.done():
        feature_refresh_task = asyncio.create_task(
            refresh_feature_caches(), name="feature-cache-refresh"
        )
    if event_loop_watchdog_task is None or event_loop_watchdog_task.done():
        event_loop_watchdog_task = asyncio.create_task(
            monitor_event_loop_lag(), name="event-loop-watchdog"
        )
    if deployment_settings.dashboard_enabled and (
        dashboard_action_task is None or dashboard_action_task.done()
    ):
        dashboard_action_task = asyncio.create_task(
            dashboard_api.control_action_worker(bot), name="dashboard-control-actions"
        )

    if deployment_settings.profile.value == "private" and not legacy_adoption_checked:
        legacy_adoption_checked = True
        configured_legacy_guild = os.getenv("POTATOBOT_LEGACY_GUILD_ID")
        if configured_legacy_guild:
            try:
                legacy_guild_id = int(configured_legacy_guild)
            except ValueError:
                bot_logger.critical("POTATOBOT_LEGACY_GUILD_ID must be an integer")
                legacy_guild_id = None
        elif len(bot.guilds) == 1:
            legacy_guild_id = next(iter(bot.guilds)).id
        else:
            legacy_guild_id = None
        if legacy_guild_id and bot.get_guild(legacy_guild_id):
            adoption = await database_layer.run_write(
                database_layer.adopt_legacy_database, legacy_guild_id
            )
            if adoption["adopted"]:
                bot_logger.info(
                    "Legacy user state seeded into guild and instance scopes "
                    "(users=%s)",
                    adoption["user_count"],
                )
        elif legacy_guild_id:
            bot_logger.critical(
                "Legacy guild is not connected (guild_id=%s)", legacy_guild_id
            )
        else:
            bot_logger.critical(
                "Legacy scope adoption requires POTATOBOT_LEGACY_GUILD_ID "
                "when more than one guild is connected"
            )

    if not commands_synchronized:
        try:
            synced = await bot.tree.sync()
            commands_synchronized = True
            bot_logger.info("Synchronized %s application commands", len(synced))
        except Exception:
            bot_logger.exception("Application-command synchronization failed")


@bot.event
async def on_guild_join(guild):
    await database_layer.run_write(database_layer.register_guild, guild.id, guild.name)
    await refresh_feature_cache_async(guild.id, force=True)


@bot.event
async def on_guild_remove(guild):
    await database_layer.run_write(database_layer.mark_guild_inactive, guild.id)

@bot.command()
async def ping(ctx):
    await ctx.send(t("system.ping", latency=round(bot.latency * 1000)))

@bot.check
async def maintenance_check(ctx):
    # Prefix and hybrid invocations share one predicate with the command tree
    # and component callbacks so maintenance cannot be bypassed by entry point.
    command_name = getattr(ctx.command, "qualified_name", "")
    if not maintenance_blocks(ctx.guild, ctx.author, command_name):
        return True

    await ctx.send(t("system.maintenance_blocked"), ephemeral=True)
    return False

@bot.event
async def on_command_error(ctx, error):
    root_error = getattr(error, "original", error)
    if isinstance(root_error, database_layer.DatabaseOperationError):
        await ctx.send(t("utils.database_error"), ephemeral=True)
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            t("utils.command_cooldown", seconds=max(1, int(error.retry_after) + 1)),
            ephemeral=True,
        )
    elif isinstance(error, commands.MaxConcurrencyReached):
        await ctx.send(t("utils.command_in_progress"), ephemeral=True)
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send(t("system.command_not_found"))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(t("system.missing_argument", prefix=ctx.prefix, command=ctx.command.name))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(t("system.bad_argument"))
    elif isinstance(error, commands.CheckFailure):
        # Individual checks provide their own localized denial where appropriate.
        return
    else:
        # Unexpected failures are logged without exposing internal details to users.
        bot_logger.error(
            "Unhandled command error: %s",
            error,
            exc_info=(type(root_error), root_error, root_error.__traceback__),
        )

@bot.command()
@commands.is_owner()
async def reload(ctx, cog_name: str):
    await bot.reload_extension(f"cogs.{cog_name}")
    await ctx.send(t("system.extension_reloaded", cog=cog_name))

@bot.command(name="reloadconfig")
@commands.is_owner()
async def reload_config_cmd(ctx):
    """Re-read the legacy fallback file and the stored settings.

    Settings converge on their own now — the poll notices a dashboard save
    within a couple of seconds — so this is the manual "now, please" path and
    the way to pick up a hand-edited `config.json`, which is still the fallback
    for anything an installation has never saved.
    """
    try:
        await asyncio.to_thread(reload_config)
        settings_cache.invalidate()
        await settings_cache.refresh([guild.id for guild in bot.guilds], force=True)
        await ctx.send(t("system.config_reloaded"))
    except Exception:
        bot_logger.exception("Configuration reload failed")
        await ctx.send(t("system.config_reload_failed"))

if deployment_settings.dashboard_enabled:
    dashboard_api.start_dashboard_thread(bot)
else:
    bot_logger.info(
        "Dashboard is disabled for the %s deployment profile",
        deployment_settings.profile.value,
    )

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN is None:
    bot_logger.critical("DISCORD_TOKEN is not configured; startup aborted")
    exit()

# `log_handler=None` because discord.py otherwise calls `setup_logging()` on the
# `discord` logger this module has already configured, which was the other half
# of the duplication.
bot.run(TOKEN, log_handler=None)
