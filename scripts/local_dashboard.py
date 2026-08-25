"""Run the dashboard on this machine, with no Discord and no server.

This exists for one situation: the deployment is unreachable and you still want
to look at the control plane while developing. It is a development tool and it
must never be used to serve a real installation, so it refuses to start unless
it is binding loopback with no proxied origin configured, and it prints what it
is faking every time.

What it does, in order:

1. Copies the repository's `economy.db` to `.local-dev/economy.dev.db`, so the
   only copy of real data you have is never the file being migrated. The copy
   carries its WAL sidecars, because a database checkpointed elsewhere keeps
   committed rows in them.
2. Fingerprints the copy, runs `database.initialize_database()` against it, then
   fingerprints it again and prints the comparison. That is the same before and
   after check `scripts/rehearse_migration.py` performs, so a stale copy is also
   a migration rehearsal: this one is at schema 2, so it exercises every ordered
   migration including the schema 8 table rebuild.
3. Builds a stand-in Discord guild from the ids in `config.json` and the stored
   settings, so every channel and role selector resolves a real configured id to
   a readable name instead of rendering "unavailable".
4. Signs you in as the host without OAuth, by injecting a session ahead of every
   other request hook.
5. Serves on 127.0.0.1.

Nothing here changes the production code path. The session injection and the
stand-in guild are installed on the imported module from this script, so there
is no development branch inside `dashboard_api.py` that a deployment could
accidentally take.

Usage:
    python scripts/local_dashboard.py
    python scripts/local_dashboard.py --fresh          # re-copy the database
    python scripts/local_dashboard.py --port 5050
    python scripts/local_dashboard.py --source /path/to/economy.db
"""

