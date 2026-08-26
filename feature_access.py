"""Feature gates and acknowledgement policy for application commands."""

from dataclasses import dataclass
from enum import Enum
import logging
import threading
import time

import discord
from discord import app_commands
from discord.ext import commands

import database
from bounded import BoundedTimestampMap
from settings_registry import FEATURE_DEFINITIONS

logger = logging.getLogger("PotatoBot.FeatureAccess")


class ResponsePolicy(str, Enum):
    """Visibility of the acknowledgement sent before a command callback runs."""

    PUBLIC = "public"
    PRIVATE = "private"
    MODAL = "modal"


class FeatureCacheState(str, Enum):
    UNINITIALIZED = "uninitialized"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class CommandPolicy:
    feature_key: str | None
    response: ResponsePolicy = ResponsePolicy.PUBLIC


def _command(feature_key: str | None, response: ResponsePolicy = ResponsePolicy.PUBLIC):
    return CommandPolicy(feature_key=feature_key, response=response)


# This is intentionally exhaustive. Tests compare it with every loaded hybrid command.
COMMAND_POLICIES = {
    "help": _command("general", ResponsePolicy.PRIVATE),
    "version": _command("general"),
    "search": _command("lfg"),
    "profile": _command("profiles"),
    "lvls": _command("profiles"),
    "ranks": _command("profiles"),
    "topstreak": _command("profiles"),
    "bal": _command("economy"),
    "daily": _command("economy"),
    "work": _command("economy"),
    "rob": _command("economy"),
    "pay": _command("economy"),
    "shop": _command("shop", ResponsePolicy.PRIVATE),
    # PUBLIC because the result embed *is* the command. Declared PRIVATE, the
    # tree deferred ephemerally and the public success branch made
    # PotatoContext.send delete the original response — stranding Discord's
    # "used /gacha" header above every single pull as "Message could not be
    # loaded". The three refusal branches now take the swap instead, which is
    # rare and costs a brief public placeholder rather than a permanent artefact.
    "gacha": _command("shop_gacha"),
    "inventory": _command("economy", ResponsePolicy.PRIVATE),
    "pity": _command("shop_gacha", ResponsePolicy.PRIVATE),
    "redeem": _command("shop_gacha", ResponsePolicy.PRIVATE),
    "bj": _command("casino_blackjack"),
    "dice": _command("casino_dice"),
    "roulette": _command("casino_roulette"),
    "slots": _command("casino_slots"),
    "mines": _command("casino_mines"),
    "freemines": _command("casino_mines"),
    "loldle": _command("everydle_loldle", ResponsePolicy.PRIVATE),
    "valdle": _command("everydle_valdle", ResponsePolicy.PRIVATE),
    "dbdle": _command("everydle_dbdle", ResponsePolicy.PRIVATE),
    "skip": _command("music"),
    "queue": _command("music"),
    "shuffle": _command("music"),
    "remove": _command("music"),
    "loop": _command("music"),
    "np": _command("music"),
    "join": _command("music"),
    "stop": _command("music"),
    "play": _command("music"),
    "kick": _command("moderation"),
    "ban": _command("moderation"),
    "timeout": _command("moderation"),
    "untimeout": _command("moderation"),
    "msgdel": _command("moderation", ResponsePolicy.PRIVATE),
    "warn": _command("moderation"),
    "unwarn": _command("moderation", ResponsePolicy.PRIVATE),
    "modlogs": _command("moderation", ResponsePolicy.PRIVATE),
    "manage": _command("factions", ResponsePolicy.MODAL),
    "sync_autoroles": _command("onboarding", ResponsePolicy.PRIVATE),
    "setup_tickets": _command("tickets", ResponsePolicy.PRIVATE),
    "setup_games": _command("role_menus", ResponsePolicy.PRIVATE),
    "update_games": _command("role_menus", ResponsePolicy.PRIVATE),
    "setup_news": _command("role_menus", ResponsePolicy.PRIVATE),
    "update_news": _command("role_menus", ResponsePolicy.PRIVATE),
    "setup_themes": _command("role_menus", ResponsePolicy.PRIVATE),
    "update_themes": _command("role_menus", ResponsePolicy.PRIVATE),
    "setup_enter": _command("onboarding", ResponsePolicy.PRIVATE),
    "update_enter": _command("onboarding", ResponsePolicy.PRIVATE),
    "rules_group": _command("onboarding", ResponsePolicy.PRIVATE),
    "update_rules_group": _command("onboarding", ResponsePolicy.PRIVATE),
    "rules_verify": _command("onboarding", ResponsePolicy.PRIVATE),
    "rent_start": _command("rentals", ResponsePolicy.PRIVATE),
    "testreset": _command("economy", ResponsePolicy.PRIVATE),
    "award": _command("economy"),
    "awardall": _command("economy"),
    "testboost": _command("member_announcements", ResponsePolicy.PRIVATE),
    "checkperms": _command("general"),
    # Maintenance is an emergency administrator control, not a toggleable feature.
    "maintenance": _command(None),
    # A member's right to export or erase their own data cannot be conditional on
    # a guild toggle, so these two are deliberately ungated.
    "mydata": _command(None, ResponsePolicy.PRIVATE),
    "deletemydata": _command(None, ResponsePolicy.PRIVATE),
    "embedsend": _command("general", ResponsePolicy.MODAL),
    "getraw": _command("general", ResponsePolicy.PRIVATE),
}

