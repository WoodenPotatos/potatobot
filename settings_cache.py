"""In-memory typed settings, so a command path never issues a SQLite read.

`feature_access` is the model, not a thing to reinvent: per-guild state held in
memory, reconciled against a persisted revision, and updated immediately for a
dashboard change made in this process. Two settings are on paths hot enough that
this is the whole point — `is_channel` resolves a channel list on every command
invocation, and `maintenance_blocks` runs on every interaction including every
component and modal.

**The fallback is the file, never nothing.** This is the one place where copying
`feature_access` verbatim would be a defect. `is_enabled` fails *closed* because
running a paid command under unknown policy is worse than refusing it. A setting
has no such safe refusal: an empty `economy_channels` does not "refuse", it
changes where every economy command is allowed. So when the cache does not know,
this resolves the way the bot resolved before the cache existed — the stored row
if there is one, then `config.json` through
`settings_registry.legacy_config_value`, then the registry default.

`config.json` is now **read-only and nothing writes it**. It survives as the
source for a setting an installation has never saved, which is what makes
`scripts/import_config.py` a migration an operator can take at their own pace
rather than a prerequisite for upgrading: pull this and the file still answers,
run the import and the rows answer instead.

That also gives `maintenance` the fail-*open* it requires: its legacy path and
its registry default are both "not in maintenance", so an unreadable cache
cannot take the bot down. `feature_access.maintenance_blocks` depends on that,
and `tests/test_settings_cache.py` pins both directions, because the two are one
line apart and opposite.
"""

import logging
import threading

import database
from settings_registry import (
    SETTING_DEFINITIONS,
    SettingScope,
    legacy_config_value,
)

logger = logging.getLogger("PotatoBot.SettingsCache")

# guild_id -> {setting_key: value}. Only rows that exist; a key with no row is
# absent here and resolves through the fallback chain, which is what keeps a
# missing row and a row holding the default distinguishable.
_GUILD_VALUES: dict[int, dict] = {}
# setting_key -> value, for the installation-wide table.
_INSTANCE_VALUES: dict = {}
# The highest settings audit id this process has loaded. One global number
# rather than one per guild: an instance setting's audit row records the guild it
# was changed *from*, so a per-guild revision would let another guild miss it.
_REVISION = None
_LOCK = threading.RLock()


def _config() -> dict:
    """The live config dictionary, imported late to avoid an import cycle."""
    from cogs.utils import config
    return config


def setting(guild_id: int | None, key: str):
    """The effective value of one typed setting, from memory.

    Raises `KeyError` for a key that is not registered. That is deliberate: a
    misspelled setting key is a programming error, and returning a plausible
    default for it is how a gate silently stops gating.
    """
    definition = SETTING_DEFINITIONS[key]
    with _LOCK:
        if definition.scope is SettingScope.INSTANCE:
            if key in _INSTANCE_VALUES:
                return _INSTANCE_VALUES[key]
        elif guild_id is not None:
            values = _GUILD_VALUES.get(int(guild_id))
            if values is not None and key in values:
                return values[key]
    return legacy_config_value(definition, _config())


def settings(guild_id: int | None, keys) -> dict:
    """Several settings at once, resolved the same way."""
    return {key: setting(guild_id, key) for key in keys}


def is_loaded(guild_id: int | None = None) -> bool:
    """Whether this process has read the stored rows at least once.

    Reported rather than acted on. Nothing may refuse work because the cache is
    cold — the fallback chain covers that — but a diagnostic wants to know.
    """
    with _LOCK:
        if guild_id is None:
            return _REVISION is not None
        return int(guild_id) in _GUILD_VALUES


def store(guild_id: int, stored: dict[str, dict]) -> None:
    """Replace one guild's cached rows from a `get_guild_settings` result.

    That accessor returns guild and instance rows merged, so they are split back
    apart here by the registry's declared scope: the instance half is shared, and
    letting a guild's snapshot own a copy of it would let two guilds disagree
    about an installation-wide value.
    """
    guild_id = int(guild_id)
    guild_values, instance_values = {}, {}
    for key, row in stored.items():
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            # A row for a retired setting. Not an error: the row outlives the
            # definition, and the reader simply never asks for it again.
            continue
        target = (instance_values if definition.scope is SettingScope.INSTANCE
                  else guild_values)
        target[key] = row["value"]
    with _LOCK:
        _GUILD_VALUES[guild_id] = guild_values
        _INSTANCE_VALUES.update(instance_values)


def apply_changes(guild_id: int, changes: dict[str, dict]) -> None:
    """Apply a committed settings patch to this process immediately.

    The dashboard and the bot converge by revision polling when they are
    separate processes; when they are the same process, waiting for a poll would
    mean a save the operator just made not being visible yet.
    """
    guild_id = int(guild_id)
    with _LOCK:
        guild_values = _GUILD_VALUES.setdefault(guild_id, {})
        for key, change in changes.items():
            definition = SETTING_DEFINITIONS.get(key)
            if definition is None:
                continue
            if definition.scope is SettingScope.INSTANCE:
                _INSTANCE_VALUES[key] = change["value"]
            else:
                guild_values[key] = change["value"]


def invalidate() -> None:
    """Forget everything, so the next refresh reloads unconditionally."""
    global _REVISION
    with _LOCK:
        _GUILD_VALUES.clear()
        _INSTANCE_VALUES.clear()
        _REVISION = None


def load_instance_sync() -> bool:
    """Load the installation-wide rows synchronously, before the loop exists.

    `main.py` needs the command prefix before it constructs the bot, and a cog
    needs the language before its import-time metadata is built — both happen
    before there is an event loop to await on. This is the one synchronous read,
    it happens once at startup, and it is **allowed to fail**: an unreadable
    database must not stop the bot coming up on the default prefix, or a database
    problem locks the operator out of the very commands that would fix it.
    """
    try:
        rows = database.get_instance_settings()
    except Exception:
        logger.exception("Could not load installation settings at startup")
        return False
    with _LOCK:
        _INSTANCE_VALUES.update({key: row["value"] for key, row in rows.items()})
    return True


async def refresh(guild_ids, *, force: bool = False) -> bool:
    """Reload cached rows when the persisted settings revision has moved.

    Returns whether anything was reloaded. A failure leaves the last known-good
    cache in place and propagates, exactly as the feature cache does: the caller
    logs it, and every reader keeps resolving through the fallback chain.
    """
    global _REVISION
    revision = await database.run_read(database.get_settings_revision)
    with _LOCK:
        unchanged = _REVISION == revision and _REVISION is not None
        known = set(_GUILD_VALUES)
    wanted = {int(guild_id) for guild_id in guild_ids}
    if not force and unchanged and wanted <= known:
        return False
    for guild_id in wanted:
        stored = await database.run_read(database.get_guild_settings, guild_id)
        store(guild_id, stored)
    with _LOCK:
        _REVISION = revision
    return True
