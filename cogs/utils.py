import discord
import re
import asyncio
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import logging
import time

# Resolve repository resources independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database

# Re-exported so the many cogs that already import these from here keep working.
from bounded import BoundedCooldownMap, BoundedValueMap  # noqa: F401

from discord.ext import commands
from datetime import datetime

utility_logger = logging.getLogger("PotatoBot.Utils")
TOP_RANKER_DEBOUNCE_SECONDS = 30
_top_ranker_dirty = {}
_top_ranker_tasks = {}

# Keep this dictionary object stable because cogs import it by reference.
config = {}
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
CONFIG_LOCK = threading.RLock()

def reload_config():
    """Reload configuration while preserving the shared dictionary identity."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded_config = json.load(f)
        with CONFIG_LOCK:
            config.clear()
            config.update(loaded_config)
        utility_logger.info("Configuration loaded successfully")
    else:
        utility_logger.critical("Configuration file not found: %s", CONFIG_PATH)
        sys.exit()


def snapshot_config():
    """Return an isolated deep copy of the live configuration.

    Callers that read, mutate and then save must hold CONFIG_LOCK across the
    whole sequence; taking the snapshot under the lock alone is not enough to
    prevent a concurrent writer from losing the other's keys.
    """
    with CONFIG_LOCK:
        return copy.deepcopy(config)


def save_config(new_config):
    """Atomically persists a validated in-memory configuration object."""
    if not isinstance(new_config, dict):
        raise ValueError("config root must be an object")
    temp_path = None
    with CONFIG_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=ROOT_DIR, delete=False
            ) as temp_file:
                json.dump(new_config, temp_file, indent=4, ensure_ascii=False)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = temp_file.name
            os.replace(temp_path, CONFIG_PATH)
            config.clear()
            config.update(new_config)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

# Configuration must exist before decorators and persistent views are constructed.
reload_config()

# Load locale catalogs once; deployment reloads currently affect configuration only.
LOCALES_DIR = os.path.join(ROOT_DIR, "locales")
# Hungarian is the project's complete catalog and the fallback for every other.
PRIMARY_LANGUAGE = "hu"
locales = {}

for lang in [PRIMARY_LANGUAGE, "en"]:
    file_path = os.path.join(LOCALES_DIR, f"{lang}.json")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            locales[lang] = json.load(f)
    else:
        locales[lang] = {}

def _lookup(catalog: dict, path: str):
    """Resolve one dotted key, or None when it is absent or not a string."""
    node = catalog
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


# Fallback if the key is absent from config entirely. Kept in step with the
# registry default by a test rather than by hope.
DEFAULT_CURRENCY_EMOJI = "🥔"


def currency_emoji() -> str:
    """The symbol balances, prices and payouts are printed with.

    An instance setting rather than a per-guild one, for the same reason
    `language` is: `t()` is synchronous and has no guild context. Both move
    together when it grows one.

    It exists at all because this was a hard-coded custom emoji from one guild,
    which every other installation rendered as the literal text
    `<:potatocoins:1489…>` on every balance and price.
    """
    value = guild_setting_sync(None, "currency_emoji")
    return value if isinstance(value, str) and value.strip() else DEFAULT_CURRENCY_EMOJI


def currency_select_emoji():
    """The currency as a Discord select-option emoji, or None if it cannot be one.

    A select option's *label* is plain text: `<:name:id>` renders literally there,
    which is why the shop menu displayed a raw emoji id while every other surface
    showed the symbol. `SelectOption(emoji=...)` is the supported route.

    Discord rejects an option carrying an emoji it cannot resolve, and that fails
    the whole command rather than merely looking wrong, so anything not clearly an
    emoji is dropped. `PartialEmoji.from_str` cannot be trusted for that check on
    its own — it never raises, and turns arbitrary prose into a "unicode emoji"
    named after the prose.
    """
    raw = currency_emoji().strip()
    parsed = discord.PartialEmoji.from_str(raw)
    if parsed.id is not None:
        # A well-formed custom reference: from_str only fills `id` when the name
        # and snowflake both validate.
        return parsed
    # Otherwise it has to be a short Unicode emoji. Anything with ASCII letters,
    # digits or emoji-markup punctuation is prose or a malformed reference.
    if raw and len(raw) <= 8 and not re.search(r"[A-Za-z0-9<>:]", raw):
        return raw
    return None


def t(path: str, lang: str = None, **kwargs) -> str:
    """Resolve and format a dotted locale key for the requested language.

    Resolution walks *requested language, then English, then Hungarian*. Both
    of those are complete by policy, so a third language that is only partly
    translated degrades to readable text.

    A present-but-empty value counts as a miss. This matters more than it looks:
    the catalogs are structurally identical, so an untranslated key exists with
    an empty string, and treating that as a successful lookup made a switched
    language answer with blank embeds and blank button labels rather than
    falling back to anything.
    """
    if lang is None:
        lang = guild_setting_sync(None, "language")

    template = None
    for candidate in dict.fromkeys((lang, "en", PRIMARY_LANGUAGE)):
        value = _lookup(locales.get(candidate, {}), path)
        if value is not None and value.strip():
            template = value
            break
    if template is None:
        utility_logger.error("Missing locale key: %s", path)
        return f"[{path}]"

    # Supplied for every key rather than by each caller. `str.format` ignores a
    # keyword the template does not use, so the keys without `{coin}` — nearly
    # all of them — are untouched, and no call site has to know about this.
    kwargs.setdefault("coin", currency_emoji())
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError) as exc:
        # A caller that forgot a placeholder is its own bug, and it used to be
        # indistinguishable from a missing key: the same handler caught both and
        # returned the other catalog's value, or an empty string. Return the
        # unformatted template so the braces are visible rather than the text
        # silently disappearing.
        utility_logger.error(
            "Locale key %s is missing a format argument (%s)", path, exc
        )
        return template


def get_locale_catalog(lang: str = None) -> dict:
    """Return an isolated locale catalog for clients such as the dashboard."""
    selected_language = lang or guild_setting_sync(None, "language")
    return copy.deepcopy(locales.get(selected_language, {}))


def available_languages() -> list[str]:
    """List the catalogs that were actually loaded at import time."""
    return sorted(locales)


def _overlay_translated(base: dict, overlay: dict) -> dict:
    """Copy non-empty overlay leaves over base, preserving base elsewhere."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _overlay_translated(base[key], value)
        elif isinstance(value, str) and value:
            base[key] = value
    return base


