"""The schema owner: DDL, migrations, connections and every query.

Rules for changing it, including the engine-neutral SQL the PostgreSQL move
depends on: docs/subsystems/database.md.
"""

import logging
import math
import os
import json
import asyncio
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.adapt import Dumper
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from core.clock import local_date, parse_stored, stored_iso, utc_now

from core import item_catalog

# Reuse the application's database logger so file and console policies stay centralized.
db_logger = logging.getLogger('PotatoBot.Database')

def _read_worker_count() -> int:
    try:
        workers = int(os.getenv("POTATOBOT_DB_READ_WORKERS", "4"))
    except ValueError as exc:
        raise ValueError("POTATOBOT_DB_READ_WORKERS must be an integer") from exc
    if not 2 <= workers <= 8:
        raise ValueError("POTATOBOT_DB_READ_WORKERS must be between 2 and 8")
    return workers


# WAL permits independent readers while SQLite still requires deliberate write
# serialization. Bot reads and writes therefore use separate executors. Direct
# synchronous callers (the dashboard and tests) take the write lock by default.
_READ_EXECUTOR = ThreadPoolExecutor(
    max_workers=_read_worker_count(),
    thread_name_prefix="db-read",
)
_WRITE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-write")
_DB_WRITE_LOCK = threading.RLock()
_DB_EXECUTION = threading.local()

# Legacy callers may still use ``run``. Keeping this classification explicit
# prevents an accidental write from reaching the concurrent read pool.
READ_ONLY_OPERATIONS = {
    "get_active_guild_ids", "get_active_guilds", "get_feature_states",
    "get_feature_revision", "is_feature_enabled", "get_realms",
    "resolve_data_context",
    "get_guild_data_scopes", "get_user_balance", "get_voice_settings",
    "get_voice_permissions", "get_active_channel_owner", "get_ticket_opener",
    "get_open_support_ticket", "get_ticket_claimer",
    "user_exists", "get_warning_count", "get_warnings", "get_user_intel",
    "get_top_levels", "get_top_balances", "get_top_streaks", "get_top_gacha_luck",
    "get_user_profile", "get_user_rank", "get_inactivity_data",
    "get_all_rentals", "get_cooldown", "get_rob_stats",
    "get_top_xp_user", "get_full_user_data", "get_streak_data",
    "get_shop_price", "get_shop_prices", "get_reward", "get_config_id",
    "get_gacha_banner", "list_gacha_banners", "get_work_responses",
    "get_five_star_history", "get_gacha_pity", "get_active_entitlements",
    "get_minigame_state",
    "get_lfg_post",
    "get_expired_lfg_posts",
    "get_user_inventory",
    "get_user_vouchers",
    "get_guild_settings", "get_instance_settings", "get_schema_version",
    "list_managed_messages", "get_managed_message",
    "get_settings_revision",
    "get_settings_audit", "get_privacy_audit", "get_shop_item_definitions",
    "list_dashboard_documents", "get_control_action",
    "get_fulfillment_requests",
    "get_expired_entitlements",
    "export_user_data", "get_active_entitlements_for_user",
    "get_retention_candidates",
    "get_game_patch_state", "get_guild_patch_announcement",
    "get_latest_access_invalidation_id", "get_access_invalidations_since",
}


async def _run_in_executor(executor, mode, fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    submitted_at = time.monotonic()

    def invoke():
        queue_seconds = time.monotonic() - submitted_at
        if queue_seconds >= 0.25:
            db_logger.warning(
                "Database executor queue delay "
                "(mode=%s, operation=%s, delay_ms=%s)",
                mode, getattr(fn, "__name__", "unknown"),
                round(queue_seconds * 1000),
            )
        started_at = time.monotonic()
        previous_mode = getattr(_DB_EXECUTION, "mode", None)
        _DB_EXECUTION.mode = mode
        try:
            lock = _DB_WRITE_LOCK if mode == "write" else nullcontext()
            with lock:
                return fn(*args, **kwargs)
        finally:
            _DB_EXECUTION.mode = previous_mode
            duration = time.monotonic() - started_at
            if duration >= 0.25:
                db_logger.warning(
                    "Slow database operation (mode=%s, operation=%s, duration_ms=%s)",
                    mode, getattr(fn, "__name__", "unknown"),
                    round(duration * 1000),
                )

    return await loop.run_in_executor(executor, invoke)


async def run_read(fn, *args, **kwargs):
    """Execute a read-only synchronous accessor on the concurrent read pool."""
    return await _run_in_executor(_READ_EXECUTOR, "read", fn, *args, **kwargs)


def run_read_sync(fn, *args, **kwargs):
    """Run a classified read accessor on the calling thread without the writer lock.

    The dashboard is served by Waitress threads that have no event loop, so they
    cannot use ``run_read``. Marking the thread as a reader gives them the same
    ``query_only`` connection the read pool uses and keeps them from serializing
    against the bot's writer for the duration of every dashboard page load.
    """
    name = getattr(fn, "__name__", "")
    if name not in READ_ONLY_OPERATIONS:
        raise ValueError(f"{name!r} is not a classified read-only operation")
    previous_mode = getattr(_DB_EXECUTION, "mode", None)
    _DB_EXECUTION.mode = "read"
    started_at = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        _DB_EXECUTION.mode = previous_mode
        duration = time.monotonic() - started_at
        if duration >= 0.25:
            db_logger.warning(
                "Slow database operation (mode=read-sync, operation=%s, duration_ms=%s)",
                name, round(duration * 1000),
            )


async def run_write(fn, *args, **kwargs):
    """Execute a write or transaction on the serialized writer."""
    return await _run_in_executor(_WRITE_EXECUTOR, "write", fn, *args, **kwargs)


async def run(fn, *args, **kwargs):
    """Compatibility path with explicit read classification and safe write fallback."""
    if getattr(fn, "__name__", "") in READ_ONLY_OPERATIONS:
        return await run_read(fn, *args, **kwargs)
    return await run_write(fn, *args, **kwargs)


def shutdown_executors(wait: bool = True):
    """Release database worker threads and connections during orderly shutdown."""
    _READ_EXECUTOR.shutdown(wait=wait, cancel_futures=True)
    _WRITE_EXECUTOR.shutdown(wait=wait, cancel_futures=True)
    close_connection_pool()

# Resolve the database from an explicit deployment override or the repository
# root. This module lives in `core/`, so the root is two levels up -- one too
# few would point a whole installation at `core/economy.db`.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The SQLite file 2.20 and earlier ran on. Nothing here opens it any more; it
# is what `data_dir()` falls back to beside, so an upgraded installation's
# session secret and cards stay where they were, and what the copy script
# reads (`scripts/migrate_sqlite_to_postgres.py`).
DB_PATH = os.getenv("POTATOBOT_DB_PATH", os.path.join(BASE_DIR, "economy.db"))

# Which PostgreSQL to use: a libpq connection string, or empty for libpq's own
# environment (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD or PGPASSFILE),
# which is how a deployment configures it, so no code here parses or logs a
# secret. A module global read at call time, like DB_PATH: a test or a tool
# points it at another database by construction.
DSN = ""

# Where the installation's own files live -- the dashboard session secret, the
# welcome-card backgrounds -- named apart from the database, because on
# PostgreSQL the database has no directory to live beside (todo.md, "2.20.0:
# PostgreSQL"). Unset, empty or blank means beside the database, where they have
# always been, so nothing moves until a deployment sets it. A module global read
# at call time, like DB_PATH: a test or a copy points it elsewhere by
# construction.
DATA_DIR = os.getenv("POTATOBOT_DATA_DIR", "").strip() or None


def data_dir() -> str:
    """The directory the installation's own files live in."""
    if DATA_DIR:
        return os.path.abspath(DATA_DIR)
    return os.path.dirname(os.path.abspath(DB_PATH))

VALID_COOLDOWN_COLUMNS = {
    "last_daily", "last_job", "last_rob",
    "last_loldle_easy", "last_loldle_medium", "last_loldle_hard",
    "last_valdle", "last_genshindle", "last_dbdle_killer",
    "last_dbdle_survivor", "last_dbdle_perk",
}

# The largest amount any single balance change, stake or transfer may carry.
# Far above any real balance, and far below 2**63, the edge of a `bigint`: an
# amount past that once reached the driver as an int it could not bind and
# raised `OverflowError`, which no `except EngineError` catches -- so `/pay`
# with a twenty-digit amount died silently from the member's side. Every command refuses first with a message; this is
# the backstop for the paths that do not.
MAX_AMOUNT = 10**12

# A claimed control action is re-queued only once its lease expires. Nothing
# renews it today: `renew_control_action_lease` exists but has no runtime
# caller. A slow publish still cannot be posted twice, because the one worker is
# sequential and only reclaims lapsed leases when it claims its *next* action.
# A second concurrent worker would need the renewal wired in first.
CONTROL_ACTION_LEASE_SECONDS = 120

USER_COLUMNS = {
    "balance": "INTEGER DEFAULT 100",
    "xp": "INTEGER DEFAULT 0",
    "level": "INTEGER DEFAULT 1",
    "bj_wins": "INTEGER DEFAULT 0",
    "bj_losses": "INTEGER DEFAULT 0",
    "last_daily": "TEXT",
    "last_job": "TEXT",
    "rob_bonus": "REAL DEFAULT 0.0",
    "rob_defense": "REAL DEFAULT 1.0",
    "vault_protection": "REAL DEFAULT 0.0",
    "protected_reserve": "INTEGER NOT NULL DEFAULT 0",
    "bodyguard_until": "TEXT",
    "last_rob": "TEXT",
    "last_loldle_easy": "TEXT",
    "last_loldle_medium": "TEXT",
    "last_loldle_hard": "TEXT",
    "last_valdle": "TEXT",
    "last_genshindle": "TEXT",
    "last_dbdle_killer": "TEXT",
    "last_dbdle_survivor": "TEXT",
    "last_dbdle_perk": "TEXT",
    "streak_count": "INTEGER DEFAULT 0",
    "last_streak_update": "TEXT",
    "last_active": "TEXT DEFAULT NULL",
    "inactive_warned": "INTEGER DEFAULT 0",
    "rules_read_time": "INTEGER DEFAULT NULL",
}

# Prices come from the shared item catalog rather than a second literal, so the
# shop and the gacha can never disagree about which built-in items exist.
SHOP_DEFAULTS = item_catalog.shop_default_prices()

# A gacha reward key names a locale entry and is written into every immutable
# pull row, so its shape is constrained rather than left as free text.
_GACHA_REWARD_KEY = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
# A banner key is written into every pull row and is addressed from the
# Discord command, so it takes the same shape as a reward key.
_GACHA_BANNER_KEY = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
# The installation default. It is the only banner a pull may create on
# demand and the only one that cannot be deleted, so a guild always has
# somewhere for `/gacha` to land.
DEFAULT_GACHA_BANNER_KEY = "standard"
# Long enough for a readable banner name, short enough to render in a
# Discord choice label without being truncated.
GACHA_BANNER_NAME_MAX_LENGTH = 64
# One banner per Discord choice list, which holds 25 options.
GACHA_BANNER_LIMIT = 25

# Built-in shop keys are reserved. A dashboard-defined item carrying one of them
# would replace that item's purchase handler and price in the live shop menu, so
# both the creation route and the menu builder screen against this single set.
BUILTIN_SHOP_KEYS = frozenset(SHOP_DEFAULTS)

REWARD_DEFAULTS = {
    "loldle_easy": (2500, 100), "loldle_medium": (5000, 100),
    "loldle_hard": (7500, 150), "valdle": (5000, 100),
    "genshindle": (5000, 100),
    "dbdle": (5000, 100), "daily_normal": (5000, 50),
    "daily_premium": (10000, 50), "chat_message": (5, 2),
    "voice_minute_normal": (5, 5), "voice_minute_premium": (10, 10),
}

DEFAULT_GACHA_CONFIG: dict = {
    "cost": 5000,
    "hard_pity": 100,
    "soft_pity_start": 75,
    "soft_pity_multiplier": 3,
    "four_star_guarantee_interval": 10,
    "duplicate_percent": 10,
    # The featured split, as a percentage. Consulted only when a tier has a
    # featured reward, so the shipped banner — which has none, and must not gain
    # one — is unaffected. `featured` itself is deliberately absent from every
    # shipped reward: `missing_shipped_rewards` matches on the key alone and
    # pushes these dicts straight into an operator's table, so a shipped flag
    # would leak into every banner and could never be reconciled away.
    "featured_split": 50,
    "tiers": {"3": 97800, "4": 1600, "5": 600},
    "rewards": {
        "3": [
            {"key": "loaded_die", "kind": "item", "amount": 1, "weight": 400},
            {"key": "lockpick", "kind": "item", "amount": 1, "weight": 100},
            # The casino consumables sit here rather than in tier 4 because they
            # cost what a loaded die costs. Each ships at the lockpick's weight;
            # a banner tunes its own table from the dashboard.
            {"key": "lucky_charm", "kind": "item", "amount": 1, "weight": 100},
            {"key": "stacked_deck", "kind": "item", "amount": 1, "weight": 100},
            {"key": "marked_card", "kind": "item", "amount": 1, "weight": 100},
            {"key": "metal_detector", "kind": "item", "amount": 1, "weight": 100},
            {"key": "parachute", "kind": "item", "amount": 1, "weight": 100},
            # No coin reward ships, in any tier. An amount is one guild's
            # economy — 250 means one thing at this scaling and nothing at
            # another — so shipping four fixed amounts is the same mistake as a
            # role id in a registry default: a fact about one installation
            # travelling to every one. A guild that wants coins builds a
            # `coin_bundle` item at its own amount, or types a key; the `coins`
            # kind and the `gacha.rewards.coins_*` labels both stay, because
            # banners saved before this and every `gacha_pulls` row still name
            # them. That is the `vault_25000` rule.
        ],
        "4": [
            {"key": "emoji_30d", "kind": "voucher", "amount": 30, "weight": 1},
            {"key": "sticker_30d", "kind": "voucher", "amount": 30, "weight": 1},
            {"key": "sound_30d", "kind": "voucher", "amount": 30, "weight": 1},
            {"key": "vault_glove", "kind": "item", "amount": 1, "weight": 1},
            {"key": "streak_freeze", "kind": "item", "amount": 1, "weight": 1},
            # Vaults use the shop's item keys and the catalog's reserves, so the
            # same key always means the same protection whichever system awarded
            # it. Banners saved before this change keep their vault_25000 and
            # vault_500000 rows; those keys stay renderable for pull history.
            {"key": "small_vault", "kind": "vault",
             "amount": item_catalog.VAULT_ITEMS["small_vault"].value, "weight": 1},
            {"key": "med_vault", "kind": "vault",
             "amount": item_catalog.VAULT_ITEMS["med_vault"].value, "weight": 1},
        ],
        "5": [
            {"key": "big_vault", "kind": "vault",
             "amount": item_catalog.VAULT_ITEMS["big_vault"].value, "weight": 1},
            {"key": "premium_30d", "kind": "voucher", "amount": 30, "weight": 1},
            {"key": "emoji_180d", "kind": "voucher", "amount": 180, "weight": 1},
            {"key": "sticker_180d", "kind": "voucher", "amount": 180, "weight": 1},
            {"key": "sound_180d", "kind": "voucher", "amount": 180, "weight": 1},
        ],
    },
}


# The driver's exceptions under engine-neutral names. Handlers catch these and
# never the driver's own module, so an engine change repoints these two lines
# rather than every handler (2.20.1a). ``EngineError`` is the
# driver's root class, DB-API ``Error`` -- deliberately not DB-API's narrower
# ``DatabaseError``, which would miss an interface error.
EngineError = psycopg.Error
IntegrityError = psycopg.IntegrityError

_PLAIN_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def sql_identifier(name: str) -> str:
    """A table or column name to interpolate into SQL, or `ValueError`.

    Only a plain lowercase identifier passes, and it is returned unchanged:
    every name in this schema is one, both engines accept it unquoted, and the
    SQL text stays byte-identical. Quoting is deliberately not the answer --
    SQLite reads a double-quoted name that matches no column as a string
    literal, so a typo would select text instead of failing. Every runtime
    f-string that names a table or column goes through here
    (`TransactionShapeTests`, docs/subsystems/database.md).
    """
    if not isinstance(name, str) or not _PLAIN_IDENTIFIER.fullmatch(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


class DatabaseOperationError(RuntimeError):
    """Raised when a critical database write cannot be committed."""


class ValidationError(ValueError):
    """A rejected value that names why, using a stable machine-readable reason.

    The message stays English developer prose for logs. Callers that show
    something to a person map ``reason`` to their own localized text, so the
    model layer never has to know about locale catalogs.
    """

    def __init__(self, reason: str, message: str, **params):
        super().__init__(message)
        self.reason = reason
        self.params = params


class RevisionConflictError(DatabaseOperationError):
    """Raised when an optimistic revision no longer matches the stored row.

    Callers map this to HTTP 409. It is a distinct type so that behaviour cannot
    be broken by rewording a message, which a substring check would not survive.
    """


def write_settings_audit(conn, guild_id: int | None, actor_id: int, action: str,
                         target_key: str, old_value=None, new_value=None):
    """Append an audit record on an existing connection.

    Callers that are already inside a transaction must use this so the audit row
    commits together with the change it describes, instead of being a separate
    write that a failure in between could leave out.
    """
    conn.execute(
        "INSERT INTO settings_audit "
        "(guild_id, actor_id, action, target_key, old_value_json, "
        "new_value_json, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            guild_id, int(actor_id), action, target_key,
            json.dumps(old_value, sort_keys=True) if old_value is not None else None,
            json.dumps(new_value, sort_keys=True) if new_value is not None else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def record_settings_audit(guild_id: int | None, actor_id: int, action: str,
                          target_key: str, old_value=None, new_value=None):
    """Append a structured privileged-change audit record in its own transaction."""
    with get_connection() as conn:
        write_settings_audit(conn, guild_id, actor_id, action, target_key,
                             old_value, new_value)


# Schema 8. Every table below is keyed by ``guild_id`` where 0 means "installation
# default": a guild's own row wins and a missing one falls back to the default, so
# no legacy guild is ever guessed at migration time. Adding a column to a primary
# key is not something SQLite can do in place, so these tables are rebuilt rather
# than altered.
# The role menus 2.x seeded from the old `*_roles` settings. The settings are
# long gone and the seeding with them (SQLite's migration chain, retired with
# the PostgreSQL move); the menu keys stay because their locale labels do
# (`scripts/locale_audit.py`).
SEEDED_ROLE_MENUS = (
    ("game_roles", "games"),
    ("news_roles", "news"),
    ("theme_roles", "themes"),
)


def repair_warning_provenance(conn) -> int:
    """Attribute pre-schema-8 rows that carry no guild, where that is knowable.

    `adopt_legacy_database` already does this, but only in the `private` profile
    and only once — so an installation that upgraded under another profile keeps
    rows a threshold cannot count. Gated on there being exactly one active guild:
    with several, a NULL row is genuinely unattributable and guessing would move
    somebody's moderation history into a guild it never happened in.

    Idempotent, and a no-op wherever adoption has already run.
    """
    guilds = [row[0] for row in conn.execute(
        "SELECT guild_id FROM guilds WHERE active = 1")]
    if len(guilds) != 1:
        return 0
    repaired = 0
    for table in ("warnings", "tickets", "rented_items"):
        repaired += conn.execute(
            f"UPDATE {sql_identifier(table)} SET guild_id = %s WHERE guild_id IS NULL",
            (guilds[0],),
        ).rowcount
    return repaired


# The schema is the baseline plus numbered migrations, one file each, applied in
# order and recorded in `schema_migrations` (docs/subsystems/database.md).
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")
_SCHEMA_FILE = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")
BASELINE_VERSION = 26


def schema_files() -> list[tuple[int, str]]:
    """(version, path) for the baseline and every migration, in order."""
    files = []
    for name in sorted(os.listdir(SCHEMA_DIR)):
        match = _SCHEMA_FILE.fullmatch(name)
        if match:
            files.append((int(match.group(1)), os.path.join(SCHEMA_DIR, name)))
    versions = [version for version, _ in files]
    if not files or versions[0] != BASELINE_VERSION or len(set(versions)) != len(versions):
        raise DatabaseOperationError("core/schema must start at the baseline, one file per version")
    return files


LATEST_SCHEMA_VERSION = schema_files()[-1][0]


def _seed_installation_defaults(conn) -> int:
    """The rows a fresh installation needs: the installation-default prices and
    rewards at guild 0, which a guild without an override reads, and the
    shipped `/work` responses. Idempotent, so every start runs it."""
    for key, value in SHOP_DEFAULTS.items():
        conn.execute(
            "INSERT INTO shop_prices (guild_id, item_id, price) "
            "VALUES (0, %s, %s) ON CONFLICT DO NOTHING",
            (key, value),
        )
    for key, values in REWARD_DEFAULTS.items():
        conn.execute(
            "INSERT INTO rewards "
            "(guild_id, activity_id, coin_reward, xp_reward) VALUES (0, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (key, values[0], values[1]),
        )
    return seed_default_work_responses(conn)


def _pre_migration_backup(version: int) -> str:
    """A `pg_dump` of the whole database before a migration touches it.

    Written owner-only into `data_dir()/backups`, named by the version it was
    taken at and the local time (a filename an operator reads, not a value
    anything parses). A migration with no backup behind it is refused: the dump
    is the rollback.
    """
    import subprocess

    directory = os.path.join(data_dir(), "backups")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, f"potatobot-v{version}-{stamp}.dump")
    # O_EXCL refuses to write into a file already there, and 0600 holds
    # whatever umask the caller has: the dump is every member's data.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            completed = subprocess.run(
                ["pg_dump", "--format=custom", "--dbname", DSN],
                stdout=output, stderr=subprocess.PIPE, check=False, timeout=600,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        os.unlink(path)
        raise DatabaseOperationError("pre-migration backup failed") from exc
    if completed.returncode != 0:
        os.unlink(path)
        raise DatabaseOperationError("pre-migration backup failed")
    db_logger.info("Pre-migration database backup created: %s", path)
    return path


def initialize_database():
    """Bring the database to the latest schema, then seed what a fresh
    installation needs.

    An empty database gets the baseline; one that already has the schema gets
    every numbered migration past its recorded version, after a `pg_dump`
    whenever it holds data. Everything runs in one transaction under the write
    lock, and PostgreSQL's DDL is transactional, so an interrupted start leaves
    the schema it found and two processes starting at once take turns. Only
    the bot calls this; the dashboard never creates the schema.
    """
    files = schema_files()
    try:
        with get_connection() as conn:
            present = conn.execute(
                "SELECT to_regclass('public.schema_migrations')").fetchone()[0]
            if present is None:
                if conn.execute("SELECT count(*) FROM pg_tables "
                                "WHERE schemaname = 'public'").fetchone()[0]:
                    raise DatabaseOperationError(
                        "the database has tables but no schema_migrations; "
                        "refusing to guess what it holds")
                current = 0
            else:
                current = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            if current > LATEST_SCHEMA_VERSION:
                raise DatabaseOperationError(
                    f"database schema {current} is newer than supported "
                    f"version {LATEST_SCHEMA_VERSION}")
            pending = [(version, path) for version, path in files if version > current]
            if current and pending and conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM users)").fetchone()[0]:
                _pre_migration_backup(current)
            for version, path in pending:
                with open(path, encoding="utf-8") as schema_file:
                    conn.execute(schema_file.read())
                if version != BASELINE_VERSION:  # the baseline records itself
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) "
                        "VALUES (%s, %s)", (version, utc_now()))
            if pending:
                db_logger.info("Applied schema %s.",
                               ", ".join(str(version) for version, _ in pending))
            attributed = repair_warning_provenance(conn)
            if attributed:
                db_logger.info(
                    "Attributed %s legacy row(s) with no guild provenance.",
                    attributed)
            seeded = _seed_installation_defaults(conn)
            if seeded:
                db_logger.info(
                    "Seeded %s installation-default /work responses.", seeded)
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            database_name = conn.info.dbname
        db_logger.info(
            "Database ready (database=%s, schema=%s, users=%s)",
            database_name, LATEST_SCHEMA_VERSION, user_count,
        )
    except DatabaseOperationError:
        raise
    except EngineError as exc:
        db_logger.exception("Database migration failed")
        raise DatabaseOperationError("database migration failed") from exc
def _ensure_user(conn, user_id: int, timestamp: str = None):
    conn.execute(
        """
        INSERT INTO users (
            user_id, balance, xp, level, bj_wins, bj_losses,
            rob_bonus, rob_defense, vault_protection, last_active,
            inactive_warned
        ) VALUES (%s, 100, 0, 1, 0, 0, 0.0, 1.0, 0.0, %s, 0)
        ON CONFLICT DO NOTHING
        """,
        (user_id, timestamp or utc_now().isoformat()),
    )


def level_for_xp(xp: int) -> int:
    """The level a given amount of XP is worth.

    The one definition of the curve. It was written inline in two places and was
    about to be written in a third — with its inverse beside it — which is how a
    formula comes to disagree with itself. Level is **derived** and never stored
    independently: every write recomputes it, so anything that set `users.level`
    on its own would be undone by the member's next message.
    """
    return int(math.sqrt(max(0, xp) / 10)) + 1


def xp_for_level(level: int) -> int:
    """The XP a level starts at — the inverse of `level_for_xp`.

    Level 1 begins at zero, and each level is the next square: 10, 40, 90, 160.
    `/setlevel` writes this, which is why it puts a member at the *floor* of the
    level rather than anywhere inside it.
    """
    return 10 * (max(1, level) - 1) ** 2