_FEATURE_CACHE = {}
_FEATURE_REVISIONS = {}
_FEATURE_CACHE_STATES = {}
_FEATURE_CACHE_LOCK = threading.RLock()
# Only PotatoContext.send retires an entry, so commands that raise before
# replying would otherwise leak one snowflake key each for the process lifetime.
_INTERACTION_STARTED = BoundedTimestampMap(max_age=900, max_entries=4096)


def command_policy(command_name: str) -> CommandPolicy | None:
    return COMMAND_POLICIES.get(command_name.split()[0]) if command_name else None


def feature_for_command(command_name: str) -> str | None:
    policy = command_policy(command_name)
    return policy.feature_key if policy else None


# Maintenance status must stay inspectable while the bot refuses everything else.
MAINTENANCE_EXEMPT_COMMANDS = frozenset({"version"})


def maintenance_blocks(guild, actor, command_name: str = "") -> bool:
    """Report whether maintenance mode must refuse this actor's request.

    Maintenance is an emergency override above normal feature flags, so every
    entry point consults this single predicate: prefix and hybrid commands via
    the bot-wide check, native application commands via the command tree, and
    component/modal callbacks via require_interaction_feature.
    """
    import settings_cache

    if command_name and command_name.split()[0] in MAINTENANCE_EXEMPT_COMMANDS:
        return False
    # Read from memory: this runs on every interaction, including every
    # component and modal callback. It must also fail *open* — the mirror image
    # of `is_enabled`, which fails closed. An unreadable cache resolves
    # `maintenance` through config.json and then the registry default, both of
    # which are "not in maintenance", so a settings problem cannot take the
    # whole bot down. Do not copy this from `is_enabled`; they are one line
    # apart and opposite.
    guild_id = getattr(guild, "id", None)
    try:
        if not settings_cache.setting(guild_id, "maintenance"):
            return False
    except Exception:
        logger.exception("Maintenance state unreadable; failing open")
        return False
    # Administrators need access for diagnostics and recovery.
    permissions = getattr(actor, "guild_permissions", None)
    if guild is not None and permissions is not None and permissions.administrator:
        return False
    return True


async def _deny_interaction(interaction: discord.Interaction, locale_key: str) -> None:
    """Send one ephemeral refusal regardless of prior acknowledgement state."""
    from cogs.utils import t

    if interaction.response.is_done():
        await interaction.followup.send(t(locale_key), ephemeral=True)
    else:
        await interaction.response.send_message(t(locale_key), ephemeral=True)