def get_dashboard_locale_catalog(lang: str = None) -> dict:
    """Return a dashboard catalog with untranslated keys filled from Hungarian.

    This deliberately does not reuse ``t()``: bot output falls back Hungarian to
    English, while the dashboard needs the opposite direction because Hungarian
    is the only complete catalog. Falling back keeps a partially translated
    interface readable instead of showing raw ``[dashboard.key]`` placeholders.
    """
    selected_language = lang or guild_setting_sync(None, "language")
    catalog = copy.deepcopy(locales.get(PRIMARY_LANGUAGE, {}))
    if selected_language == PRIMARY_LANGUAGE:
        return catalog
    return _overlay_translated(catalog, locales.get(selected_language, {}))


def guild_member_ids(guild) -> list[int]:
    """Return the human members of one guild, for guild-filtered rankings.

    Wallets are still installation-wide, so a leaderboard is scoped by asking who
    is present rather than by partitioning the economy. The members intent is
    enabled, so this reads from the cache rather than hitting Discord.
    """
    return [member.id for member in guild.members if not member.bot]


# Length of the pseudonym shown instead of a departed member's identity. Six
# hexadecimal characters keep two different members apart in a ten-row
# leaderboard without being long enough to read as an identifier.
ANONYMOUS_TAG_LENGTH = 6


def anonymous_member_tag(guild_id: int, user_id: int) -> str:
    """Return a stable, guild-local pseudonym for one account.

    The digest is salted with the guild id so the same account does not carry a
    recognisable label from one guild into another, and it is derived rather
    than stored so nothing has to be written, migrated or cleaned up.
    """
    digest = hashlib.blake2s(
        f"{int(guild_id)}:{int(user_id)}".encode("utf-8"),
        digest_size=ANONYMOUS_TAG_LENGTH // 2,
    )
    return digest.hexdigest()