def _apply_stats_locked(conn, user_id: int, balance_change: int = 0,
                        xp_change: int = 0, win_inc: int = 0,
                        loss_inc: int = 0, clamp_balance: bool = True,
                        casino_rate_limit: tuple[int, int] | None = None):
    """`casino_rate_limit`, when given, is `(window_seconds, max_coins)`,
    already resolved by the caller from that guild's settings -- only the six
    casino payout resolvers pass it, and only for their net-positive credit.
    A credit that would push the user's rolling window total past max_coins is
    clamped to whatever budget remains (never refused outright, never negative,
    never clawing back a smaller win already shown in Discord); the result
    reports whether this call was throttled so the caller can alert staff."""
    if abs(balance_change) > MAX_AMOUNT or abs(xp_change) > MAX_AMOUNT:
        raise ValidationError("amount_out_of_range",
                              "balance or xp change exceeds MAX_AMOUNT")
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT balance, xp, level, bj_wins, bj_losses FROM users WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    old_balance, old_xp, old_level, old_wins, old_losses = row

    casino_rate_limited = False
    if casino_rate_limit is not None and balance_change > 0:
        window_seconds, max_coins = casino_rate_limit
        now = utc_now()
        window_row = conn.execute(
            "SELECT window_started_at, window_total FROM casino_earnings_window "
            "WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if window_row is None:
            window_total, window_start = 0, now
        else:
            window_started_at = parse_stored(window_row[0])
            window_total = window_row[1]
            if (now - window_started_at).total_seconds() > window_seconds:
                # Expired: start a fresh window rather than sliding it forward
                # on every call, or the window would never actually elapse.
                window_total, window_start = 0, now
            else:
                window_start = window_started_at
        remaining = max(0, max_coins - window_total)
        if balance_change > remaining:
            casino_rate_limited = True
            balance_change = remaining
        conn.execute(
            """
            INSERT INTO casino_earnings_window (user_id, window_started_at, window_total)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                window_started_at = excluded.window_started_at,
                window_total = excluded.window_total
            """,
            (user_id, window_start.isoformat(), window_total + balance_change),
        )

    new_balance = old_balance + balance_change
    if clamp_balance:
        new_balance = max(0, new_balance)
    elif new_balance < 0:
        return None

    new_xp = max(0, old_xp + xp_change)
    new_level = level_for_xp(new_xp)
    new_wins = old_wins + win_inc
    new_losses = old_losses + loss_inc
    conn.execute(
        """
        UPDATE users
        SET balance = %s, xp = %s, level = %s, bj_wins = %s, bj_losses = %s
        WHERE user_id = %s
        """,
        (new_balance, new_xp, new_level, new_wins, new_losses, user_id),
    )
    return {
        "stats": (new_balance, new_xp, new_level, new_wins, new_losses),
        "old_level": old_level,
        # Reported rather than reconstructed by the caller: XP is clamped at
        # zero, so subtracting the delta back off the new total is wrong exactly
        # when a negative change hit the floor.
        "old_xp": old_xp,
        "xp_changed": new_xp != old_xp,
        "casino_rate_limited": casino_rate_limited,
        # The actual, possibly-clamped amount this call credited -- a caller
        # that also reports its own locally-computed payout figure must use
        # this instead once a clamp happened, or the embed and the balance
        # tell two different stories about the same spin.
        "applied_credit": balance_change,
    }


def apply_user_delta(user_id: int, balance_change: int = 0, xp_change: int = 0,
                     win_inc: int = 0, loss_inc: int = 0):
    """Atomically applies a user's balance, XP, and game-stat changes."""
    try:
        with get_connection() as conn:
            result = _apply_stats_locked(
                conn, user_id, balance_change, xp_change, win_inc, loss_inc
            )
            conn.commit()
            return result
    except EngineError as exc:
        db_logger.exception("Atomic user update failed (user=%s)", user_id)
        raise DatabaseOperationError("atomic user update failed") from exc


def set_user_experience(user_id: int, new_xp: int):
    """Set a member's XP outright, and with it the level it works out to.

    The correction tool `/setlevel` and `/givexp` need, and the reason it writes
    XP rather than a level: the level column is recomputed from XP on every
    write, so setting it alone would survive until the member's next message and
    no longer.

    An absolute write rather than a read-modify-write of a value the caller
    computed, so it does not reintroduce the split-update pattern the hardening
    baseline forbids — the caller says what the XP should *be*, and the row is
    read and written in one write transaction.

    Returns the shape `_apply_stats_locked` returns, so the Discord-side helpers
    that follow a level change need no second case for it.
    """
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            row = conn.execute(
                "SELECT balance, xp, level, bj_wins, bj_losses FROM users "
                "WHERE user_id = %s", (user_id,),
            ).fetchone()
            balance, old_xp, old_level, wins, losses = row
            target_xp = max(0, int(new_xp))
            target_level = level_for_xp(target_xp)
            conn.execute(
                "UPDATE users SET xp = %s, level = %s WHERE user_id = %s",
                (target_xp, target_level, user_id),
            )
            conn.commit()
            return {
                "stats": (balance, target_xp, target_level, wins, losses),
                "old_level": old_level,
                "old_xp": old_xp,
                "xp_changed": target_xp != old_xp,
            }
    except EngineError as exc:
        db_logger.exception("Setting a member's experience failed (user=%s)", user_id)
        raise DatabaseOperationError("setting experience failed") from exc


def reserve_wager(user_id: int, amount: int):
    """Atomically deducts a wager, returning the remaining balance or None."""
    if amount <= 0:
        return None
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            changed = conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (amount, user_id, amount),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            conn.commit()
            return balance
    except EngineError as exc:
        db_logger.exception("Wager reservation failed (user=%s)", user_id)
        raise DatabaseOperationError("wager reservation failed") from exc


def settle_wager(user_id: int, credit: int = 0, win_inc: int = 0,
                  loss_inc: int = 0):
    """Credits a previously reserved wager exactly once at the caller level."""
    if credit < 0:
        raise ValueError("wager credit cannot be negative")
    return apply_user_delta(user_id, credit, 0, win_inc, loss_inc)


def begin_interactive_wager(wager_id: str, guild_id: int, user_id: int,
                            game_key: str, stake: int, consume_item: str = None):
    """Reserve funds and persist a recoverable interactive wager atomically.

    Returns ``{"balance", "consumed"}`` or None when the stake could not be
    reserved. ``consume_item`` spends one of a guild-local consumable **in this
    same transaction** and reports whether one was there — which is the only
    place a modifier for an interactive game can safely be spent. Blackjack and
    mines both decide their layout in the cog, after this call has already
    committed, so consuming the item separately would leave a spent item with no
    wager, or a wager with a free item, whenever one of the two writes failed.
    """
    if not wager_id or not guild_id or stake <= 0 or stake > MAX_AMOUNT:
        return None
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            changed = conn.execute(
                "UPDATE users SET balance = balance - %s "
                "WHERE user_id = %s AND balance >= %s",
                (stake, user_id, stake),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            conn.execute(
                "INSERT INTO casino_wagers "
                "(wager_id, guild_id, user_id, game_key, stake, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 'pending', %s)",
                (wager_id, guild_id, user_id, game_key, stake, created_at),
            )
            # After the debit, so an unaffordable wager never spends an item.
            consumed = bool(consume_item and _consume_inventory_item(
                conn, guild_id, user_id, consume_item, created_at))
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            conn.commit()
            return {"balance": balance, "consumed": consumed}
    except IntegrityError as exc:
        db_logger.warning("Interactive wager identity collision (wager_id=%s)", wager_id)
        raise DatabaseOperationError("interactive wager identity collision") from exc
    except EngineError as exc:
        db_logger.exception("Interactive wager reservation failed (wager_id=%s)", wager_id)
        raise DatabaseOperationError("interactive wager reservation failed") from exc


def increase_interactive_wager(wager_id: str, user_id: int, amount: int):
    """Atomically reserve an additional stake while the wager is pending."""
    if amount <= 0:
        return None
    try:
        with get_connection() as conn:
            pending = conn.execute(
                "SELECT 1 FROM casino_wagers "
                "WHERE wager_id = %s AND user_id = %s AND status = 'pending'",
                (wager_id, user_id),
            ).fetchone()
            if pending is None:
                conn.rollback()
                return None
            changed = conn.execute(
                "UPDATE users SET balance = balance - %s "
                "WHERE user_id = %s AND balance >= %s",
                (amount, user_id, amount),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            conn.execute(
                "UPDATE casino_wagers SET stake = stake + %s WHERE wager_id = %s",
                (amount, wager_id),
            )
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            conn.commit()
            return balance
    except EngineError as exc:
        db_logger.exception("Interactive wager increase failed (wager_id=%s)", wager_id)
        raise DatabaseOperationError("interactive wager increase failed") from exc


def resolve_interactive_wager(wager_id: str, user_id: int, credit: int = 0,
                              win_inc: int = 0, loss_inc: int = 0,
                              outcome: str = "settled",
                              casino_rate_limit: tuple[int, int] | None = None):
    """Settle a pending wager once and apply its payout in the same transaction."""
    if credit < 0:
        raise ValueError("wager credit cannot be negative")
    resolved_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            changed = conn.execute(
                "UPDATE casino_wagers SET status = 'settled', resolved_at = %s, "
                "resolution_json = %s WHERE wager_id = %s AND user_id = %s "
                "AND status = 'pending'",
                (
                    resolved_at,
                    json.dumps({"outcome": outcome, "credit": credit}),
                    wager_id,
                    user_id,
                ),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            result = _apply_stats_locked(
                conn, user_id, credit, 0, win_inc, loss_inc,
                clamp_balance=False, casino_rate_limit=casino_rate_limit,
            )
            conn.commit()
            return result
    except EngineError as exc:
        db_logger.exception("Interactive wager settlement failed (wager_id=%s)", wager_id)
        raise DatabaseOperationError("interactive wager settlement failed") from exc


def refund_interactive_wager(wager_id: str, reason: str = "delivery_failed"):
    """Refund one pending wager exactly once."""
    resolved_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT user_id, stake FROM casino_wagers "
                "WHERE wager_id = %s AND status = 'pending'", (wager_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                (row[1], row[0]),
            )
            conn.execute(
                "UPDATE casino_wagers SET status = 'refunded', resolved_at = %s, "
                "resolution_json = %s WHERE wager_id = %s AND status = 'pending'",
                (resolved_at, json.dumps({"reason": reason}), wager_id),
            )
            conn.commit()
            return True
    except EngineError as exc:
        db_logger.exception("Interactive wager refund failed (wager_id=%s)", wager_id)
        raise DatabaseOperationError("interactive wager refund failed") from exc


def refund_pending_wagers(reason: str = "process_restart"):
    """Refund all orphaned pending wagers at startup and return a summary."""
    resolved_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT wager_id, user_id, stake FROM casino_wagers "
                "WHERE status = 'pending'"
            ).fetchall()
            for wager_id, user_id, stake in rows:
                conn.execute(
                    "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                    (stake, user_id),
                )
                conn.execute(
                    "UPDATE casino_wagers SET status = 'refunded', resolved_at = %s, "
                    "resolution_json = %s WHERE wager_id = %s AND status = 'pending'",
                    (resolved_at, json.dumps({"reason": reason}), wager_id),
                )
            conn.commit()
            return {"count": len(rows), "amount": sum(row[2] for row in rows)}
    except EngineError as exc:
        db_logger.exception("Pending wager recovery failed")
        raise DatabaseOperationError("pending wager recovery failed") from exc


def claim_periodic_reward(guild_id: int, user_id: int, reward_key: str,
                          amount: int, interval_days: int = 30):
    """Claim and pay a guild-local periodic reward as one transaction."""
    if not guild_id or amount <= 0 or interval_days <= 0:
        return None
    now = datetime.now(timezone.utc)
    try:
        with get_connection() as conn:
            previous = conn.execute(
                "SELECT claimed_at FROM reward_claims "
                "WHERE guild_id = %s AND user_id = %s AND reward_key = %s",
                (guild_id, user_id, reward_key),
            ).fetchone()
            if previous is not None:
                last_claim = parse_stored(previous[0])
                if now - last_claim < timedelta(days=interval_days):
                    conn.rollback()
                    return None
            _ensure_user(conn, user_id)
            result = _apply_stats_locked(conn, user_id, amount, 0, 0, 0)
            conn.execute(
                "INSERT INTO reward_claims (guild_id, user_id, reward_key, claimed_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT(guild_id, user_id, reward_key) "
                "DO UPDATE SET claimed_at = excluded.claimed_at",
                (guild_id, user_id, reward_key, now.isoformat()),
            )
            conn.commit()
            return result
    except EngineError as exc:
        db_logger.exception(
            "Periodic reward claim failed (guild=%s, user=%s, reward=%s)",
            guild_id, user_id, reward_key,
        )
        raise DatabaseOperationError("periodic reward claim failed") from exc


def resolve_instant_wager(user_id: int, stake: int, credit: int = 0,
                          win_inc: int = 0, loss_inc: int = 0,
                          casino_rate_limit: tuple[int, int] | None = None):
    """Reserves and settles a non-interactive wager in one transaction."""
    if stake <= 0 or stake > MAX_AMOUNT or credit < 0:
        return None
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            changed = conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (stake, user_id, stake),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            result = _apply_stats_locked(
                conn, user_id, credit, 0, win_inc, loss_inc,
                clamp_balance=False, casino_rate_limit=casino_rate_limit,
            )
            conn.commit()
            return result
    except EngineError as exc:
        db_logger.exception("Instant wager failed (user=%s)", user_id)
        raise DatabaseOperationError("instant wager failed") from exc


def resolve_dice_wager(guild_id: int, user_id: int, stake: int,
                       first_roll: int, second_roll: int, bot_roll: int,
                       casino_rate_limit: tuple[int, int] | None = None):
    """Settle dice and consume a loaded die only after a valid paid wager."""
    if stake <= 0 or stake > MAX_AMOUNT or any(roll not in range(1, 7)
                         for roll in (first_roll, second_roll, bot_roll)):
        return None
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            if conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (stake, user_id, stake),
            ).rowcount != 1:
                conn.rollback()
                return None
            loaded = conn.execute(
                "SELECT quantity FROM user_inventory WHERE guild_id = %s AND user_id = %s "
                "AND item_key = 'loaded_die' AND quantity > 0",
                (int(guild_id), int(user_id)),
            ).fetchone() is not None
            player_roll = max(first_roll, second_roll) if loaded else first_roll
            if loaded:
                conn.execute(
                    "UPDATE user_inventory SET quantity = quantity - 1, updated_at = %s "
                    "WHERE guild_id = %s AND user_id = %s AND item_key = 'loaded_die'",
                    (datetime.now(timezone.utc).isoformat(), int(guild_id), int(user_id)),
                )
            if player_roll > bot_roll:
                credit, win_inc, loss_inc, outcome = stake * 2, 1, 0, "win"
            elif player_roll < bot_roll:
                credit, win_inc, loss_inc, outcome = 0, 0, 1, "loss"
            else:
                credit, win_inc, loss_inc, outcome = stake, 0, 0, "tie"
            result = _apply_stats_locked(
                conn, user_id, credit, 0, win_inc, loss_inc, clamp_balance=False,
                casino_rate_limit=casino_rate_limit,
            )
            conn.commit()
            # The amount actually credited above the stake, not the
            # theoretically-computed one: a rate-limit clamp must change what
            # the embed reports, or it tells the player a number their balance
            # never received.
            result.update({"outcome": outcome, "player_roll": player_roll,
                           "bot_roll": bot_roll, "loaded_die": loaded,
                           "first_roll": first_roll, "second_roll": second_roll,
                           "net_winnings": result["applied_credit"] - stake})
            return result
    except EngineError as exc:
        db_logger.exception("Dice wager failed (guild=%s, user=%s)", guild_id, user_id)
        raise DatabaseOperationError("dice wager failed") from exc


# Roulette's own arithmetic, here rather than in the cog, because the item that
# changes the outcome has to be consumed in the same transaction that settles it.
ROULETTE_RED_NUMBERS = frozenset(
    {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
)
SLOT_SYMBOLS = ("🍒", "🍋", "🍇", "💎", "7️⃣", "🔔", "🍉", "⭐", "🍊")
SLOT_JACKPOT_SYMBOL = "7️⃣"


def _roulette_spin(rng) -> tuple[int, str]:
    number = rng.randint(0, 36)
    if number == 0:
        return number, "green"
    return number, "red" if number in ROULETTE_RED_NUMBERS else "black"


def _roulette_payout(stake: int, number: int, colour: str,
                     selected_colour: str | None, selected_number: int | None) -> int:
    """What this spin pays, or 0. Green pays 14 to 1 and a straight number 35."""
    if selected_colour is not None and selected_colour == colour:
        return stake * (14 if selected_colour == "green" else 1)
    if selected_number is not None and selected_number == number:
        return stake * 35
    return 0


def _slots_payout(stake: int, reels: tuple[str, str, str]) -> int:
    first, second, third = reels
    if first == second == third:
        return stake * (50 if first == SLOT_JACKPOT_SYMBOL else 10)
    if first == second or second == third or first == third:
        return int(stake * 1.5)
    return 0


def resolve_roulette_wager(guild_id: int, user_id: int, stake: int,
                           selected_colour: str | None,
                           selected_number: int | None, rng=None,
                           casino_rate_limit: tuple[int, int] | None = None):
    """Spin, settle, and consume a loaded die only after a valid paid wager.

    The spin happens *inside* the transaction that debits the stake, which is
    what the loaded die requires: consuming the item from the cog would be a
    second, separately-committed write, so a crash between them leaves either a
    spent item with no wager or a wager with a free item. `resolve_dice_wager` is
    the same shape and the reason this one exists.

    A loaded die spins twice and keeps whichever spin matches the bet — the
    roulette reading of "keeps the higher of two rolls". It is spent whether or
    not the second spin helped, exactly as in dice.
    """
    if stake <= 0 or stake > MAX_AMOUNT or (selected_colour is None and selected_number is None):
        return None
    if selected_colour is not None and selected_colour not in {"red", "black", "green"}:
        return None
    if selected_number is not None and not 0 <= selected_number <= 36:
        return None
    rng = rng or secrets.SystemRandom()
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            if conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (stake, user_id, stake),
            ).rowcount != 1:
                conn.rollback()
                return None
            number, colour = _roulette_spin(rng)
            payout = _roulette_payout(stake, number, colour,
                                      selected_colour, selected_number)
            # Only now, and only when the item is actually there, is a second
            # spin drawn — so a member without one consumes no extra randomness.
            loaded = _consume_inventory_item(conn, guild_id, user_id, "loaded_die")
            second_number = second_colour = None
            if loaded:
                second_number, second_colour = _roulette_spin(rng)
                second_payout = _roulette_payout(
                    stake, second_number, second_colour,
                    selected_colour, selected_number)
                if second_payout > payout:
                    number, colour, payout = second_number, second_colour, second_payout
            won = payout > 0
            result = _apply_stats_locked(
                conn, user_id, stake + payout if won else 0, 0,
                1 if won else 0, 0 if won else 1, clamp_balance=False,
                casino_rate_limit=casino_rate_limit,
            )
            conn.commit()
            # Reported payout is the amount actually credited above the stake,
            # not the theoretical one: a rate-limit clamp must change what the
            # embed says, or it names a number the balance never received.
            result.update({"outcome": "win" if won else "loss", "number": number,
                           "colour": colour,
                           "payout": result["applied_credit"] - stake if won else 0,
                           "loaded_die": loaded,
                           "second_number": second_number,
                           "second_colour": second_colour})
            return result
    except EngineError as exc:
        db_logger.exception("Roulette wager failed (guild=%s, user=%s)",
                            guild_id, user_id)
        raise DatabaseOperationError("roulette wager failed") from exc


# The wheel's segments, as (payout multiplier in hundredths, weight). The
# weights are chosen so the expected return is exactly 98% — the same 2% edge
# `/mines` derives — rather than a table somebody eyeballed:
#
#   0.00*54 + 1.00*19 + 1.50*12 + 2.00*7 + 3.00*4 + 5.00*3 + 20.00*1 = 98
#
# A change to any row has to keep that sum at 98 per 100 weight, which
# `tests/test_casino_items.py` asserts rather than trusting.
WHEEL_SEGMENTS = ((0, 54), (100, 19), (150, 12), (200, 7), (300, 4), (500, 3),
                  (2000, 1))
WHEEL_TOTAL_WEIGHT = sum(weight for _, weight in WHEEL_SEGMENTS)


def guild_wheel_segments(guild_id: int) -> tuple:
    """This guild's `/wheel` segments, as `((multiplier, weight), ...)`.

    Read through the settings cache like every other setting, so a dashboard
    change is visible immediately and a spin does not open a connection per
    round. Falls back to the shipped table on any failure or malformed shape:
    a settlement transaction must never raise because a cached read came back
    strange, and reverting to the shipped odds is a game that still works.
    """
    try:
        from core import settings_cache

        value = settings_cache.setting(int(guild_id), "casino_wheel_segments")
        if not isinstance(value, dict) or not value:
            return WHEEL_SEGMENTS
        segments = tuple((int(m), int(w)) for m, w in value.items())
    except Exception:
        db_logger.exception(
            "Could not read wheel segments (guild=%s); using the shipped table.",
            guild_id)
        return WHEEL_SEGMENTS
    return segments


def _wheel_spin(rng, segments, total_weight) -> int:
    """One segment's multiplier in hundredths."""
    point = rng.randrange(total_weight)
    for multiplier, weight in segments:
        point -= weight
        if point < 0:
            return multiplier
    return segments[-1][0]


def resolve_wheel_wager(guild_id: int, user_id: int, stake: int, rng=None,
                        casino_rate_limit: tuple[int, int] | None = None):
    """Spin the wheel, settle, and consume a lucky charm if one is held.

    Same shape as `resolve_slots_wager`, and for the same reason: the spin has to
    happen inside the transaction that debits the stake, or the item that changes
    it would be a second, separately-committed write.
    """
    if stake <= 0 or stake > MAX_AMOUNT:
        return None
    rng = rng or secrets.SystemRandom()
    segments = guild_wheel_segments(guild_id)
    total_weight = sum(weight for _, weight in segments)
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            if conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (stake, user_id, stake),
            ).rowcount != 1:
                conn.rollback()
                return None
            multiplier = _wheel_spin(rng, segments, total_weight)
            charm = _consume_inventory_item(conn, guild_id, user_id, "lucky_charm")
            second = None
            if charm:
                second = _wheel_spin(rng, segments, total_weight)
                multiplier = max(multiplier, second)
            payout = stake * multiplier // 100
            won = payout > 0
            result = _apply_stats_locked(
                conn, user_id, payout, 0, 1 if won else 0, 0 if won else 1,
                clamp_balance=False, casino_rate_limit=casino_rate_limit,
            )
            conn.commit()
            # `payout` here already is the full credited amount (a 100
            # multiplier is break-even, not profit), so a rate-limit clamp
            # replaces it outright rather than being subtracted from it.
            result.update({"outcome": "win" if won else "loss",
                           "multiplier": multiplier,
                           "payout": result["applied_credit"],
                           "lucky_charm": charm, "second_multiplier": second})
            return result
    except EngineError as exc:
        db_logger.exception("Wheel wager failed (guild=%s, user=%s)",
                            guild_id, user_id)
        raise DatabaseOperationError("wheel wager failed") from exc


def resolve_slots_wager(guild_id: int, user_id: int, stake: int, rng=None,
                        casino_rate_limit: tuple[int, int] | None = None):
    """Spin, settle, and consume a lucky charm only after a valid paid wager.

    Same reasoning as roulette. A lucky charm spins a second set of reels and
    keeps whichever pays more, and is spent either way.
    """
    if stake <= 0 or stake > MAX_AMOUNT:
        return None
    rng = rng or secrets.SystemRandom()
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            if conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (stake, user_id, stake),
            ).rowcount != 1:
                conn.rollback()
                return None
            reels = tuple(rng.choice(SLOT_SYMBOLS) for _ in range(3))
            payout = _slots_payout(stake, reels)
            charm = _consume_inventory_item(conn, guild_id, user_id, "lucky_charm")
            second_reels = None
            if charm:
                second_reels = tuple(rng.choice(SLOT_SYMBOLS) for _ in range(3))
                if _slots_payout(stake, second_reels) > payout:
                    reels, payout = second_reels, _slots_payout(stake, second_reels)
            won = payout > 0
            result = _apply_stats_locked(
                conn, user_id, stake + payout if won else 0, 0,
                1 if won else 0, 0 if won else 1, clamp_balance=False,
                casino_rate_limit=casino_rate_limit,
            )
            conn.commit()
            # Reported payout is the amount actually credited above the stake,
            # not the theoretical one, the same correction roulette needs.
            result.update({"outcome": "win" if won else "loss",
                           "reels": list(reels),
                           "payout": result["applied_credit"] - stake if won else 0,
                           "lucky_charm": charm,
                           "second_reels": list(second_reels) if second_reels else None})
            return result
    except EngineError as exc:
        db_logger.exception("Slots wager failed (guild=%s, user=%s)",
                            guild_id, user_id)
        raise DatabaseOperationError("slots wager failed") from exc


def transfer_balance(sender_id: int, recipient_id: int, amount: int,
                     sender_xp: int = 0):
    """Moves funds between two users in one transaction."""
    if amount <= 0 or amount > MAX_AMOUNT or sender_id == recipient_id:
        return None
    try:
        with get_connection() as conn:
            _ensure_user(conn, sender_id)
            _ensure_user(conn, recipient_id)
            sender = conn.execute(
                "SELECT balance, xp, level FROM users WHERE user_id = %s", (sender_id,)
            ).fetchone()
            if sender[0] < amount:
                conn.rollback()
                return None
            new_xp = max(0, sender[1] + sender_xp)
            new_level = level_for_xp(new_xp)
            conn.execute(
                "UPDATE users SET balance = balance - %s, xp = %s, level = %s WHERE user_id = %s",
                (amount, new_xp, new_level, sender_id),
            )
            conn.execute(
                "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                (amount, recipient_id),
            )
            new_balance = sender[0] - amount
            conn.commit()
            return {
                "stats": (new_balance, new_xp, new_level, None, None),
                "old_level": sender[2],
                "xp_changed": new_xp != sender[1],
            }
    except EngineError as exc:
        db_logger.exception("Balance transfer failed (sender=%s)", sender_id)
        raise DatabaseOperationError("balance transfer failed") from exc


def apply_batch_balance(user_ids, amount: int):
    """Applies one balance adjustment to all supplied users atomically."""
    unique_ids = list(dict.fromkeys(int(user_id) for user_id in user_ids))
    if not unique_ids:
        return 0
    try:
        with get_connection() as conn:
            for user_id in unique_ids:
                _apply_stats_locked(conn, user_id, balance_change=amount)
            conn.commit()
            return len(unique_ids)
    except (EngineError, ValueError) as exc:
        db_logger.exception("Batch balance update failed (users=%s)", len(unique_ids))
        raise DatabaseOperationError("batch balance update failed") from exc


def apply_batch_user_deltas(deltas):
    """Apply many independent balance/XP rewards in one write transaction.

    ``deltas`` contains ``(user_id, balance_change, xp_change)`` tuples. The
    returned mapping uses the same result shape as :func:`apply_user_delta`.
    """
    if not deltas:
        return {}
    try:
        with get_connection() as conn:
            results = {}
            for user_id, balance_change, xp_change in deltas:
                results[int(user_id)] = _apply_stats_locked(
                    conn,
                    int(user_id),
                    int(balance_change),
                    int(xp_change),
                )
            conn.commit()
            return results
    except EngineError as exc:
        db_logger.exception("Batch user reward update failed (users=%s)", len(deltas))
        raise DatabaseOperationError("batch user reward update failed") from exc


def claim_timed_reward(user_id: int, cooldown_column: str, timestamp: str,
                       coin_reward: int, xp_reward: int,
                       interval_seconds: int = None, once_per_day: bool = False):
    """Checks a cooldown and grants its reward in one write transaction."""
    if cooldown_column not in VALID_COOLDOWN_COLUMNS:
        raise ValueError(f"invalid cooldown column: {cooldown_column}")
    now = parse_stored(timestamp)
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id, timestamp)
            last_value = conn.execute(
                f"SELECT {sql_identifier(cooldown_column)} FROM users WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            if last_value:
                last = parse_stored(last_value)
                blocked = (
                    # "Today" is the host's calendar day, not UTC's: the gate keeps
                    # the midnight it always had while the interval arithmetic
                    # below stops caring about the clock change.
                    once_per_day and local_date(last) == local_date(now)
                ) or (
                    interval_seconds is not None
                    and (now - last).total_seconds() < interval_seconds
                )
                if blocked:
                    conn.rollback()
                    return {"claimed": False, "last_claim": last_value}

            result = _apply_stats_locked(conn, user_id, coin_reward, xp_reward)
            conn.execute(
                f"UPDATE users SET {sql_identifier(cooldown_column)} = %s WHERE user_id = %s",
                (timestamp, user_id),
            )
            conn.commit()
            result.update({"claimed": True, "last_claim": last_value})
            return result
    except (EngineError, ValueError) as exc:
        db_logger.exception("Timed reward failed (user=%s, cooldown=%s)", user_id, cooldown_column)
        raise DatabaseOperationError("timed reward failed") from exc


def _consume_inventory_item(conn, guild_id: int, user_id: int, item_key: str,
                            timestamp: str = None) -> bool:
    """Spend one of a guild-local consumable, reporting whether one was there.

    Takes the caller's connection rather than opening its own, so it commits with
    the action that spent it — the property that makes "consumed only by a valid
    paid wager" structurally true rather than a convention someone has to
    remember. The decrement is conditional on `quantity > 0` and the rowcount is
    the answer, so two concurrent settlements cannot spend one item.
    """
    return conn.execute(
        "UPDATE user_inventory SET quantity = quantity - 1, updated_at = %s "
        "WHERE guild_id = %s AND user_id = %s AND item_key = %s AND quantity > 0",
        (timestamp or datetime.now(timezone.utc).isoformat(),
         int(guild_id), int(user_id), item_key),
    ).rowcount == 1


def _consume_streak_freeze(conn, guild_id: int, user_id: int,
                          timestamp: str) -> bool:
    """Spend one streak freeze from this guild's inventory, if there is one.

    Takes the caller's connection rather than opening its own, so it commits
    with the claim that spent it. Decrements conditionally on `quantity > 0`
    and reports whether the row actually moved, so two claims cannot spend one
    freeze.
    """
    return conn.execute(
        "UPDATE user_inventory SET quantity = quantity - 1, updated_at = %s "
        "WHERE guild_id = %s AND user_id = %s AND item_key = 'streak_freeze' "
        "AND quantity > 0",
        (timestamp, int(guild_id), int(user_id)),
    ).rowcount == 1


def claim_everydle_reward(user_id: int, cooldown_column: str, timestamp: str,
                          base_coin: int, xp_reward: int,
                          guild_id: int = None):
    """Atomically grants one daily Everydle reward and updates its streak.

    A `streak_freeze` in the member's guild inventory buys one extra forgiven
    day, spent here rather than by a scheduled job: the claim already knows how
    many days it has been, so the freeze is applied retroactively at the moment
    the gap would have reset the streak. It is consumed on the same connection
    and in the same transaction as the claim, because a crash between the two
    would either hand out a free freeze or charge for one that did nothing.

    `guild_id` is optional because inventory is guild-local and the streak is
    not. Without it there is no inventory to look in, so the streak resets the
    way it always did.
    """
    if cooldown_column not in VALID_COOLDOWN_COLUMNS:
        raise ValueError(f"invalid cooldown column: {cooldown_column}")
    now = parse_stored(timestamp)
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id, timestamp)
            row = conn.execute(
                f"SELECT {sql_identifier(cooldown_column)}, streak_count, last_streak_update "
                "FROM users WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            last_claim, streak_count, last_streak_update = row
            if last_claim and local_date(parse_stored(last_claim)) == local_date(now):
                conn.rollback()
                return {"claimed": False, "last_claim": last_claim}

            streak_count = streak_count or 0
            froze_streak = False
            if not last_streak_update:
                new_streak = 1
            else:
                day_gap = (local_date(now)
                           - local_date(parse_stored(last_streak_update))).days
                if day_gap == 0:
                    new_streak = streak_count
                elif day_gap in (1, 2):
                    # A single missed day is already forgiven, and always was.
                    new_streak = streak_count + 1
                elif (guild_id is not None
                      and day_gap <= 2 + int(
                          item_catalog.mechanic_value(
                              "streak_freeze",
                              guild_item_values(guild_id)) or 0)
                      and _consume_streak_freeze(conn, guild_id, user_id,
                                                 timestamp)):
                    # A freeze covers its configured number of days beyond the
                    # built-in grace of two, one by default. A longer absence
                    # resets, or the item would be a permanent streak rather
                    # than a bounded forgiveness. The bound is checked *before*
                    # the item is spent, so a gap nothing can cover never
                    # consumes one.
                    new_streak = streak_count + 1
                    froze_streak = True
                else:
                    new_streak = 1
            # `new_streak`, not `streak_count + 1`. The old form paid the
            # streak the member was on *before* this claim, which broke in two
            # directions: a streak reset after a gap still paid the lapsed
            # bonus, and a second Everydle game on the same day — `day_gap == 0`,
            # so `new_streak == streak_count` — paid one step above the streak it
            # was on. The embed showed `new_streak` throughout, so the number a
            # member read and the number they were paid for disagreed.
            effective_streak = min(new_streak, 100)
            reward = int(base_coin * (1.0 + effective_streak / 100.0))
            result = _apply_stats_locked(conn, user_id, reward, xp_reward)
            conn.execute(
                f"""
                UPDATE users
                SET {sql_identifier(cooldown_column)} = %s, streak_count = %s,
                    last_streak_update = %s
                WHERE user_id = %s
                """,
                (timestamp, new_streak, timestamp, user_id),
            )
            conn.commit()
            result.update({"claimed": True, "reward": reward,
                           "streak": new_streak, "froze_streak": froze_streak})
            return result
    except (EngineError, ValueError) as exc:
        db_logger.exception("Everydle reward failed (user=%s, cooldown=%s)", user_id, cooldown_column)
        raise DatabaseOperationError("Everydle reward failed") from exc


def purchase_upgrade(user_id: int, price: int, upgrade: str, value=None):
    """Conditionally debits a user and applies a database-backed shop effect.

    Stackable consumables are not handled here: they are guild-local inventory
    rows and go through ``purchase_inventory_item`` instead, which is the same
    storage the gacha grants them into.
    """
    if price < 0 or upgrade not in {"vault", "bodyguard", "debit"}:
        raise ValueError("invalid purchase")
    try:
        with get_connection() as conn:
            _ensure_user(conn, user_id)
            if upgrade == "vault":
                current = conn.execute(
                    "SELECT protected_reserve FROM users WHERE user_id = %s", (user_id,)
                ).fetchone()[0]
                if current >= int(value):
                    conn.rollback()
                    return {"purchased": False, "reason": "already_owned"}

            changed = conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (price, user_id, price),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return {"purchased": False, "reason": "insufficient_funds"}

            if upgrade == "vault":
                conn.execute(
                    "UPDATE users SET protected_reserve = %s WHERE user_id = %s", (int(value), user_id)
                )
            elif upgrade == "bodyguard":
                conn.execute(
                    "UPDATE users SET rob_defense = 0.7, bodyguard_until = %s WHERE user_id = %s",
                    (value, user_id),
                )
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            conn.commit()
            return {"purchased": True, "balance": balance}
    except EngineError as exc:
        db_logger.exception("Purchase failed (user=%s, upgrade=%s)", user_id, upgrade)
        raise DatabaseOperationError("purchase failed") from exc


def purchase_inventory_item(guild_id: int, user_id: int, price: int,
                            item_key: str, quantity: int = 1) -> dict:
    """Buy a stackable consumable into the same inventory the gacha grants into.

    Identity is shared with Potato Gacha — the row this writes is
    indistinguishable from one a pull produced — while the acquisition rule is
    the shop's: a debit that must succeed before anything is granted. The
    charged price travels back with the result so any compensating refund uses
    what was actually taken.
    """
    if price < 0 or quantity <= 0 or item_key not in item_catalog.INVENTORY_ITEM_KEYS:
        raise ValueError("invalid inventory purchase")
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            _ensure_user(conn, int(user_id), timestamp)
            if conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (price, int(user_id), price),
            ).rowcount != 1:
                conn.rollback()
                return {"purchased": False, "reason": "insufficient_funds"}
            conn.execute(
                "INSERT INTO user_inventory (guild_id, user_id, item_key, quantity, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT(guild_id, user_id, item_key) "
                "DO UPDATE SET quantity = user_inventory.quantity + excluded.quantity, "
                "updated_at = excluded.updated_at",
                (int(guild_id), int(user_id), item_key, int(quantity), timestamp),
            )
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = %s", (int(user_id),)
            ).fetchone()[0]
            conn.commit()
            return {"purchased": True, "balance": balance, "price": price,
                    "item_key": item_key, "quantity": int(quantity)}
    except EngineError as exc:
        db_logger.exception("Inventory purchase failed (guild=%s, user=%s, item=%s)",
                            guild_id, user_id, item_key)
        raise DatabaseOperationError("inventory purchase failed") from exc


def refund_balance(user_id: int, amount: int):
    if amount <= 0:
        return
    apply_user_delta(user_id, balance_change=amount)

# One installation-wide advisory lock key ("Potato"), taken by every write
# transaction. PostgreSQL scopes advisory locks to the database.
_WRITE_LOCK_KEY = 0x506F7461746F
# Read workers, the one writer and the dashboard's Waitress threads, with room.
_POOL_MAX_SIZE = 16


class _FlagDumper(Dumper):
    """A Python bool as the 0 or 1 a FLAG (`smallint`) column holds, which is
    what SQLite stored; the schema has no boolean column for it to mean."""

    oid = psycopg.adapters.types["int2"].oid

    def dump(self, obj):
        return b"1" if obj else b"0"


def _configure_connection(conn):
    conn.adapters.register_dumper(bool, _FlagDumper)
    # Every instant comes back in UTC whatever the server's own zone is.
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


_pool_lock = threading.Lock()
_pool = None
_pool_dsn = None


def _connection_pool() -> ConnectionPool:
    """The process's pool, rebuilt if `DSN` has changed since it opened."""
    global _pool, _pool_dsn
    with _pool_lock:
        if _pool is None or _pool_dsn != DSN:
            if _pool is not None:
                _pool.close()
            _pool = ConnectionPool(
                DSN, min_size=1, max_size=_POOL_MAX_SIZE, timeout=30,
                configure=_configure_connection, open=True, name="potatobot",
            )
            _pool_dsn = DSN
        return _pool


def wait_for_database(timeout: float = 60.0) -> None:
    """Block until PostgreSQL accepts a connection, or raise after `timeout`.

    The database container can come up a few seconds after the bot's. Waiting
    here, with backoff, keeps that ordinary start from becoming a crash loop;
    60 seconds stays well inside systemd's five restarts in 300
    (`RestartLimitTests`), so a database that is really gone still ends in a
    visible failure rather than a silent one.
    """
    deadline = time.monotonic() + timeout
    delay = 0.5
    while True:
        try:
            with psycopg.connect(DSN, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            if time.monotonic() + delay > deadline:
                raise DatabaseOperationError("PostgreSQL did not become reachable") from exc
            db_logger.warning("Waiting for PostgreSQL (retry_in_s=%.1f)", delay)
            time.sleep(delay)
            delay = min(delay * 2, 5.0)


def close_connection_pool():
    """Close the pool during an orderly shutdown, or before pointing `DSN`
    elsewhere for good."""
    global _pool, _pool_dsn
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool, _pool_dsn = None, None


def _begin_write_transaction(conn):
    """Open the transaction and take the database's write lock at once.

    The one place a write transaction is started. Taking the lock at the start,
    not at the first write, is what lets a read-then-write body (a balance
    check, a revision compare) act on what it read: no other writer, in this
    process or another, can commit in between. SQLite spelled it
    ``BEGIN IMMEDIATE``; here it is a transaction-scoped advisory lock, held
    until the commit or rollback, which every write takes, so a dashboard run as
    its own process serializes with the bot exactly as it did on SQLite.
    """
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,))
    except EngineError as exc:
        raise DatabaseOperationError("could not begin the write transaction") from exc


@contextmanager
def get_connection():
    """Lend a pooled PostgreSQL connection for one transaction.

    Read-pool workers do not take the process write lock and get a
    `REPEATABLE READ, READ ONLY` transaction: no write, and one snapshot across
    a multi-statement read, as SQLite's WAL gave. Every other connection --
    serialized writer workers and direct synchronous callers -- holds the
    process write lock and arrives with the write transaction already begun, so
    no accessor starts one itself. Commits on a clean exit, rolls back on an
    exception. Nothing nests a second connection inside an open one; with the
    write lock taken up front, one would wait forever behind its own caller.
    """
    reading = getattr(_DB_EXECUTION, "mode", None) == "read"
    lock = nullcontext() if reading else _DB_WRITE_LOCK
    with lock:
        with _connection_pool().connection() as conn:
            try:
                if reading:
                    conn.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                else:
                    _begin_write_transaction(conn)
                yield conn
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except EngineError:
                    pass  # the connection is gone; the pool replaces it
                raise


# ==========================================
# Guild control-plane and feature persistence
# ==========================================

def register_guild(guild_id: int, display_name: str = None):
    """Create or refresh the installation-local record for a Discord guild."""
    now = utc_now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO guilds (guild_id, display_name, active, first_seen_at, last_seen_at)
            VALUES (%s, %s, 1, %s, %s)
            ON CONFLICT(guild_id) DO UPDATE SET
                display_name = excluded.display_name,
                active = 1,
                last_seen_at = excluded.last_seen_at
            """,
            (int(guild_id), display_name, now, now),
        )


def mark_guild_inactive(guild_id: int):
    """Retain tenant data while marking a guild unavailable to the dashboard."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE guilds SET active = 0, last_seen_at = %s WHERE guild_id = %s",
            (utc_now().isoformat(), int(guild_id)),
        )


def get_active_guild_ids() -> set[int]:
    with get_connection() as conn:
        return {
            row[0]
            for row in conn.execute("SELECT guild_id FROM guilds WHERE active = 1")
        }