def is_enabled(guild_id: int | None, feature_key: str) -> bool:
    definition = FEATURE_DEFINITIONS.get(feature_key)
    if definition is None:
        # A renamed or misspelled key must not silently disable its own gate.
        logger.error("Unknown feature key requested (feature_key=%r)", feature_key)
        return False
    if guild_id is None:
        return True
    with _FEATURE_CACHE_LOCK:
        guild_states = _FEATURE_CACHE.get(int(guild_id))
        cache_state = _FEATURE_CACHE_STATES.get(
            int(guild_id), FeatureCacheState.UNINITIALIZED
        )
        if guild_states is None or cache_state is not FeatureCacheState.READY:
            # Feature-controlled work must not run while its persisted policy is
            # unknown. A failed refresh keeps the last known-good cache below.
            return False
        return guild_states.get(feature_key, definition.default)


def _store_feature_states(guild_id: int, states: dict[str, dict]):
    guild_id = int(guild_id)
    with _FEATURE_CACHE_LOCK:
        _FEATURE_CACHE[guild_id] = {
            key: value["enabled"] for key, value in states.items()
        }
        _FEATURE_CACHE_STATES[guild_id] = FeatureCacheState.READY


def refresh_feature_cache(guild_id: int):
    _store_feature_states(guild_id, database.get_feature_states(guild_id))


async def refresh_feature_cache_async(guild_id: int, *, force: bool = False):
    """Reload feature rows only when the persisted revision changed."""
    guild_id = int(guild_id)
    try:
        revision = await database.run_read(database.get_feature_revision, guild_id)
    except Exception:
        with _FEATURE_CACHE_LOCK:
            if guild_id not in _FEATURE_CACHE:
                _FEATURE_CACHE_STATES[guild_id] = FeatureCacheState.FAILED
        raise
    with _FEATURE_CACHE_LOCK:
        current_revision = _FEATURE_REVISIONS.get(guild_id)
        cache_exists = guild_id in _FEATURE_CACHE
    if not force and cache_exists and current_revision == revision:
        return False
    try:
        states = await database.run_read(database.get_feature_states, guild_id)
    except Exception:
        with _FEATURE_CACHE_LOCK:
            if guild_id not in _FEATURE_CACHE:
                _FEATURE_CACHE_STATES[guild_id] = FeatureCacheState.FAILED
        raise
    _store_feature_states(guild_id, states)
    with _FEATURE_CACHE_LOCK:
        _FEATURE_REVISIONS[guild_id] = revision
    return True


def seed_cached_feature(guild_id: int, feature_key: str, enabled: bool):
    """Seed one flag *and* mark the guild loaded. For tests and seeding only.

    Deliberately does what `update_cached_features` must not: it promotes the
    guild to READY. It was named `update_cached_feature`, one character from the
    function the dashboard calls, which is how the promotion ended up in both.
    """
    with _FEATURE_CACHE_LOCK:
        _FEATURE_CACHE.setdefault(int(guild_id), {})[feature_key] = enabled
        _FEATURE_CACHE_STATES[int(guild_id)] = FeatureCacheState.READY


def update_cached_features(guild_id: int, changes: dict[str, dict]):
    """Apply a committed group of feature changes to the in-process cache.

    A change never *promotes* the cache to READY. It used to, and the consequence
    was the opposite of the fail-closed rule: after a failed startup load,
    toggling one feature from the dashboard marked the whole guild loaded, so
    `is_enabled` began answering every other key from its registry default and
    silently switched on every default-on feature for a guild whose rows had
    never been read. A change to an unloaded guild is recorded and left for the
    next poll to reconcile against the database.
    """
    guild_id = int(guild_id)
    with _FEATURE_CACHE_LOCK:
        guild_cache = _FEATURE_CACHE.setdefault(guild_id, {})
        for feature_key, change in changes.items():
            guild_cache[feature_key] = bool(change["enabled"])


async def require_interaction_feature(
    interaction: discord.Interaction, feature_key: str | None
) -> bool:
    """Reject a component interaction under maintenance or a disabled feature.

    A None feature key means the caller owns no flag of its own and only needs
    the maintenance gate, so every component still passes through one predicate.
    """
    if maintenance_blocks(interaction.guild, interaction.user):
        await _deny_interaction(interaction, "system.maintenance_blocked")
        return False
    if feature_key is None or is_enabled(interaction.guild_id, feature_key):
        return True
    await _deny_interaction(interaction, "utils.feature_disabled")
    return False