def display_member_name(guild, user_id: int) -> str:
    """Name one account for a guild-facing list without leaking its identity.

    Rankings are already filtered to the guild's cached members, so an
    unresolvable id means the member left between the query and the render, or
    has not been seen since. Showing the raw snowflake there published a
    departed member's Discord identity to everyone who ran the command. The
    pseudonym replaces only the display, so nothing is deleted and nothing has
    to be undone: the moment the member is resolvable again — because they
    rejoined or the cache filled — their real display name is shown again.
    """
    member = guild.get_member(int(user_id)) if guild else None
    if member is not None:
        return member.display_name
    return t(
        "utils.anonymous_member",
        tag=anonymous_member_tag(guild.id if guild else 0, user_id),
    )


def guild_setting_sync(guild_id: int, key: str):
    """One typed setting, from memory, with no await and no database read.

    This is the accessor a synchronous read site uses — a command decorator, a
    permission check, anything that cannot await. `settings_cache` owns the
    fallback chain (stored row, then `config.json`, then the registry default),
    so a cold cache resolves exactly the way the bot resolved before the cache
    existed rather than resolving to nothing.
    """
    import settings_cache
    return settings_cache.setting(guild_id, key)


def guild_settings_sync(guild_id: int, keys) -> dict:
    """Several typed settings from memory, for a synchronous read site."""
    import settings_cache
    return settings_cache.settings(guild_id, keys)


async def set_guild_setting(guild_id: int, actor_id: int, key: str, value):
    """Write one typed setting from the bot side, through the one write path.

    `database.set_guild_settings` is the only writer: it validates the value
    against the registry, routes it by scope, bumps the revision and commits the
    audit row on the same connection. A Discord command that changes a setting —
    `/maintenance` is the one that does — goes through here rather than through
    raw SQL or, as it used to, by rewriting `config.json`.

    The revision is read immediately before the write, so a concurrent dashboard
    save still conflicts rather than being silently overwritten.
    """
    import settings_cache

    stored = await database.run_read(database.get_guild_settings, guild_id)
    revision = (stored.get(key) or {}).get("revision", 0)
    result = await database.run_write(
        database.set_guild_settings, guild_id, actor_id,
        [{"key": key, "value": value, "revision": revision}],
    )
    settings_cache.apply_changes(guild_id, result)
    return result[key]["value"]


async def guild_setting(guild_id: int, key: str):
    """One typed guild setting.

    Reads the in-process cache, which is refreshed on a revision poll and
    updated immediately for a dashboard change made in this process, so this no
    longer issues a SQLite read per key. It stays async because every caller
    awaits it and because a future per-guild resolver may need to.
    """
    return guild_setting_sync(guild_id, key)


async def guild_settings_many(guild_id: int, keys) -> dict:
    """Several typed settings at once, from the in-process cache."""
    return guild_settings_sync(guild_id, keys)


async def update_top_ranker_role(guild):
    top_user_id = await database.run_read(
        database.get_top_xp_user, guild_member_ids(guild)
    )
    if not top_user_id: return

    # Prefer the guild's typed setting, then the mirrored config id, and retain
    # the historical name lookup for an installation that set neither.
    top_role_id = await guild_setting(guild.id, "top_ranker_role")
    if top_role_id:
        role = guild.get_role(int(top_role_id))
    else:
        role = discord.utils.get(guild.roles, name="No. 1")

    if not role: return

    current_leader = role.members[0] if role.members else None

    if current_leader and current_leader.id == top_user_id:
        return 

    # Resolve the new holder *before* stripping the old one. A NotFound from
    # fetch_member used to leave the role on nobody until the next XP change.
    try:
        new_leader = (guild.get_member(top_user_id)
                      or await guild.fetch_member(top_user_id))
    except discord.HTTPException:
        return

    if current_leader:
        await current_leader.remove_roles(role)

    if new_leader:
        await new_leader.add_roles(role)
        
        # Resolve the announcement channel at use time so hot reload takes effect.
        levels_channels = guild_setting_sync(guild.id, "levels_channels")
        if levels_channels:
            channel = guild.get_channel(levels_channels[0])
            if channel:
                await channel.send(t("utils.top_ranker", user=new_leader.display_name))