def get_schema_version() -> int:
    """The schema version the database is actually at."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def get_active_guilds(guild_ids=None) -> list[dict]:
    """Return safe dashboard metadata for active installation guilds."""
    with get_connection() as conn:
        if guild_ids is None:
            rows = conn.execute(
                "SELECT guild_id, display_name FROM guilds WHERE active = 1 "
                "ORDER BY display_name NULLS FIRST, guild_id"
            ).fetchall()
        else:
            normalized = sorted({int(guild_id) for guild_id in guild_ids})
            if not normalized:
                return []
            placeholders = ",".join("%s" for _ in normalized)
            rows = conn.execute(
                f"SELECT guild_id, display_name FROM guilds WHERE active = 1 "
                f"AND guild_id IN ({placeholders}) "
                "ORDER BY display_name NULLS FIRST, guild_id",
                normalized,
            ).fetchall()
    return [{"id": str(row[0]), "name": row[1] or str(row[0])} for row in rows]


def get_feature_states(guild_id: int) -> dict[str, dict]:
    """Return all registered flags, applying safe defaults for missing rows."""
    from core.settings_registry import FEATURE_DEFINITIONS

    with get_connection() as conn:
        rows = {
            row[0]: (bool(row[1]), row[2])
            for row in conn.execute(
                "SELECT feature_key, enabled, revision FROM feature_flags "
                "WHERE guild_id = %s",
                (int(guild_id),),
            )
        }
    return {
        key: {
            "enabled": rows.get(key, (definition.default, 0))[0],
            "revision": rows.get(key, (definition.default, 0))[1],
            "dependencies": list(definition.dependencies),
            "apply_behavior": definition.apply_behavior.value,
            "locale_key": definition.locale_key,
            "group": definition.group,
            "parent": definition.parent,
            "child_pages": definition.child_pages,
            "value_type": definition.value_type.__name__,
            "sensitive": definition.sensitive,
            "required_discord_permissions": list(
                definition.required_discord_permissions
            ),
        }
        for key, definition in FEATURE_DEFINITIONS.items()
    }


def get_feature_revision(guild_id: int) -> int:
    """Return a lightweight revision marker for feature-cache reconciliation."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(audit_id), 0) FROM settings_audit "
            "WHERE guild_id = %s AND action = 'feature.update'",
            (int(guild_id),),
        ).fetchone()
    return int(row[0]) if row else 0


def is_feature_enabled(guild_id: int, feature_key: str) -> bool:
    """Resolve a guild flag; missing tenant context preserves legacy behavior."""
    from core.settings_registry import validate_feature_key

    definition = validate_feature_key(feature_key)
    if guild_id is None:
        return definition.default
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled FROM feature_flags WHERE guild_id = %s AND feature_key = %s",
            (int(guild_id), feature_key),
        ).fetchone()
    return bool(row[0]) if row else definition.default


def set_feature_state(guild_id: int, feature_key: str, enabled: bool,
                      actor_id: int, expected_revision: int):
    """Update a flag and atomically disable its enabled dependents.

    Enabling remains strict: all dependencies must already be enabled. Disabling
    cascades transitively so the persisted feature graph is never inconsistent.
    """
    from core.settings_registry import FEATURE_DEFINITIONS, validate_feature_state

    guild_id = int(guild_id)
    with get_connection() as conn:
        register = conn.execute(
            "SELECT 1 FROM guilds WHERE guild_id = %s AND active = 1", (guild_id,)
        ).fetchone()
        if not register:
            raise ValueError("guild is not registered and active")
        stored = {
            row[0]: {"enabled": bool(row[1]), "revision": int(row[2])}
            for row in conn.execute(
                "SELECT feature_key, enabled, revision FROM feature_flags "
                "WHERE guild_id = %s", (guild_id,)
            )
        }
        current = {
            key: stored.get(
                key, {"enabled": definition.default, "revision": 0}
            )
            for key, definition in FEATURE_DEFINITIONS.items()
        }
        # `bool(...)`: the dict literals above hold "enabled" (a bool) beside
        # "revision" (an int), so their inferred value type is the join of
        # both rather than the bool this is actually always holding.
        states = {key: bool(item["enabled"]) for key, item in current.items()}
        validate_feature_state(feature_key, enabled, states)
        if current[feature_key]["revision"] != int(expected_revision):
            raise RevisionConflictError("feature revision conflict")

        targets = {feature_key}
        if not enabled:
            pending = [feature_key]
            while pending:
                parent = pending.pop()
                for key, definition in FEATURE_DEFINITIONS.items():
                    if (
                        parent in definition.dependencies
                        and states[key]
                        and key not in targets
                    ):
                        targets.add(key)
                        pending.append(key)

        now = utc_now().isoformat()
        changes = {}
        for key in sorted(targets):
            new_enabled = enabled if key == feature_key else False
            old = current[key]
            if old["enabled"] == new_enabled and key != feature_key:
                continue
            new_revision = old["revision"] + 1
            conn.execute(
                """
                INSERT INTO feature_flags
                    (guild_id, feature_key, enabled, revision, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(guild_id, feature_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    revision = excluded.revision,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                WHERE feature_flags.revision = %s
                """,
                (
                    guild_id, key, int(new_enabled), new_revision,
                    int(actor_id), now, old["revision"],
                ),
            )
            conn.execute(
                """
                INSERT INTO settings_audit
                    (guild_id, actor_id, action, target_key, old_value_json,
                     new_value_json, created_at)
                VALUES (%s, %s, 'feature.update', %s, %s, %s, %s)
                """,
                (
                    guild_id, int(actor_id), key,
                    json.dumps(old["enabled"]), json.dumps(new_enabled), now,
                ),
            )
            changes[key] = {
                "old_enabled": old["enabled"],
                "enabled": new_enabled,
                "revision": new_revision,
            }
    root = changes[feature_key]
    return {
        "enabled": root["enabled"],
        "revision": root["revision"],
        "changes": changes,
    }


def create_realm(name: str, actor_id: int) -> int:
    """Create a host-governed sharing realm and return its local identifier."""
    normalized = " ".join(name.split()) if isinstance(name, str) else ""
    if not 3 <= len(normalized) <= 64:
        raise ValueError("realm name must contain between 3 and 64 characters")
    now = utc_now().isoformat()
    with get_connection() as conn:
        return conn.execute(
            "INSERT INTO realms (name, created_by, status, created_at) "
            "VALUES (%s, %s, 'active', %s) RETURNING realm_id",
            (normalized, int(actor_id), now),
        ).fetchone()[0]


def get_realms() -> list[dict]:
    """Return realm metadata and membership states for host administration."""
    with get_connection() as conn:
        realms = conn.execute(
            "SELECT realm_id, name, status FROM realms ORDER BY name, realm_id"
        ).fetchall()
        memberships = conn.execute(
            "SELECT realm_id, guild_id, status FROM realm_guilds "
            "ORDER BY realm_id, guild_id"
        ).fetchall()
    by_realm: dict[int, list[dict]] = {}
    for realm_id, guild_id, status in memberships:
        by_realm.setdefault(realm_id, []).append(
            {"guild_id": str(guild_id), "status": status}
        )
    return [
        {
            "realm_id": row[0], "name": row[1], "status": row[2],
            "memberships": by_realm.get(row[0], []),
        }
        for row in realms
    ]


def request_realm_membership(realm_id: int, guild_id: int):
    """Create a pending membership; only a later host action can approve it."""
    with get_connection() as conn:
        active_realm = conn.execute(
            "SELECT 1 FROM realms WHERE realm_id = %s AND status = 'active'",
            (int(realm_id),),
        ).fetchone()
        active_guild = conn.execute(
            "SELECT 1 FROM guilds WHERE guild_id = %s AND active = 1",
            (int(guild_id),),
        ).fetchone()
        if not active_realm or not active_guild:
            raise ValueError("realm and guild must both be active")
        conn.execute(
            """
            INSERT INTO realm_guilds (realm_id, guild_id, status)
            VALUES (%s, %s, 'pending')
            ON CONFLICT(realm_id, guild_id) DO UPDATE SET
                status = 'pending', approved_by = NULL, joined_at = NULL
            """,
            (int(realm_id), int(guild_id)),
        )


def approve_realm_membership(realm_id: int, guild_id: int, actor_id: int):
    """Approve a pending membership as an explicit host operation."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE realm_guilds
            SET status = 'approved', approved_by = %s, joined_at = %s
            WHERE realm_id = %s AND guild_id = %s AND status = 'pending'
            """,
            (
                int(actor_id), utc_now().isoformat(),
                int(realm_id), int(guild_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("pending realm membership was not found")


def get_guild_data_scopes(guild_id: int) -> dict[str, dict]:
    """Return all curated categories with guild-local defaults."""
    from core.settings_registry import DataCategory

    with get_connection() as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT category, scope_type, realm_id, revision "
                "FROM guild_data_scopes WHERE guild_id = %s",
                (int(guild_id),),
            )
        }
    return {
        category.value: {
            "scope_type": rows.get(category.value, ("guild", None, 0))[0],
            "realm_id": rows.get(category.value, ("guild", None, 0))[1],
            "revision": rows.get(category.value, ("guild", None, 0))[2],
        }
        for category in DataCategory
    }


def resolve_data_context(guild_id: int, category: str, user_id: int = None):
    """Resolve a guild operation to its isolated, realm, or instance account."""
    from core.settings_registry import DataCategory, DataContext, DataScopeType

    guild_id = int(guild_id)
    category_value = DataCategory(category)
    state = get_guild_data_scopes(guild_id)[category_value.value]
    scope_type = DataScopeType(state["scope_type"])
    if scope_type is DataScopeType.REALM:
        scope_id = int(state["realm_id"])
    elif scope_type is DataScopeType.INSTANCE:
        scope_id = 0
    else:
        scope_id = guild_id

    if user_id is not None and category_value is not DataCategory.MODERATION:
        with get_connection() as conn:
            opted_out = conn.execute(
                "SELECT opted_out FROM user_sharing_preferences "
                "WHERE user_id = %s AND guild_id = %s AND category = %s",
                (int(user_id), guild_id, category_value.value),
            ).fetchone()
        if opted_out and opted_out[0]:
            scope_type = DataScopeType.GUILD
            scope_id = guild_id

    return DataContext(
        origin_guild_id=guild_id,
        category=category_value,
        scope_type=scope_type,
        scope_id=scope_id,
    )


def set_guild_data_scope(guild_id: int, category: str, scope_type: str,
                         realm_id: int, actor_id: int, expected_revision: int):
    """Select a data view without merging or deleting dormant scoped state."""
    from core.settings_registry import DataCategory, DataScopeType

    try:
        category_value = DataCategory(category).value
        scope_value = DataScopeType(scope_type).value
    except ValueError as exc:
        raise ValueError("invalid data category or scope") from exc
    if scope_value == DataScopeType.REALM.value and realm_id is None:
        raise ValueError("realm scope requires a realm identifier")
    if scope_value != DataScopeType.REALM.value and realm_id is not None:
        raise ValueError("realm identifier is valid only for realm scope")

    guild_id = int(guild_id)
    current = get_guild_data_scopes(guild_id)[category_value]
    if current["revision"] != int(expected_revision):
        raise RevisionConflictError("data-scope revision conflict")
    now = utc_now().isoformat()
    with get_connection() as conn:
        if scope_value == DataScopeType.REALM.value:
            membership = conn.execute(
                "SELECT 1 FROM realm_guilds WHERE realm_id = %s AND guild_id = %s "
                "AND status = 'approved'",
                (int(realm_id), guild_id),
            ).fetchone()
            if not membership:
                raise ValueError("guild is not an approved member of the realm")
        new_revision = current["revision"] + 1
        if current["revision"] == 0:
            conn.execute(
                """
                INSERT INTO guild_data_scopes
                    (guild_id, category, scope_type, realm_id, revision,
                     updated_by, updated_at)
                VALUES (%s, %s, %s, %s, 1, %s, %s)
                """,
                (guild_id, category_value, scope_value, realm_id, int(actor_id), now),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE guild_data_scopes
                SET scope_type = %s, realm_id = %s, revision = %s, updated_by = %s,
                    updated_at = %s
                WHERE guild_id = %s AND category = %s AND revision = %s
                """,
                (
                    scope_value, realm_id, new_revision, int(actor_id), now,
                    guild_id, category_value, current["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflictError("data-scope revision conflict")
        conn.execute(
            """
            INSERT INTO settings_audit
                (guild_id, actor_id, action, target_key, old_value_json,
                 new_value_json, created_at)
            VALUES (%s, %s, 'data_scope.update', %s, %s, %s, %s)
            """,
            (
                guild_id, int(actor_id), category_value,
                json.dumps(current, sort_keys=True),
                json.dumps({"scope_type": scope_value, "realm_id": realm_id}), now,
            ),
        )
    return {
        "scope_type": scope_value, "realm_id": realm_id,
        "revision": new_revision,
    }


def set_user_sharing_preference(user_id: int, guild_id: int, category: str,
                                opted_out: bool):
    """Persist a member's local fallback choice for shareable categories."""
    from core.settings_registry import DataCategory

    if category not in {
        DataCategory.ECONOMY.value,
        DataCategory.PROFILE.value,
        DataCategory.GAME_STATS.value,
    }:
        raise ValueError("category does not support member opt-out")
    if not isinstance(opted_out, bool):
        raise ValueError("opted_out must be a boolean")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_sharing_preferences
                (user_id, guild_id, category, opted_out, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(user_id, guild_id, category) DO UPDATE SET
                opted_out = excluded.opted_out, updated_at = excluded.updated_at
            """,
            (
                int(user_id), int(guild_id), category, int(opted_out),
                utc_now().isoformat(),
            ),
        )


def adopt_legacy_database(guild_id: int) -> dict:
    """Seed guild and instance scopes once without changing legacy live rows.

    The copied instance state is dormant until a later, explicit scope cutover.
    This function is safe to retry and also assigns guild provenance to legacy
    guild-owned records whose source predates multi-guild support.
    """
    guild_id = int(guild_id)
    adoption_key = f"legacy-users-v1:{guild_id}"
    now = utc_now().isoformat()
    user_columns = list(USER_COLUMNS)
    select_columns = ", ".join(sql_identifier(c) for c in ["user_id", *user_columns])
    insert_columns = ", ".join(sql_identifier(c) for c in
        ["scope_type", "scope_id", "user_id", *user_columns, "created_at", "updated_at"]
    )
    placeholders = ", ".join("%s" for _ in range(5 + len(user_columns)))

    try:
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT user_count, adopted_at FROM scope_adoptions "
                "WHERE adoption_key = %s",
                (adoption_key,),
            ).fetchone()
            if existing:
                return {
                    "adopted": False,
                    "user_count": existing[0],
                    "adopted_at": existing[1],
                }

            guild = conn.execute(
                "SELECT 1 FROM guilds WHERE guild_id = %s AND active = 1",
                (guild_id,),
            ).fetchone()
            if not guild:
                raise ValueError("legacy adoption requires an active registered guild")

            users = conn.execute(f"SELECT {select_columns} FROM users").fetchall()
            for user in users:
                user_id, *state = user
                for scope_type, scope_id in (("guild", guild_id), ("instance", 0)):
                    conn.execute(
                        f"INSERT INTO scoped_accounts ({insert_columns}) "
                        f"VALUES ({placeholders})",
                        (scope_type, scope_id, user_id, *state, now, now),
                    )
                last_active = state[user_columns.index("last_active")] or now
                conn.execute(
                    """
                    INSERT INTO user_identities (user_id, first_seen_at, last_seen_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at
                    """,
                    (user_id, last_active, last_active),
                )
                conn.execute(
                    """
                    INSERT INTO activity_events
                        (user_id, origin_guild_id, category, event_type,
                         metadata_json, created_at)
                    VALUES (%s, %s, 'migration', 'legacy_snapshot', %s, %s)
                    """,
                    (
                        user_id, guild_id,
                        json.dumps({"schema": 1, "source": "users"}), now,
                    ),
                )

            for table_name in ("tickets", "warnings", "rented_items"):
                conn.execute(
                    f"UPDATE {sql_identifier(table_name)} SET guild_id = %s "
                    "WHERE guild_id IS NULL",
                    (guild_id,),
                )
            conn.execute(
                "INSERT INTO scope_adoptions "
                "(adoption_key, guild_id, user_count, adopted_at) VALUES (%s, %s, %s, %s)",
                (adoption_key, guild_id, len(users), now),
            )
            return {"adopted": True, "user_count": len(users), "adopted_at": now}
    except (EngineError, ValueError) as exc:
        db_logger.exception("Legacy scope adoption failed (guild=%s)", guild_id)
        raise DatabaseOperationError("legacy scope adoption failed") from exc

# ==========================================
# User account reads and writes
# ==========================================

def get_user_balance(user_id: int) -> int:
    """Return the stored balance, or zero when the user does not exist."""
    try:
        # The connection context guarantees rollback and closure on every path.
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0
    except Exception as e:
        db_logger.error(f"Failed to read user balance (User: {user_id}): {e}")
        return 0

# ==========================================
# Temporary voice-room persistence
# ==========================================
#
# Schema 8 scopes these tables per guild. Writes always carry the real guild id;
# reads fall back to the pre-schema-8 rows, which migrated to guild_id 0, so a
# member keeps their saved room until they next change it in a specific guild.

def _upsert_voice_setting(guild_id: int, user_id: int, column: str, value):
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO voice_settings (guild_id, user_id, {sql_identifier(column)}) "
            f"VALUES (%s, %s, %s) ON CONFLICT(guild_id, user_id) "
            f"DO UPDATE SET {sql_identifier(column)}=excluded.{sql_identifier(column)}",
            (int(guild_id), int(user_id), value),
        )

def set_voice_limit(guild_id: int, user_id: int, limit: int):
    try:
        _upsert_voice_setting(guild_id, user_id, "user_limit", limit)
    except Exception as e:
        db_logger.error(f"Failed to save voice-room user limit: {e}")
        raise DatabaseOperationError("voice limit update failed") from e

def set_voice_name(guild_id: int, user_id: int, name: str):
    try:
        _upsert_voice_setting(guild_id, user_id, "channel_name", name)
    except Exception as e:
        db_logger.error(f"Failed to save voice-room name: {e}")
        raise DatabaseOperationError("voice name update failed") from e

def set_voice_bitrate(guild_id: int, user_id: int, bitrate: int):
    try:
        _upsert_voice_setting(guild_id, user_id, "bitrate", bitrate)
    except Exception as e:
        db_logger.error(f"Failed to save voice-room bitrate: {e}")
        raise DatabaseOperationError("voice bitrate update failed") from e

def set_voice_lock(guild_id: int, user_id: int, locked: int):
    try:
        _upsert_voice_setting(guild_id, user_id, "locked", locked)
    except Exception as e:
        db_logger.error(f"Failed to save voice-room lock state: {e}")
        raise DatabaseOperationError("voice lock update failed") from e

def set_voice_permission(guild_id: int, owner_id: int, target_id: int, is_allowed: int):
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO voice_permissions "
                "(guild_id, owner_id, target_id, is_allowed) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT(guild_id, owner_id, target_id) "
                "DO UPDATE SET is_allowed=excluded.is_allowed",
                (int(guild_id), owner_id, target_id, is_allowed),
            )
    except Exception as e:
        db_logger.error(f"Failed to save voice-room permission: {e}")
        raise DatabaseOperationError("voice permission update failed") from e

def get_voice_settings(guild_id: int, user_id: int):
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT channel_name, user_limit, locked, bitrate FROM voice_settings "
                "WHERE user_id = %s AND guild_id IN (%s, 0) "
                "ORDER BY guild_id DESC LIMIT 1",
                (user_id, int(guild_id)),
            )
            return cursor.fetchone()
    except Exception as e:
        db_logger.error(f"Failed to read voice-room settings: {e}")
        return None

def get_voice_permissions(guild_id: int, owner_id: int):
    """Return this guild's saved allow/deny list, or the legacy default if unset."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT target_id, is_allowed FROM voice_permissions "
                "WHERE owner_id = %s AND guild_id = %s",
                (owner_id, int(guild_id)),
            ).fetchall()
            if rows:
                return rows
            return conn.execute(
                "SELECT target_id, is_allowed FROM voice_permissions "
                "WHERE owner_id = %s AND guild_id = 0",
                (owner_id,),
            ).fetchall()
    except Exception as e:
        db_logger.error(f"Failed to read voice-room permissions: {e}")
        return []

def get_active_channel_owner(channel_id: int):
    """Channel snowflakes are globally unique, so this needs no guild predicate."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("SELECT owner_id FROM active_channels WHERE channel_id = %s", (channel_id,))
            result = cursor.fetchone()
            return result[0] if result else None
    except Exception as e:
        db_logger.error(f"Failed to read active voice-room owner: {e}")
        return None

def add_active_channel(guild_id: int, channel_id: int, owner_id: int):
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO active_channels (channel_id, owner_id, guild_id) "
                "VALUES (%s, %s, %s)",
                (channel_id, owner_id, int(guild_id)),
            )
    except Exception as e:
        db_logger.error(f"Failed to register active voice room: {e}")
        raise DatabaseOperationError("active channel insert failed") from e

def remove_active_channel(channel_id: int):
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM active_channels WHERE channel_id = %s", (channel_id,))
    except Exception as e:
        db_logger.error(f"Failed to remove active voice room: {e}")
        raise DatabaseOperationError("active channel delete failed") from e

def update_active_channel_owner(channel_id: int, new_owner_id: int):
    try:
        with get_connection() as conn:
            conn.execute("UPDATE active_channels SET owner_id = %s WHERE channel_id = %s", (new_owner_id, channel_id))
    except Exception as e:
        db_logger.error(f"Failed to transfer voice-room ownership: {e}")
        raise DatabaseOperationError("active channel owner update failed") from e


def add_ticket(channel_id: int, opener_id: int, guild_id: int = None,
               ticket_type: str = None):
    try:
        with get_connection() as conn:
            # A support ticket is a plain insert, so a second one for the same
            # member hits the partial unique index and is refused. Any other
            # ticket replaces a stale row on its channel completely -- what
            # SQLite's INSERT OR REPLACE did by deleting it -- so every column
            # outside the insert is reset here; TicketReplacementTests pins
            # the column set, so a new column cannot be missed.
            replace = "" if ticket_type == "support" else (
                " ON CONFLICT (channel_id) DO UPDATE SET "
                "opener_id = excluded.opener_id, created_at = excluded.created_at, "
                "guild_id = excluded.guild_id, ticket_type = excluded.ticket_type, "
                "claimer_id = NULL"
            )
            conn.execute(
                "INSERT INTO tickets "
                "(channel_id, opener_id, created_at, guild_id, ticket_type) "
                "VALUES (%s, %s, %s, %s, %s)" + replace,
                (
                    channel_id, opener_id, datetime.now(timezone.utc).isoformat(),
                    guild_id, ticket_type,
                ),
            )
    except EngineError as exc:
        db_logger.exception("Ticket registration failed (channel=%s)", channel_id)
        raise DatabaseOperationError("ticket registration failed") from exc


def get_open_support_ticket(guild_id: int, opener_id: int):
    """Return the member's guild-local support ticket channel, if present."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT channel_id FROM tickets WHERE guild_id = %s "
                "AND opener_id = %s AND ticket_type = 'support'",
                (guild_id, opener_id),
            ).fetchone()
            return row[0] if row else None
    except EngineError as exc:
        db_logger.exception(
            "Support ticket lookup failed (guild=%s, opener=%s)",
            guild_id, opener_id,
        )
        raise DatabaseOperationError("support ticket lookup failed") from exc


def set_ticket_claimer(channel_id: int, claimer_id: int) -> None:
    """Record which staff member claimed a ticket.

    Held in the database rather than on the persistent view, which is one shared
    instance serving every ticket, and in memory only, which a restart lost.
    """
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE tickets SET claimer_id = %s WHERE channel_id = %s",
                (int(claimer_id), int(channel_id)),
            )
    except EngineError as exc:
        db_logger.exception("Ticket claim failed (channel=%s)", channel_id)
        raise DatabaseOperationError("ticket claim failed") from exc


def get_ticket_claimer(channel_id: int):
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT claimer_id FROM tickets WHERE channel_id = %s", (int(channel_id),)
            ).fetchone()
            return row[0] if row else None
    except EngineError as exc:
        db_logger.exception("Ticket claimer lookup failed (channel=%s)", channel_id)
        raise DatabaseOperationError("ticket claimer lookup failed") from exc


def get_ticket_opener(channel_id: int):
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT opener_id FROM tickets WHERE channel_id = %s", (channel_id,)
            ).fetchone()
            return row[0] if row else None
    except EngineError as exc:
        db_logger.exception("Ticket lookup failed (channel=%s)", channel_id)
        raise DatabaseOperationError("ticket lookup failed") from exc


def remove_ticket(channel_id: int):
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM tickets WHERE channel_id = %s", (channel_id,))
    except EngineError as exc:
        db_logger.exception("Ticket removal failed (channel=%s)", channel_id)
        raise DatabaseOperationError("ticket removal failed") from exc

# ==========================================
# Administration and moderation persistence
# ==========================================

def set_rules_read_time(user_id: int, seconds: int):
    """Store how many seconds the member took to accept the rules."""
    try:
        with get_connection() as conn:
            conn.execute("UPDATE users SET rules_read_time = %s WHERE user_id = %s", (seconds, user_id))
    except Exception as e: 
        db_logger.error(f"Failed to save rules acceptance time: {e}")
        raise DatabaseOperationError("rules read time update failed") from e

def user_exists(user_id: int) -> bool:
    """Return whether an account row exists for the user."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        db_logger.error(f"Failed to check whether user exists: {e}")
        return False

def reset_user_cooldowns(user_id: int):
    """Reset every business cooldown and daily streak for test administration."""
    try:
        with get_connection() as conn:
            conn.execute("""
                UPDATE users 
                SET last_daily = NULL, 
                    last_job = NULL, 
                    last_rob = NULL, 
                    last_loldle_easy = NULL,
                    last_loldle_medium = NULL,
                    last_loldle_hard = NULL,
                    last_valdle = NULL,
                    last_genshindle = NULL,
                    last_dbdle_killer = NULL,
                    last_dbdle_survivor = NULL,
                    last_dbdle_perk = NULL,
                    bodyguard_until = NULL,
                    last_streak_update = NULL,
                    streak_count = 0
                WHERE user_id = %s
            """, (user_id,))
    except Exception as e: 
        db_logger.error(f"Failed to reset user cooldowns: {e}")
        raise DatabaseOperationError("cooldown reset failed") from e

def add_rented_item(item_type: str, item_id: str, expires_at: str,
                    guild_id: int = None):
    """Register a rented Discord asset and its expiry timestamp."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO rented_items "
                "(item_type, discord_item_id, expires_at, guild_id) VALUES (%s, %s, %s, %s)",
                (item_type, item_id, expires_at, guild_id),
            )
    except Exception as e: 
        db_logger.error(f"Failed to register rented item: {e}")
        raise DatabaseOperationError("rental insert failed") from e

# ==========================================
# Moderation records
# ==========================================

def _warn_tag_filter(tag: str | None) -> tuple[str, tuple]:
    """SQL fragment restricting a warning query to one tag.

    The default tag absorbs a NULL: every row written before schema 10 has no
    tag and has always effectively been a general warning, so counting the
    default tag has to include them or an upgrade would silently reset every
    member's history to zero.
    """
    if tag is None:
        return "", ()
    from core.settings_registry import WARN_DEFAULT_TAG
    if tag == WARN_DEFAULT_TAG:
        return " AND (tag = %s OR tag IS NULL)", (tag,)
    return " AND tag = %s", (tag,)


def add_warning(user_id: int, mod_id: int, reason: str, date: str,
                guild_id: int = None, tag: str = None):
    """Append a moderation warning to the user's record."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO warnings (user_id, mod_id, reason, date, guild_id, tag) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, mod_id, reason, date, guild_id, tag),
            )
    except Exception as e:
        db_logger.error(f"Failed to save warning: {e}")
        raise DatabaseOperationError("warning insert failed") from e


def record_warning(user_id: int, mod_id: int, reason: str, date: str,
                   guild_id: int, tag: str) -> dict:
    """Insert one warning and report the counts that insert produced.

    Deliberately one transaction rather than an insert followed by a count. The
    threshold this feeds can time out, kick or ban, so it has to be compared
    against the total *including* this warning; two moderators warning the same
    member at once would otherwise both read the pre-insert total and neither
    would see the threshold crossed.
    """
    try:
        with get_connection() as conn:
            warning_id = conn.execute(
                "INSERT INTO warnings (user_id, mod_id, reason, date, guild_id, tag) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (int(user_id), int(mod_id), reason, date, int(guild_id), tag),
            ).fetchone()[0]
            # Guild-scoped only, deliberately. A pre-schema-8 warning carries
            # no provenance, and counting it in every guild meant one
            # installation's history could push a member over a threshold in a
            # guild it never happened in. A threshold can ban, so it fails safe:
            # unattributable evidence counts nowhere. `get_warnings` and
            # `remove_warning` still match NULL, so the row stays visible in
            # /modlogs and stays removable — invisible history would be worse.
            # `repair_warning_provenance` attributes these on any installation
            # where the answer is unambiguous, so this is a no-op there.
            scope = "user_id = %s AND guild_id = %s"
            total = conn.execute(
                f"SELECT COUNT(*) FROM warnings WHERE {scope}",
                (int(user_id), int(guild_id)),
            ).fetchone()[0]
            clause, parameters = _warn_tag_filter(tag)
            tag_count = conn.execute(
                f"SELECT COUNT(*) FROM warnings WHERE {scope}{clause}",
                (int(user_id), int(guild_id), *parameters),
            ).fetchone()[0]
            conn.commit()
            return {"warning_id": warning_id, "total": total,
                    "tag_count": tag_count, "tag": tag}
    except Exception as e:
        db_logger.error(f"Failed to record warning: {e}")
        raise DatabaseOperationError("warning insert failed") from e


def get_warning_count(user_id: int, guild_id: int = None,
                      tag: str = None) -> int:
    """Return the number of warnings stored for a user, optionally by tag."""
    try:
        clause, parameters = _warn_tag_filter(tag)
        with get_connection() as conn:
            if guild_id is None:
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM warnings WHERE user_id = %s{clause}",
                    (user_id, *parameters),
                )
            else:
                # Guild-scoped for the same reason `record_warning` is: this
                # is the count a member is told after an /unwarn, and it has to
                # agree with the number the threshold compares against.
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM warnings WHERE user_id = %s "
                    f"AND guild_id = %s{clause}",
                    (user_id, guild_id, *parameters),
                )
            result = cursor.fetchone()
            return result[0] if result else 0
    except Exception as e:
        db_logger.error(f"Failed to read warning count: {e}")
        return 0

def get_warnings(user_id: int, guild_id: int = None):
    """Return all warnings stored for a user, newest column order first.

    Each row is (id, reason, date, mod_id, tag); a pre-schema-10 row reports a
    NULL tag, which renders as the default rather than as a missing value.
    """
    try:
        with get_connection() as conn:
            if guild_id is None:
                cursor = conn.execute(
                    "SELECT id, reason, date, mod_id, tag FROM warnings "
                    "WHERE user_id = %s",
                    (user_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, reason, date, mod_id, tag FROM warnings "
                    "WHERE user_id = %s AND (guild_id = %s OR guild_id IS NULL)",
                    (user_id, guild_id),
                )
            return cursor.fetchall()
    except Exception as e:
        db_logger.error(f"Failed to read warnings: {e}")
        return []


def remove_warning(warning_id: int, user_id: int, guild_id: int,
                   actor_id: int):
    """Remove one precisely identified guild warning and retain an audit record."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT reason, date, mod_id, guild_id FROM warnings
                WHERE id = %s AND user_id = %s
                  AND (guild_id = %s OR guild_id IS NULL)
                """,
                (int(warning_id), int(user_id), int(guild_id)),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                "DELETE FROM warnings WHERE id = %s AND user_id = %s",
                (int(warning_id), int(user_id)),
            )
            if cursor.rowcount != 1:
                raise DatabaseOperationError("warning removal conflict")
            now = utc_now().isoformat()
            conn.execute(
                """
                INSERT INTO settings_audit
                    (guild_id, actor_id, action, target_key, old_value_json,
                     new_value_json, created_at)
                VALUES (%s, %s, 'warning.delete', %s, %s, NULL, %s)
                """,
                (
                    int(guild_id), int(actor_id), str(int(warning_id)),
                    json.dumps(
                        {
                            "user_id": int(user_id), "reason": row[0],
                            "date": stored_iso(row[1]), "moderator_id": row[2],
                            "source_guild_id": row[3],
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            return {"warning_id": int(warning_id), "reason": row[0]}
    except DatabaseOperationError:
        raise
    except EngineError as exc:
        db_logger.exception(
            "Warning removal failed (warning=%s, guild=%s)", warning_id, guild_id
        )
        raise DatabaseOperationError("warning removal failed") from exc

def get_user_intel(user_id: int):
    """Return moderation-facing profile metadata and activity state."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("SELECT rules_read_time, last_active FROM users WHERE user_id = %s", (user_id,))
            return cursor.fetchone()
    except Exception as e:
        db_logger.error(f"Failed to read moderation profile data: {e}")
        return None
    
# ==========================================
# Profiles and leaderboards
# ==========================================

# A leaderboard cannot be partitioned per guild while there is one wallet per user,
# so it is filtered by guild membership instead: each guild ranks only its own
# members. Member lists can be large, so the id set is chunked well under SQLite's
# variable limit and the per-chunk winners are merged here. Erased members keep an
# anonymous economy row under a negative tombstone id and must never be ranked.
# Each ranking query also leaves out a NULL value: the merge below sorts in
# Python, where None does not compare with an int, and one such row emptied the
# whole board through the caller's error handler.
_MEMBER_ID_CHUNK = 900


def _ranked_members(conn, sql: str, member_ids, limit: int, order_index: int):
    """Run one ranking query per chunk of member ids and merge the results."""
    ids = sorted({int(member_id) for member_id in member_ids if int(member_id) > 0})
    if not ids:
        return []
    merged = []
    for start in range(0, len(ids), _MEMBER_ID_CHUNK):
        chunk = ids[start:start + _MEMBER_ID_CHUNK]
        placeholders = ",".join(["%s"] * len(chunk))
        merged.extend(
            conn.execute(
                sql.format(placeholders=placeholders), (*chunk, limit)
            ).fetchall()
        )
    merged.sort(key=lambda row: row[order_index], reverse=True)
    return merged[:limit]


def get_top_levels(member_ids, limit: int = 10):
    try:
        with get_connection() as conn:
            return _ranked_members(
                conn,
                "SELECT user_id, level, xp FROM users "
                "WHERE user_id IN ({placeholders}) AND xp IS NOT NULL "
                "ORDER BY xp DESC, user_id LIMIT %s",
                member_ids, limit, 2,
            )
    except Exception as e:
        db_logger.error(f"Failed to read level leaderboard: {e}")
        return []