def _requested_ephemeral(command_name: str) -> bool | None:
    policy = command_policy(command_name)
    if policy is None or policy.response is ResponsePolicy.MODAL:
        return None
    return policy.response is ResponsePolicy.PRIVATE


class PotatoContext(commands.Context):
    """Preserve requested visibility after the tree has acknowledged an interaction."""

    async def send(self, content=None, **kwargs):
        interaction = self.interaction
        requested_ephemeral = bool(kwargs.get("ephemeral", False))
        if interaction is not None and not interaction.is_expired():
            command_name = getattr(interaction.command, "qualified_name", "")
            deferred_ephemeral = _requested_ephemeral(command_name)
            if (
                interaction.response.is_done()
                and deferred_ephemeral is not None
                and deferred_ephemeral != requested_ephemeral
            ):
                # Removing the deferred original lets the follow-up use the branch's
                # requested visibility instead of inheriting the acknowledgement.
                # Resolve it first because Discord ignores the ephemeral flag on
                # the first message that completes a deferred response.
                try:
                    await interaction.edit_original_response(content="\u200b")
                    await interaction.delete_original_response()
                except (discord.NotFound, discord.HTTPException) as exc:
                    logger.warning(
                        "Could not replace deferred response visibility "
                        "(command=%s, error=%s)",
                        command_name,
                        type(exc).__name__,
                    )
                logger.info(
                    "Interaction response visibility changed "
                    "(command=%s, from_ephemeral=%s, to_ephemeral=%s)",
                    command_name,
                    deferred_ephemeral,
                    requested_ephemeral,
                )

        started_at = None
        if interaction is not None:
            started_at = _INTERACTION_STARTED.pop(interaction.id)
        result = await super().send(content, **kwargs)
        if started_at is not None:
            logger.info(
                "Interaction completed (command=%s, duration_ms=%s)",
                getattr(interaction.command, "qualified_name", "unknown"),
                round((time.monotonic() - started_at) * 1000),
            )
        return result


class PotatoBot(commands.Bot):
    async def get_context(self, origin, /, *, cls=PotatoContext):
        return await super().get_context(origin, cls=cls)

    async def close(self):
        try:
            await super().close()
        finally:
            database.shutdown_executors(wait=False)


class PotatoCommandTree(app_commands.CommandTree):
    """Gate and immediately acknowledge native and hybrid application commands."""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        command_name = getattr(interaction.command, "qualified_name", "")
        if interaction.guild_id is None:
            from cogs.utils import t
            await interaction.response.send_message(
                t("utils.guild_only"), ephemeral=True
            )
            return False
        policy = command_policy(command_name)
        if policy is None:
            logger.error("Application command has no response policy (command=%s)", command_name)
            from cogs.utils import t
            await interaction.response.send_message(
                t("utils.command_unavailable"), ephemeral=True
            )
            return False

        # Maintenance outranks the feature flag, so it is evaluated first.
        if maintenance_blocks(interaction.guild, interaction.user, command_name):
            await _deny_interaction(interaction, "system.maintenance_blocked")
            return False

        if (
            policy.feature_key is not None
            and not is_enabled(interaction.guild_id, policy.feature_key)
        ):
            await _deny_interaction(interaction, "utils.feature_disabled")
            return False

        if (
            interaction.type is discord.InteractionType.application_command
            and policy.response is not ResponsePolicy.MODAL
            and not interaction.response.is_done()
        ):
            started_at = time.monotonic()
            try:
                await interaction.response.defer(
                    ephemeral=policy.response is ResponsePolicy.PRIVATE
                )
            except (discord.NotFound, discord.HTTPException) as exc:
                logger.warning(
                    "Failed to defer interaction; command execution was cancelled "
                    "(command=%s, guild_id=%s, error=%s)",
                    command_name,
                    interaction.guild_id,
                    type(exc).__name__,
                )
                return False
            _INTERACTION_STARTED.start(interaction.id, started_at)
            logger.info(
                "Interaction acknowledged (command=%s, ephemeral=%s, ack_ms=%s)",
                command_name,
                policy.response is ResponsePolicy.PRIVATE,
                round((time.monotonic() - started_at) * 1000),
            )
        return True
