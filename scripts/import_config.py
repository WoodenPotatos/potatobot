"""Give every value in `config.json` a database row, once.

`guild_settings` is **sparse**: a row exists only once somebody has saved that
setting in the dashboard. Everything else still resolves through
`settings_registry.legacy_config_value`, so the file is the effective source for
anything never touched there. That is why retiring the file needs a one-time
import rather than only a reader change — without it, every untouched setting
would silently fall back to its registry default, and a registry default for a
channel is empty.

Three properties are load-bearing.

**It never overwrites.** A row that exists is newer than the file, because the
only way a row exists is that somebody saved it. This writes absent rows only.

**It writes through `database.set_guild_settings`.** Raw SQL would skip the type
validation, the revision, the scope routing that decides whether a value belongs
in `instance_settings`, and the audit row that has to commit with the change.
There is one write path and this is not a second one.

**The whole preflight runs before the first write.** A script that checks and
mutates in the same pass leaves half-done state and an alarming traceback; this
one resolves and validates every value, then writes.

Usage:
    python scripts/import_config.py --dry-run          # report, write nothing
    python scripts/import_config.py                    # import
    python scripts/import_config.py --guild 123        # name the target guild
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database  # noqa: E402
import settings_registry  # noqa: E402
from settings_registry import (  # noqa: E402
    SETTING_DEFINITIONS,
    SettingScope,
    legacy_config_value,
    validate_setting_value,
)

# Read at call time, so a rehearsal can point this at a copy the way the tests
# and `scripts/local_dashboard.py` already point `database.DB_PATH`.
CONFIG_PATH = ROOT / "config.json"

# What the audit row records as the actor. Not a Discord id: this was not a
# person, and claiming it was would put a real member's name against every row.
IMPORT_ACTOR_ID = 0


class Refusal(RuntimeError):
    """A preflight failure. Raised before anything is written."""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise Refusal(f"{CONFIG_PATH} does not exist")
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Refusal(f"{CONFIG_PATH} is not valid JSON: {exc}") from exc


def resolve_target_guild(requested: int | None) -> int:
    """Which guild the guild-scoped values belong to.

    Inferred from the installation when there is exactly one active guild, and
    refused otherwise: guessing which guild a single-tenant file describes is
    the kind of assumption that writes somebody else's channel ids.
    """
    if requested is not None:
        return int(requested)
    guilds = database.get_active_guilds()
    if len(guilds) != 1:
        raise Refusal(
            f"{len(guilds)} active guilds; name the target with --guild "
            "rather than letting the script guess which one config.json is for"
        )
    return int(guilds[0]["id"])


def coerce_to_declared_type(definition, value):
    """Bring a legacy value to the type its setting declares, losslessly.

    `roles.ignored_users` is the case this exists for: it holds Discord ids and
    is declared a string list, because a snowflake cannot cross to a browser as
    a number — but `config.json` holds them as integers. Refusing would make the
    import unusable on a real installation; coercing silently would hide a
    genuine type mismatch. So this converts only where the conversion cannot
    lose anything, and `main` reports every conversion it made.

    Anything else is left alone and fails validation, which is the right
    outcome: a value that is the wrong *shape* is a mistake in the file.
    """
    from settings_registry import SettingValueType

    if definition.value_type is SettingValueType.STRING_LIST:
        if isinstance(value, list) and any(isinstance(item, int) for item in value):
            return [str(item) for item in value]
    return value


def plan_import(config: dict, guild_id: int) -> tuple[list[dict], list[str]]:
    """What would be written, and what is already present.

    Resolves and validates every value before returning, so a refusal happens
    here rather than half way through a write.
    """
    stored = database.get_guild_settings(guild_id)
    pending, skipped = [], []
    for key, definition in SETTING_DEFINITIONS.items():
        if definition.sensitive or not definition.legacy_path:
            continue
        if key in stored:
            skipped.append(key)
            continue
        value = coerce_to_declared_type(definition,
                                        legacy_config_value(definition, config))
        # An absent path resolves to the registry default. Importing that writes
        # a row that says exactly what no row says, so leave it absent: a
        # missing row and a row holding the default are different states and the
        # dashboard shows the second as configured.
        if value == definition.default:
            continue
        try:
            value = validate_setting_value(definition, value)
        except ValueError as exc:
            raise Refusal(
                f"config.json holds a value for {key} that the registry "
                f"rejects: {exc}. Fix the file, then re-run."
            ) from exc
        pending.append({"key": key, "value": value, "revision": 0,
                        "scope": definition.scope,
                        "coerced": value != legacy_config_value(definition, config)})
    return pending, sorted(skipped)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--guild", type=int, default=None,
                        help="target guild id for guild-scoped settings")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written and stop")
    arguments = parser.parse_args()

    try:
        # --- preflight: everything that can refuse, before anything is written
        config = load_config()
        version = database.get_schema_version()
        if version != database.LATEST_SCHEMA_VERSION:
            raise Refusal(
                f"database is at schema {version}, not "
                f"{database.LATEST_SCHEMA_VERSION}; run update_db.py first"
            )
        guild_id = resolve_target_guild(arguments.guild)
        pending, skipped = plan_import(config, guild_id)
    except Refusal as refusal:
        print(f"  refused: {refusal}")
        return 1

    print(f"  target guild: {guild_id}")
    print(f"  already stored, left alone: {len(skipped)}")
    if not pending:
        print("  nothing to import; every configured value already has a row")
        return 0

    for entry in pending:
        scope = "instance" if entry["scope"] is SettingScope.INSTANCE else "guild"
        rendered = json.dumps(entry["value"], ensure_ascii=False)
        note = "  (converted to the declared type)" if entry["coerced"] else ""
        print(f"    + [{scope:8}] {entry['key']} = {rendered[:70]}{note}")

    if arguments.dry_run:
        print(f"  dry run: {len(pending)} row(s) would be written")
        return 0

    changes = [{"key": entry["key"], "value": entry["value"], "revision": 0}
               for entry in pending]
    try:
        written = database.set_guild_settings(guild_id, IMPORT_ACTOR_ID, changes)
    except database.RevisionConflictError:
        print("  refused: a row appeared while this was running; re-run to "
              "pick up the new state")
        return 1
    except (database.ValidationError, database.DatabaseOperationError) as exc:
        print(f"  refused: {exc}")
        return 1
    print(f"  imported {len(written)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