def get_top_balances(member_ids, limit: int = 10):
    try:
        with get_connection() as conn:
            return _ranked_members(
                conn,
                "SELECT user_id, balance, level FROM users "
                "WHERE user_id IN ({placeholders}) AND balance IS NOT NULL "
                "ORDER BY balance DESC, user_id LIMIT %s",
                member_ids, limit, 1,
            )
    except Exception as e:
        db_logger.error(f"Failed to read balance leaderboard: {e}")
        return []

def get_top_streaks(member_ids, limit: int = 10):
    try:
        with get_connection() as conn:
            # Only positive streaks belong on the public leaderboard.
            return _ranked_members(
                conn,
                "SELECT user_id, streak_count FROM users "
                "WHERE user_id IN ({placeholders}) AND streak_count > 0 "
                "ORDER BY streak_count DESC, user_id LIMIT %s",
                member_ids, limit, 1,
            )
    except Exception as e:
        db_logger.error(f"Failed to read streak leaderboard: {e}")
        return []

def get_top_gacha_luck(guild_id: int, member_ids, banner_key: str = None,
                        limit: int = 10, minimum_five_stars: int = 3):
    """Ranked by best average luck -- lowest average pulls-per-5-star on one
    banner, not the live pity counter -- so this is a different query shape
    from `_ranked_members`: `gacha_pulls` already carries a real `guild_id`,
    where `users` does not, so there is no member-id IN-list to chunk. A
    member still needs `minimum_five_stars` 5-star pulls to qualify, or one
    early lucky pull would top the board on no evidence, and the result is
    still filtered to current guild members so a departed member drops off,
    matching the other three leaderboards."""
    key = banner_key or DEFAULT_GACHA_BANNER_KEY
    ids = {int(member_id) for member_id in member_ids if int(member_id) > 0}
    if not ids:
        return []
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT user_id, AVG(pity_before + 1), COUNT(*) "
                "FROM gacha_pulls WHERE guild_id = %s AND banner_key = %s "
                "AND rarity = 5 GROUP BY user_id HAVING COUNT(*) >= %s "
                "ORDER BY 2 ASC, user_id",
                (guild_id, key, minimum_five_stars),
            ).fetchall()
    except Exception as e:
        db_logger.error(f"Failed to read gacha luck leaderboard: {e}")
        return []
    # AVG over integers is a float on SQLite and a Decimal on PostgreSQL;
    # the caller gets a float from either.
    return [(user_id, float(average), count)
            for user_id, average, count in rows if user_id in ids][:limit]


def get_user_profile(user_id: int):
    try:
        with get_connection() as conn:
            cursor = conn.execute("""
                SELECT level, xp, balance, bj_wins, bj_losses, streak_count,
                       last_streak_update
                FROM users WHERE user_id = %s
            """, (user_id,))
            return cursor.fetchone()
    except Exception as e:
        db_logger.error(f"Failed to read user profile: {e}")
        return None

def get_user_rank(xp: int, member_ids) -> int:
    """Calculate the user's rank by XP among the members of one guild."""
    try:
        ids = sorted({int(member_id) for member_id in member_ids if int(member_id) > 0})
        ahead = 0
        with get_connection() as conn:
            for start in range(0, len(ids), _MEMBER_ID_CHUNK):
                chunk = ids[start:start + _MEMBER_ID_CHUNK]
                placeholders = ",".join(["%s"] * len(chunk))
                ahead += conn.execute(
                    f"SELECT COUNT(*) FROM users WHERE xp > %s "
                    f"AND user_id IN ({placeholders})",
                    (xp, *chunk),
                ).fetchone()[0]
        return ahead + 1
    except Exception as e:
        db_logger.error(f"Failed to calculate leaderboard rank: {e}")
        return 1
    
# ==========================================
# Server activity persistence
# ==========================================

def update_last_active(user_id: int, timestamp: str):
    """Record recent activity and clear the inactivity-warning flag."""
    try:
        with get_connection() as conn:
            conn.execute("UPDATE users SET last_active = %s, inactive_warned = 0 WHERE user_id = %s", (timestamp, user_id))
    except Exception as e:
        db_logger.error(f"Failed to update last-active timestamp: {e}")
        raise DatabaseOperationError("last-active update failed") from e

def get_inactivity_data(user_id: int):
    """Return the user's last-active timestamp and warning state."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("SELECT last_active, inactive_warned FROM users WHERE user_id = %s", (user_id,))
            return cursor.fetchone()
    except Exception as e:
        db_logger.error(f"Failed to read inactivity data: {e}")
        return None

def set_inactive_warned(user_id: int):
    """Mark the user as having received an inactivity warning."""
    try:
        with get_connection() as conn:
            conn.execute("UPDATE users SET inactive_warned = 1 WHERE user_id = %s", (user_id,))
    except Exception as e:
        db_logger.error(f"Failed to set inactivity warning state: {e}")
        raise DatabaseOperationError("inactive warning update failed") from e

def create_new_user(user_id: int, timestamp: str):
    """Create a user row with the normal account defaults."""
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO users (user_id, balance, xp, level, bj_wins, bj_losses, 
                                rob_bonus, rob_defense, vault_protection, last_active, inactive_warned) 
                VALUES (%s, 100, 0, 1, 0, 0, 0.0, 1.0, 0.0, %s, 0)
            """, (user_id, timestamp))
    except Exception as e:
        db_logger.error(f"Failed to create user: {e}")
        raise DatabaseOperationError("user creation failed") from e

# ==========================================
# Shop and economy legacy helpers
# ==========================================

# ``set_user_balance``, ``add_user_balance``, ``buy_vault``, ``buy_bodyguard``,
# ``save_user_stats``, ``get_user_rob_bonus``, ``buy_lockpick`` and
# ``break_lockpick`` were all removed together. Each took a balance the caller
# had already computed and wrote it with no ``balance >= ?`` predicate — the
# split read/modify/write the hardening baseline forbids — and none of them had
# a caller anywhere, including the tests. ``set_user_balance`` in particular was
# the most convenient-looking name in this module and the one function certain
# to silently destroy a concurrent write. Money moves through
# ``_apply_stats_locked`` and the conditional-UPDATE purchase paths.

def get_all_rentals(guild_id: int):
    """Return one guild's rented assets for the expiry cleanup task.

    Legacy rows predate the guild column and carry NULL, so they are returned to
    the guild the caller is cleaning up rather than being attributed by guesswork —
    which is what ``CLAUDE.md`` forbids. On a single-guild installation that is the
    same set as before; on a multi-guild one no guild ever deletes another's asset,
    because ``delete_rental`` is only reached for an asset this guild owns.
    """
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, item_type, discord_item_id, expires_at, guild_id "
                "FROM rented_items r WHERE (r.guild_id = %s OR r.guild_id IS NULL) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM timed_entitlements e WHERE e.guild_id = r.guild_id "
                "AND e.entitlement_key = r.item_type "
                "AND e.discord_item_id = r.discord_item_id)",
                (int(guild_id),),
            )
            return cursor.fetchall()
    except Exception as e:
        db_logger.error(f"Failed to read rented items: {e}")
        return []

def delete_rental(rental_id: int):
    """Delete one expired rental record."""
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM rented_items WHERE id = %s", (rental_id,))
    except Exception as e:
        db_logger.error(f"Failed to delete rental: {e}")
        raise DatabaseOperationError("rental delete failed") from e

# ==========================================
# Casino and business cooldown persistence
# ==========================================

def get_cooldown(user_id: int, col_name: str):
    """Read an allowlisted cooldown timestamp."""
    if col_name not in VALID_COOLDOWN_COLUMNS:
        raise ValueError(f"invalid cooldown column: {col_name}")
    try:
        with get_connection() as conn:
            # The allowlist makes the interpolated identifier safe; values remain parameterized.
            cursor = conn.execute(f"SELECT {sql_identifier(col_name)} FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else None
    except EngineError as exc:
        # Raised, not swallowed: returning None here read as "never played
        # today" to the Everydle gates, so a database error opened a second
        # game. The payout stays guarded by the atomic claim regardless.
        db_logger.exception("Cooldown read failed (column=%s)", col_name)
        raise DatabaseOperationError("cooldown read failed") from exc

def set_cooldown(user_id: int, col_name: str, timestamp: str):
    """Write an allowlisted cooldown timestamp."""
    if col_name not in VALID_COOLDOWN_COLUMNS:
        raise ValueError(f"invalid cooldown column: {col_name}")
    try:
        with get_connection() as conn:
            conn.execute(f"UPDATE users SET {sql_identifier(col_name)} = %s WHERE user_id = %s", (timestamp, user_id))
    except EngineError as exc:
        db_logger.exception("Cooldown update failed (column=%s)", col_name)
        raise DatabaseOperationError("cooldown update failed") from exc


def resolve_robbery(attacker_id: int, victim_id: int, timestamp: str,
                    base_chance: float, victim_passive_defense: float,
                    chance_roll: float, steal_percent: float, guild_id: int = 0):
    """Checks eligibility and resolves all robbery effects in one transaction."""
    now = parse_stored(timestamp)
    try:
        with get_connection() as conn:
            attacker = conn.execute(
                """
                SELECT balance, xp, level, bj_wins, bj_losses, rob_bonus, last_rob,
                    COALESCE((SELECT quantity FROM user_inventory
                        WHERE guild_id = %s AND user_id = users.user_id
                        AND item_key = 'lockpick'), 0),
                    COALESCE((SELECT quantity FROM user_inventory
                        WHERE guild_id = %s AND user_id = users.user_id
                        AND item_key = 'vault_glove'), 0)
                FROM users WHERE user_id = %s
                """,
                (int(guild_id), int(guild_id), attacker_id),
            ).fetchone()
            victim = conn.execute(
                """
                SELECT balance, rob_defense, protected_reserve, bodyguard_until
                FROM users WHERE user_id = %s
                """,
                (victim_id,),
            ).fetchone()
            if not attacker or not victim:
                conn.rollback()
                return {"resolved": False, "reason": "missing_user"}
            if attacker[6]:
                last = parse_stored(attacker[6])
                if (now - last).total_seconds() < 3600:
                    conn.rollback()
                    return {"resolved": False, "reason": "cooldown", "last_claim": attacker[6]}
            if attacker[0] < 5000:
                conn.rollback()
                return {"resolved": False, "reason": "attacker_poor"}
            if victim[0] < 5000:
                conn.rollback()
                return {"resolved": False, "reason": "victim_poor"}

            victim_defense = victim[1]
            if victim[3] and now > parse_stored(victim[3]):
                victim_defense = 1.0
                conn.execute(
                    "UPDATE users SET rob_defense = 1.0, bodyguard_until = NULL WHERE user_id = %s",
                    (victim_id,),
                )
            # attacker[5] is the legacy column-backed lockpick. Nothing writes it
            # any more: both systems now grant the inventory item, and this read
            # only honours members who bought one before that change. It is
            # cleared below on every resolved attempt, so the column drains and
            # this is a finite compatibility path, not a second live mechanic.
            # It was never migrated because rob_bonus has no guild dimension and
            # CLAUDE.md forbids guessing which guild a legacy row belonged to.
            inventory_lockpick = attacker[7] > 0
            inventory_glove = attacker[8] > 0
            # Read rather than written: this was the literal `0.15`, which
            # merely duplicated `ItemDefinition.value` and meant a guild could
            # never change it — and would have made a configurable lockpick
            # silently do nothing.
            lockpick_bonus = item_catalog.mechanic_value(
                "lockpick", guild_item_values(guild_id)) or 0.0
            final_chance = (base_chance + attacker[5]
                            + (lockpick_bonus if inventory_lockpick else 0.0)) * (
                victim_passive_defense * victim_defense
            )
            won = chance_roll < final_chance
            if won:
                protected = min(victim[0], max(0, victim[2]))
                stealable = max(0, victim[0] - protected)
                if inventory_glove:
                    # Was the literal `0.25`, for the same reason.
                    exposure = item_catalog.mechanic_value(
                        "vault_glove", guild_item_values(guild_id)) or 0.0
                    stealable += int(protected * exposure)
                amount = max(1, int(stealable * steal_percent)) if stealable else 0
                attacker_result = _apply_stats_locked(
                    conn, attacker_id, amount, 30, win_inc=1, clamp_balance=False
                )
                # `clamp_balance=False` means `_apply_stats_locked` writes nothing
                # and returns None rather than going negative. Discarding that
                # return would credit the attacker while the victim kept their
                # coins — a mint. Today `amount <= stealable <= victim balance`
                # makes it unreachable, but the arithmetic that guarantees it sits
                # sixty lines away, so the refusal is honoured rather than assumed.
                if _apply_stats_locked(
                        conn, victim_id, -amount, clamp_balance=False) is None:
                    conn.rollback()
                    db_logger.error(
                        "Robbery debit refused; settlement rolled back "
                        "(attacker_id=%s, victim_id=%s, amount=%s)",
                        attacker_id, victim_id, amount,
                    )
                    return {"resolved": False, "reason": "victim_poor"}
            else:
                amount = int(attacker[0] * 0.10)
                attacker_result = _apply_stats_locked(
                    conn, attacker_id, -amount, 5, loss_inc=1, clamp_balance=False
                )
                _apply_stats_locked(conn, victim_id, amount, clamp_balance=False)

            consumed_lockpick = attacker[5] > 0
            conn.execute(
                "UPDATE users SET last_rob = %s, rob_bonus = 0.0 WHERE user_id = %s",
                (timestamp, attacker_id),
            )
            for item_key, present in (("lockpick", inventory_lockpick),
                                      ("vault_glove", inventory_glove)):
                if present:
                    conn.execute(
                        "UPDATE user_inventory SET quantity = quantity - 1, updated_at = %s "
                        "WHERE guild_id = %s AND user_id = %s AND item_key = %s AND quantity > 0",
                        (timestamp, int(guild_id), attacker_id, item_key),
                    )
            conn.commit()
            attacker_result.update({
                "resolved": True,
                "won": won,
                "amount": amount,
                "vault": victim[2],
                "consumed_lockpick": consumed_lockpick,
                "consumed_inventory_lockpick": inventory_lockpick,
                "consumed_glove": inventory_glove,
            })
            return attacker_result
    except (EngineError, ValueError) as exc:
        db_logger.exception("Robbery transaction failed (attacker=%s)", attacker_id)
        raise DatabaseOperationError("robbery transaction failed") from exc

def get_rob_stats(user_id: int):
    """Return all account values required to resolve a robbery."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("SELECT balance, rob_bonus, rob_defense, protected_reserve, bodyguard_until FROM users WHERE user_id = %s", (user_id,))
            return cursor.fetchone()
    except Exception as e:
        db_logger.error(f"Failed to read robbery state: {e}")
        return None

def expire_bodyguard(user_id: int):
    """Clear expired bodyguard protection."""
    try:
        with get_connection() as conn:
            conn.execute("UPDATE users SET rob_defense = 1.0, bodyguard_until = NULL WHERE user_id = %s", (user_id,))
    except Exception as e:
        db_logger.error(f"Failed to expire bodyguard: {e}")
        raise DatabaseOperationError("bodyguard expiry failed") from e

# ==========================================
# Core account helpers
# ==========================================

def get_top_xp_user(member_ids):
    """Return the highest-XP member of one guild.

    This used to take no argument, so every guild computed the same winner and the
    ``No. 1`` role could land on someone who was not even present.
    """
    try:
        with get_connection() as conn:
            leaders = _ranked_members(
                conn,
                "SELECT user_id, xp FROM users WHERE user_id IN ({placeholders}) "
                "AND xp IS NOT NULL ORDER BY xp DESC, user_id LIMIT %s",
                member_ids, 1, 1,
            )
        return leaders[0][0] if leaders else None
    except Exception as e:
        db_logger.error(f"Failed to read top XP user: {e}")
        return None

def get_full_user_data(user_id: int):
    """Return the account fields used by legacy update paths."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("""SELECT balance, xp, level, bj_wins, bj_losses, 
                                  rob_bonus, rob_defense, protected_reserve, bodyguard_until
                                  FROM users WHERE user_id = %s""", (user_id,))
            return cursor.fetchone()
    except Exception as e:
        db_logger.error(f"Failed to read full user data: {e}")
        return None
        

def get_streak_data(user_id: int):
    """Return the user's daily streak state."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("SELECT streak_count, last_streak_update FROM users WHERE user_id = %s", (user_id,))
            return cursor.fetchone()
    except Exception as e:
        db_logger.error(f"Failed to read streak state: {e}")
        return None

def save_streak_data(user_id: int, streak_count: int, timestamp: str):
    """Persist an already-calculated daily streak state."""
    try:
        with get_connection() as conn:
            conn.execute("UPDATE users SET streak_count = %s, last_streak_update = %s WHERE user_id = %s", 
                         (streak_count, timestamp, user_id))
    except Exception as e:
        db_logger.error(f"Failed to save streak state: {e}")
        raise DatabaseOperationError("streak update failed") from e

# ==========================================
# Central configuration tables
# ==========================================

def setup_central_tables():
    """Backward-compatible entry point for initializing the complete schema."""
    initialize_database()

# Read-only configuration accessors.
#
# Since schema 8 these three tables are keyed by (guild_id, key) with guild_id 0
# holding the installation default. Every read therefore matches the guild's own
# row first and falls back to the default, which is what keeps a single-guild
# installation behaving exactly as it did before the column existed.

def get_shop_price(guild_id: int, item_id: str, default: int = 0) -> int:
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT price FROM shop_prices WHERE item_id = %s "
                "AND guild_id IN (%s, 0) ORDER BY guild_id DESC LIMIT 1",
                (item_id, int(guild_id)),
            )
            res = cursor.fetchone()
            return res[0] if res and res[0] >= 0 else default
    except Exception:
        # Logged, unlike every other reader in this module until now. This value
        # renders a dashboard form default, so a swallowed error showed the
        # operator a price of 0 with nothing in the journal to explain it.
        db_logger.exception(
            "Failed to read a shop price (guild_id=%s, item_id=%s)",
            guild_id, item_id,
        )
        return default

def get_shop_prices(guild_id: int) -> dict[str, int]:
    """Return this guild's effective shop prices in one read transaction."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT item_id, price, guild_id FROM shop_prices "
            "WHERE guild_id IN (%s, 0) ORDER BY guild_id ASC",
            (int(guild_id),),
        ).fetchall()
    # Ascending order means a guild override overwrites the default it shadows.
    return {str(item_id): int(price) for item_id, price, _ in rows}

def get_reward(guild_id: int, activity_id: str, def_coin: int = 0, def_xp: int = 0):
    """Return the nonnegative coin and XP rewards for an activity in one guild."""
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT coin_reward, xp_reward FROM rewards WHERE activity_id = %s "
                "AND guild_id IN (%s, 0) ORDER BY guild_id DESC LIMIT 1",
                (activity_id, int(guild_id)),
            )
            res = cursor.fetchone()
            if res and res[0] >= 0 and res[1] >= 0:
                return res
            return (def_coin, def_xp)
    except Exception:
        db_logger.exception(
            "Failed to read an activity reward (guild_id=%s, activity_id=%s)",
            guild_id, activity_id,
        )
        return (def_coin, def_xp)

def get_config_id(guild_id: int, config_key: str) -> int | None:
    """Return a configured Discord snowflake as an integer, or `None` if unset
    or unreadable. Unused: `server_config` has no writer, kept only for the
    uniformity `docs/development.md`'s schema log describes."""
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "SELECT config_value FROM server_config WHERE config_key = %s "
                "AND guild_id IN (%s, 0) ORDER BY guild_id DESC LIMIT 1",
                (config_key, int(guild_id)),
            )
            res = cursor.fetchone()
            return int(res[0]) if res and res[0].isdigit() else None
    except Exception:
        db_logger.exception(
            "Failed to read a configured id (guild_id=%s, config_key=%s)",
            guild_id, config_key,
        )
        return None


# ==========================================
# Typed control plane and gacha (schema 5)
# ==========================================

def get_guild_settings(guild_id: int) -> dict[str, dict]:
    """Every stored setting that applies to this guild, guild and instance both.

    Deliberately merged rather than split into two calls. Every caller wants the
    *effective* stored value for a guild, and a second accessor would have meant
    every one of them remembering which scope a key has — which is the mistake
    schema 11 exists to make impossible. Writes are the half that must know:
    `set_guild_settings` routes by `definition.scope`, so an instance setting
    cannot land in `guild_settings` at all.

    An instance row wins on a key collision, which can only happen for a row
    written before schema 11 moved it; the migration removes those, so it is a
    belt-and-braces ordering rather than a live case.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT setting_key, value_json, revision FROM guild_settings WHERE guild_id = %s",
            (int(guild_id),),
        ).fetchall()
        instance_rows = conn.execute(
            "SELECT setting_key, value_json, revision FROM instance_settings"
        ).fetchall()
    return {key: {"value": json.loads(value), "revision": revision}
            for key, value, revision in [*rows, *instance_rows]}


def get_instance_settings() -> dict[str, dict]:
    """Only the installation-wide rows, for a caller with no guild in hand."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT setting_key, value_json, revision FROM instance_settings"
        ).fetchall()
    return {key: {"value": json.loads(value), "revision": revision}
            for key, value, revision in rows}


def set_guild_settings(guild_id: int, actor_id: int, changes: list[dict]):
    """Apply a typed settings patch atomically with optimistic revisions."""
    from core.settings_registry import SETTING_DEFINITIONS, validate_setting_value
    if not isinstance(changes, list) or not changes:
        raise ValidationError("settings_patch_empty", "settings patch must be a non-empty list")
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            results = {}
            for change in changes:
                if not isinstance(change, dict) or set(change) != {"key", "value", "revision"}:
                    raise ValidationError("settings_patch_entry", "invalid settings patch entry")
                key = change["key"]
                definition = SETTING_DEFINITIONS.get(key)
                if definition is None or definition.sensitive:
                    raise ValidationError("settings_unknown_or_sensitive", "unknown or sensitive setting")
                value = validate_setting_value(definition, change["value"])
                # An instance setting has no guild dimension, so it goes to the
                # table that has none either. The audit row still records the
                # guild the change was made from, because that is who did it.
                from core.settings_registry import SettingScope
                instance = definition.scope is SettingScope.INSTANCE
                table = "instance_settings" if instance else "guild_settings"
                where = ("setting_key = %s" if instance
                         else "guild_id = %s AND setting_key = %s")
                identity = (key,) if instance else (int(guild_id), key)
                existing = conn.execute(
                    f"SELECT value_json, revision FROM {sql_identifier(table)} WHERE {where}",
                    identity
                ).fetchone()
                if existing:
                    if change["revision"] != existing[1]:
                        raise RevisionConflictError("settings revision conflict")
                    revision, old_value = existing[1] + 1, json.loads(existing[0])
                    if key == "shop_hidden_items":
                        _assert_unhide_has_room(conn, guild_id, old_value, value)
                    conn.execute(
                        f"UPDATE {sql_identifier(table)} SET value_json = %s, revision = %s, "
                        f"updated_by = %s, updated_at = %s WHERE {where}",
                        (json.dumps(value), revision, int(actor_id), timestamp,
                         *identity),
                    )
                else:
                    if change["revision"] not in (None, 0):
                        raise RevisionConflictError("settings revision conflict")
                    revision, old_value = 1, None
                    if instance:
                        conn.execute(
                            "INSERT INTO instance_settings "
                            "(setting_key, value_json, revision, updated_by, updated_at) "
                            "VALUES (%s, %s, 1, %s, %s)",
                            (key, json.dumps(value), int(actor_id), timestamp),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO guild_settings "
                            "(guild_id, setting_key, value_json, revision, updated_by, updated_at) "
                            "VALUES (%s, %s, %s, 1, %s, %s)",
                            (int(guild_id), key, json.dumps(value), int(actor_id), timestamp),
                        )
                conn.execute(
                    "INSERT INTO settings_audit "
                    "(guild_id, actor_id, action, target_key, old_value_json, "
                    "new_value_json, created_at) VALUES (%s, %s, 'setting.update', %s, %s, %s, %s)",
                    (int(guild_id), int(actor_id), key,
                     json.dumps(old_value) if old_value is not None else None,
                     json.dumps(value), timestamp),
                )
                results[key] = {"value": value, "revision": revision}
            conn.commit()
            return results
    except DatabaseOperationError:
        raise
    except EngineError as exc:
        db_logger.exception("Guild settings update failed (guild=%s)", guild_id)
        raise DatabaseOperationError("guild settings update failed") from exc


def get_settings_revision() -> int:
    """The highest settings-change audit id, as one installation-wide number.

    Deliberately not per guild. An instance setting's audit row records the
    guild the change was made *from*, so a per-guild revision would let every
    other guild miss an installation-wide change. One number means any settings
    change anywhere makes every guild reload, which costs a few reloads on a
    multi-guild installation and cannot miss one.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(audit_id) FROM settings_audit "
            "WHERE action = 'setting.update'"
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _audit_rows(guild_id: int, limit: int, actions: tuple[str, ...] = ()) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    action_filter = (
        f" AND action IN ({', '.join('%s' for _ in actions)})" if actions else "")
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT audit_id, actor_id, action, target_key, old_value_json, "
            "new_value_json, created_at FROM settings_audit WHERE guild_id = %s"
            + action_filter + " ORDER BY audit_id DESC LIMIT %s",
            (int(guild_id), *actions, limit)
        ).fetchall()
    return [{"audit_id": row[0], "actor_id": str(row[1]), "action": row[2],
             "target_key": row[3], "old_value": json.loads(row[4]) if row[4] else None,
             "new_value": json.loads(row[5]) if row[5] else None,
             "created_at": stored_iso(row[6])}
            for row in rows]


def get_settings_audit(guild_id: int, limit: int = 100) -> list[dict]:
    return _audit_rows(guild_id, limit)


PRIVACY_AUDIT_ACTIONS = ("user.erase", "user.retention_sweep")


def get_privacy_audit(guild_id: int, limit: int = 500) -> list[dict]:
    """Erasure and retention records only, filtered in SQL.

    The dashboard once picked these out of the last 100 rows of the whole feed,
    so an erasure older than 100 setting changes silently left its list.
    """
    return _audit_rows(guild_id, limit, PRIVACY_AUDIT_ACTIONS)


def prune_settings_audit(max_age_days: int) -> int:
    """Delete staff audit rows older than the window; erasure records never.

    Nothing removed these rows before, so the feed held every setting change
    since the installation began, and a removed warning's payload (subject id,
    staff reason) with it. `audit_retention_days` sets the window; 0 keeps
    everything. The rows in `PRIVACY_AUDIT_ACTIONS` are the proof of what an
    erasure deleted and name only a tombstone, so they stay whatever the
    window -- the Privacy Policy promises them "indefinitely, anonymised".
    """
    if int(max_age_days) <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(max_age_days))).isoformat()
    placeholders = ",".join(["%s"] * len(PRIVACY_AUDIT_ACTIONS))
    with get_connection() as conn:
        return conn.execute(
            f"DELETE FROM settings_audit WHERE created_at < %s "
            f"AND action NOT IN ({placeholders})",
            (cutoff, *PRIVACY_AUDIT_ACTIONS),
        ).rowcount


# Long enough that a dashboard process down for a while still sees what it
# missed, and short enough that the table stays a handful of rows. A dashboard
# that missed more than this starts with an empty permission cache anyway.
ACCESS_INVALIDATION_RETENTION_HOURS = 24
ACCESS_INVALIDATION_READ_LIMIT = 500


def record_access_invalidations(rows) -> int:
    """Record `(guild_id, user_id or None)` events and prune old ones."""
    now = utc_now()
    cutoff = (now - timedelta(hours=ACCESS_INVALIDATION_RETENTION_HOURS)).isoformat()
    values = [(int(guild_id), None if user_id is None else int(user_id), now.isoformat())
              for guild_id, user_id in rows]
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM dashboard_access_invalidations "
                         "WHERE created_at < %s", (cutoff,))
            conn.cursor().executemany(
                "INSERT INTO dashboard_access_invalidations "
                "(guild_id, user_id, created_at) VALUES (%s, %s, %s)", values)
            conn.commit()
            return len(values)
    except EngineError as exc:
        db_logger.exception("Dashboard access invalidation write failed")
        raise DatabaseOperationError("access invalidation write failed") from exc


def get_latest_access_invalidation_id() -> int:
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(invalidation_id), 0) "
                               "FROM dashboard_access_invalidations").fetchone()
            return int(row[0])
    except EngineError as exc:
        db_logger.exception("Dashboard access invalidation read failed")
        raise DatabaseOperationError("access invalidation read failed") from exc


def get_access_invalidations_since(after_id: int) -> list[dict]:
    """Rows newer than `after_id`, oldest first, ids as strings for comparison."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT invalidation_id, guild_id, user_id "
                "FROM dashboard_access_invalidations WHERE invalidation_id > %s "
                "ORDER BY invalidation_id LIMIT %s",
                (int(after_id), ACCESS_INVALIDATION_READ_LIMIT)).fetchall()
    except EngineError as exc:
        db_logger.exception("Dashboard access invalidation read failed")
        raise DatabaseOperationError("access invalidation read failed") from exc
    return [{"invalidation_id": row[0], "guild_id": str(row[1]),
             "user_id": None if row[2] is None else str(row[2])} for row in rows]


def _validated_gacha_config(config_value: dict, banner_key: str | None = None) -> dict:
    """Validate and normalise one banner's configuration.

    ``banner_key`` is supplied by the write paths only. The standard banner may
    not carry a featured reward — it is the pool a lost split draws *from*, and
    a rate-up on it would have nothing to fall back to — but that rule is
    enforced on save rather than on load, so a config written by some other
    build can never make a live banner unpullable.
    """
    if not isinstance(config_value, dict):
        raise ValidationError("gacha_config_not_object", "gacha config must be an object")
    value = json.loads(json.dumps(config_value))
    # Defaulted rather than required: every banner stored before schema 14 lacks
    # it, and a missing split is not a misconfiguration.
    value.setdefault("featured_split", DEFAULT_GACHA_CONFIG["featured_split"])
    integer_keys = ("cost", "hard_pity", "soft_pity_start",
                    "soft_pity_multiplier", "four_star_guarantee_interval",
                    "duplicate_percent", "featured_split")
    if any(isinstance(value.get(key), bool) or not isinstance(value.get(key), int)
           for key in integer_keys):
        raise ValidationError("gacha_numbers_not_integers", "gacha numeric settings must be integers")
    if value["cost"] <= 0 or value["hard_pity"] <= 0:
        raise ValidationError("gacha_cost_or_pity_not_positive", "gacha cost and hard pity must be positive")
    if not 0 <= value["soft_pity_start"] < value["hard_pity"]:
        raise ValidationError("gacha_soft_pity_after_hard", "soft pity must start before hard pity")
    if not 1 <= value["soft_pity_multiplier"] <= 20:
        raise ValidationError("gacha_multiplier_range", "soft pity multiplier is out of range")
    if not 1 <= value["four_star_guarantee_interval"] <= 1000:
        raise ValidationError("gacha_guarantee_interval_range", "four-star guarantee interval is out of range")
    if not 0 <= value["duplicate_percent"] <= 100:
        raise ValidationError("gacha_duplicate_percent_range", "duplicate compensation is out of range")
    if not 0 <= value["featured_split"] <= 100:
        raise ValidationError("gacha_featured_split_range",
                              "the featured split is out of range")
    tiers = value.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != {"3", "4", "5"}:
        raise ValidationError("gacha_tiers_invalid", "gacha tiers are invalid")
    if any(isinstance(weight, bool) or not isinstance(weight, int) or weight < 0
           for weight in tiers.values()) or sum(tiers.values()) != 100000:
        raise ValidationError("gacha_tier_total", "gacha tier weights must total exactly 100000")
    # Soft pity multiplies both rare tiers and absorbs the increase from the
    # 3-star pool, so a banner whose expansion cannot fit must be rejected here
    # rather than failing on the first soft-pity pull after it is already live.
    expanded_rare = (tiers["4"] + tiers["5"]) * value["soft_pity_multiplier"]
    if expanded_rare > 100000:
        raise ValidationError("gacha_soft_pity_overflow", "soft-pity weights exceed 100 percent")
    rewards = value.get("rewards")
    if not isinstance(rewards, dict) or set(rewards) != {"3", "4", "5"}:
        raise ValidationError("gacha_rewards_invalid", "gacha rewards are invalid")
    for entries in rewards.values():
        if not isinstance(entries, list) or not entries:
            raise ValidationError("gacha_tier_empty", "each gacha tier needs rewards")
        for entry in entries:
            # `enabled` and `featured` are optional so older stored banners stay
            # valid; absent means enabled and not featured. They are the only
            # fields a row may add.
            if not isinstance(entry, dict) or not (
                {"key", "kind", "amount", "weight"} <= set(entry)
                <= {"key", "kind", "amount", "weight", "enabled", "featured"}
            ):
                raise ValidationError("gacha_reward_fields", "gacha reward has unexpected fields")
            if (not isinstance(entry["kind"], str)
                    or entry["kind"] not in {"coins", "item", "vault", "voucher"}):
                raise ValidationError("gacha_reward_kind", "gacha reward kind is invalid")
            if "enabled" in entry and not isinstance(entry["enabled"], bool):
                raise ValidationError("gacha_reward_enabled", "gacha reward enabled must be boolean")
            if "featured" in entry and not isinstance(entry["featured"], bool):
                raise ValidationError("gacha_reward_featured",
                                      "gacha reward featured must be boolean")
            # A key names a locale entry and is written into every pull row, so
            # an empty or oddly shaped one would persist as unrenderable history.
            if not isinstance(entry["key"], str) or not _GACHA_REWARD_KEY.fullmatch(
                entry["key"]
            ):
                raise ValidationError("gacha_reward_key_invalid",
                                      "gacha reward key is invalid")
            if any(
                isinstance(entry[field], bool) or not isinstance(entry[field], int)
                or entry[field] <= 0 for field in ("amount", "weight")
            ):
                raise ValidationError("gacha_reward_values", "gacha reward values are invalid")
            # A catalog item means the same thing however it was obtained, so a
            # banner may choose whether to award `big_vault` and how often, but
            # not to make it protect a different reserve than the shop sells it
            # for. Banners saved before the keys were shared still carry
            # vault_25000/vault_500000, which are absent from the catalog and so
            # stay valid.
            catalog_item = item_catalog.ITEM_DEFINITIONS.get(entry["key"])
            if (catalog_item is not None
                    and catalog_item.effect is item_catalog.ItemEffect.VAULT
                    and entry["amount"] != catalog_item.value):
                raise ValidationError(
                    "gacha_vault_amount_mismatch",
                    "a catalog vault reward must award its catalog reserve",
                    item=entry["key"], amount=catalog_item.value,
                )
        keys = [entry["key"] for entry in entries]
        if len(keys) != len(set(keys)):
            # Two rows for one reward silently double its odds and make the
            # displayed per-row chance a lie.
            raise ValidationError("gacha_reward_duplicate",
                                  "a gacha tier cannot list one reward twice")
        if not any(reward_is_enabled(entry) for entry in entries):
            # A tier can still be drawn, so it must have something to award.
            raise ValidationError("gacha_tier_all_disabled",
                                  "each gacha tier needs one enabled reward")
    for tier, entries in rewards.items():
        featured = [entry for entry in entries if entry.get("featured")]
        if not featured:
            continue
        if tier == "3":
            # Only the rare tiers split. A 3-star rate-up would be a rate-up on
            # the pool nobody is chasing, and the loss branch has no meaning.
            raise ValidationError("gacha_featured_tier",
                                  "only the 4-star and 5-star tiers may feature a reward")
        if len(featured) > 1:
            # "Guaranteed featured" has to name one reward, or the guarantee is
            # a second lottery rather than a guarantee.
            raise ValidationError("gacha_featured_duplicate",
                                  "a tier may feature at most one reward")
        if not reward_is_enabled(featured[0]):
            raise ValidationError("gacha_featured_disabled",
                                  "a featured reward cannot be disabled")
        if banner_key == DEFAULT_GACHA_BANNER_KEY:
            raise ValidationError("gacha_featured_on_standard",
                                  "the standard banner cannot feature a reward")
    return value