async def _run_top_ranker_update(guild_id: int):
    try:
        await asyncio.sleep(TOP_RANKER_DEBOUNCE_SECONDS)
        guild = _top_ranker_dirty.pop(guild_id, None)
        if guild is not None:
            await update_top_ranker_role(guild)
    except asyncio.CancelledError:
        raise
    except Exception:
        utility_logger.exception(
            "Top-ranker reconciliation failed (guild_id=%s)", guild_id
        )
    finally:
        _top_ranker_tasks.pop(guild_id, None)
        guild = _top_ranker_dirty.get(guild_id)
        if guild is not None:
            mark_top_ranker_dirty(guild)


def mark_top_ranker_dirty(guild):
    """Coalesce high-frequency XP changes into one Discord role reconciliation."""
    if guild is None:
        return
    guild_id = int(guild.id)
    _top_ranker_dirty[guild_id] = guild
    task = _top_ranker_tasks.get(guild_id)
    if task is None or task.done():
        _top_ranker_tasks[guild_id] = asyncio.create_task(
            _run_top_ranker_update(guild_id),
            name=f"top-ranker-{guild_id}",
        )

async def update_user_data(member, balance_change=0, xp_change=0, win_inc=0, loss_inc=0):
    if xp_change:
        from feature_access import is_enabled
        if not is_enabled(member.guild.id if member.guild else None, "levels"):
            xp_change = 0
    result = await database.run_write(
        database.apply_user_delta,
        member.id, balance_change, xp_change, win_inc, loss_inc,
    )
    return await apply_database_result(member, result)


async def apply_database_result(member, result):
    """Applies Discord-side level effects after a committed database update."""
    stats = result["stats"]
    new_level = stats[2]
    if new_level > result["old_level"]:
        await check_level_roles(member, new_level)
    if result.get("xp_changed"):
        mark_top_ranker_dirty(member.guild)
    return stats

async def update_streak(user_id):
    now = datetime.now()
    today = now.date()
    result = await database.run_read(database.get_streak_data, user_id)

    if not result:
        return

    streak_count = result[0] if result[0] is not None else 0
    last_update_str = result[1]

    if not last_update_str:
        streak_count = 1
    else:
        last_update = datetime.fromisoformat(last_update_str).date()
        diff = (today - last_update).days

        if diff == 0:
            return 
        elif diff == 1 or diff == 2:
            streak_count += 1
        else:
            streak_count = 1

    await database.run_write(
        database.save_streak_data, user_id, streak_count, now.isoformat()
    )

def level_milestones(configured) -> dict[int, object]:
    """Parse a level-role map, dropping anything unusable rather than raising.

    The value is operator-edited JSON, so a key that is not a number or a value
    that is neither a role id nor a role name has to be skipped here — this runs
    inside the level-up path, and raising would swallow a member's level-up.
    """
    milestones = {}
    for raw_level, role_value in (configured or {}).items():
        try:
            threshold = int(raw_level)
        except (TypeError, ValueError):
            utility_logger.warning(
                "Ignoring a level-role milestone that is not a level (%r)", raw_level
            )
            continue
        usable = (
            isinstance(role_value, int) and not isinstance(role_value, bool)
        ) or (isinstance(role_value, str) and role_value.strip())
        if not usable:
            utility_logger.warning(
                "Ignoring the level %s role, which is neither an id nor a name (%r)",
                threshold, role_value,
            )
            continue
        milestones[threshold] = role_value
    return milestones