import argparse
import importlib.util
import os
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The synthetic host identity. It only has to be a plausible snowflake: the
# dashboard stores it in the session and compares it with ADMIN_DISCORD_ID, and
# no Discord call is ever made with it.
LOCAL_HOST_ID = "1"
LOCAL_DEV_DIR = ROOT / ".local-dev"
DEFAULT_PORT = 5001


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Run the dashboard locally against a copy of the database.",
    )
    parser.add_argument(
        "--source", type=Path, default=ROOT / "economy.db",
        help="Database to copy from (default: ./economy.db).",
    )
    parser.add_argument(
        "--db", type=Path, default=LOCAL_DEV_DIR / "economy.dev.db",
        help="Working copy to serve (default: ./.local-dev/economy.dev.db).",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Re-copy from --source even if the working copy already exists.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Loopback port to serve on (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--auto-port", action="store_true",
        help="If the port is busy, use the next free one instead of failing.",
    )
    parser.add_argument(
        "--guild", type=int, default=None,
        help="Guild id to expose. Defaults to every active guild in the copy.",
    )
    parser.add_argument(
        "--skip-migration", action="store_true",
        help="Serve the copy as-is. Most pages need schema 9 and will fail.",
    )
    return parser.parse_args()


# Third-party modules the dashboard needs. `sqlite3` and the rest of the
# migration path are stdlib, which is why running this with the wrong
# interpreter used to copy and migrate a database and *then* fail on an import.
REQUIRED_MODULES = ("flask", "waitress", "discord", "requests")
# Set on the re-executed process so a broken virtual environment cannot make
# this loop forever.
REEXEC_MARKER = "POTATOBOT_LOCAL_DASHBOARD_REEXEC"


def _venv_interpreter() -> Path | None:
    """The repository's own interpreter, if there is one."""
    for candidate in (
        ROOT / "venv" / "bin" / "python",
        ROOT / "venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return candidate
    return None


def _missing_modules() -> list[str]:
    return [name for name in REQUIRED_MODULES
            if importlib.util.find_spec(name) is None]


def ensure_dependencies() -> None:
    """Run under an interpreter that actually has the dependencies.

    This is the first thing `main` does, before anything is copied or migrated,
    because the previous failure mode was a traceback after the database had
    already been rewritten. When the current interpreter is missing modules and
    the repository has its own virtual environment, this re-executes into it
    rather than telling the operator to retype the command.
    """
    missing = _missing_modules()
    if not missing:
        return

    interpreter = _venv_interpreter()
    already_tried = os.environ.get(REEXEC_MARKER) == "1"
    # `sys.prefix` is the environment, not the binary. Comparing binaries fails
    # here because `venv/bin/python` is a symlink to the system interpreter, so
    # a resolved path makes the two look identical.
    already_inside = interpreter is not None and Path(sys.prefix) in (
        interpreter.parent.parent, interpreter.parent.parent.resolve()
    )
    if interpreter is not None and not already_tried and not already_inside:
        print(f"  {Path(sys.executable).name} is missing "
              f"{', '.join(missing)}; re-running with {interpreter}")
        os.execve(
            str(interpreter),
            [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]],
            # Unbuffered, or the re-executed process shows nothing until it
            # flushes and the startup report appears to hang.
            {**os.environ, REEXEC_MARKER: "1", "PYTHONUNBUFFERED": "1"},
        )

    raise SystemExit(
        "Missing dependencies: " + ", ".join(missing) + "\n\n"
        "The dashboard needs the project's runtime dependencies. Either:\n"
        f"  {interpreter or 'venv/bin/python'} scripts/local_dashboard.py\n"
        "or install them into the current interpreter:\n"
        "  python -m pip install --requirement requirements.lock"
    )


def resolve_port(requested: int, auto: bool) -> int:
    """Return a port that can actually be bound.

    Waitress fails with a bare `OSError: [Errno 98] Address already in use`
    after the whole startup sequence has run, which reads like a crash rather
    than "something is already listening".
    """
    def free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    if free(requested):
        return requested
    if not auto:
        raise SystemExit(
            f"127.0.0.1:{requested} is already in use.\n"
            "Another copy of the dashboard is probably still running:\n"
            "  pgrep -af local_dashboard.py\n"
            f"Then either stop it, pass --port <other>, or use --auto-port."
        )
    for candidate in range(requested + 1, requested + 50):
        if free(candidate):
            print(f"  port {requested} was busy; using {candidate}")
            return candidate
    raise SystemExit(f"no free port between {requested} and {requested + 49}")


def refuse_unsafe_environment() -> None:
    """Never let this become the thing serving a real installation."""
    external = os.getenv("POTATOBOT_DASHBOARD_EXTERNAL_URL", "").strip()
    if external:
        raise SystemExit(
            "POTATOBOT_DASHBOARD_EXTERNAL_URL is set, so this environment is "
            "configured for a proxied deployment. Refusing to start the local "
            "development dashboard here."
        )
    profile = os.getenv("POTATOBOT_DEPLOYMENT_PROFILE", "private").strip().lower()
    if profile == "managed":
        raise SystemExit(
            "POTATOBOT_DEPLOYMENT_PROFILE=managed. Refusing to start the local "
            "development dashboard against a managed deployment's environment."
        )


def copy_database(source: Path, target: Path, fresh: bool) -> bool:
    """Put a working copy in place. Returns True when it was (re-)copied.

    The WAL and shared-memory sidecars are copied with the database: a file
    checkpointed by another process keeps committed rows in `-wal`, so copying
    the main file alone can silently lose the most recent writes.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not fresh:
        print(f"  reusing existing working copy {target}")
        return False
    if not source.exists():
        raise SystemExit(f"source database not found: {source}")
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(source) + suffix)
        if candidate.exists():
            shutil.copy2(candidate, Path(str(target) + suffix))
            print(f"  copied {candidate.name} -> {Path(str(target) + suffix).name}")
    return True


def fingerprint(path: Path) -> dict:
    from db_snapshot import record
    return record(path)


def migrate_and_report(path: Path) -> None:
    """Migrate the working copy and print the before/after comparison."""
    from db_snapshot import compare

    import database

    before = fingerprint(path)
    print(f"  before: schema {before['schema_version']}, "
          f"integrity {before['integrity_check']}, "
          f"{sum(before['table_counts'].values())} rows in "
          f"{len(before['table_counts'])} tables")

    database.initialize_database()

    after = fingerprint(path)
    print(f"  after:  schema {after['schema_version']}, "
          f"integrity {after['integrity_check']}, "
          f"{sum(after['table_counts'].values())} rows in "
          f"{len(after['table_counts'])} tables")
    print("  comparison:")
    # `compare` reports any row-count change as a problem and prints FAILED,
    # which is the right default for a real rehearsal. On a throwaway copy it is
    # informational, and one change is expected rather than suspicious: seeding
    # the built-in shop items adds `shop_prices` rows for items that did not
    # exist when the copy was taken.
    problems = compare(before, after)
    if problems:
        print("  (on this working copy a row-count change is expected: the "
              "built-in shop item seeding adds shop_prices rows. Integrity and "
              "column checks above are the ones that matter.)")


# --------------------------------------------------------------- stand-in Discord

def _collect_configured_ids() -> tuple[dict, dict]:
    """Map every Discord id in `config.json` to a readable label and a kind.

    Selectors are the main thing worth looking at locally, and a stored id that
    is absent from the resource list renders as "unavailable". Naming the ids the
    configuration already references is what makes the page look real.
    """
    from cogs.utils import config
    from settings_registry import (
        CATEGORY_CHANNEL_TYPES,
        SETTING_DEFINITIONS,
        VOICE_CHANNEL_TYPES,
    )

    channels: dict[int, tuple[str, str]] = {}
    roles: dict[int, str] = {}

    def add_channel(identifier, label, kind="text"):
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            channels.setdefault(int(identifier), (label, kind))

    def add_role(identifier, label):
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            roles.setdefault(int(identifier), label)

    # Channel kinds come from the registry, so a category setting produces a
    # category here and a voice lobby produces a voice channel.
    kind_by_legacy_key = {}
    for definition in SETTING_DEFINITIONS.values():
        if not definition.legacy_path or definition.legacy_path[0] != "channels":
            continue
        if set(definition.channel_types) & set(CATEGORY_CHANNEL_TYPES):
            kind_by_legacy_key[definition.legacy_path[-1]] = "category"
        elif set(definition.channel_types) & set(VOICE_CHANNEL_TYPES):
            kind_by_legacy_key[definition.legacy_path[-1]] = "voice"

    for key, value in (config.get("channels") or {}).items():
        kind = kind_by_legacy_key.get(key, "text")
        for index, entry in enumerate(value if isinstance(value, list) else [value]):
            suffix = f"-{index + 1}" if isinstance(value, list) and len(value) > 1 else ""
            add_channel(entry, f"{key.replace('_', '-')}{suffix}", kind)

    for key, value in (config.get("roles") or {}).items():
        if key == "ignored_users":
            continue  # Member ids, not roles.
        for index, entry in enumerate(value if isinstance(value, list) else [value]):
            suffix = f"-{index + 1}" if isinstance(value, list) and len(value) > 1 else ""
            add_role(entry, f"{key.replace('_', '-')}{suffix}")

    socials = config.get("socials") or {}
    add_channel(socials.get("notification_channel"), "social-notifications")
    add_role(socials.get("twitch_role_id"), "twitch")
    add_role(socials.get("youtube_role_id"), "youtube")


    for name, faction in (config.get("factions") or {}).items():
        if not isinstance(faction, dict):
            continue
        add_role(faction.get("leader_role_id"), f"{name}-leader")
        for index, entry in enumerate(faction.get("manageable_ids") or []):
            add_role(entry, f"{name}-member-{index + 1}")

    for channel_id, role_id in (config.get("lfg_channels") or {}).items():
        if str(channel_id).isdigit():
            add_channel(int(channel_id), "lfg", "text")
        add_role(role_id, "lfg-target")

    return channels, roles


def _collect_stored_setting_ids(guild_ids) -> tuple[set, set]:
    """Snowflakes already saved in the database, so those resolve too.

    `guild_settings` plus the role-menu entries, which stopped being settings at
    schema 12 — without them every menu row on the builder page would render as
    an unavailable role.
    """
    import database
    from settings_registry import SETTING_DEFINITIONS, SettingValueType

    channel_types = {SettingValueType.CHANNEL, SettingValueType.CHANNEL_LIST}
    role_types = {SettingValueType.ROLE, SettingValueType.ROLE_LIST}
    channels, roles = set(), set()
    for guild_id in guild_ids:
        for key, row in database.get_guild_settings(guild_id).items():
            definition = SETTING_DEFINITIONS.get(key)
            if definition is None:
                continue
            value = row["value"]
            entries = value if isinstance(value, list) else [value]
            target = (channels if definition.value_type in channel_types
                      else roles if definition.value_type in role_types else None)
            if target is None:
                continue
            target.update(
                int(entry) for entry in entries
                if isinstance(entry, int) and not isinstance(entry, bool)
            )
        for menu in database.list_managed_messages(guild_id, "role_menu"):
            stored = database.get_managed_message(guild_id, "role_menu",
                                                  menu["menu_key"])
            roles.update(
                int(entry["role_id"]) for entry in (stored or {})["entries"]
                if entry.get("role_id")
            )
    return channels, roles


class DevRole:
    """Enough of `discord.Role` for the resource list and the permission audit."""

    def __init__(self, role_id, name, position, managed=False, default=False):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class DevChannel:
    def __init__(self, channel_id, name, channel_type, permissions,
                 category_id=None, position=0):
        self.id = channel_id
        self.name = name
        self.type = channel_type
        self.category_id = category_id
        self.position = position
        self._permissions = permissions

    def permissions_for(self, _member):
        return self._permissions


class DevMember:
    def __init__(self, permissions, top_role):
        self.guild_permissions = permissions
        self.top_role = top_role


class DevGuild:
    def __init__(self, guild_id, name, channels, roles, me):
        self.id = guild_id
        self.name = name
        self.icon = None
        self.channels = channels
        self.roles = roles
        self.me = me
        self._channels = {channel.id: channel for channel in channels}
        self._roles = {role.id: role for role in roles}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


class DevBot:
    def __init__(self, guilds):
        self._guilds = {guild.id: guild for guild in guilds}

    def get_guild(self, guild_id):
        return self._guilds.get(int(guild_id))


def build_dev_bot(guild_ids):
    """A stand-in bot cache the real resource and permission code can read.

    `_resources_from_bot_cache` and `permission_audit.build_report` both take a
    guild object and nothing else, so supplying one means neither needs a
    development branch of its own.
    """
    import discord

    configured_channels, configured_roles = _collect_configured_ids()
    stored_channels, stored_roles = _collect_stored_setting_ids(guild_ids)
    for channel_id in stored_channels:
        configured_channels.setdefault(channel_id, (f"stored-{channel_id}", "text"))
    for role_id in stored_roles:
        configured_roles.setdefault(role_id, f"stored-{role_id}")

    # A realistic bot permission set rather than administrator: the permissions
    # page is one of the things worth looking at, and granting everything would
    # make it report a clean result no matter what the configuration says.
    granted = discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True,
        read_message_history=True, external_emojis=True, attach_files=True,
        add_reactions=True, manage_messages=True, manage_channels=True,
        manage_roles=True, move_members=True, connect=True, speak=True,
        kick_members=True, ban_members=True, moderate_members=True,
        mention_everyone=True, manage_expressions=True,
    )
    channel_permissions = discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True,
        read_message_history=True, attach_files=True, add_reactions=True,
        manage_messages=True, manage_channels=True, connect=True, speak=True,
        move_members=True,
    )

    channel_type_by_kind = {
        "text": discord.ChannelType.text,
        "voice": discord.ChannelType.voice,
        "category": discord.ChannelType.category,
    }

    # One category to parent the configured channels, so the selectors show the
    # optgroup grouping rather than one flat list.
    parent = DevChannel(90_000_000_000_000_001, "configured",
                        discord.ChannelType.category, channel_permissions)
    channels = [parent]
    for position, (channel_id, (label, kind)) in enumerate(
        sorted(configured_channels.items()), start=1
    ):
        channel_type = channel_type_by_kind.get(kind, discord.ChannelType.text)
        channels.append(DevChannel(
            channel_id, label, channel_type, channel_permissions,
            category_id=None if channel_type is discord.ChannelType.category
            else parent.id,
            position=position,
        ))

    # Spare options, so a selection can actually be changed while looking around.
    spare = [
        ("dev-spare-text", "text"), ("dev-spare-text-2", "text"),
        ("dev-spare-voice", "voice"), ("dev-spare-category", "category"),
    ]
    for offset, (label, kind) in enumerate(spare, start=1):
        channel_type = channel_type_by_kind[kind]
        channels.append(DevChannel(
            90_000_000_000_000_100 + offset, label, channel_type,
            channel_permissions,
            category_id=None if channel_type is discord.ChannelType.category
            else parent.id,
            position=900 + offset,
        ))

    # The bot's own role sits above everything configured, so role settings are
    # reported as assignable; drop its position to see the hierarchy findings.
    bot_role = DevRole(90_000_000_000_000_900, "PotatoBot (dev)", 500)
    roles = [bot_role, DevRole(90_000_000_000_000_901, "dev-spare-role", 10)]
    for position, (role_id, label) in enumerate(sorted(configured_roles.items()), start=1):
        roles.append(DevRole(role_id, label, position))

    me = DevMember(granted, bot_role)
    guilds = [
        DevGuild(guild_id, f"Local dev guild {guild_id}", channels, roles, me)
        for guild_id in guild_ids
    ]
    print(f"  stand-in guild: {len(channels)} channels, {len(roles)} roles, "
          f"{len(guilds)} guild(s)")
    return DevBot(guilds)


def install_session_injection(dashboard_api, guild_ids) -> None:
    """Sign the browser in as the host without an OAuth round trip.

    The handler is inserted at the front of the request hooks rather than
    appended, because the CSRF check and the absolute-lifetime gate both run
    before an appended one would and would reject the first mutation of the
    session's life.
    """
    from flask import session

    authorized = [str(guild_id) for guild_id in guild_ids]

    def inject_local_session():
        if session.get("logged_in") is True:
            return None
        session.permanent = True
        session["logged_in"] = True
        session["user_id"] = LOCAL_HOST_ID
        session["display"] = {"username": "local-dev", "avatar": None}
        session["csrf_token"] = "local-development-csrf-token"
        session["server_session_id"] = "local-development-session"
        session["authorized_guild_ids"] = authorized
        session["authenticated_at"] = __import__("time").time()
        return None

    hooks = dashboard_api.app.before_request_funcs.setdefault(None, [])
    hooks.insert(0, inject_local_session)


def redirect_config_writes(target_dir: Path) -> None:
    """Point `save_config` at a throwaway copy of config.json.

    The mirror is already disabled by POTATOBOT_LEGACY_GUILD_ID=0, but this is
    the file holding the live deployment's channel, role and faction ids, and it
    is tracked. A second guard costs one copy and removes any path by which a
    local click rewrites it.
    """
    import cogs.utils as utils

    local_config = target_dir / "config.json"
    if not local_config.exists():
        shutil.copy2(utils.CONFIG_PATH, local_config)
    utils.CONFIG_PATH = str(local_config)
    print(f"  config writes redirected to {local_config}")


def active_guild_ids(requested):
    """The guilds to expose, preferring what the copy actually has."""
    import database

    stored = database.get_active_guild_ids()
    if requested is not None:
        if requested not in stored:
            print(f"  warning: guild {requested} is not active in this database")
        return [requested]
    if stored:
        return sorted(stored)
    # An empty copy still has to show something, or the page opens on the
    # no-guilds state and nothing else can be reviewed.
    placeholder = 1
    database.register_guild(placeholder, "Local dev guild")
    print(f"  no active guild in the copy; registered placeholder {placeholder}")
    return [placeholder]


def main() -> int:
    arguments = parse_arguments()

    print("PotatoBot local development dashboard")
    print("=" * 72)
    # Everything that can refuse happens before anything is written, so a
    # failed start never leaves a half-migrated working copy behind.
    print("Preflight")
    refuse_unsafe_environment()
    ensure_dependencies()
    port = resolve_port(arguments.port, arguments.auto_port)
    print(f"  interpreter {sys.executable}")
    print(f"  serving on 127.0.0.1:{port}")

    print("\nDatabase")
    copy_database(arguments.source, arguments.db, arguments.fresh)

    # Every module below reads its configuration at import time, so the
    # environment has to be complete before the first import.
    os.environ["POTATOBOT_DB_PATH"] = str(arguments.db)
    os.environ["POTATOBOT_DASHBOARD_HOST"] = "127.0.0.1"
    os.environ["POTATOBOT_DASHBOARD_PORT"] = str(port)
    os.environ["ADMIN_DISCORD_ID"] = LOCAL_HOST_ID
    os.environ.pop("POTATOBOT_DASHBOARD_EXTERNAL_URL", None)
    os.environ.pop("DISCORD_REDIRECT_URI", None)
    # config.json is mirrored into the one designated legacy guild, and with the
    # private profile and a single active guild that designation is *inferred* —
    # so leaving this unset makes a local save rewrite the tracked config.json.
    # Zero is a legal digit string that can never be a guild id, which turns the
    # mirror off outright.
    os.environ["POTATOBOT_LEGACY_GUILD_ID"] = "0"
    sys.path.insert(0, str(ROOT / "scripts"))

    if arguments.skip_migration:
        print("  migration skipped at your request")
    else:
        import database
        try:
            migrate_and_report(arguments.db)
        except (database.DatabaseOperationError, SystemExit) as error:
            raise SystemExit(
                f"migrating the working copy failed: {error}\n"
                "The copy may be from an interrupted run. Re-copy it with:\n"
                "  python scripts/local_dashboard.py --fresh"
            ) from error

    redirect_config_writes(arguments.db.parent)

    print("\nDiscord stand-ins")
    import dashboard_api

    guild_ids = active_guild_ids(arguments.guild)
    dashboard_api._dashboard_bot = build_dev_bot(guild_ids)
    install_session_injection(dashboard_api, guild_ids)
    print(f"  signed in as host {LOCAL_HOST_ID}; OAuth is bypassed")
    print(f"  guilds exposed: {', '.join(str(item) for item in guild_ids)}")

    print("\nNot real, and known to be wrong here:")
    print("  - channel and role names are labels derived from configuration")
    print("  - channel overwrites are permissive, so overwrite findings cannot")
    print("    appear; role hierarchy findings can")
    print("  - queued Discord publishes stay pending; no bot consumes the outbox")
    print("  - saves are written to the working copy, not to the server")
    print("  - config.json mirroring is off and writes go to .local-dev/")

    print("\n" + "=" * 72)
    print(f"Open http://127.0.0.1:{port}/")
    print("=" * 72 + "\n")
    dashboard_api.run_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