# `/work` outcome tiers. They are mechanics rather than text: the free tier pays
# nothing, the high tier pays the large range, and normal pays the ordinary one.
WORK_TIERS = ("normal", "free", "high")

# The installation's own guild id for a work response, following the convention
# schema 8 established for `active_channels`: a guild's own rows win, and a guild
# with none for a tier falls back to these.
WORK_DEFAULT_GUILD_ID = 0

# Responses a fresh installation ships with, seeded at WORK_DEFAULT_GUILD_ID.
#
# They live here rather than in a locale catalog on purpose. A guild's work
# responses are that guild's own flavour text and every guild speaks one
# language, so a response has no language dimension — which means the shipped
# ones cannot be localized either, and English is the neutral choice. Anything a
# guild writes replaces these for that tier, so nobody is stuck with them.
WORK_DEFAULT_RESPONSES = (
    ("normal", "You walked the neighbour's dog and earned "
               "**{earnings} {coin}**."),
    ("normal", "You spent an hour stacking shelves for "
               "**{earnings} {coin}**."),
    ("normal", "You fixed a rattling bicycle and were paid "
               "**{earnings} {coin}**."),
    ("normal", "You delivered a stack of parcels and made "
               "**{earnings} {coin}**."),
    ("normal", "You sold lemonade on the corner and took in "
               "**{earnings} {coin}**."),
    ("normal", "You washed a few cars and pocketed "
               "**{earnings} {coin}**."),
    ("normal", "You helped at the market stall for "
               "**{earnings} {coin}**."),
    ("normal", "You mowed a very large lawn and earned "
               "**{earnings} {coin}**."),
    ("free", "You volunteered at the animal shelter. Paid entirely in wagging "
             "tails."),
    ("free", "You helped a neighbour carry their shopping upstairs. No charge."),
    ("free", "You spent the afternoon picking up litter in the park. Someone "
             "should give you a medal."),
    ("free", "You talked a friend through a very long problem. Priceless, "
             "apparently."),
    ("high", "You found a briefcase nobody was looking for. Inside: "
             "**{earnings} {coin}**."),
    ("high", "Your questionable startup was acquired. Your cut: "
             "**{earnings} {coin}**."),
    ("high", "You won a bet you should never have taken and collected "
             "**{earnings} {coin}**."),
)
# A response is an embed description, so it is bounded well below Discord's
# 4096-character limit and long enough for a couple of sentences.
WORK_MESSAGE_MAX_LENGTH = 500
# Enough room for a rich response pool per tier without letting one guild's
# table grow unbounded.
WORK_RESPONSES_PER_TIER = 50
# The one substitution a response may use. Substitution is a literal replace
# rather than str.format, so an operator's stray brace cannot raise at pull time
# and no other attribute is reachable from the template.
WORK_EARNINGS_PLACEHOLDER = "{earnings}"
# The currency symbol, substituted the same way and for the same reason. The
# shipped responses carry the token rather than a literal emoji, because a custom
# emoji belongs to one guild and renders as raw text everywhere else.
WORK_COIN_PLACEHOLDER = "{coin}"


def validate_work_tier(tier) -> str:
    if tier not in WORK_TIERS:
        raise ValidationError("work_tier_invalid", "unknown work tier")
    return tier


def validate_work_message(message) -> str:
    if not isinstance(message, str):
        raise ValidationError("work_message_invalid", "work message must be a string")
    message = message.strip()
    if not message or len(message) > WORK_MESSAGE_MAX_LENGTH:
        raise ValidationError("work_message_invalid", "work message is invalid",
                              limit=WORK_MESSAGE_MAX_LENGTH)
    return message


def validate_work_weight(weight) -> int:
    if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 1000000:
        raise ValidationError("work_weight_invalid", "work weight is invalid")
    return weight


def get_work_responses(guild_id: int) -> list[dict]:
    """The `/work` responses that are actually in effect for this guild.

    Resolved **per tier**: a guild that owns rows for a tier gets those, and a
    tier it has never touched falls back to the shipped set at
    WORK_DEFAULT_GUILD_ID. Writing your own "big payday" lines therefore replaces
    that tier only and keeps the other two.

    Both sets used to be returned together, which meant the dashboard showed a
    guild's own lines *and* the shipped ones for the same tier, half of them
    read-only with a badge — a list of things that look configured but are not.
    Returning only what is in effect makes the page a plain list of what `/work`
    will say, and each row's `scope` says whether editing it will copy it into
    this guild first.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT response_id, tier, weight, message, enabled, revision, guild_id "
            "FROM work_responses WHERE guild_id IN (%s, %s) "
            "ORDER BY guild_id DESC, tier, response_id",
            (int(guild_id), WORK_DEFAULT_GUILD_ID),
        ).fetchall()
    guild_id = int(guild_id)
    owned_tiers = {row[1] for row in rows if row[6] == guild_id}
    return [{"response_id": row[0], "tier": row[1], "weight": row[2],
             "message": row[3], "enabled": bool(row[4]), "revision": row[5],
             "scope": ("default" if row[6] == WORK_DEFAULT_GUILD_ID
                       and guild_id != WORK_DEFAULT_GUILD_ID else "guild")}
            for row in rows
            # A shipped row for a tier this guild owns is not in effect, so it is
            # not shown and not drawn from.
            if row[6] == guild_id or row[1] not in owned_tiers
            or guild_id == WORK_DEFAULT_GUILD_ID]


def seed_default_work_responses(conn) -> int:
    """Put the shipped responses in place, once.

    Gated on absence rather than on a schema version, so re-running the migration
    is a no-op and an operator's edits to the defaults are never overwritten.
    """
    existing = conn.execute(
        "SELECT COUNT(*) FROM work_responses WHERE guild_id = %s",
        (WORK_DEFAULT_GUILD_ID,),
    ).fetchone()[0]
    if existing:
        return 0
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.cursor().executemany(
        "INSERT INTO work_responses (guild_id, tier, weight, message, enabled, "
        "revision, updated_at) VALUES (%s, %s, 1, %s, 1, 1, %s)",
        [(WORK_DEFAULT_GUILD_ID, tier, message, timestamp)
         for tier, message in WORK_DEFAULT_RESPONSES],
    )
    return len(WORK_DEFAULT_RESPONSES)


def create_work_response(guild_id: int, actor_id: int, tier: str, message: str,
                         weight: int = 1, enabled: bool = True) -> dict:
    tier = validate_work_tier(tier)
    message = validate_work_message(message)
    weight = validate_work_weight(weight)
    if not isinstance(enabled, bool):
        raise ValidationError("work_enabled_invalid", "work enabled must be boolean")
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM work_responses WHERE guild_id = %s AND tier = %s",
            (int(guild_id), tier),
        ).fetchone()[0]
        if count >= WORK_RESPONSES_PER_TIER:
            conn.rollback()
            raise ValidationError("work_response_limit", "work response limit reached",
                                  limit=WORK_RESPONSES_PER_TIER)
        response_id = conn.execute(
            "INSERT INTO work_responses (guild_id, tier, weight, message, enabled, "
            "revision, updated_by, updated_at) VALUES (%s, %s, %s, %s, %s, 1, %s, %s) "
            "RETURNING response_id",
            (int(guild_id), tier, weight, message, int(enabled),
             int(actor_id), timestamp),
        ).fetchone()[0]
        write_settings_audit(
            conn, int(guild_id), actor_id, "work_response.create",
            f"work.{tier}.{response_id}", None,
            {"tier": tier, "weight": weight, "enabled": enabled},
        )
        conn.commit()
    return {"response_id": response_id, "tier": tier, "weight": weight,
            "message": message, "enabled": enabled, "revision": 1}


def _adopt_for_shipped_response(conn, guild_id: int, actor_id: int,
                                response_id: int) -> int:
    """Resolve a response id the guild may not own yet.

    If it names a shipped row, that row's whole tier is adopted into this guild
    and the id of the guild's copy is returned. If it names one of the guild's
    own rows, or nothing at all, the id comes back unchanged and the caller's
    ordinary guild-scoped lookup decides.

    The tier is read from the shipped row rather than taken from the caller, so a
    request cannot adopt a tier it did not name.
    """
    shipped = conn.execute(
        "SELECT tier FROM work_responses WHERE guild_id = %s AND response_id = %s",
        (WORK_DEFAULT_GUILD_ID, int(response_id)),
    ).fetchone()
    if shipped is None:
        return int(response_id)
    adopted = adopt_work_tier(conn, guild_id, actor_id, shipped[0])
    return adopted.get(int(response_id), int(response_id))


def adopt_work_tier(conn, guild_id: int, actor_id: int, tier: str) -> dict:
    """Copy a tier's shipped responses into this guild, once, on the caller's
    connection.

    The shipped set lives at `WORK_DEFAULT_GUILD_ID` and is protected by the same
    guild filter that isolates one guild from another — so an operator could see
    a default but never edit or delete it, and the page had to render it
    read-only with a badge and offer a separate "copy them all" button. Presented
    that way it read as something broken rather than as a starting point.

    This adopts **one tier**, because `cogs.casino.work_response_text` already
    resolves per tier: adopting `normal` leaves `free` and `high` still answering
    from the shipped set, so a guild that only wants its own big-payday lines
    gets exactly that.

    Returns a map from the shipped `response_id` to the guild's new one, so the
    caller can apply an edit to the copy the operator was actually looking at.
    Idempotent by absence: a tier the guild already owns is left alone.
    """
    tier = validate_work_tier(tier)
    owned = conn.execute(
        "SELECT COUNT(*) FROM work_responses WHERE guild_id = %s AND tier = %s",
        (int(guild_id), tier),
    ).fetchone()[0]
    if owned:
        return {}
    shipped = conn.execute(
        "SELECT response_id, message, weight, enabled FROM work_responses "
        "WHERE guild_id = %s AND tier = %s ORDER BY response_id",
        (WORK_DEFAULT_GUILD_ID, tier),
    ).fetchall()
    timestamp = datetime.now(timezone.utc).isoformat()
    adopted = {}
    for response_id, message, weight, enabled in shipped:
        adopted[int(response_id)] = int(conn.execute(
            "INSERT INTO work_responses (guild_id, tier, message, weight, "
            "enabled, revision, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 1, %s, %s) RETURNING response_id",
            (int(guild_id), tier, message, weight, enabled,
             int(actor_id), timestamp),
        ).fetchone()[0])
    if adopted:
        write_settings_audit(
            conn, guild_id, actor_id, "work_response.adopt_tier", tier,
            None, {"tier": tier, "adopted": len(adopted)},
        )
    return adopted


def update_work_response(guild_id: int, actor_id: int, response_id: int,
                         tier: str, message: str, weight: int, enabled: bool,
                         expected_revision: int) -> dict:
    tier = validate_work_tier(tier)
    message = validate_work_message(message)
    weight = validate_work_weight(weight)
    if not isinstance(enabled, bool):
        raise ValidationError("work_enabled_invalid", "work enabled must be boolean")
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        # Editing a shipped response adopts its whole tier into this guild first
        # and then edits the copy, in this transaction, so a half-adopted tier
        # cannot exist. The operator sees one plain editable list; what actually
        # happens is a copy-on-write per tier.
        resolved = _adopt_for_shipped_response(conn, guild_id, actor_id, response_id)
        adopted = resolved != int(response_id)
        response_id = resolved
        row = conn.execute(
            "SELECT tier, weight, message, enabled, revision FROM work_responses "
            "WHERE guild_id = %s AND response_id = %s",
            (int(guild_id), int(response_id)),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise LookupError("work response not found")
        if adopted:
            # The copy starts at revision 1 whatever the shipped row said, so an
            # edit arriving with the shipped revision must not read as a
            # conflict.
            expected_revision = row[4]
        if int(expected_revision) != row[4]:
            conn.rollback()
            raise RevisionConflictError("work response revision conflict")
        revision = row[4] + 1
        previous = {"tier": row[0], "weight": row[1], "message": row[2],
                    "enabled": bool(row[3])}
        conn.execute(
            "UPDATE work_responses SET tier = %s, weight = %s, message = %s, "
            "enabled = %s, revision = %s, updated_by = %s, updated_at = %s "
            "WHERE guild_id = %s AND response_id = %s",
            (tier, weight, message, int(enabled), revision, int(actor_id),
             timestamp, int(guild_id), int(response_id)),
        )
        write_settings_audit(
            conn, int(guild_id), actor_id, "work_response.update",
            f"work.{tier}.{response_id}", previous,
            {"tier": tier, "weight": weight, "message": message, "enabled": enabled},
        )
        conn.commit()
    return {"response_id": int(response_id), "tier": tier, "weight": weight,
            "message": message, "enabled": enabled, "revision": revision}


def delete_work_response(guild_id: int, actor_id: int, response_id: int,
                         expected_revision: int) -> dict:
    with get_connection() as conn:
        # Deleting a shipped response means "not that line, in this guild", which
        # needs the guild to own the tier first — otherwise the row is not the
        # guild's to remove and the tier keeps resolving to the shipped set,
        # including the line just deleted.
        resolved = _adopt_for_shipped_response(conn, guild_id, actor_id, response_id)
        adopted = resolved != int(response_id)
        response_id = resolved
        row = conn.execute(
            "SELECT tier, weight, message, enabled, revision FROM work_responses "
            "WHERE guild_id = %s AND response_id = %s",
            (int(guild_id), int(response_id)),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise LookupError("work response not found")
        # A copy adopted a moment ago carries a revision the client never saw, so
        # comparing against what it sent would be a conflict every time.
        if adopted:
            expected_revision = row[4]
        if int(expected_revision) != row[4]:
            conn.rollback()
            raise RevisionConflictError("work response revision conflict")
        conn.execute(
            "DELETE FROM work_responses WHERE guild_id = %s AND response_id = %s",
            (int(guild_id), int(response_id)),
        )
        write_settings_audit(
            conn, int(guild_id), actor_id, "work_response.delete",
            f"work.{row[0]}.{response_id}",
            {"tier": row[0], "weight": row[1], "message": row[2],
             "enabled": bool(row[3])}, None,
        )
        conn.commit()
    return {"response_id": int(response_id)}


def validate_gacha_banner_key(banner_key) -> str:
    """Accept a banner key that is safe to store and to address from Discord."""
    if not isinstance(banner_key, str) or not _GACHA_BANNER_KEY.fullmatch(banner_key):
        raise ValidationError("gacha_banner_key_invalid", "banner key is invalid")
    return banner_key


def validate_gacha_banner_name(display_name) -> str:
    """Accept an operator-facing banner name.

    It is shown in a Discord choice label and in the dashboard, so it is length
    bounded and must not be blank; a blank name would render as an empty option.
    """
    if not isinstance(display_name, str):
        raise ValidationError("gacha_banner_name_invalid", "banner name must be a string")
    display_name = display_name.strip()
    if not display_name or len(display_name) > GACHA_BANNER_NAME_MAX_LENGTH:
        raise ValidationError("gacha_banner_name_invalid", "banner name is invalid")
    return display_name


def validate_gacha_banner_schedule(starts_at, ends_at) -> tuple:
    """Accept an optional start/end window, refusing rather than clamping.

    Each bound is independently optional -- `None` means "no bound on that
    side," exactly as it means "no schedule" when both are `None`. A bound
    that is present must be a real timestamp (`datetime.fromisoformat` raises
    on anything else) and is normalized to UTC ISO before being stored, so a
    later string comparison against another UTC ISO timestamp orders
    correctly regardless of what offset the caller sent.
    """
    def _normalize(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError("gacha_banner_schedule_invalid",
                                  "banner schedule bound must be a string or null")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValidationError("gacha_banner_schedule_invalid",
                                  "banner schedule bound is not a valid timestamp")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    starts_at, ends_at = _normalize(starts_at), _normalize(ends_at)
    if starts_at is not None and ends_at is not None and starts_at >= ends_at:
        raise ValidationError("gacha_banner_schedule_order",
                              "banner start must be before its end")
    return starts_at, ends_at


# The scalars a stored config may predate. A reader must fill these in, because
# a banner saved before one existed simply has no key for it — and the dashboard
# builds a number input per scalar, so an absent one renders empty and saves back
# as 0. That is how `featured_split` would have silently become "always lose the
# split" on the first save of every banner that already existed. Defaulted here
# rather than by running `_validated_gacha_config` on read, which can raise and
# would take the whole page down over one unreadable banner.
_GACHA_SCALAR_DEFAULTS = ("featured_split",)


def _gacha_config_for_read(config_value: dict) -> dict:
    if not isinstance(config_value, dict):
        return config_value
    for key in _GACHA_SCALAR_DEFAULTS:
        config_value.setdefault(key, DEFAULT_GACHA_CONFIG[key])
    return config_value


def _banner_pull_status(enabled: bool, starts_at, ends_at, now: str) -> str:
    """Whether a banner is currently pullable, and why not if it is not.

    `enabled` is a manual override ANDed with the schedule, not replaced by
    it: an operator can still force a banner off mid-window, and a
    schedule-less banner (`starts_at`/`ends_at` both `None`) is always
    `"live"` when enabled, exactly as every banner behaved before schema 21.
    The bounds are compared as instants, so the stored text SQLite returns
    and the `datetime` PostgreSQL returns order alike.
    """
    if not enabled:
        return "disabled"
    moment = parse_stored(now)
    if starts_at and moment < parse_stored(starts_at):
        return "scheduled"
    if ends_at and moment >= parse_stored(ends_at):
        return "expired"
    return "live"


def _banner_row_dict(row, now: str) -> dict:
    """Shape one gacha_banners row, defaulting a pre-schema-9 name to its key."""
    enabled = bool(row["enabled"])
    starts_at, ends_at = row["starts_at"], row["ends_at"]
    status = _banner_pull_status(enabled, starts_at, ends_at, now)
    return {
        "banner_key": row["banner_key"],
        "display_name": row["display_name"] or row["banner_key"],
        "enabled": enabled,
        "config": _gacha_config_for_read(json.loads(row["config_json"])),
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "starts_at": starts_at,
        "ends_at": ends_at,
        "status": status,
        "pullable": status == "live",
        "is_default": row["banner_key"] == DEFAULT_GACHA_BANNER_KEY,
    }


def list_gacha_banners(guild_id: int) -> list[dict]:
    """Return every banner a guild has, default first then by name.

    A guild that has never saved one gets the installation default described
    from `DEFAULT_GACHA_CONFIG` rather than an empty list, so the dashboard and
    `/gacha` both have something to show before the first save.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT banner_key, display_name, enabled, config_json, revision, "
            "updated_at, starts_at, ends_at FROM gacha_banners WHERE guild_id = %s",
            (int(guild_id),)
        ).fetchall()
    banners = [_banner_row_dict(row, now) for row in rows]
    if not any(banner["is_default"] for banner in banners):
        banners.append({
            "banner_key": DEFAULT_GACHA_BANNER_KEY,
            "display_name": DEFAULT_GACHA_BANNER_KEY,
            "enabled": True,
            "config": json.loads(json.dumps(DEFAULT_GACHA_CONFIG)),
            "revision": 0,
            "updated_at": None,
            "starts_at": None,
            "ends_at": None,
            "status": "live",
            "pullable": True,
            "is_default": True,
        })
    # The key is the tiebreaker for two banners of one name: the order SQLite's
    # primary-key scan gave them, which PostgreSQL does not promise.
    banners.sort(key=lambda banner: (not banner["is_default"],
                                     banner["display_name"].lower(),
                                     banner["banner_key"]))
    return banners


def get_gacha_banner(guild_id: int, banner_key: str = DEFAULT_GACHA_BANNER_KEY) -> dict:
    with get_connection() as conn:
        row = conn.cursor(row_factory=dict_row).execute(
            "SELECT banner_key, display_name, enabled, config_json, revision, "
            "updated_at, starts_at, ends_at FROM gacha_banners "
            "WHERE guild_id = %s AND banner_key = %s",
            (int(guild_id), banner_key)
        ).fetchone()
    if row is None:
        # Only the installation default is described without a stored row. Any
        # other unknown key would otherwise look like a configured banner.
        if banner_key != DEFAULT_GACHA_BANNER_KEY:
            raise ValidationError("gacha_banner_unknown", "no such banner",
                                  banner=banner_key)
        return {"banner_key": banner_key, "display_name": banner_key,
                "enabled": True, "revision": 0, "updated_at": None,
                "starts_at": None, "ends_at": None, "status": "live",
                "pullable": True, "is_default": True,
                "config": json.loads(json.dumps(DEFAULT_GACHA_CONFIG))}
    return _banner_row_dict(row, datetime.now(timezone.utc).isoformat())


def create_lfg_post(guild_id: int, message_id: int, channel_id: int,
                    host_id: int, needed: int, game_role_id=None,
                    game_text=None) -> None:
    """Record a posted LFG message so its buttons survive a restart.

    Written *after* the message is sent, because the id does not exist before
    then — the same order `record_managed_post` uses. A caller whose insert fails
    is holding a post with dead buttons and must remove it.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO lfg_posts (guild_id, message_id, channel_id, "
                "host_id, game_role_id, game_text, needed, joined_json, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, '[]', %s)",
                (int(guild_id), int(message_id), int(channel_id), int(host_id),
                 int(game_role_id) if game_role_id else None, game_text,
                 max(0, int(needed)), timestamp),
            )
            conn.commit()
    except EngineError as exc:
        db_logger.exception("Could not record an LFG post (guild=%s, message=%s)",
                            guild_id, message_id)
        raise DatabaseOperationError("lfg post insert failed") from exc


def get_lfg_post(guild_id: int, message_id: int):
    """One LFG post, or None when the row is gone.

    None is the ordinary case for a post made before this table existed, and the
    caller answers the click with "this post has expired" rather than failing.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT channel_id, host_id, game_role_id, game_text, needed, "
            "joined_json, created_at FROM lfg_posts "
            "WHERE guild_id = %s AND message_id = %s",
            (int(guild_id), int(message_id)),
        ).fetchone()
    if row is None:
        return None
    try:
        joined = json.loads(row[5])
    except (TypeError, ValueError):
        joined = []
    return {"channel_id": row[0], "host_id": row[1], "game_role_id": row[2],
            "game_text": row[3], "needed": row[4],
            "joined": [int(uid) for uid in joined if isinstance(uid, int)],
            "created_at": row[6]}


def _set_lfg_party(conn, guild_id: int, message_id: int, expected, party):
    """Commit a party list only if the stored one is still what we read.

    A conditional UPDATE rather than a read followed by a write: two people
    pressing Join in the same instant would otherwise both read the same list
    and one of them would vanish. The rowcount is the answer, exactly as
    `advance_minigame` does it.
    """
    return conn.execute(
        "UPDATE lfg_posts SET joined_json = %s "
        "WHERE guild_id = %s AND message_id = %s AND joined_json = %s",
        (json.dumps(party), int(guild_id), int(message_id),
         json.dumps(expected)),
    ).rowcount == 1


def join_lfg_post(guild_id: int, message_id: int, user_id: int):
    """Add somebody to a party, or say why not.

    Returns the post with its new party, or a `{"error": …}` naming the reason.
    Every refusal is checked inside the transaction, so a full party cannot gain
    an extra member between the check and the write.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT host_id, needed, joined_json FROM lfg_posts "
                "WHERE guild_id = %s AND message_id = %s",
                (int(guild_id), int(message_id)),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"error": "gone"}
            host_id, needed, stored = row
            party = [int(uid) for uid in json.loads(stored)]
            if int(user_id) == int(host_id):
                conn.rollback()
                return {"error": "host"}
            if int(user_id) in party:
                conn.rollback()
                return {"error": "already"}
            if needed > 0 and len(party) >= needed:
                conn.rollback()
                return {"error": "full"}
            updated = party + [int(user_id)]
            if not _set_lfg_party(conn, guild_id, message_id, party, updated):
                conn.rollback()
                return {"error": "raced"}
            conn.commit()
            return {"joined": updated, "needed": needed, "host_id": host_id,
                    "full": needed > 0 and len(updated) >= needed}
    except EngineError as exc:
        db_logger.exception("LFG join failed (guild=%s, message=%s)",
                            guild_id, message_id)
        raise DatabaseOperationError("lfg join failed") from exc


def leave_lfg_post(guild_id: int, message_id: int, user_id: int):
    """Remove somebody from a party, or say why not."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT needed, joined_json FROM lfg_posts "
                "WHERE guild_id = %s AND message_id = %s",
                (int(guild_id), int(message_id)),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"error": "gone"}
            needed, stored = row
            party = [int(uid) for uid in json.loads(stored)]
            if int(user_id) not in party:
                conn.rollback()
                return {"error": "absent"}
            updated = [uid for uid in party if uid != int(user_id)]
            if not _set_lfg_party(conn, guild_id, message_id, party, updated):
                conn.rollback()
                return {"error": "raced"}
            conn.commit()
            return {"joined": updated, "needed": needed,
                    "full": needed > 0 and len(updated) >= needed}
    except EngineError as exc:
        db_logger.exception("LFG leave failed (guild=%s, message=%s)",
                            guild_id, message_id)
        raise DatabaseOperationError("lfg leave failed") from exc


def get_expired_lfg_posts(guild_id: int, older_than_hours: int) -> list[dict]:
    """A guild's posts made more than `older_than_hours` ago, oldest first.

    Read by the closing loop, which edits each message and then deletes the
    row. `created_at` is written UTC-aware by `create_lfg_post`, so the
    ISO strings compare in time order.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=max(1, int(older_than_hours)))).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT message_id, channel_id, host_id, game_role_id, game_text, "
            "needed, joined_json, created_at FROM lfg_posts "
            "WHERE guild_id = %s AND created_at < %s ORDER BY created_at, message_id",
            (int(guild_id), cutoff),
        ).fetchall()
    posts = []
    for row in rows:
        try:
            joined = json.loads(row[6])
        except (TypeError, ValueError):
            joined = []
        posts.append({"message_id": row[0], "channel_id": row[1],
                      "host_id": row[2], "game_role_id": row[3],
                      "game_text": row[4], "needed": row[5],
                      "joined": [int(uid) for uid in joined if isinstance(uid, int)],
                      "created_at": row[7]})
    return posts


def delete_lfg_post(guild_id: int, message_id: int) -> bool:
    """Forget a post. True when a row was actually removed."""
    try:
        with get_connection() as conn:
            removed = conn.execute(
                "DELETE FROM lfg_posts WHERE guild_id = %s AND message_id = %s",
                (int(guild_id), int(message_id)),
            ).rowcount
            conn.commit()
            return removed == 1
    except EngineError as exc:
        db_logger.exception("LFG delete failed (guild=%s, message=%s)",
                            guild_id, message_id)
        raise DatabaseOperationError("lfg delete failed") from exc


def prune_lfg_posts(older_than_days: int = 7) -> int:
    """Drop posts nobody will use again.

    A post is a moment rather than a record, so the row has no reason to outlive
    the message by much. This is housekeeping, not correctness: a stale row only
    ever answers a click on a message that is almost certainly gone.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, int(older_than_days)))).isoformat()
    try:
        with get_connection() as conn:
            removed = conn.execute(
                "DELETE FROM lfg_posts WHERE created_at < %s", (cutoff,)
            ).rowcount
            conn.commit()
            return removed
    except EngineError as exc:
        db_logger.exception("LFG prune failed")
        raise DatabaseOperationError("lfg prune failed") from exc


def get_minigame_state(guild_id: int, game_key: str) -> dict:
    """Where a channel game is up to. A guild that has never played reads as new.

    Absent and "nothing yet" are the same thing here, deliberately: the first
    message in a counting channel is the first turn either way, and inventing a
    third state would only give the caller something else to handle.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value, last_user_id, streak, best_streak FROM minigame_state "
            "WHERE guild_id = %s AND game_key = %s", (int(guild_id), game_key)
        ).fetchone()
    if row is None:
        return {"value": "", "last_user_id": None, "streak": 0, "best_streak": 0}
    return {"value": row[0], "last_user_id": row[1], "streak": row[2],
            "best_streak": row[3]}


def _claim_used_value(conn, guild_id: int, game_key: str,
                      value: str | None, timestamp: str) -> bool:
    """Take a value out of a chain's supply, or report it was already gone.

    Takes the caller's connection so the claim commits with the turn that made
    it: a claim on its own connection would survive a rolled-back turn and
    forbid a word nobody successfully played.
    """
    if value is None:
        return True
    return bool(conn.execute(
        "INSERT INTO minigame_used_words "
        "(guild_id, game_key, word, used_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (int(guild_id), game_key, value, timestamp)).rowcount)


def advance_minigame(guild_id: int, game_key: str, value: str, user_id: int,
                     expected: str, unique_value: str | None = None
                     ) -> dict | None:
    """Record one accepted turn, or say why it was not one.

    `expected` is the value the caller believed was current. The UPDATE is
    conditional on it, so two people posting the next number in the same instant
    cannot both be accepted — one wins and the other is told the chain moved.
    Without that, the check would be a read followed by a write and a fast
    channel would let the count skip.

    `unique_value` is a value this chain may only ever use once, already folded
    by the caller. Claiming it is an `ON CONFLICT DO NOTHING` insert against a
    primary key, which means the insert **is** the check: a `SELECT` followed
    by an `INSERT` would be exactly the read-then-write shape the paragraph
    above exists to avoid, and two people posting one word in the same instant
    would both pass it. Word chain passes the word; counting passes nothing,
    because its values are unique by construction — the chain only ever goes
    up.

    Three answers, and a caller has to tell them apart before reading a streak:
    `None` when somebody got there first, `{"duplicate": True}` when the value
    has been used before, and the new state otherwise.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT value, streak, best_streak FROM minigame_state "
                "WHERE guild_id = %s AND game_key = %s", (int(guild_id), game_key)
            ).fetchone()
            if row is None:
                if expected != "":
                    conn.rollback()
                    return None
                if not _claim_used_value(conn, guild_id, game_key,
                                         unique_value, timestamp):
                    conn.rollback()
                    return {"duplicate": True}
                streak, best = 1, 1
                conn.execute(
                    "INSERT INTO minigame_state (guild_id, game_key, value, "
                    "last_user_id, streak, best_streak, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (int(guild_id), game_key, value, int(user_id), streak, best,
                     timestamp),
                )
            else:
                if row[0] != expected:
                    conn.rollback()
                    return None
                if not _claim_used_value(conn, guild_id, game_key,
                                         unique_value, timestamp):
                    conn.rollback()
                    return {"duplicate": True}
                streak = row[1] + 1
                best = max(row[2], streak)
                conn.execute(
                    "UPDATE minigame_state SET value = %s, last_user_id = %s, "
                    "streak = %s, best_streak = %s, updated_at = %s "
                    "WHERE guild_id = %s AND game_key = %s AND value = %s",
                    (value, int(user_id), streak, best, timestamp,
                     int(guild_id), game_key, expected),
                )
            conn.commit()
            return {"value": value, "streak": streak, "best_streak": best}
    except EngineError as exc:
        db_logger.exception("Minigame turn failed (guild=%s, game=%s)",
                            guild_id, game_key)
        raise DatabaseOperationError("minigame turn failed") from exc


def reset_minigame(guild_id: int, game_key: str) -> None:
    """Start the chain over, keeping the best streak as a record."""
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO minigame_state (guild_id, game_key, value, "
            "last_user_id, streak, best_streak, updated_at) "
            "VALUES (%s, %s, '', NULL, 0, 0, %s) "
            "ON CONFLICT(guild_id, game_key) DO UPDATE SET value = '', "
            "last_user_id = NULL, streak = 0, updated_at = excluded.updated_at",
            (int(guild_id), game_key, timestamp),
        )
        # A reset that left ten thousand words forbidden would not be a reset,
        # and this is the only escape valve a long chain has.
        conn.execute(
            "DELETE FROM minigame_used_words "
            "WHERE guild_id = %s AND game_key = %s", (int(guild_id), game_key))
        conn.commit()


# ==========================================
# Patchbot: per-game patch-notes state
# ==========================================