async def check_level_roles(member, level):
    """Announce a milestone and move the member onto the role it grants.

    A milestone's value may be a role id or a role name: the id is what an
    operator should configure, and the name is retained because an installation
    that never configured this has always relied on it.
    """
    level_milestones_map = level_milestones(
        await guild_setting(member.guild.id, "level_roles")
    )

    milestones = list(level_milestones_map.keys())
    if 2 not in milestones:
        milestones.append(2)

    if level in milestones:
        levels_channels = await guild_setting(member.guild.id, "levels_channels")
        if levels_channels:
            channel = member.guild.get_channel(levels_channels[0])
            if channel:
                if level == 2:
                    msg = t("utils.level_up_2", user=member.mention)
                else:
                    msg = t("utils.level_up_x", user=member.mention, level=level)
                await channel.send(msg)
    
    role_value = None
    for m_level in sorted(level_milestones_map.keys(), reverse=True):
        if level >= m_level:
            role_value = level_milestones_map[m_level]
            break
            
    if role_value:
        if isinstance(role_value, int):
            role = member.guild.get_role(role_value)
        else:
            role = discord.utils.get(member.guild.roles, name=role_value)
            
        if role and role not in member.roles:
            all_role_values = list(level_milestones_map.values())
            to_remove = []
            for r in member.roles:
                if r.id in all_role_values or r.name in all_role_values:
                    to_remove.append(r)
            
            if to_remove:
                await member.remove_roles(*to_remove)
            await member.add_roles(role)


def is_staff():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        staff_role_ids = guild_setting_sync(ctx.guild.id, "admin_roles")
        if any(role.id in staff_role_ids for role in ctx.author.roles):
            return True
        await ctx.send(t("utils.err_no_perms"), ephemeral=True)
        return False
    return commands.check(predicate)

def is_channel(allowed_ids):
    """Restrict a command to the channels one typed setting names.

    Takes a **setting key** rather than a dotted `config.json` path, and
    resolves it through `settings_cache`. This predicate runs on every
    invocation of every command carrying it, which makes it the most-executed
    configuration read in the project — it must not touch SQLite, and it must
    not stop working while the cache is cold, which is why the cache falls back
    to the file rather than to nothing.

    A bare int or list is still accepted, for a gate that is fixed rather than
    configurable.
    """
    async def predicate(ctx):
        if ctx.guild is None:
            await ctx.send(t("utils.err_no_dm"), ephemeral=True)
            return False

        resolved_ids = allowed_ids
        if isinstance(allowed_ids, str):
            resolved_ids = guild_setting_sync(ctx.guild.id, allowed_ids)
        # An unset channel setting is an empty gate, exactly as an absent
        # config path was: nobody but an administrator passes.
        if resolved_ids is None:
            resolved_ids = []
        clean_ids = (
            [resolved_ids]
            if isinstance(resolved_ids, int)
            else [int(x) for x in resolved_ids]
        )

        if ctx.author.guild_permissions.administrator:
            return True
        elif ctx.channel.id in clean_ids:
            return True
        
        try:
            channel_mentions = ", ".join([f"<#{id}>" for id in clean_ids])
        except:
            channel_mentions = t("utils.configured_channels")

        await ctx.send(t("utils.err_wrong_channel", channels=channel_mentions), ephemeral=True)
        return False
    return commands.check(predicate)

def is_higher_than(moderator, victim):
    if victim.id == moderator.guild.owner_id:
        return False
    if moderator.id == moderator.guild.owner_id:
        return True
    return moderator.top_role.position > victim.top_role.position