def get_game_patch_state(game_key: str) -> dict | None:
    """What patchbot last saw upstream for a game, or `None` if never checked.

    One row per game, not per guild: the newest patch for a game is the same
    fact everywhere, so the poll loop fetches it once and compares against
    this rather than once per guild watching that game.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT latest_version, latest_url, latest_title, checked_at "
            "FROM game_patch_state WHERE game_key = %s", (game_key,)
        ).fetchone()
    if row is None:
        return None
    return {
        "latest_version": row[0], "latest_url": row[1],
        "latest_title": row[2], "checked_at": row[3],
    }


def set_game_patch_state(game_key: str, version: str, url: str, title: str) -> None:
    """Record the latest patch seen upstream for a game."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO game_patch_state "
            "(game_key, latest_version, latest_url, latest_title, checked_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT(game_key) DO UPDATE SET "
            "latest_version = excluded.latest_version, "
            "latest_url = excluded.latest_url, "
            "latest_title = excluded.latest_title, "
            "checked_at = excluded.checked_at",
            (game_key, version, url, title, utc_now().isoformat()),
        )


def get_guild_patch_announcement(guild_id: int, game_key: str) -> str | None:
    """The version a guild was last told about for a game, or `None`."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_announced_version FROM guild_patch_announcements "
            "WHERE guild_id = %s AND game_key = %s", (int(guild_id), game_key)
        ).fetchone()
    return row[0] if row else None


def set_guild_patch_announcement(guild_id: int, game_key: str, version: str) -> None:
    """Record that a guild has now been told about this version of a game.

    Persisted rather than kept in memory like socials.py's live-stream dedup:
    a restart losing this would either re-announce an old patch to every
    enabled guild or, if naively rebaselined, skip a real one — both worse
    here than the live-stream case socials.py was built to tolerate.
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO guild_patch_announcements "
            "(guild_id, game_key, last_announced_version) VALUES (%s, %s, %s) "
            "ON CONFLICT(guild_id, game_key) DO UPDATE SET "
            "last_announced_version = excluded.last_announced_version",
            (int(guild_id), game_key, version),
        )


def get_five_star_history(guild_id: int, user_id: int, limit: int = 5) -> list[dict]:
    """A member's most recent 5-star pulls, newest first.

    `gacha_pulls` has recorded everything this needs since schema 5 and nothing
    has ever read it — the only pity a member could see was the number returned
    by the pull they had just made.

    **`pity_before` is the pity *before* the pull**, so the count it landed at is
    `pity_before + 1`. That off-by-one would be invisible in the interface and
    wrong forever, so it is resolved here rather than at each display site.
    """
    limit = max(1, min(int(limit), 25))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT banner_key, reward_key, pity_before, hard_pity, featured, "
            "featured_guaranteed, created_at FROM gacha_pulls "
            "WHERE guild_id = %s AND user_id = %s AND rarity = 5 "
            "ORDER BY pull_id DESC LIMIT %s",
            (int(guild_id), int(user_id), limit),
        ).fetchall()
    return [
        {"banner_key": row[0], "reward_key": row[1], "pity": row[2] + 1,
         "hard_pity": bool(row[3]), "featured": bool(row[4]),
         "featured_guaranteed": bool(row[5]), "created_at": row[6]}
        for row in rows
    ]


def get_gacha_pity(guild_id: int, user_id: int,
                   banner_key: str = DEFAULT_GACHA_BANNER_KEY) -> dict:
    """One member's live pity on one banner, for a profile line.

    A member with no row has pulled nothing on it, which is zero rather than
    absent — every counter starts there.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pulls_since_five_star, pulls_toward_four_star, "
            "guaranteed_featured_five, guaranteed_featured_four FROM gacha_pity "
            "WHERE guild_id = %s AND user_id = %s AND banner_key = %s",
            (int(guild_id), int(user_id), banner_key),
        ).fetchone()
        total = conn.execute(
            "SELECT COUNT(*) FROM gacha_pulls WHERE guild_id = %s AND user_id = %s",
            (int(guild_id), int(user_id)),
        ).fetchone()[0]
        five_stars = conn.execute(
            "SELECT COUNT(*) FROM gacha_pulls "
            "WHERE guild_id = %s AND user_id = %s AND rarity = 5",
            (int(guild_id), int(user_id)),
        ).fetchone()[0]
    return {
        "banner_key": banner_key,
        "pity": row[0] if row else 0,
        "four_star_counter": row[1] if row else 0,
        "guaranteed_featured_five": bool(row[2]) if row else False,
        "guaranteed_featured_four": bool(row[3]) if row else False,
        "total_pulls": total,
        "five_stars": five_stars,
    }


def shipped_reward_table() -> dict:
    """The reward table a fresh installation ships with, deep-copied."""
    return json.loads(json.dumps(DEFAULT_GACHA_CONFIG["rewards"]))


def missing_shipped_rewards(config_value: dict) -> dict:
    """The shipped rewards a stored banner's table does not have, per tier.

    A stored banner is frozen at the shipped set of the day it was first saved,
    and nothing has ever reconciled the two — so when a reward is added to
    `DEFAULT_GACHA_CONFIG`, every already-saved banner keeps the old table and
    can never award it. `streak_freeze` reached the live installation's shop and
    its 4-star tier and was unobtainable from its actual banner for exactly that
    reason.

    Matched on the reward key alone: a key is what a pull row records and what a
    locale entry is named after, so two entries with one key would double its odds
    and make the displayed chance a lie. An operator who deleted a shipped reward
    on purpose will be offered it again — which is why this is a separate action
    from a reset rather than something that happens on save.
    """
    stored = config_value.get("rewards") or {}
    missing = {}
    for tier, entries in shipped_reward_table().items():
        have = {entry.get("key") for entry in stored.get(tier, [])
                if isinstance(entry, dict)}
        absent = [entry for entry in entries if entry["key"] not in have]
        if absent:
            missing[tier] = absent
    return missing


def new_banner_config() -> dict:
    """The config a freshly created banner starts with.

    The cost and the pity numbers come from the shipped defaults, because they
    are sensible starting points an operator would otherwise retype.

    The **reward table is split by what a tier is for**, and that distinction is
    the whole of it. Tier 3 is filler: it can never carry a featured reward — a
    rate-up on the pool nobody is chasing has no meaning and no loss branch — so
    every banner's 3-star tier wants the same shipped rows, and starting empty
    meant adding seven of them by hand for each new banner. It is seeded from the
    shipped table. Tiers 4 and 5 are what an event banner is *about*, so copying
    the shipped rows there would mean the first thing you do is prune them.

    A tier cannot be literally empty — `_validated_gacha_config` requires one
    enabled reward per tier, because a tier can still be rolled and must have
    something to award — so the two curated tiers get one placeholder each.
    `coins_250` stays that placeholder deliberately: it is a valid reward with a
    locale name, and now that **no shipped reward is a coin** it cannot be
    mistaken for one, nor re-offered by `missing_shipped_rewards`.
    """
    config = json.loads(json.dumps(DEFAULT_GACHA_CONFIG))
    placeholder = {"key": "coins_250", "kind": "coins", "amount": 250,
                   "weight": 1, "enabled": True}
    config["rewards"] = {"3": shipped_reward_table()["3"],
                         "4": [dict(placeholder)],
                         "5": [dict(placeholder)]}
    return config


def create_gacha_banner(guild_id: int, actor_id: int, banner_key: str,
                        display_name: str, config_value: dict,
                        enabled: bool = False, starts_at=None, ends_at=None) -> dict:
    """Add a second, third … banner to one guild.

    A new banner starts disabled unless asked otherwise, so an unfinished reward
    table is never pullable, and the cap matches the 25 options a Discord choice
    list holds — beyond that the command could not offer the banner at all.

    `starts_at`/`ends_at` are an optional schedule on top of `enabled`, not a
    replacement for it: both `None` means no schedule, and either bound
    present narrows `enabled` further rather than overriding it.
    """
    banner_key = validate_gacha_banner_key(banner_key)
    display_name = validate_gacha_banner_name(display_name)
    if not isinstance(enabled, bool):
        raise ValidationError("gacha_enabled_not_boolean", "banner enabled must be boolean")
    config_value = _validated_gacha_config(config_value, banner_key)
    starts_at, ends_at = validate_gacha_banner_schedule(starts_at, ends_at)
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT COUNT(*), COUNT(CASE WHEN banner_key = %s THEN 1 END) "
            "FROM gacha_banners WHERE guild_id = %s",
            (banner_key, int(guild_id)),
        ).fetchone()
        if existing[1]:
            conn.rollback()
            raise ValidationError("gacha_banner_exists", "banner key already exists",
                                  banner=banner_key)
        if existing[0] >= GACHA_BANNER_LIMIT:
            conn.rollback()
            raise ValidationError("gacha_banner_limit", "banner limit reached",
                                  limit=GACHA_BANNER_LIMIT)
        conn.execute(
            "INSERT INTO gacha_banners (guild_id, banner_key, display_name, enabled, "
            "config_json, revision, updated_by, updated_at, starts_at, ends_at) "
            "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s)",
            (int(guild_id), banner_key, display_name, int(enabled),
             json.dumps(config_value, sort_keys=True), int(actor_id), timestamp,
             starts_at, ends_at),
        )
        write_settings_audit(
            conn, int(guild_id), actor_id, "gacha.create", f"gacha.{banner_key}",
            None, {"display_name": display_name, "enabled": enabled},
        )
        conn.commit()
    return {"banner_key": banner_key, "display_name": display_name,
            "enabled": enabled, "config": config_value, "revision": 1,
            "updated_at": timestamp, "starts_at": starts_at, "ends_at": ends_at,
            "is_default": banner_key == DEFAULT_GACHA_BANNER_KEY}


def set_gacha_banner(guild_id: int, actor_id: int, enabled: bool,
                     config_value: dict, expected_revision: int,
                     banner_key: str = DEFAULT_GACHA_BANNER_KEY,
                     display_name=None, starts_at=None, ends_at=None) -> dict:
    if not isinstance(enabled, bool):
        raise ValidationError("gacha_enabled_not_boolean", "banner enabled must be boolean")
    banner_key = validate_gacha_banner_key(banner_key)
    if display_name is not None:
        display_name = validate_gacha_banner_name(display_name)
    config_value = _validated_gacha_config(config_value, banner_key)
    starts_at, ends_at = validate_gacha_banner_schedule(starts_at, ends_at)
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled, config_json, revision, display_name FROM gacha_banners "
            "WHERE guild_id = %s AND banner_key = %s", (int(guild_id), banner_key)
        ).fetchone()
        if row:
            if expected_revision != row[2]:
                raise RevisionConflictError("gacha revision conflict")
            revision = row[2] + 1
            old = {"enabled": bool(row[0]), "config": json.loads(row[1]),
                   "display_name": row[3]}
            # An omitted name keeps the stored one, so saving the reward table
            # cannot silently rename the banner.
            stored_name = display_name if display_name is not None else row[3]
            conn.execute(
                "UPDATE gacha_banners SET enabled = %s, config_json = %s, revision = %s, "
                "display_name = %s, updated_by = %s, updated_at = %s, "
                "starts_at = %s, ends_at = %s "
                "WHERE guild_id = %s AND banner_key = %s",
                (int(enabled), json.dumps(config_value, sort_keys=True), revision,
                 stored_name, int(actor_id), timestamp, starts_at, ends_at,
                 int(guild_id), banner_key),
            )
        else:
            # Only the installation default may appear on first save. Every other
            # banner has to be created explicitly, or a typo in a key would
            # create a banner instead of being rejected.
            if banner_key != DEFAULT_GACHA_BANNER_KEY:
                conn.rollback()
                raise ValidationError("gacha_banner_unknown", "no such banner",
                                      banner=banner_key)
            if expected_revision not in (0, None):
                raise RevisionConflictError("gacha revision conflict")
            revision, old = 1, None
            stored_name = display_name
            conn.execute(
                "INSERT INTO gacha_banners "
                "(guild_id, banner_key, display_name, enabled, config_json, revision, "
                "updated_by, updated_at, starts_at, ends_at) "
                "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s)",
                (int(guild_id), banner_key, stored_name, int(enabled),
                 json.dumps(config_value, sort_keys=True), int(actor_id), timestamp,
                 starts_at, ends_at),
            )
        new = {"enabled": enabled, "config": config_value,
               "display_name": stored_name}
        conn.execute(
            "INSERT INTO settings_audit "
            "(guild_id, actor_id, action, target_key, old_value_json, new_value_json, created_at) "
            "VALUES (%s, %s, 'gacha.update', %s, %s, %s, %s)",
            (int(guild_id), int(actor_id), f"gacha.{banner_key}",
             json.dumps(old) if old else None, json.dumps(new), timestamp),
        )
        conn.commit()
    return {"banner_key": banner_key,
            "display_name": stored_name or banner_key, "enabled": enabled,
            "config": config_value, "revision": revision,
            "updated_at": timestamp, "starts_at": starts_at, "ends_at": ends_at,
            "is_default": banner_key == DEFAULT_GACHA_BANNER_KEY}


def delete_gacha_banner(guild_id: int, actor_id: int, banner_key: str,
                        expected_revision: int) -> dict:
    """Remove one banner, keeping the pull history that references it.

    Pull rows and pity counters are deliberately left alone: pull history is
    immutable, and a member who rejoins a re-created banner keeps the pity they
    paid for. The installation default cannot be deleted, because `/gacha` with
    no banner argument resolves to it.
    """
    banner_key = validate_gacha_banner_key(banner_key)
    if banner_key == DEFAULT_GACHA_BANNER_KEY:
        raise ValidationError("gacha_banner_default_undeletable",
                              "the default banner cannot be deleted")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT revision, display_name, enabled FROM gacha_banners "
            "WHERE guild_id = %s AND banner_key = %s", (int(guild_id), banner_key)
        ).fetchone()
        if row is None:
            conn.rollback()
            raise ValidationError("gacha_banner_unknown", "no such banner",
                                  banner=banner_key)
        if int(expected_revision) != row[0]:
            conn.rollback()
            raise RevisionConflictError("gacha revision conflict")
        conn.execute(
            "DELETE FROM gacha_banners WHERE guild_id = %s AND banner_key = %s",
            (int(guild_id), banner_key),
        )
        write_settings_audit(
            conn, int(guild_id), actor_id, "gacha.delete", f"gacha.{banner_key}",
            {"display_name": row[1], "enabled": bool(row[2])}, None,
        )
        conn.commit()
    return {"banner_key": banner_key, "deleted": True}


def reward_is_enabled(entry: dict) -> bool:
    """A reward row without an explicit flag is enabled, for older banners."""
    return entry.get("enabled", True) is not False


def _weighted_choice(entries: list[dict], rng) -> dict:
    point = rng.randrange(sum(entry["weight"] for entry in entries))
    for entry in entries:
        point -= entry["weight"]
        if point < 0:
            return entry
    raise RuntimeError("weighted choice failed")


def _voucher_subject_for(conn, guild_id: int, reward_key: str):
    """What a voucher for this reward key is *for*, or None to derive it later.

    None for a built-in key, so a shipped voucher behaves exactly as it always
    has and every existing row keeps its meaning. For one of the guild's own
    items the subject comes from the item's config — an asset type, or a role.

    Read on the caller's connection rather than through `get_shop_item_definitions`,
    which opens its own: this runs inside the pull's transaction and a second
    connection would see a different snapshot.
    """
    if reward_key in item_catalog.ITEM_DEFINITIONS or reward_key == "premium_30d":
        return None
    row = conn.execute(
        "SELECT template_type, config_json FROM shop_item_definitions "
        "WHERE guild_id = %s AND item_key = %s", (int(guild_id), reward_key)
    ).fetchone()
    if row is None:
        return None
    template, raw = row
    try:
        config_value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if template == "fulfillment_voucher":
        asset = config_value.get("asset_type")
        return asset if asset in {"emoji", "sticker", "sound"} else None
    if template == "timed_role":
        role_id = config_value.get("role_id")
        # `role:<id>` is the entitlement key the shop already writes for a custom
        # timed role, so a redeemed one expires through the pass that already
        # revokes anything starting with `role:`.
        return f"role:{int(role_id)}" if role_id else None
    return None


def _grant_gacha_reward_locked(conn, guild_id: int, user_id: int,
                               reward: dict, config_value: dict, timestamp: str) -> dict:
    kind, amount = reward["kind"], reward["amount"]
    granted = {"key": reward["key"], "kind": kind, "amount": amount}
    if kind == "coins":
        conn.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
    elif kind == "item":
        conn.execute(
            "INSERT INTO user_inventory (guild_id, user_id, item_key, quantity, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT(guild_id, user_id, item_key) "
            "DO UPDATE SET quantity = user_inventory.quantity + excluded.quantity, updated_at = excluded.updated_at",
            (guild_id, user_id, reward["key"], amount, timestamp),
        )
    elif kind == "vault":
        current = conn.execute("SELECT protected_reserve FROM users WHERE user_id = %s", (user_id,)).fetchone()[0]
        if current >= amount:
            compensation = amount * config_value["duplicate_percent"] // 100
            conn.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (compensation, user_id))
            granted["duplicate_compensation"] = compensation
        else:
            conn.execute("UPDATE users SET protected_reserve = %s WHERE user_id = %s", (amount, user_id))
            granted["protected_reserve"] = amount
    else:
        voucher_id = secrets.token_urlsafe(12)
        # The subject is written **now**, not resolved at redemption: a voucher
        # can outlive the item that produced it, and resolving late would strand
        # every one of them the moment an operator deleted the item.
        conn.execute(
            "INSERT INTO reward_vouchers "
            "(voucher_id, guild_id, user_id, reward_key, source_type, "
            "duration_days, subject, acquired_at) "
            "VALUES (%s, %s, %s, %s, 'gacha', %s, %s, %s)",
            (voucher_id, guild_id, user_id, reward["key"], amount,
             _voucher_subject_for(conn, guild_id, reward["key"]), timestamp),
        )
        granted["voucher_id"] = voucher_id
    return granted


def _resolve_featured_pools(conn, guild_id: int, banner_key: str,
                            config_value: dict, now: str) -> tuple[dict, dict]:
    """Work out, per rare tier, what a won and a lost split award.

    Returns ``(featured_entries, standard_pools)`` keyed by "4"/"5". A tier is
    absent from ``featured_entries`` when the banner features nothing there, and
    absent from ``standard_pools`` when there is no standard pool to lose to. The
    caller runs the ordinary weighted draw unless *both* are present, which is
    what keeps a banner without a rate-up byte-for-byte unchanged — including its
    RNG consumption.

    The standard banner has four distinct states and only one of them supports a
    split:

    * it *is* the banner being pulled — no split, because standard has no
      rate-up and all its rares are one rate;
    * stored and currently pullable — split against its saved pool;
    * stored but **not currently pullable** (disabled, or outside its own
      schedule) — no split, so rares come only from this banner's own table;
    * **absent** — split against the shipped table. `get_gacha_banner` and
      `list_gacha_banners` already synthesise an absent standard banner as
      enabled, so a guild that has simply never pulled it still gets a working
      event banner. The row is deliberately not created here: another banner's
      pull must not conjure one.
    """
    featured_entries = {}
    for tier in ("4", "5"):
        for entry in config_value["rewards"].get(tier, []):
            if entry.get("featured") and reward_is_enabled(entry):
                featured_entries[tier] = entry
                break
    if not featured_entries or banner_key == DEFAULT_GACHA_BANNER_KEY:
        return featured_entries, {}

    row = conn.execute(
        "SELECT enabled, config_json, starts_at, ends_at FROM gacha_banners "
        "WHERE guild_id = %s AND banner_key = %s",
        (int(guild_id), DEFAULT_GACHA_BANNER_KEY),
    ).fetchone()
    if row is None:
        standard_config = DEFAULT_GACHA_CONFIG
    elif _banner_pull_status(bool(row[0]), row[2], row[3], now) != "live":
        return featured_entries, {}
    else:
        try:
            standard_config = _validated_gacha_config(json.loads(row[1]))
        except (ValidationError, ValueError):
            # A standard banner this build cannot read is not a reason to fail a
            # pull on a different banner. No pool means no split.
            db_logger.exception(
                "Standard banner config is unreadable; no featured split applied "
                "(guild_id=%s)", guild_id,
            )
            return featured_entries, {}

    standard_pools = {}
    for tier in featured_entries:
        pool = [entry for entry in standard_config["rewards"].get(tier, [])
                if reward_is_enabled(entry)]
        if pool:
            standard_pools[tier] = pool
    return featured_entries, standard_pools


def perform_gacha_pulls(guild_id: int, user_id: int, count: int,
                        banner_key: str = DEFAULT_GACHA_BANNER_KEY,
                        rng=None) -> dict:
    """Debit and settle one or ten sequential pulls in one transaction."""
    if count not in (1, 10):
        raise ValueError("gacha pull count must be 1 or 10")
    banner_key = validate_gacha_banner_key(banner_key)
    rng, timestamp = rng or secrets.SystemRandom(), datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            banner = conn.execute(
                "SELECT enabled, config_json, revision, display_name, "
                "starts_at, ends_at FROM gacha_banners "
                "WHERE guild_id = %s AND banner_key = %s", (int(guild_id), banner_key)
            ).fetchone()
            banner_name = banner_key
            if banner:
                status = _banner_pull_status(bool(banner[0]), banner[4], banner[5], timestamp)
                if status != "live":
                    conn.rollback()
                    # Distinct reasons: "not yet" and "no longer" are different
                    # things to tell a member, and neither is "disabled" once a
                    # schedule exists to explain it.
                    return {"purchased": False,
                            "reason": {"disabled": "banner_disabled",
                                       "scheduled": "banner_not_started",
                                       "expired": "banner_expired"}[status]}
                config_value, revision = _validated_gacha_config(json.loads(banner[1])), banner[2]
                banner_name = banner[3] or banner_key
            elif banner_key != DEFAULT_GACHA_BANNER_KEY:
                # The banner argument is user supplied. Creating a default-priced
                # banner for any key that was asked for would let a member
                # conjure a banner the operator never configured.
                conn.rollback()
                return {"purchased": False, "reason": "banner_unknown"}
            else:
                config_value, revision = _validated_gacha_config(DEFAULT_GACHA_CONFIG), 1
                conn.execute(
                    "INSERT INTO gacha_banners "
                    "(guild_id, banner_key, enabled, config_json, revision, updated_at) "
                    "VALUES (%s, %s, 1, %s, 1, %s)",
                    (int(guild_id), banner_key, json.dumps(config_value, sort_keys=True), timestamp),
                )
            _ensure_user(conn, int(user_id), timestamp)
            total_cost = config_value["cost"] * count
            if conn.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
                (total_cost, int(user_id), total_cost),
            ).rowcount != 1:
                conn.rollback()
                return {"purchased": False, "reason": "insufficient_funds"}
            pity_row = conn.execute(
                "SELECT pulls_since_five_star, pulls_toward_four_star, "
                "guaranteed_featured_five, guaranteed_featured_four FROM gacha_pity "
                "WHERE guild_id = %s AND user_id = %s AND banner_key = %s",
                (int(guild_id), int(user_id), banner_key),
            ).fetchone()
            pity = pity_row[0] if pity_row else 0
            four_counter = pity_row[1] if pity_row else 0
            guaranteed = {"5": bool(pity_row[2]) if pity_row else False,
                          "4": bool(pity_row[3]) if pity_row else False}
            # Resolved once for the whole batch, on this connection: reading the
            # standard banner through get_gacha_banner would open a second
            # connection and see a different snapshot mid-transaction, and would
            # raise on an unknown key. It must also never *create* the standard
            # row as a side effect of another banner's pull.
            featured_entries, standard_pools = _resolve_featured_pools(
                conn, int(guild_id), banner_key, config_value, timestamp)
            split = config_value["featured_split"]
            results = []
            for _ in range(count):
                pity_before, pull_number = pity, pity + 1
                four_before = four_counter
                four_guarantee = (
                    four_counter + 1 >= config_value["four_star_guarantee_interval"]
                )
                hard = pull_number >= config_value["hard_pity"]
                soft = not hard and pull_number > config_value["soft_pity_start"]
                if hard:
                    rarity = "5"
                else:
                    tiers = dict(config_value["tiers"])
                    if soft:
                        tiers["4"] *= config_value["soft_pity_multiplier"]
                        tiers["5"] *= config_value["soft_pity_multiplier"]
                        tiers["3"] = 100000 - tiers["4"] - tiers["5"]
                        if tiers["3"] < 0:
                            raise ValidationError("gacha_soft_pity_overflow", "soft-pity weights exceed 100 percent")
                    rarity = _weighted_choice([{"key": key, "weight": weight}
                                               for key, weight in tiers.items()], rng)["key"]
                if four_guarantee and rarity == "3":
                    rarity = "4"
                # Everything above decided the *tier*; everything below decides
                # the reward's *identity*. Hard pity, soft pity and the tenth-pull
                # floor stay orthogonal to the split, and nothing here can change
                # `rarity`, so the 97.8/1.6/0.6 totals are untouched.
                featured_entry = featured_entries.get(rarity)
                standard_pool = standard_pools.get(rarity)
                featured_hit = featured_from_guarantee = False
                if featured_entry is not None and standard_pool:
                    if guaranteed.get(rarity):
                        reward = featured_entry
                        featured_hit = featured_from_guarantee = True
                        guaranteed[rarity] = False
                    elif rng.randrange(100) < split:
                        reward = featured_entry
                        featured_hit = True
                    else:
                        # A loss draws the standard banner's pool as it stands.
                        # No exclusion: the operator curates that pool, and the
                        # dashboard warns when a featured key is still in it.
                        reward = _weighted_choice(standard_pool, rng)
                        guaranteed[rarity] = True
                else:
                    # No featured reward in this tier, or no standard pool to lose
                    # to: exactly the draw this made before the split existed, and
                    # crucially with no extra RNG consumed.
                    #
                    # A disabled row keeps its history and weight on record but is
                    # never drawn. The validator guarantees each tier keeps one.
                    reward = _weighted_choice(
                        [entry for entry in config_value["rewards"][rarity]
                         if reward_is_enabled(entry)],
                        rng,
                    )
                granted = _grant_gacha_reward_locked(conn, int(guild_id), int(user_id),
                                                     reward, config_value, timestamp)
                pity = 0 if rarity == "5" else pity + 1
                four_counter = 0 if four_guarantee else four_counter + 1
                conn.execute(
                    "INSERT INTO gacha_pulls "
                    "(guild_id, user_id, banner_key, banner_revision, rarity, reward_key, "
                    "reward_json, pity_before, soft_pity, hard_pity, "
                    "four_star_guarantee, featured, featured_guaranteed, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (int(guild_id), int(user_id), banner_key, revision, int(rarity),
                     reward["key"], json.dumps(granted, sort_keys=True), pity_before,
                     int(soft), int(hard), int(four_guarantee), int(featured_hit),
                     int(featured_from_guarantee), timestamp),
                )
                results.append({"rarity": int(rarity), "soft_pity": soft,
                                "hard_pity": hard,
                                "four_star_guarantee": four_guarantee,
                                "four_star_counter_before": four_before,
                                "featured": featured_hit,
                                "featured_guaranteed": featured_from_guarantee,
                                "guarantee_held": guaranteed.get(rarity, False),
                                **granted})
            conn.execute(
                "INSERT INTO gacha_pity "
                "(guild_id, user_id, banner_key, pulls_since_five_star, "
                "pulls_toward_four_star, guaranteed_featured_five, "
                "guaranteed_featured_four, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT(guild_id, user_id, banner_key) "
                "DO UPDATE SET pulls_since_five_star = excluded.pulls_since_five_star, "
                "pulls_toward_four_star = excluded.pulls_toward_four_star, "
                "guaranteed_featured_five = excluded.guaranteed_featured_five, "
                "guaranteed_featured_four = excluded.guaranteed_featured_four, "
                "updated_at = excluded.updated_at",
                (int(guild_id), int(user_id), banner_key, pity, four_counter,
                 int(guaranteed["5"]), int(guaranteed["4"]), timestamp),
            )
            balance = conn.execute("SELECT balance FROM users WHERE user_id = %s", (int(user_id),)).fetchone()[0]
            conn.commit()
            return {"purchased": True, "cost": total_cost, "balance": balance,
                    "pity": pity, "four_star_counter": four_counter,
                    "four_star_interval": config_value["four_star_guarantee_interval"],
                    "featured_split": split,
                    "featured_tiers": sorted(
                        tier for tier in featured_entries if standard_pools.get(tier)),
                    "guaranteed_featured": {tier: value for tier, value
                                            in guaranteed.items() if value},
                    "results": results, "banner_revision": revision,
                    "banner_key": banner_key, "banner_name": banner_name}
    except (EngineError, TypeError) as exc:
        db_logger.exception("Gacha transaction failed (guild=%s, user=%s)", guild_id, user_id)
        raise DatabaseOperationError("gacha transaction failed") from exc


def get_user_inventory(guild_id: int, user_id: int) -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT item_key, quantity FROM user_inventory "
            "WHERE guild_id = %s AND user_id = %s AND quantity > 0 ORDER BY item_key",
            (int(guild_id), int(user_id)),
        ).fetchall()
    return dict(rows)


def get_user_vouchers(guild_id: int, user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT voucher_id, reward_key, duration_days, status, acquired_at, expires_at "
            "FROM reward_vouchers WHERE guild_id = %s AND user_id = %s "
            # A ten-pull grants its vouchers in one instant; they list in the
            # order granted, as SQLite's rowid kept them (decided 2026-10-31).
            "ORDER BY acquired_at DESC, insertion_order",
            (int(guild_id), int(user_id)),
        ).fetchall()
    keys = ("voucher_id", "reward_key", "duration_days", "status", "acquired_at", "expires_at")
    return [dict(zip(keys, row)) for row in rows]


def _extend_timed_entitlement(conn, guild_id: int, user_id: int,
                              entitlement_key: str, duration_days: int,
                              now, voucher_id: str, timestamp: str) -> str:
    """Start or extend one timed grant, returning when it now expires.

    Extending rather than stacking: a member redeeming a second voucher while the
    first is live gets the time added, which is what the premium path has always
    done. Factored out because premium and a role voucher differ only in the
    entitlement key, and two copies of "work out the new expiry" is two places
    for an off-by-one that costs somebody a month.
    """
    active = conn.execute(
        "SELECT entitlement_id, expires_at FROM timed_entitlements "
        "WHERE guild_id = %s AND user_id = %s AND entitlement_key = %s "
        "AND status = 'active' ORDER BY expires_at DESC, entitlement_id LIMIT 1",
        (int(guild_id), int(user_id), entitlement_key),
    ).fetchone()
    start = now
    if active:
        current_expiry = parse_stored(active[1])
        if current_expiry > start:
            start = current_expiry
    expires = (start + timedelta(days=duration_days)).isoformat()
    if active:
        conn.execute(
            "UPDATE timed_entitlements SET expires_at = %s WHERE entitlement_id = %s",
            (expires, active[0]),
        )
    else:
        conn.execute(
            "INSERT INTO timed_entitlements "
            "(guild_id, user_id, entitlement_key, starts_at, expires_at, "
            "source_voucher_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (int(guild_id), int(user_id), entitlement_key, timestamp, expires,
             voucher_id),
        )
    return expires


def redeem_voucher(guild_id: int, user_id: int, voucher_id: str) -> dict:
    """Activate premium now or open asset fulfillment without starting its timer."""
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT reward_key, duration_days, status, subject FROM reward_vouchers "
            "WHERE voucher_id = %s AND guild_id = %s AND user_id = %s",
            (voucher_id, int(guild_id), int(user_id)),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"redeemed": False, "reason": "not_found"}
        reward_key, duration_days, status, subject = row
        if status != "available":
            conn.rollback()
            return {"redeemed": False, "reason": "already_redeemed"}
        # A guild's own role voucher. Recorded here and applied by the caller,
        # because a Discord call may not happen inside this transaction — which
        # is the same reason premium is a voucher at all rather than a reward the
        # pull grants directly.
        if isinstance(subject, str) and subject.startswith("role:"):
            try:
                role_id = int(subject.split(":", 1)[1])
            except (ValueError, IndexError):
                conn.rollback()
                return {"redeemed": False, "reason": "not_redeemable"}
            expires = _extend_timed_entitlement(
                conn, guild_id, user_id, subject, duration_days, now,
                voucher_id, timestamp)
            conn.execute(
                "UPDATE reward_vouchers SET status = 'active', redeemed_at = %s, "
                "expires_at = %s WHERE voucher_id = %s",
                (timestamp, expires, voucher_id),
            )
            conn.commit()
            return {"redeemed": True, "kind": "role", "role_id": role_id,
                    "entitlement_key": subject, "expires_at": expires}
        if reward_key == "premium_30d":
            # `_extend_timed_entitlement` is exactly this branch's own logic,
            # factored out for precisely this reason: two copies of "work out
            # the new expiry" is two places for an off-by-one. This branch
            # duplicated it anyway, which is also how it drifted into holding
            # `expires` as a `datetime` in one place and a `str` everywhere
            # else -- caught by running a type checker, not by inspection.
            expires = _extend_timed_entitlement(
                conn, guild_id, user_id, "premium", duration_days, now,
                voucher_id, timestamp)
            conn.execute(
                "UPDATE reward_vouchers SET status = 'active', redeemed_at = %s, expires_at = %s "
                "WHERE voucher_id = %s", (timestamp, expires, voucher_id)
            )
            result = {"redeemed": True, "kind": "premium", "expires_at": expires}
        else:
            # The stored subject when there is one, and the old key parse when
            # there is not — which is every voucher granted before the column
            # existed, and every built-in one.
            asset_type = subject or reward_key.split("_", 1)[0]
            if asset_type not in {"emoji", "sticker", "sound"}:
                raise ValidationError("voucher_not_redeemable", "voucher cannot be redeemed")
            conn.execute(
                "UPDATE reward_vouchers SET status = 'pending', redeemed_at = %s "
                "WHERE voucher_id = %s", (timestamp, voucher_id)
            )
            request_id = conn.execute(
                "INSERT INTO fulfillment_requests "
                "(voucher_id, guild_id, user_id, asset_type, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING request_id",
                (voucher_id, int(guild_id), int(user_id), asset_type, timestamp),
            ).fetchone()[0]
            result = {"redeemed": True, "kind": "fulfillment", "asset_type": asset_type,
                      "request_id": request_id}
        conn.commit()
        return result


def rollback_premium_redemption(guild_id: int, user_id: int, voucher_id: str):
    """Restore a premium voucher when Discord role assignment fails."""
    return rollback_voucher_redemption(guild_id, user_id, voucher_id, "premium")


def rollback_voucher_redemption(guild_id: int, user_id: int, voucher_id: str,
                                entitlement_key: str):
    """Put a redeemed voucher back when Discord refused the grant.

    A pull cannot be refunded — it has already spent pity and coins by the time
    anything reaches Discord — so a redemption that fails must leave the voucher
    **unredeemed** rather than consumed. That was the premium path's whole reason
    for existing and a role voucher needs it identically, so the entitlement key
    is a parameter rather than a second copy of the function.
    """
    with get_connection() as conn:
        voucher = conn.execute(
            "SELECT duration_days, status FROM reward_vouchers WHERE voucher_id = %s "
            "AND guild_id = %s AND user_id = %s",
            (voucher_id, int(guild_id), int(user_id)),
        ).fetchone()
        active = conn.execute(
            "SELECT entitlement_id, expires_at FROM timed_entitlements WHERE guild_id = %s "
            "AND user_id = %s AND entitlement_key = %s AND status = 'active' "
            "ORDER BY expires_at DESC, entitlement_id LIMIT 1",
            (int(guild_id), int(user_id), entitlement_key),
        ).fetchone()
        if not voucher or voucher[1] != "active" or not active:
            conn.rollback()
            return False
        restored = parse_stored(active[1]) - timedelta(days=int(voucher[0]))
        if restored <= datetime.now(timezone.utc) + timedelta(minutes=1):
            conn.execute("DELETE FROM timed_entitlements WHERE entitlement_id = %s", (active[0],))
        else:
            conn.execute("UPDATE timed_entitlements SET expires_at = %s WHERE entitlement_id = %s",
                         (restored.isoformat(), active[0]))
        conn.execute(
            "UPDATE reward_vouchers SET status = 'available', redeemed_at = NULL, expires_at = NULL "
            "WHERE voucher_id = %s", (voucher_id,)
        )
        conn.commit()
        return True


def get_fulfillment_requests(guild_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT request_id, voucher_id, user_id, asset_type, status, created_at "
            "FROM fulfillment_requests WHERE guild_id = %s ORDER BY request_id DESC",
            (int(guild_id),),
        ).fetchall()
    keys = ("request_id", "voucher_id", "user_id", "asset_type", "status", "created_at")
    # `user_id` is a Discord snowflake and this row is rendered by the browser,
    # where a 64-bit integer does not survive JSON.parse. Sent as a string for
    # the same reason `get_active_guilds` and the audit feed send theirs.
    return [{**dict(zip(keys, row)), "user_id": str(row[2])} for row in rows]


def fulfill_voucher_request(guild_id: int, request_id: int, actor_id: int,
                            discord_item_id: str) -> dict:
    """Start an asset rental only after staff confirms Discord fulfillment."""
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT f.voucher_id, f.asset_type, f.status, v.user_id, v.duration_days, "
            "f.ticket_channel_id "
            "FROM fulfillment_requests f JOIN reward_vouchers v ON v.voucher_id = f.voucher_id "
            "WHERE f.request_id = %s AND f.guild_id = %s",
            (int(request_id), int(guild_id)),
        ).fetchone()
        if not row or row[2] != "open":
            conn.rollback()
            return {"fulfilled": False, "reason": "unavailable"}
        voucher_id, asset_type, _, user_id, days, ticket_channel_id = row
        expires = now + timedelta(days=days)
        conn.execute(
            "UPDATE fulfillment_requests SET status = 'fulfilled', completed_at = %s, "
            "completed_by = %s WHERE request_id = %s",
            (now.isoformat(), int(actor_id), int(request_id)),
        )
        conn.execute(
            "UPDATE reward_vouchers SET status = 'fulfilled', fulfilled_at = %s, expires_at = %s, "
            "discord_item_id = %s WHERE voucher_id = %s",
            (now.isoformat(), expires.isoformat(), str(discord_item_id), voucher_id),
        )
        conn.execute(
            "INSERT INTO timed_entitlements "
            "(guild_id, user_id, entitlement_key, starts_at, expires_at, source_voucher_id, "
            "discord_item_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (int(guild_id), user_id, asset_type, now.isoformat(), expires.isoformat(),
             voucher_id, str(discord_item_id)),
        )
        conn.commit()
        return {"fulfilled": True, "expires_at": expires.isoformat(),
                "ticket_channel_id": ticket_channel_id}


def link_fulfillment_ticket(guild_id: int, request_id: int, channel_id: int):
    """Store the redeem ticket a fulfillment request opened, so the two can
    find each other -- before this, the only connection was a human-readable
    "#request_id" printed into the ticket's embed. Best-effort: the ticket
    must still stand even if this write fails, because the voucher is already
    spent (same reasoning `support_tickets.open_ticket` itself documents)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE fulfillment_requests SET ticket_channel_id = %s "
            "WHERE request_id = %s AND guild_id = %s",
            (int(channel_id), int(request_id), int(guild_id)),
        )


def get_open_fulfillment_by_channel(channel_id: int) -> dict | None:
    """The open request a redeem ticket's in-channel button settles.

    Looked up by channel at click time rather than baked into the persistent
    view's own state: one registered instance answers every redeem ticket's
    button, so it must hold no per-message state at all.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT request_id, guild_id, asset_type FROM fulfillment_requests "
            "WHERE ticket_channel_id = %s AND status = 'open'",
            (int(channel_id),),
        ).fetchone()
    if row is None:
        return None
    return {"request_id": row[0], "guild_id": row[1], "asset_type": row[2]}


def cancel_fulfillment_request(guild_id: int, request_id: int, actor_id: int) -> dict:
    """Cancel an open fulfillment request and its paired voucher.

    Ends in 'cancelled' rather than deleting either row, the same "nothing is
    migrated, a row is frozen at the day it was saved" principle a retired
    gacha reward follows -- staff may need to see why a redemption never
    completed. Reports the linked ticket, if any, so the caller can notify it.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT voucher_id, status, ticket_channel_id FROM fulfillment_requests "
            "WHERE request_id = %s AND guild_id = %s",
            (int(request_id), int(guild_id)),
        ).fetchone()
        if not row or row[1] != "open":
            conn.rollback()
            return {"cancelled": False, "reason": "unavailable"}
        voucher_id, _, ticket_channel_id = row
        conn.execute(
            "UPDATE fulfillment_requests SET status = 'cancelled', completed_at = %s, "
            "completed_by = %s WHERE request_id = %s",
            (now, int(actor_id), int(request_id)),
        )
        conn.execute(
            "UPDATE reward_vouchers SET status = 'cancelled' WHERE voucher_id = %s",
            (voucher_id,),
        )
        conn.commit()
        return {"cancelled": True, "ticket_channel_id": ticket_channel_id}


def revoke_timed_entitlement(guild_id: int, entitlement_id: int, actor_id: int) -> dict:
    """Mark an active entitlement cancelled from the Redeems page.

    The row is never deleted: `status` moves to 'cancelled' and `revoked_at`
    records when, while `starts_at`/`expires_at` stay exactly what was
    granted, preserving the grant's own history. The Discord-side teardown
    (removing a role, or deleting an emoji/sticker/sound) is a separate,
    best-effort step the caller queues through `control_actions` -- this
    function never makes a Discord call, matching every other mutation here.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, entitlement_key, discord_item_id FROM timed_entitlements "
            "WHERE entitlement_id = %s AND guild_id = %s AND status = 'active'",
            (int(entitlement_id), int(guild_id)),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"revoked": False, "reason": "unavailable"}
        user_id, entitlement_key, discord_item_id = row
        conn.execute(
            "UPDATE timed_entitlements SET status = 'cancelled', revoked_at = %s "
            "WHERE entitlement_id = %s",
            (now, int(entitlement_id)),
        )
        conn.commit()
        return {
            "revoked": True, "user_id": user_id,
            "entitlement_key": entitlement_key, "discord_item_id": discord_item_id,
        }


def grant_manual_entitlement(guild_id: int, user_id: int, asset_type: str,
                             discord_item_id: str, duration_days: int,
                             granted_by: int) -> dict:
    """Start a timed asset entitlement with no voucher or pull behind it.

    For an emoji, sticker or soundboard sound staff already created in Discord
    for a special event and want to hand to a member outright. Mirrors the
    `INSERT` half of `fulfill_voucher_request` exactly -- same table, same
    shape -- with `source_voucher_id` left NULL since there is no redemption
    to link back to, and `source_type`/`granted_by` written directly onto the
    row so `entitlement_cleanup` can find and revoke it without a voucher join.

    Refuses a second active entitlement for the same Discord asset in this
    guild: two live rows guarding one physical asset means the first expiry to
    run deletes it out from under the second, which then fails forever with
    `discord.NotFound` and never reaches `expire_entitlement`.
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=duration_days)
    with get_connection() as conn:
        duplicate = conn.execute(
            "SELECT 1 FROM timed_entitlements WHERE guild_id = %s "
            "AND discord_item_id = %s AND status = 'active'",
            (int(guild_id), str(discord_item_id)),
        ).fetchone()
        if duplicate:
            conn.rollback()
            return {"granted": False, "reason": "duplicate_active"}
        entitlement_id = conn.execute(
            "INSERT INTO timed_entitlements "
            "(guild_id, user_id, entitlement_key, starts_at, expires_at, "
            "discord_item_id, source_type, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'manual', %s) RETURNING entitlement_id",
            (int(guild_id), int(user_id), asset_type, now.isoformat(),
             expires.isoformat(), str(discord_item_id), int(granted_by)),
        ).fetchone()[0]
        conn.commit()
        return {"granted": True, "entitlement_id": entitlement_id,
                "expires_at": expires.isoformat()}


def get_active_entitlements(guild_id: int, timestamp: str) -> list[dict]:
    """Every live grant in one guild, soonest to expire first.

    Nothing read this before. `get_expired_entitlements` is a one-way filter for
    the cleanup loops — it returns only what is already past — and
    `get_active_entitlements_for_user` is cross-guild and exists for erasure. So
    an operator could see what a member had *bought*, in the audit feed, but
    never what the server was currently paying out or for how much longer.

    `expires_at` is a real timestamp on every row, so the remaining time is a
    subtraction rather than a window that has to be known; the caller does it,
    because "how long is left" depends on when you ask.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT e.entitlement_id, e.user_id, e.entitlement_key, e.starts_at, "
            "e.expires_at, e.discord_item_id, COALESCE(e.source_type, v.source_type), "
            "v.reward_key, e.granted_by, e.discord_missing_since "
            "FROM timed_entitlements e "
            "LEFT JOIN reward_vouchers v ON v.voucher_id = e.source_voucher_id "
            "WHERE e.guild_id = %s AND e.status = 'active' AND e.expires_at > %s "
            "ORDER BY e.expires_at ASC, e.entitlement_id",
            (int(guild_id), timestamp),
        ).fetchall()
    keys = ("entitlement_id", "user_id", "entitlement_key", "starts_at",
            "expires_at", "discord_item_id", "source_type", "reward_key",
            "granted_by", "discord_missing_since")
    return [dict(zip(keys, row)) for row in rows]


def get_active_asset_entitlements() -> list[dict]:
    """Every still-active emoji/sticker/sound grant, across every guild, for
    `entitlement_cleanup`'s verification pass -- not the expiry pass.

    Unlike `get_expired_entitlements`, this has no `expires_at` filter: an
    asset can vanish from Discord years before its own entitlement expires
    (an operator deleting it by hand, as in the incident this exists for),
    and nothing before this ever looked at an active grant to notice.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT entitlement_id, guild_id, entitlement_key, discord_item_id, "
            "discord_missing_since FROM timed_entitlements "
            "WHERE status = 'active' AND entitlement_key IN ('emoji', 'sticker', 'sound') "
            "AND discord_item_id IS NOT NULL"
        ).fetchall()
    keys = ("entitlement_id", "guild_id", "entitlement_key", "discord_item_id",
            "discord_missing_since")
    return [dict(zip(keys, row)) for row in rows]


def set_entitlement_discord_presence(entitlement_id: int, missing: bool):
    """Record whether `entitlement_cleanup`'s verification pass could find the
    asset on Discord. Only ever called with a definite answer -- a transient
    or ambiguous check result must leave the row untouched rather than
    guessing, the same way a refused debit is never discarded as a success."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE timed_entitlements SET discord_missing_since = %s "
            "WHERE entitlement_id = %s AND status = 'active'",
            (datetime.now(timezone.utc).isoformat() if missing else None,
             int(entitlement_id)),
        )


def get_expired_entitlements(timestamp: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT e.entitlement_id, e.guild_id, e.user_id, e.entitlement_key, "
            "e.discord_item_id, COALESCE(e.source_type, v.source_type) "
            "FROM timed_entitlements e LEFT JOIN reward_vouchers v "
            "ON v.voucher_id = e.source_voucher_id "
            "WHERE e.status = 'active' AND e.expires_at <= %s",
            (timestamp,),
        ).fetchall()
    keys = (
        "entitlement_id", "guild_id", "user_id", "entitlement_key",
        "discord_item_id", "source_type",
    )
    return [dict(zip(keys, row)) for row in rows]


def expire_entitlement(entitlement_id: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE timed_entitlements SET status = 'expired' "
            "WHERE entitlement_id = %s AND status = 'active'", (int(entitlement_id),)
        )


def delete_rental_for_item(guild_id: int, item_type: str, discord_item_id: str):
    """Remove a schema-5 duplicate rental row after entitlement cleanup."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM rented_items WHERE guild_id = %s AND item_type = %s "
            "AND discord_item_id = %s",
            (int(guild_id), item_type, str(discord_item_id)),
        )


def consume_inventory_item(guild_id: int, user_id: int, item_key: str) -> bool:
    with get_connection() as conn:
        changed = conn.execute(
            "UPDATE user_inventory SET quantity = quantity - 1, updated_at = %s "
            "WHERE guild_id = %s AND user_id = %s AND item_key = %s AND quantity > 0",
            (datetime.now(timezone.utc).isoformat(), int(guild_id), int(user_id), item_key),
        ).rowcount
    return changed == 1


# A custom item is written once, in whatever language the guild speaks.
#
# This briefly held two languages, on the reasoning that every built-in has both.
# That was the wrong analogy: a built-in ships to every installation and must
# therefore read in each of them, while a custom item exists only in one guild's
# own database and is read only by that guild's members. A server with two main
# languages would use English for both rather than maintain two columns, so the
# second field was work with no reader. The row keeps its `language` column, so
# nothing has to migrate and a per-guild language later costs nothing.
CUSTOM_ITEM_LANGUAGE = "hu"


def get_shop_item_definitions(guild_id: int, language: str = None) -> list[dict]:
    """Every custom item with its operator-authored name and description.

    ``language`` is accepted and ignored: a custom item has exactly one text,
    whatever language its operator wrote it in. The parameter stays so callers
    need not change if a guild language is ever added.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT item_key, template_type, enabled, price, config_json, revision, "
            "category FROM shop_item_definitions WHERE guild_id = %s ORDER BY item_key",
            (int(guild_id),),
        ).fetchall()
        localized = conn.execute(
            "SELECT item_key, language, name, description "
            "FROM shop_item_localizations WHERE guild_id = %s", (int(guild_id),)
        ).fetchall()
    texts: dict[str, dict[str, dict]] = {}
    for item_key, lang, name, description in localized:
        texts.setdefault(item_key, {})[lang] = {"name": name,
                                                "description": description}
    items = []
    for row in rows:
        stored = texts.get(row[0], {})
        # The stored language if it is there, otherwise whatever single row this
        # item has — an item written before the column meant anything still reads.
        written = stored.get(CUSTOM_ITEM_LANGUAGE) or next(iter(stored.values()), {})
        config_value = json.loads(row[4])
        items.append({
            "item_key": row[0], "template_type": row[1], "enabled": bool(row[2]),
            "price": row[3], "config": config_value, "revision": row[5],
            "name": written.get("name"),
            "description": written.get("description"),
            # Both, named so they cannot be confused: `category_stored` is the
            # raw column, which the editor round-trips so "Automatic" survives a
            # save, and `category` is the shelf the menu actually uses.
            "category_stored": row[6],
            "category": item_catalog.resolve_custom_category(
                row[1], config_value, row[6]),
        })
    return items


def _shop_section_usage(conn, guild_id: int) -> dict[str, int]:
    """How many custom items each shelf already holds, on the caller's
    connection.

    The category is resolved in Python rather than in SQL because `COALESCE`
    cannot express the default: it depends on `template_type` and, for a
    consumable, on the item it wraps. A guild's rows are few, so reading them and
    resolving keeps one implementation of the rule.
    """
    usage: dict[str, int] = {}
    rows = conn.execute(
        "SELECT template_type, config_json, category FROM shop_item_definitions "
        "WHERE guild_id = %s", (int(guild_id),)
    ).fetchall()
    for template_type, config_json, category in rows:
        try:
            config_value = json.loads(config_json)
        except (TypeError, ValueError):
            config_value = {}
        shelf = item_catalog.resolve_custom_category(
            template_type, config_value, category)
        usage[shelf] = usage.get(shelf, 0) + 1
    return usage


def _hidden_shop_items(conn, guild_id: int) -> list[str]:
    """This guild's hidden built-in keys, read on the caller's connection.

    Not through `settings_cache`: a cap is a write-side correctness boundary and
    must see the same snapshot as the count and the insert.
    """
    row = conn.execute(
        "SELECT value_json FROM guild_settings "
        "WHERE guild_id = %s AND setting_key = 'shop_hidden_items'",
        (int(guild_id),)
    ).fetchone()
    if row is None:
        return []
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    return [key for key in value if isinstance(key, str)] if isinstance(value, list) else []


def _assert_section_has_room(conn, guild_id: int, shelf: str,
                             usage: dict[str, int]) -> None:
    hidden = _hidden_shop_items(conn, guild_id)
    capacity = item_catalog.custom_item_capacity(shelf, hidden)
    if usage.get(shelf, 0) >= capacity:
        conn.rollback()
        raise ValidationError("shop_item_limit", "shop section is full",
                              limit=capacity, category=shelf)


def guild_item_values(guild_id: int) -> dict:
    """This guild's overrides for the built-in item mechanics.

    Read through the settings cache like every other setting, so a dashboard
    change is visible immediately and a game does not open a connection per
    round. Falls back to no overrides on any failure: a mechanic reverting to its
    shipped number is a game that still works, where an exception inside a
    settlement transaction is not.
    """
    try:
        from core import settings_cache

        value = settings_cache.setting(int(guild_id), "shop_item_values")
    except Exception:
        db_logger.exception("Could not read item value overrides (guild=%s)",
                            guild_id)
        return {}
    return value if isinstance(value, dict) else {}


def _assert_unhide_has_room(conn, guild_id: int, previous, value) -> None:
    """Refuse to un-hide a built-in whose shelf has no room for it.

    Un-hiding is the one way to overfill a section without creating anything:
    hide the sound rental, fill Rentals with custom items, un-hide it, and the
    shelf holds 26. The render trim would then drop one silently, and a silent
    trim is worse than a refusal an operator can read and act on — they can
    delete a custom item, or leave the built-in hidden, and either way they chose
    it.

    Only a *removal* is checked. Hiding always frees a slot, and saving the same
    list unchanged must never refuse.
    """
    was_hidden = set(previous or ())
    now_hidden = set(value or ())
    restored = was_hidden - now_hidden
    if not restored:
        return
    usage = _shop_section_usage(conn, guild_id)
    for key in sorted(restored):
        definition = item_catalog.SHOP_ITEMS.get(key)
        if definition is None:
            continue
        shelf = definition.category.value
        capacity = item_catalog.custom_item_capacity(shelf, now_hidden)
        if usage.get(shelf, 0) > capacity:
            raise ValidationError(
                "shop_unhide_no_room", "shop section has no room to restore",
                item=key, category=shelf, limit=capacity)


def create_shop_item_definition(guild_id: int, actor_id: int, item: dict) -> dict:
    """Create one custom shop item, refusing a full section.

    Shop items were the one managed thing this module could not create: the
    insert *and* the cap check lived in the Flask route, so the count and the
    write were not one transaction and two concurrent creates could both pass a
    cap with one slot left. `create_gacha_banner` has always done it this way for
    `GACHA_BANNER_LIMIT`, and the per-section cap makes it necessary rather than
    merely tidier — it has to read the hidden list, count the rows and insert
    against one snapshot.
    """
    item_key = item["item_key"]
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM shop_item_definitions "
                "WHERE guild_id = %s AND item_key = %s", (int(guild_id), item_key)
            ).fetchone()
            if existing:
                conn.rollback()
                raise ValidationError("shop_item_exists", "item key already exists",
                                      item=item_key)
            shelf = item_catalog.resolve_custom_category(
                item["template_type"], item["config"], item.get("category"))
            _assert_section_has_room(
                conn, guild_id, shelf, _shop_section_usage(conn, guild_id))
            conn.execute(
                "INSERT INTO shop_item_definitions (guild_id, item_key, "
                "template_type, category, enabled, price, config_json, revision, "
                "updated_by, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)",
                (int(guild_id), item_key, item["template_type"],
                 item.get("category"), int(item["enabled"]), int(item["price"]),
                 json.dumps(item["config"], sort_keys=True), int(actor_id),
                 timestamp),
            )
            conn.execute(
                "INSERT INTO shop_item_localizations (guild_id, item_key, language, "
                "name, description) VALUES (%s, %s, %s, %s, %s)",
                (int(guild_id), item_key, CUSTOM_ITEM_LANGUAGE,
                 item["text"]["name"], item["text"]["description"]),
            )
            # The bare key, matching `shop_item.update` and `shop_item.delete`:
            # a prefixed target here would split one item's audit trail in two.
            write_settings_audit(
                conn, int(guild_id), actor_id, "shop_item.create", item_key,
                None,
                {"template_type": item["template_type"], "price": item["price"],
                 "category": shelf},
            )
            conn.commit()
    except EngineError as exc:
        db_logger.exception("Shop item creation failed (guild=%s, item=%s)",
                            guild_id, item_key)
        raise DatabaseOperationError("shop item creation failed") from exc
    return {"item_key": item_key, "revision": 1, "category": shelf}


def update_shop_item_definition(guild_id: int, actor_id: int, item_key: str,
                                item: dict, expected_revision: int) -> dict:
    """Edit one custom item under an optimistic revision.

    The stable key is not editable: inventory rows, purchases and pull history
    all reference it, so a rename would orphan them.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT template_type, enabled, price, config_json, revision, "
                "category FROM shop_item_definitions "
                "WHERE guild_id = %s AND item_key = %s",
                (int(guild_id), item_key),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError("shop item not found")
            if int(expected_revision) != row[4]:
                raise RevisionConflictError("shop item revision conflict")
            revision = row[4] + 1
            previous_config = json.loads(row[3])
            previous = {"template_type": row[0], "enabled": bool(row[1]),
                        "price": row[2], "config": previous_config,
                        "category": row[5]}
            # An edit cannot change the item *count*, so creation's cap used to
            # be enough — but it can now move an item onto a different shelf,
            # which makes the cap bypassable in two steps: create in an empty
            # section, then edit across into a full one. Only a move is checked,
            # so saving an item that is already there never refuses.
            was = item_catalog.resolve_custom_category(
                row[0], previous_config, row[5])
            shelf = item_catalog.resolve_custom_category(
                item["template_type"], item["config"], item.get("category"))
            if shelf != was:
                usage = _shop_section_usage(conn, guild_id)
                usage[was] = max(0, usage.get(was, 0) - 1)
                _assert_section_has_room(conn, guild_id, shelf, usage)
            conn.execute(
                "UPDATE shop_item_definitions SET template_type = %s, category = %s, "
                "enabled = %s, price = %s, config_json = %s, revision = %s, "
                "updated_by = %s, updated_at = %s "
                "WHERE guild_id = %s AND item_key = %s",
                (item["template_type"], item.get("category"),
                 int(item["enabled"]), int(item["price"]),
                 json.dumps(item["config"], sort_keys=True), revision,
                 int(actor_id), timestamp, int(guild_id), item_key),
            )
            conn.execute(
                "INSERT INTO shop_item_localizations "
                "(guild_id, item_key, language, name, description) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT(guild_id, item_key, language) DO UPDATE SET "
                "name = excluded.name, description = excluded.description",
                (int(guild_id), item_key, CUSTOM_ITEM_LANGUAGE,
                 item["text"]["name"], item["text"]["description"]),
            )
            write_settings_audit(conn, int(guild_id), int(actor_id),
                                 "shop_item.update", item_key, previous, item)
            conn.commit()
            return {"item_key": item_key, "revision": revision}
    except (DatabaseOperationError, LookupError):
        raise
    except EngineError as exc:
        db_logger.exception("Shop item update failed (guild=%s, item=%s)", guild_id, item_key)
        raise DatabaseOperationError("shop item update failed") from exc


def delete_shop_item_definition(guild_id: int, actor_id: int, item_key: str,
                                expected_revision: int) -> dict:
    """Remove one custom item and its localizations under a revision check."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT template_type, enabled, price, config_json, revision "
                "FROM shop_item_definitions WHERE guild_id = %s AND item_key = %s",
                (int(guild_id), item_key),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError("shop item not found")
            if int(expected_revision) != row[4]:
                raise RevisionConflictError("shop item revision conflict")
            previous = {"template_type": row[0], "enabled": bool(row[1]),
                        "price": row[2], "config": json.loads(row[3])}
            conn.execute(
                "DELETE FROM shop_item_localizations WHERE guild_id = %s AND item_key = %s",
                (int(guild_id), item_key),
            )
            conn.execute(
                "DELETE FROM shop_item_definitions WHERE guild_id = %s AND item_key = %s",
                (int(guild_id), item_key),
            )
            write_settings_audit(conn, int(guild_id), int(actor_id),
                                 "shop_item.delete", item_key, previous, None)
            conn.commit()
            return {"item_key": item_key}
    except (DatabaseOperationError, LookupError):
        raise
    except EngineError as exc:
        db_logger.exception("Shop item deletion failed (guild=%s, item=%s)", guild_id, item_key)
        raise DatabaseOperationError("shop item deletion failed") from exc


def delete_dashboard_document(guild_id: int, actor_id: int, document_id: int,
                              expected_revision: int) -> dict:
    """Remove one builder draft under a revision check.

    Already-published messages are untouched: publishing copies the content into
    Discord, so deleting the draft only removes the template.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT document_type, name, content_json, revision FROM dashboard_documents "
                "WHERE guild_id = %s AND document_id = %s",
                (int(guild_id), int(document_id)),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError("document not found")
            if int(expected_revision) != row[3]:
                raise RevisionConflictError("document revision conflict")
            previous = {"document_type": row[0], "name": row[1],
                        "content": json.loads(row[2])}
            conn.execute(
                "DELETE FROM dashboard_documents WHERE guild_id = %s AND document_id = %s",
                (int(guild_id), int(document_id)),
            )
            write_settings_audit(conn, int(guild_id), int(actor_id),
                                 "builder.delete", row[1], previous, None)
            conn.commit()
            return {"document_id": int(document_id)}
    except (DatabaseOperationError, LookupError):
        raise
    except EngineError as exc:
        db_logger.exception("Document deletion failed (guild=%s, document=%s)",
                            guild_id, document_id)
        raise DatabaseOperationError("document deletion failed") from exc


def purchase_custom_shop_item(guild_id: int, user_id: int, item_key: str) -> dict:
    """Purchase one dashboard-defined item using only approved templates."""
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT template_type, enabled, price, config_json FROM shop_item_definitions "
            "WHERE guild_id = %s AND item_key = %s", (int(guild_id), item_key)
        ).fetchone()
        if not row or not row[1]:
            conn.rollback()
            return {"purchased": False, "reason": "unavailable"}
        template, _, price, config_value = row[0], row[1], row[2], json.loads(row[3])
        _ensure_user(conn, int(user_id), timestamp)
        if template == "vault":
            reserve = int(config_value["amount"])
            current = conn.execute(
                "SELECT protected_reserve FROM users WHERE user_id = %s", (int(user_id),)
            ).fetchone()[0]
            if current >= reserve:
                conn.rollback()
                return {"purchased": False, "reason": "already_owned"}
        if conn.execute(
            "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
            (price, int(user_id), price),
        ).rowcount != 1:
            conn.rollback()
            return {"purchased": False, "reason": "insufficient_funds"}
        # The charged amount travels with the result so a compensating rollback
        # refunds exactly this debit rather than a price snapshotted elsewhere.
        result = {"purchased": True, "template_type": template,
                  "config": config_value, "price": price}
        if template == "consumable":
            conn.execute(
                "INSERT INTO user_inventory (guild_id, user_id, item_key, quantity, updated_at) "
                "VALUES (%s, %s, %s, 1, %s) ON CONFLICT(guild_id, user_id, item_key) "
                "DO UPDATE SET quantity = user_inventory.quantity + 1, updated_at = excluded.updated_at",
                (int(guild_id), int(user_id), config_value["item_key"], timestamp),
            )
        elif template == "vault":
            conn.execute("UPDATE users SET protected_reserve = %s WHERE user_id = %s",
                         (int(config_value["amount"]), int(user_id)))
        elif template == "coin_bundle":
            conn.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s",
                         (int(config_value["amount"]), int(user_id)))
        elif template == "fulfillment_voucher":
            voucher_id = secrets.token_urlsafe(12)
            reward_key = f"{config_value['asset_type']}_{int(config_value['duration_days'])}d"
            conn.execute(
                "INSERT INTO reward_vouchers "
                "(voucher_id, guild_id, user_id, reward_key, source_type, "
                "duration_days, acquired_at) VALUES (%s, %s, %s, %s, 'shop', %s, %s)",
                (voucher_id, int(guild_id), int(user_id), reward_key,
                 int(config_value["duration_days"]), timestamp),
            )
            result["voucher_id"] = voucher_id
        elif template == "timed_role":
            entitlement_key = f"role:{int(config_value['role_id'])}"
            active = conn.execute(
                "SELECT entitlement_id, expires_at FROM timed_entitlements "
                "WHERE guild_id = %s AND user_id = %s AND entitlement_key = %s "
                "AND status = 'active' ORDER BY expires_at DESC, entitlement_id LIMIT 1",
                (int(guild_id), int(user_id), entitlement_key),
            ).fetchone()
            base = datetime.now(timezone.utc)
            if active:
                current_expiry = parse_stored(active[1])
                if current_expiry > base:
                    base = current_expiry
            expires = base + timedelta(days=int(config_value["duration_days"]))
            if active:
                conn.execute(
                    "UPDATE timed_entitlements SET expires_at = %s WHERE entitlement_id = %s",
                    (expires.isoformat(), active[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO timed_entitlements "
                    "(guild_id, user_id, entitlement_key, starts_at, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (int(guild_id), int(user_id), entitlement_key, timestamp, expires.isoformat()),
                )
            result["expires_at"] = expires.isoformat()
        balance = conn.execute("SELECT balance FROM users WHERE user_id = %s", (int(user_id),)).fetchone()[0]
        conn.commit()
        result["balance"] = balance
        return result


def rollback_custom_role_purchase(guild_id: int, user_id: int, charged_price: int,
                                  template_type: str, role_id: int,
                                  duration_days: int | None = None):
    """Compensate a role purchase when Discord refuses the role side effect.

    Both the refund amount and the granted duration come from the purchase that
    actually happened, so an administrator editing the item definition between
    the debit and the rollback cannot change what is returned.
    """
    with get_connection() as conn:
        conn.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s",
                     (int(charged_price), int(user_id)))
        if template_type == "timed_role" and duration_days is not None:
            active = conn.execute(
                "SELECT entitlement_id, expires_at FROM timed_entitlements WHERE guild_id = %s "
                "AND user_id = %s AND entitlement_key = %s AND status = 'active' "
                "ORDER BY expires_at DESC, entitlement_id LIMIT 1",
                (int(guild_id), int(user_id), f"role:{int(role_id)}"),
            ).fetchone()
            if active:
                restored = parse_stored(active[1]) - timedelta(days=int(duration_days))
                if restored <= datetime.now(timezone.utc) + timedelta(minutes=1):
                    conn.execute("DELETE FROM timed_entitlements WHERE entitlement_id = %s", (active[0],))
                else:
                    conn.execute("UPDATE timed_entitlements SET expires_at = %s WHERE entitlement_id = %s",
                                 (restored.isoformat(), active[0]))
        conn.commit()


MANAGED_MESSAGE_KINDS = ("role_menu", "rules", "ticket", "airlock", "embed")

# Discord renders at most 25 components on one message, so a role menu cannot
# hold more buttons than that. Derived from the platform, not chosen.
MANAGED_ENTRY_LIMIT = 25


def _managed_row(row, entries) -> dict:
    return {"kind": row[0], "menu_key": row[1], "display_name": row[2],
            "channel_id": str(row[3]) if row[3] else None,
            "message_id": str(row[4]) if row[4] else None,
            "title": row[5], "body": row[6], "colour": row[7],
            "options": json.loads(row[8]), "revision": row[9],
            "updated_at": row[10], "entries": entries,
            "posted": row[4] is not None}


def list_managed_messages(guild_id: int, kind: str = None) -> list[dict]:
    """Every managed message a guild has, with its entries.

    Ids are strings for the same reason every other id is: a snowflake is 64-bit
    and a browser number holds 53 bits exactly.
    """
    with get_connection() as conn:
        if kind is None:
            rows = conn.execute(
                "SELECT kind, menu_key, display_name, channel_id, message_id, "
                "title, body, colour, options_json, revision, updated_at "
                "FROM managed_messages WHERE guild_id = %s ORDER BY kind, menu_key",
                (int(guild_id),)).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, menu_key, display_name, channel_id, message_id, "
                "title, body, colour, options_json, revision, updated_at "
                "FROM managed_messages WHERE guild_id = %s AND kind = %s "
                "ORDER BY menu_key", (int(guild_id), kind)).fetchall()
        entries: dict[tuple[str, str], list[dict]] = {}
        for row in conn.execute(
            "SELECT kind, menu_key, label, role_id, emoji FROM "
            "managed_message_entries WHERE guild_id = %s ORDER BY position, entry_id",
            (int(guild_id),),
        ):
            entries.setdefault((row[0], row[1]), []).append(
                {"label": row[2],
                 "role_id": str(row[3]) if row[3] else None,
                 "emoji": row[4]})
    return [_managed_row(row, entries.get((row[0], row[1]), [])) for row in rows]