async def role_autocomplete(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
    from feature_access import is_enabled
    if not is_enabled(interaction.guild_id, "factions"):
        return []
    user = interaction.user
    allowed_roles = []
    factions_config = guild_setting_sync(interaction.guild_id, "factions")

    if user.guild_permissions.administrator:
        for data in factions_config.values():
            for r_id in data.get("manageable_ids", []):
                role_obj = interaction.guild.get_role(r_id)
                if role_obj:
                    allowed_roles.append(role_obj)
    else:
        user_role_ids = [r.id for r in user.roles]
        for data in factions_config.values():
            leader_id = data.get("leader_role_id")
            if leader_id in user_role_ids:
                for r_id in data.get("manageable_ids", []):
                    role_obj = interaction.guild.get_role(r_id)
                    if role_obj:
                        allowed_roles.append(role_obj)

    choices = []
    for role in allowed_roles:
        if current.lower() in role.name.lower():
            choices.append(discord.app_commands.Choice(name=role.name, value=str(role.id)))
    
    return choices[:25]

def _faction_entries(guild_id):
    """This guild's faction definitions, as a list of whatever is stored.

    `config["factions"]` was operator-authored JSON and still is — it is a typed
    JSON setting now, and a malformed entry has to be skipped rather than raise,
    because both callers run inside a button callback.
    """
    stored = guild_setting_sync(guild_id, "factions") if guild_id else {}
    return list(stored.values()) if isinstance(stored, dict) else []


def member_faction_role_ids(member) -> set[int]:
    """Every role id belonging to the factions this member is part of.

    A member belongs to a faction when they hold its leader role or any of the
    roles that faction manages, so one helper answers for a leader and for an
    ordinary member alike, and somebody in two factions gets the union.

    An empty set means "no faction", never "every faction" — a caller granting
    access from this must refuse rather than fall back to allowing everyone.
    """
    held = {role.id for role in getattr(member, "roles", ())}
    guild = getattr(member, "guild", None)
    allowed: set[int] = set()
    for data in _faction_entries(getattr(guild, "id", None)):
        if not isinstance(data, dict):
            continue
        leader_id = data.get("leader_role_id")
        faction_ids = {role_id for role_id in data.get("manageable_ids") or ()
                       if isinstance(role_id, int)}
        if isinstance(leader_id, int):
            faction_ids.add(leader_id)
        if held & faction_ids:
            allowed |= faction_ids
    return allowed


def all_faction_role_ids(guild_id: int) -> set[int]:
    """Every role id any configured faction claims.

    Used to reverse a faction lock without persisting which roles it granted:
    the channel's own overwrites say what to clear.
    """
    every: set[int] = set()
    for data in _faction_entries(guild_id):
        if not isinstance(data, dict):
            continue
        leader_id = data.get("leader_role_id")
        if isinstance(leader_id, int):
            every.add(leader_id)
        every |= {role_id for role_id in data.get("manageable_ids") or ()
                  if isinstance(role_id, int)}
    return every


def is_premium(member):
    premium_ids = guild_setting_sync(member.guild.id, "premium_roles")
    for role in member.roles:
        if role.id in premium_ids:
            return True
    if member.premium_since is not None:
        return True
    return False


def can_self_assign_role(guild, role):
    """Rejects privileged, staff, managed, and bot-unmanageable roles."""
    if role is None or guild.me is None:
        return False
    privileged_permissions = (
        "administrator", "manage_guild", "manage_roles", "manage_channels",
        "kick_members", "ban_members", "moderate_members", "manage_webhooks",
    )
    staff_role_ids = set(guild_setting_sync(guild.id, "admin_roles"))
    return not (
        role.is_default()
        or role.managed
        or role.id in staff_role_ids
        or role >= guild.me.top_role
        or any(
            getattr(role.permissions, permission, False)
            for permission in privileged_permissions
        )
    )


# --- Background loop supervision -------------------------------------------
#
# A discord.py `tasks.loop` stops permanently on an unhandled exception. Loops
# started from `on_ready` at least come back on a gateway reconnect; loops
# started in a cog's `__init__` have no revival path at all, so one transient
# database error silently ends premium-role revocation or rental cleanup for the
# lifetime of the process. Every loop therefore gets an `@<loop>.error` handler
# routed through here.

LOOP_RESTART_DELAY_SECONDS = 30


def restart_background_loop(bot, loop, name, logger):
    """Restart a stopped loop, unless the bot is shutting down."""
    if bot.is_closed() or loop.is_running():
        return
    try:
        loop.restart()
        logger.warning("Background loop restarted (loop=%s)", name)
    except RuntimeError:
        logger.exception("Background loop restart failed (loop=%s)", name)


async def handle_loop_error(bot, loop, name, error, logger):
    """Log a loop's fatal exception and schedule it to come back."""
    logger.exception(
        "Background loop failed; restart scheduled (loop=%s, error=%s)",
        name, type(error).__name__, exc_info=error,
    )
    bot.loop.call_later(
        LOOP_RESTART_DELAY_SECONDS, restart_background_loop, bot, loop, name, logger
    )