def get_managed_message(guild_id: int, kind: str, menu_key: str) -> dict | None:
    for message in list_managed_messages(guild_id, kind):
        if message["menu_key"] == menu_key:
            return message
    return None


def save_managed_message(guild_id: int, actor_id: int, kind: str, menu_key: str,
                         display_name: str, expected_revision: int, *,
                         title: str = None, body: str = None,
                         colour: int = None, options: dict = None,
                         entries: list = None) -> dict:
    """Create or update one managed message and its entries in one transaction.

    The entries are replaced wholesale rather than diffed: a role menu is a short
    ordered list, and rewriting it is what makes `position` mean the button order
    without a reordering protocol. `message_id` and `channel_id` are **not**
    touched here — where a message was posted is a fact about Discord, recorded
    by `record_managed_post` when a post actually succeeds, and an edit to the
    content must not silently claim the message moved.
    """
    if kind not in MANAGED_MESSAGE_KINDS:
        raise ValidationError("managed_kind_invalid", "unknown managed message kind")
    if not isinstance(menu_key, str) or not _GACHA_BANNER_KEY.fullmatch(menu_key):
        raise ValidationError("managed_key_invalid", "managed message key is invalid")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValidationError("managed_name_invalid", "a managed message needs a name")
    entries = list(entries or [])
    if len(entries) > MANAGED_ENTRY_LIMIT:
        raise ValidationError("managed_entry_limit",
                              "too many entries for one message")
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT revision FROM managed_messages "
                "WHERE guild_id = %s AND kind = %s AND menu_key = %s",
                (int(guild_id), kind, menu_key)).fetchone()
            if row:
                if int(expected_revision) != row[0]:
                    conn.rollback()
                    raise RevisionConflictError("managed message revision conflict")
                revision = row[0] + 1
                conn.execute(
                    "UPDATE managed_messages SET display_name = %s, title = %s, "
                    "body = %s, colour = %s, options_json = %s, revision = %s, "
                    "updated_by = %s, updated_at = %s "
                    "WHERE guild_id = %s AND kind = %s AND menu_key = %s",
                    (display_name.strip(), title, body, colour,
                     json.dumps(options or {}, sort_keys=True), revision,
                     int(actor_id), timestamp, int(guild_id), kind, menu_key))
            else:
                if expected_revision not in (None, 0):
                    conn.rollback()
                    raise RevisionConflictError("managed message revision conflict")
                revision = 1
                conn.execute(
                    "INSERT INTO managed_messages (guild_id, kind, menu_key, "
                    "display_name, title, body, colour, options_json, revision, "
                    "updated_by, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s)",
                    (int(guild_id), kind, menu_key, display_name.strip(), title,
                     body, colour, json.dumps(options or {}, sort_keys=True),
                     int(actor_id), timestamp))
            conn.execute(
                "DELETE FROM managed_message_entries "
                "WHERE guild_id = %s AND kind = %s AND menu_key = %s",
                (int(guild_id), kind, menu_key))
            for position, entry in enumerate(entries):
                label = str(entry.get("label", "")).strip()
                if not label:
                    conn.rollback()
                    raise ValidationError("managed_entry_label",
                                          "every entry needs a label")
                role_id = entry.get("role_id")
                conn.execute(
                    "INSERT INTO managed_message_entries (guild_id, kind, "
                    "menu_key, position, label, role_id, emoji) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (int(guild_id), kind, menu_key, position, label,
                     int(role_id) if role_id else None,
                     str(entry.get("emoji") or "")))
            write_settings_audit(conn, int(guild_id), actor_id,
                                 "managed_message.save", f"{kind}:{menu_key}",
                                 None, {"entries": len(entries)})
            conn.commit()
            return {"revision": revision}
    except (DatabaseOperationError, RevisionConflictError, ValidationError):
        raise
    except EngineError as exc:
        db_logger.exception("Managed message save failed (guild=%s)", guild_id)
        raise DatabaseOperationError("managed message save failed") from exc


def record_managed_post(guild_id: int, kind: str, menu_key: str,
                        channel_id, message_id) -> None:
    """Remember where a managed message was posted.

    Separate from the content save because it records what Discord did, not what
    an operator typed — and because both the dashboard's publish worker and the
    bot's own `/setup_*` commands call it, which is what stops the two paths from
    disagreeing about which message is the live one.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE managed_messages SET channel_id = %s, message_id = %s "
            "WHERE guild_id = %s AND kind = %s AND menu_key = %s",
            (int(channel_id) if channel_id else None,
             int(message_id) if message_id else None,
             int(guild_id), kind, menu_key))


def delete_managed_message(guild_id: int, actor_id: int, kind: str,
                           menu_key: str, expected_revision: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT revision, channel_id, message_id FROM managed_messages "
            "WHERE guild_id = %s AND kind = %s AND menu_key = %s",
            (int(guild_id), kind, menu_key)).fetchone()
        if row is None:
            conn.rollback()
            return None
        if int(expected_revision) != row[0]:
            conn.rollback()
            raise RevisionConflictError("managed message revision conflict")
        conn.execute("DELETE FROM managed_message_entries "
                     "WHERE guild_id = %s AND kind = %s AND menu_key = %s",
                     (int(guild_id), kind, menu_key))
        conn.execute("DELETE FROM managed_messages "
                     "WHERE guild_id = %s AND kind = %s AND menu_key = %s",
                     (int(guild_id), kind, menu_key))
        write_settings_audit(conn, int(guild_id), actor_id,
                             "managed_message.delete", f"{kind}:{menu_key}")
        conn.commit()
        # The caller deletes the Discord message; it needs to know which.
        return {"channel_id": str(row[1]) if row[1] else None,
                "message_id": str(row[2]) if row[2] else None}


def save_dashboard_document(guild_id: int, actor_id: int, document_type: str,
                            name: str, content: dict, expected_revision: int = 0) -> dict:
    if document_type not in {"embed", "rules", "panel"} or not name.strip():
        raise ValueError("invalid dashboard document")
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT document_id, revision FROM dashboard_documents "
            "WHERE guild_id = %s AND document_type = %s AND name = %s",
            (int(guild_id), document_type, name.strip()),
        ).fetchone()
        if row:
            if expected_revision != row[1]:
                raise RevisionConflictError("document revision conflict")
            revision = row[1] + 1
            conn.execute(
                "UPDATE dashboard_documents SET content_json = %s, revision = %s, "
                "updated_by = %s, updated_at = %s WHERE document_id = %s",
                (json.dumps(content, sort_keys=True), revision, int(actor_id), timestamp, row[0]),
            )
            document_id = row[0]
        else:
            if expected_revision not in (0, None):
                raise RevisionConflictError("document revision conflict")
            revision = 1
            document_id = conn.execute(
                "INSERT INTO dashboard_documents "
                "(guild_id, document_type, name, content_json, revision, updated_by, updated_at) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s) RETURNING document_id",
                (int(guild_id), document_type, name.strip(),
                 json.dumps(content, sort_keys=True), int(actor_id), timestamp),
            ).fetchone()[0]
        conn.commit()
    return {"document_id": document_id, "revision": revision}


def list_dashboard_documents(guild_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT document_id, document_type, name, content_json, revision, updated_at "
            "FROM dashboard_documents WHERE guild_id = %s ORDER BY document_type, name",
            (int(guild_id),),
        ).fetchall()
    return [{"document_id": row[0], "document_type": row[1], "name": row[2],
             "content": json.loads(row[3]), "revision": row[4], "updated_at": row[5]}
            for row in rows]


def queue_control_action(guild_id: int, actor_id: int, action_type: str,
                         payload: dict) -> int:
    # `publish_rules`, `publish_panel` and `send_embed` are gone: each published
    # a `dashboard_documents` draft and discarded the message id, so nothing
    # could be updated afterwards. `publish_managed` posts *or* edits a
    # `managed_messages` row, which is what "add a role and press update" needs.
    # A row of a retired type left in an older queue settles as unsupported
    # rather than being retried forever.
    if action_type not in {"publish_managed", "delete_managed", "erase_member",
                          "repair_channel_permission", "notify_redeem_fulfilled",
                          "revoke_entitlement", "test_announcement"}:
        raise ValueError("unsupported control action")
    with get_connection() as conn:
        action_id = conn.execute(
            "INSERT INTO control_actions "
            "(guild_id, actor_id, action_type, payload_json, created_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING action_id",
            (int(guild_id), int(actor_id), action_type, json.dumps(payload, sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        ).fetchone()[0]
    return action_id


def get_control_action(guild_id: int, action_id: int) -> dict | None:
    """Return one queued action's progress for the guild that created it."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT action_id, action_type, status, error_code, created_at, "
            "started_at, completed_at FROM control_actions "
            "WHERE guild_id = %s AND action_id = %s",
            (int(guild_id), int(action_id)),
        ).fetchone()
    if row is None:
        return None
    keys = ("action_id", "action_type", "status", "error_code",
            "created_at", "started_at", "completed_at")
    return dict(zip(keys, row))


def prune_control_actions(max_age_days: int = 30) -> int:
    """Delete settled actions past the retention window.

    Nothing removed these rows before, so the outbox grew for the installation's
    lifetime. Only terminal states are eligible; pending and running rows stay.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(max_age_days))).isoformat()
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM control_actions WHERE status IN "
            "('completed', 'failed', 'cancelled') AND "
            "COALESCE(completed_at, started_at, created_at) < %s",
            (cutoff,),
        ).rowcount


def renew_control_action_lease(action_id: int) -> None:
    """Extend a running action's lease so a slow publish is not re-queued."""
    expires = (datetime.now(timezone.utc)
               + timedelta(seconds=CONTROL_ACTION_LEASE_SECONDS)).isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE control_actions SET lease_expires_at = %s "
            "WHERE action_id = %s AND status = 'running'",
            (expires, int(action_id)),
        )


def claim_control_action() -> dict | None:
    """Claim the oldest action so only one bot worker can execute it."""
    with get_connection() as conn:
        now = datetime.now(timezone.utc)
        # Only reclaim an action whose lease actually lapsed. Re-queueing on
        # elapsed time alone meant a slow multi-section publish was executed
        # twice, because the first worker's Discord sends had already happened.
        conn.execute(
            "UPDATE control_actions SET status = 'pending', started_at = NULL, "
            "lease_expires_at = NULL WHERE status = 'running' AND "
            "COALESCE(lease_expires_at, started_at, created_at) < %s",
            (now.isoformat(),),
        )
        row = conn.execute(
            "SELECT action_id, guild_id, actor_id, action_type, payload_json "
            "FROM control_actions WHERE status = 'pending' ORDER BY action_id LIMIT 1"
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        lease = (now + timedelta(seconds=CONTROL_ACTION_LEASE_SECONDS)).isoformat()
        if conn.execute(
            "UPDATE control_actions SET status = 'running', started_at = %s, "
            "lease_expires_at = %s WHERE action_id = %s AND status = 'pending'",
            (now.isoformat(), lease, row[0]),
        ).rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
    return {"action_id": row[0], "guild_id": row[1], "actor_id": row[2],
            "action_type": row[3], "payload": json.loads(row[4])}


def finish_control_action(action_id: int, success: bool, error_code: str | None = None):
    with get_connection() as conn:
        conn.execute(
            "UPDATE control_actions SET status = %s, completed_at = %s, error_code = %s "
            "WHERE action_id = %s AND status = 'running'",
            ("completed" if success else "failed", datetime.now(timezone.utc).isoformat(),
             error_code, int(action_id)),
        )


# ==========================================
# Member data export, erasure and retention
# ==========================================
#
# ``docs/privacy.md`` requires authenticated export and deletion that preserves the
# minimum audit record, anonymises references where possible, and never silently
# alters financial totals. Deletion here is therefore an *anonymisation*: personal
# and behavioural rows are deleted outright, while the economy rows are re-keyed to
# a tombstone identifier so the installation's coin supply is provably unchanged and
# every idempotency guard that stops a repeat payout survives.
#
# The three tables below are the single source of truth for where a member appears.
# Export, erasure and the coverage test all read them, so a new per-user table
# cannot be added to one and forgotten in the others.

EXPORT_ROW_LIMIT = 5000

# What ``remove_warning`` writes into settings_audit.old_value_json. The subject's
# id and the reason text live inside that payload, so an erasure has to rewrite it
# and an export has to return it. Declared before SUBJECT_TABLES, which quotes it.
_AUDIT_SUBJECT_ACTIONS = ("warning.delete",)
_AUDIT_SUBJECT_ACTIONS_SQL = ", ".join(f"'{action}'" for action in _AUDIT_SUBJECT_ACTIONS)

# (table, WHERE clause, number of times the subject id is bound)
#
# A member's id inside a JSON column is matched in Python, not by the engine's
# JSON functions, which SQLite and PostgreSQL spell differently. The clause
# narrows the rows with a LIKE on the id's digits, and SUBJECT_ROW_FILTERS below
# makes the exact match: a LIKE alone matches every longer id that contains the
# member's, and a malformed payload must not turn the whole export into an error.
SUBJECT_TABLES = (
    ("users", "user_id = %s", 1),
    ("scoped_accounts", "user_id = %s", 1),
    ("casino_wagers", "user_id = %s", 1),
    ("reward_claims", "user_id = %s", 1),
    ("user_inventory", "user_id = %s", 1),
    ("gacha_pity", "user_id = %s", 1),
    ("gacha_pulls", "user_id = %s", 1),
    ("reward_vouchers", "user_id = %s", 1),
    # The subject may be the recipient or the staff member who manually granted
    # someone else's entitlement, and both are personal data about them --
    # same reasoning as fulfillment_requests just below.
    ("timed_entitlements", "user_id = %s OR granted_by = %s", 2),
    # The subject may be the requester or the operator who fulfilled someone else's
    # request, and both are personal data about them.
    ("fulfillment_requests", "user_id = %s OR completed_by = %s", 2),
    ("warnings", "user_id = %s OR mod_id = %s", 2),
    ("tickets", "opener_id = %s OR claimer_id = %s", 2),
    ("voice_settings", "user_id = %s", 1),
    ("voice_permissions", "owner_id = %s OR target_id = %s", 2),
    ("active_channels", "owner_id = %s", 1),
    ("user_identities", "user_id = %s", 1),
    ("user_sharing_preferences", "user_id = %s", 1),
    ("activity_events", "user_id = %s", 1),
    ("dashboard_access_invalidations", "user_id = %s", 1),
    # The rolling anti-abuse counter is a record of the member's own play.
    ("casino_earnings_window", "user_id = %s", 1),
    # Posts the member hosts, and posts they joined: membership is a JSON list
    # of ids inside the host's row.
    ("lfg_posts", "host_id = %s OR joined_json LIKE ('%%' || %s || '%%')", 2),
    # Whose turn the counting or word chain last accepted.
    ("minigame_state", "last_user_id = %s", 1),
    # Changes the member made as an administrator, and a removed warning's
    # payload naming them as its subject or as the moderator who issued it.
    ("settings_audit",
     f"actor_id = %s OR (action IN ({_AUDIT_SUBJECT_ACTIONS_SQL}) "
     "AND old_value_json LIKE ('%%' || %s || '%%'))",
     2),
    # Outbox rows the member queued as an operator, and rows whose payload is
    # about them: an erasure, or the revocation of one of their grants.
    ("control_actions", "actor_id = %s OR payload_json LIKE ('%%' || %s || '%%')", 2),
)


def _parsed_json(text):
    """A JSON column's value, or None for NULL or malformed text -- the rows
    SQLite's json_valid() used to exclude."""
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _json_is_id(value, user_id: int) -> bool:
    """Whether a JSON value is the member's id. Numerically, as SQLite compared
    a JSON number with a bound integer; a string spelling of the id never
    matched there and does not match here, and neither does a boolean (SQLite
    read ``true`` as member 1).

    The LIKE prefilter finds an id by its decimal digits, so a number written in
    exponent notation would be missed. Nothing writes one: every id in these
    columns is ``json.dumps`` of a Python int (measured against the old clauses,
    2026-10-31; docs/subsystems/database.md)."""
    return (not isinstance(value, (str, bool))) and value == user_id


def _lfg_row_names(row: dict, user_id: int) -> bool:
    """The member hosts the post, or is in its joined list. A JSON object's
    values and a lone scalar count too, as json_each() iterated them."""
    if row["host_id"] == user_id:
        return True
    joined = _parsed_json(row["joined_json"])
    members = (joined.values() if isinstance(joined, dict)
               else joined if isinstance(joined, list) else (joined,))
    return any(_json_is_id(member, user_id) for member in members)


def _audit_row_names(row: dict, user_id: int) -> bool:
    """The member made the change, or a removed warning names them as its
    subject or as the moderator who issued it."""
    if row["actor_id"] == user_id:
        return True
    payload = _parsed_json(row["old_value_json"])
    return (row["action"] in _AUDIT_SUBJECT_ACTIONS and isinstance(payload, dict)
            and (_json_is_id(payload.get("user_id"), user_id)
                 or _json_is_id(payload.get("moderator_id"), user_id)))


def _control_action_row_names(row: dict, user_id: int) -> bool:
    """The member queued the action, or its payload is about them."""
    if row["actor_id"] == user_id:
        return True
    payload = _parsed_json(row["payload_json"])
    return isinstance(payload, dict) and _json_is_id(payload.get("user_id"), user_id)


# The exact match for a SUBJECT_TABLES clause that only narrows the rows. A
# table listed here is read without the export's LIMIT, which applies after the
# filter, so a crowd of near-miss ids cannot push the member's own rows out.
SUBJECT_ROW_FILTERS = {
    "lfg_posts": _lfg_row_names,
    "settings_audit": _audit_row_names,
    "control_actions": _control_action_row_names,
}

# Deleted outright, children before parents. Foreign keys are enforced on runtime
# connections and neither timed_entitlements.source_voucher_id nor
# fulfillment_requests.voucher_id declares an ON DELETE action, so this order is
# required rather than cosmetic.
ERASE_DELETE_ORDER = (
    ("fulfillment_requests", "user_id = %s", 1),
    ("timed_entitlements", "user_id = %s", 1),
    ("reward_vouchers", "user_id = %s", 1),
    ("gacha_pulls", "user_id = %s", 1),
    ("gacha_pity", "user_id = %s", 1),
    ("user_inventory", "user_id = %s", 1),
    ("activity_events", "user_id = %s", 1),
    ("warnings", "user_id = %s", 1),
    ("tickets", "opener_id = %s", 1),
    ("voice_permissions", "owner_id = %s OR target_id = %s", 2),
    ("voice_settings", "user_id = %s", 1),
    ("active_channels", "owner_id = %s", 1),
    ("user_sharing_preferences", "user_id = %s", 1),
    ("user_identities", "user_id = %s", 1),
    # A transient anti-abuse counter, not a record worth keeping once the
    # member it watches is gone.
    ("casino_earnings_window", "user_id = %s", 1),
    # A party post whose host is erased has no host; the row goes. Members who
    # *joined* somebody else's post are inside `joined_json` and are scrubbed
    # out of it below rather than deleting the host's post over them.
    ("lfg_posts", "host_id = %s", 1),
    # A day-long signal that the member's dashboard access may have changed;
    # nothing about it needs to outlive them.
    ("dashboard_access_invalidations", "user_id = %s", 1),
)

# Re-keyed to the tombstone instead of deleted. ``users`` and ``scoped_accounts``
# hold the money; ``reward_claims`` and settled ``casino_wagers`` are the records
# that stop a returning member being paid the same reward twice.
ERASE_REKEY_SUBJECT = ("users", "scoped_accounts", "reward_claims", "casino_wagers")

# Attribution on rows that belong to other members or to the guild. A nullable
# column becomes NULL; a NOT NULL one is pointed at the tombstone, because the row
# itself must survive and the column cannot be emptied.
ERASE_NULL_ACTOR = (
    ("warnings", "mod_id"),
    ("tickets", "claimer_id"),
    ("fulfillment_requests", "completed_by"),
    ("timed_entitlements", "granted_by"),
    ("realm_guilds", "approved_by"),
    ("guild_data_scopes", "updated_by"),
    ("feature_flags", "updated_by"),
    ("guild_settings", "updated_by"),
    ("instance_settings", "updated_by"),
    ("managed_messages", "updated_by"),
    ("shop_item_definitions", "updated_by"),
    ("gacha_banners", "updated_by"),
    ("work_responses", "updated_by"),
    # Who took the last turn in a counting or word-chain channel. Nullable, and
    # NULL already means "nobody yet" to `police`, so the chain plays on.
    ("minigame_state", "last_user_id"),
)
ERASE_REKEY_ACTOR = (
    ("settings_audit", "actor_id"),
    ("control_actions", "actor_id"),
    ("realms", "created_by"),
    ("dashboard_documents", "updated_by"),
)

# JSON columns that can carry a member id, each with the function that rewrites
# it. Declared so the coverage test can hold the schema to this list: a column
# holding member ids in JSON is invisible to the row-level lists above, and both
# `lfg_posts.joined_json` and the erasure action's own payload were exactly that.
ERASE_SCRUBBED_JSON = (
    ("lfg_posts", "joined_json"),
    ("control_actions", "payload_json"),
    ("settings_audit", "old_value_json"),
)


def _rows_as_dicts(cursor) -> list[dict]:
    """The export's rows, every stored timestamp as ISO text so the file
    encodes as JSON on either engine."""
    columns = [column[0] for column in cursor.description]
    return [{column: stored_iso(value) for column, value in zip(columns, row)}
            for row in cursor.fetchall()]


def export_user_data(user_id: int) -> dict:
    """Return everything this installation stores about one member.

    Installation-wide rather than per guild: the operator is a single data
    controller and ``users`` has no guild dimension, so a guild-filtered export
    would be a half-truth. Guild-keyed rows carry their own ``guild_id`` so the
    member can see what belongs where.
    """
    user_id = int(user_id)
    tables = {}
    truncated = []
    with get_connection() as conn:
        for table, clause, bindings in SUBJECT_TABLES:
            row_filter = SUBJECT_ROW_FILTERS.get(table)
            if row_filter is None:
                rows = _rows_as_dicts(conn.execute(
                    f"SELECT * FROM {sql_identifier(table)} WHERE {clause} LIMIT %s",
                    (*(user_id,) * bindings, EXPORT_ROW_LIMIT + 1),
                ))
            else:
                rows = [row for row in _rows_as_dicts(conn.execute(
                    f"SELECT * FROM {sql_identifier(table)} WHERE {clause}",
                    (user_id,) * bindings,
                )) if row_filter(row, user_id)]
            if len(rows) > EXPORT_ROW_LIMIT:
                rows = rows[:EXPORT_ROW_LIMIT]
                truncated.append(table)
            tables[table] = rows
        # rented_items has no user column at all; the only link back to a member is
        # the entitlement that created it, which is the join get_all_rentals uses.
        # Null-safe equality (SQLite's ``IS``) rather than ``=`` on both nullable
        # columns: a legacy row with no provenance and an entitlement with no
        # Discord asset both hold NULL, and ``NULL = NULL`` would silently drop
        # them from the export.
        tables["rented_items"] = _rows_as_dicts(conn.execute(
            "SELECT r.* FROM rented_items r JOIN timed_entitlements e "
            "ON e.guild_id IS NOT DISTINCT FROM r.guild_id "
            "AND e.entitlement_key = r.item_type "
            "AND e.discord_item_id IS NOT DISTINCT FROM r.discord_item_id "
            "WHERE e.user_id = %s LIMIT %s",
            (user_id, EXPORT_ROW_LIMIT),
        ))
    return {
        "schema_version": LATEST_SCHEMA_VERSION,
        "user_id": str(user_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "truncated_tables": truncated,
        "row_limit_per_table": EXPORT_ROW_LIMIT,
        "tables": tables,
    }


def get_active_entitlements_for_user(user_id: int) -> list[dict]:
    """Grants that must be revoked in Discord before the record is erased.

    Deleting timed_entitlements first would leave a premium role or a rented emoji
    in place with nothing left to attribute or expire it.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT entitlement_id, guild_id, user_id, entitlement_key, expires_at, "
            "source_voucher_id, discord_item_id, status FROM timed_entitlements "
            "WHERE user_id = %s AND status = 'active'", (int(user_id),)
        ).fetchall()
    keys = ("entitlement_id", "guild_id", "user_id", "entitlement_key", "expires_at",
            "source_voucher_id", "discord_item_id", "status")
    return [dict(zip(keys, row)) for row in rows]


def _allocate_tombstone_id(conn) -> int:
    """The next free negative user id.

    ``users.user_id`` carries no CHECK, so a negative integer is legal, can never
    collide with a Discord snowflake, and gives each erasure its own identifier so
    two erased members never collide on the primary key.
    """
    lowest = conn.execute(
        "SELECT MIN(user_id) FROM (SELECT user_id FROM users "
        "UNION ALL SELECT user_id FROM scoped_accounts)"
    ).fetchone()[0]
    return min(0, lowest or 0) - 1


def _refund_pending_wagers_for_user_locked(conn, user_id: int, resolved_at: str) -> dict:
    """Return the stake of every unsettled wager to the balance being anonymised.

    A pending row is a live refund obligation whose stake has already left
    ``users.balance``; deleting it would destroy the obligation and quietly shrink
    the installation's coin supply. The money lands on the row that is about to
    become the tombstone, so nothing enters or leaves the economy.
    """
    rows = conn.execute(
        "SELECT wager_id, stake FROM casino_wagers "
        "WHERE user_id = %s AND status = 'pending'", (user_id,)
    ).fetchall()
    for wager_id, stake in rows:
        conn.execute(
            "UPDATE users SET balance = balance + %s WHERE user_id = %s",
            (stake, user_id),
        )
        conn.execute(
            "UPDATE casino_wagers SET status = 'refunded', resolved_at = %s, "
            "resolution_json = %s WHERE wager_id = %s",
            (resolved_at, json.dumps({"reason": "data_erasure"}), wager_id),
        )
    return {"count": len(rows), "amount": sum(row[1] for row in rows)}


def _blank_personal_columns_locked(conn, tombstone_id: int):
    """Strip the behavioural columns from the retained economy row.

    Cooldowns, streaks and activity timestamps describe the person, not the money,
    so they are cleared even though the row survives.
    """
    cooldowns = ", ".join(f"{sql_identifier(column)} = NULL"
                          for column in sorted(VALID_COOLDOWN_COLUMNS))
    conn.execute(
        f"UPDATE users SET {cooldowns}, bodyguard_until = NULL, "
        "last_streak_update = NULL, streak_count = 0, last_active = NULL, "
        "inactive_warned = 0, rules_read_time = NULL WHERE user_id = %s",
        (tombstone_id,),
    )
    conn.execute(
        f"UPDATE scoped_accounts SET {cooldowns}, bodyguard_until = NULL, "
        "last_streak_update = NULL, streak_count = 0, last_active = NULL, "
        "inactive_warned = 0, rules_read_time = NULL WHERE user_id = %s",
        (tombstone_id,),
    )


def _scrub_audit_payloads_locked(conn, user_id: int, tombstone_id: int) -> int:
    """Rewrite audit payloads that name the subject.

    ``remove_warning`` records the deleted warning's user id, reason and moderator
    inside settings_audit.old_value_json, and that table is readable by any guild
    administrator, so leaving it would defeat the erasure.
    """
    placeholders = ",".join(["%s"] * len(_AUDIT_SUBJECT_ACTIONS))
    rows = conn.execute(
        f"SELECT audit_id, old_value_json FROM settings_audit "
        f"WHERE action IN ({placeholders}) AND old_value_json IS NOT NULL",
        _AUDIT_SUBJECT_ACTIONS,
    ).fetchall()
    scrubbed = 0
    for audit_id, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        touched = False
        for key in ("user_id", "moderator_id"):
            if payload.get(key) == user_id:
                payload[key] = tombstone_id
                touched = True
        if not touched:
            continue
        # The reason text is free-form staff prose about this member, so it goes
        # rather than being re-keyed.
        payload["reason"] = None
        payload["erased"] = True
        conn.execute(
            "UPDATE settings_audit SET old_value_json = %s WHERE audit_id = %s",
            (json.dumps(payload, sort_keys=True), audit_id),
        )
        scrubbed += 1
    return scrubbed


def _scrub_lfg_parties_locked(conn, user_id: int) -> int:
    """Take the member out of every party they joined.

    `joined_json` is an ordered list of member ids and nothing else, so the
    member simply leaves; the post keeps its other members and its host. A host
    who is erased loses the post itself through `ERASE_DELETE_ORDER`.
    """
    rows = conn.execute(
        "SELECT guild_id, message_id, joined_json FROM lfg_posts "
        "WHERE joined_json LIKE %s", (f"%{int(user_id)}%",)
    ).fetchall()
    scrubbed = 0
    for guild_id, message_id, stored in rows:
        try:
            party = [int(uid) for uid in json.loads(stored)]
        except (TypeError, ValueError):
            continue
        if int(user_id) not in party:
            continue  # the LIKE matched a longer id that contains this one
        remaining = [uid for uid in party if uid != int(user_id)]
        conn.execute(
            "UPDATE lfg_posts SET joined_json = %s "
            "WHERE guild_id = %s AND message_id = %s",
            (json.dumps(remaining), guild_id, message_id),
        )
        scrubbed += 1
    return scrubbed


def _scrub_control_action_payloads_locked(conn, user_id: int,
                                          tombstone_id: int) -> int:
    """Re-key every outbox payload that names the member.

    An operator-requested erasure travels through the outbox as
    `{"user_id": <member>}`, and that row outlives the erasure it caused until
    `prune_control_actions` removes it — so for up to a month the queue named
    the member the rest of the transaction had just forgotten. A
    `revoke_entitlement` row carries the same key for the grant's holder, and
    was missed when this matched on the action type; the match is now on the
    payload itself, so a future action type naming a member is covered the day
    it is added. A row the worker has not run yet then acts on the tombstone,
    which no guild has a member for, and settles as nothing to do — the same
    outcome as the erasure that already withdrew the grant in Discord.
    """
    # The LIKE narrows the rows; the parse below decides, numerically, exactly
    # as `_control_action_row_names` does for the export.
    rows = conn.execute(
        "SELECT action_id, payload_json FROM control_actions "
        "WHERE payload_json LIKE ('%%' || %s || '%%')",
        (int(user_id),),
    ).fetchall()
    scrubbed = 0
    for action_id, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("user_id") != int(user_id):
            continue
        payload["user_id"] = tombstone_id
        conn.execute(
            "UPDATE control_actions SET payload_json = %s WHERE action_id = %s",
            (json.dumps(payload, sort_keys=True), action_id),
        )
        scrubbed += 1
    return scrubbed


def anonymize_user(user_id: int, actor_id: int, guild_id: int = 0,
                   reason: str = "member_request") -> dict:
    """Erase one member, retaining the economy row under a tombstone identifier.

    Returns a receipt of what was deleted and what was retained, which the member
    is sent and which ``docs/privacy.md`` requires the operator to be able to show.
    """
    user_id = int(user_id)
    if user_id <= 0:
        raise ValidationError("erasure_subject_invalid",
                              "erasure subject must be a Discord user id")
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            tombstone_id = _allocate_tombstone_id(conn)
            refunded = _refund_pending_wagers_for_user_locked(conn, user_id, timestamp)

            deleted = {}
            for table, clause, bindings in ERASE_DELETE_ORDER:
                deleted[table] = conn.execute(
                    f"DELETE FROM {sql_identifier(table)} WHERE {clause}",
                    (user_id,) * bindings,
                ).rowcount

            retained = {}
            for table in ERASE_REKEY_SUBJECT:
                retained[table] = conn.execute(
                    f"UPDATE {sql_identifier(table)} SET user_id = %s WHERE user_id = %s",
                    (tombstone_id, user_id),
                ).rowcount
            _blank_personal_columns_locked(conn, tombstone_id)

            for table, column in ERASE_NULL_ACTOR:
                conn.execute(
                    f"UPDATE {sql_identifier(table)} SET {sql_identifier(column)} = NULL "
                    f"WHERE {sql_identifier(column)} = %s",
                    (user_id,),
                )
            for table, column in ERASE_REKEY_ACTOR:
                conn.execute(
                    f"UPDATE {sql_identifier(table)} SET {sql_identifier(column)} = %s "
                    f"WHERE {sql_identifier(column)} = %s",
                    (tombstone_id, user_id),
                )
            scrubbed = _scrub_audit_payloads_locked(conn, user_id, tombstone_id)
            parties = _scrub_lfg_parties_locked(conn, user_id)
            actions = _scrub_control_action_payloads_locked(conn, user_id, tombstone_id)

            receipt = {
                "tombstone_id": tombstone_id,
                "erased_at": timestamp,
                "reason": reason,
                "deleted_rows": {table: count for table, count in deleted.items() if count},
                "retained_rows": {table: count for table, count in retained.items() if count},
                "refunded_wagers": refunded,
                "audit_payloads_scrubbed": scrubbed,
                "lfg_parties_left": parties,
                "control_action_payloads_scrubbed": actions,
            }
            # Deliberately not remove_warning's shape: the audit feed is readable by
            # any guild administrator, so the payload names the tombstone and the
            # counts, never the member who was erased.
            write_settings_audit(
                conn, int(guild_id), int(actor_id), "user.erase",
                f"tombstone:{tombstone_id}", None, receipt,
            )
            conn.commit()
            db_logger.info(
                "Erased member data (tombstone=%s, reason=%s, deleted=%s)",
                tombstone_id, reason, sum(deleted.values()),
            )
            return receipt
    except ValidationError:
        raise
    except EngineError as exc:
        db_logger.exception("Member data erasure failed")
        raise DatabaseOperationError("member data erasure failed") from exc


def get_retention_candidates(cutoff_iso: str, limit: int = 25) -> list[int]:
    """Members whose last recorded activity predates the retention window.

    Membership is not knowable from SQLite, so the caller must additionally confirm
    the member is absent from every guild before erasing them. A row that has never
    recorded activity is never a candidate, because absence of a timestamp is not
    evidence of absence of a member.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE user_id > 0 AND last_active IS NOT NULL "
            "AND last_active < %s ORDER BY last_active ASC, user_id LIMIT %s",
            (cutoff_iso, max(1, int(limit))),
        ).fetchall()
    return [row[0] for row in rows]
