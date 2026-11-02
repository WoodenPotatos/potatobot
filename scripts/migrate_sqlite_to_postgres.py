"""Copy a 2.20 SQLite database into a new PostgreSQL database, once, and prove it.

The one-time move of todo.md §2, Phase 3. It reads the SQLite file read-only and
never changes it, so the file stays the rollback. Everything that can refuse
runs before the first write, the load is one PostgreSQL transaction, and the
proof runs inside that transaction before it commits: if any table's rows,
any balance or any count differs, nothing is committed.

  1. Preflight, all refusals first:
     - the source opens read-only, is at SQLite schema 26 exactly (upgrade to
       the last 2.20 release first), and passes `integrity_check` and
       `foreign_key_check`;
     - its tables and columns are exactly those `core/schema_types.py` maps;
     - every value converts to its column's kind (a census of every row);
     - `--legacy-timezone` names a real zone;
     - the target is PostgreSQL 18 or newer, holds no tables at all, and was
       created with the built-in `C.UTF-8` locale (docs/installation.md §5),
       which sorts text the way SQLite did.
  2. Load: the baseline schema, then every table in foreign-key order through
     `COPY`; each identity sequence moves past the highest id copied.
     `reward_vouchers.insertion_order` is filled from SQLite's rowid, the order
     `get_user_vouchers` breaks a tie by.
  3. Prove: per table the row count and a fingerprint of every row, normalised
     the same way on both sides (instants as UTC, JSON parsed), and the sum and
     per-user map of every balance.

A timestamp stored with no offset is a legacy *host-local* value, and is read in
`--legacy-timezone`, exactly as `clock.parse_stored` read it on the host. The
option has no default, because the copy may run in a container whose zone is
UTC, which would move every such cooldown by the offset.

Usage:
    python scripts/migrate_sqlite_to_postgres.py --source /data/economy.db \\
        --legacy-timezone Europe/Budapest                # target: libpq's PG* env
    python scripts/migrate_sqlite_to_postgres.py --source economy.db \\
        --legacy-timezone Europe/Budapest --target "dbname=potatobot" --dry-run

`--dry-run` runs the preflight and the census, prints what it would copy, and
writes nothing anywhere.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg import sql

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from core import schema_types as kinds  # noqa: E402

SOURCE_SCHEMA_VERSION = 26
# Leftovers of older versions that an upgraded installation still carries and a
# fresh schema-26 one never had. No code reads any of them, so they stay behind
# in the SQLite file, the archive (decided 2026-11-01) -- but only while they
# hold nothing a copy would lose: the columns must be empty, and every
# `economy_settings` value must equal the installation-wide value that replaced
# it. Anything else unmapped is still refused.
LEFTOVER_COLUMNS = {("active_channels", "faction_group"), ("users", "last_dbdle"),
                    ("users", "last_loldle")}
LEFTOVER_TABLES = {"economy_settings"}
BASELINE = REPOSITORY_ROOT / "core" / "schema" / "0026_baseline.sql"
# Census problems shown before refusing; the count is always reported.
PROBLEMS_SHOWN = 20
MINIMUM_SERVER_VERSION = 180000


class Refusal(Exception):
    """A reason not to copy, found before anything was written."""


# --- values -------------------------------------------------------------------

def convert(kind: str, value, legacy_zone):
    """A SQLite value as the PostgreSQL value of its column's kind, or raise
    `ValueError` saying why it cannot be one."""
    if value is None:
        return None
    if kind in (kinds.IDENTITY, kinds.BIGINT, kinds.FLAG):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"not an integer: {value!r}")
        if not -2**63 <= value < 2**63:
            raise ValueError(f"outside a bigint: {value!r}")
        if kind == kinds.FLAG and value not in (0, 1):
            raise ValueError(f"a flag holds 0 or 1, not {value!r}")
        return value
    if kind == kinds.DOUBLE:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"not a number: {value!r}")
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"not text: {type(value).__name__}")
    if "\x00" in value:
        raise ValueError("contains a NUL byte, which PostgreSQL text cannot hold")
    if kind == kinds.TEXT:
        return value
    if kind == kinds.JSON:
        try:
            json.loads(value)
        except ValueError as exc:
            raise ValueError(f"not JSON: {exc}") from None
        return value
    if kind == kinds.INSTANT:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"not a timestamp: {value!r}") from None
        if moment.tzinfo is None:
            # A legacy host-local value; fold 0 resolves an ambiguous hour the
            # way `astimezone()` did in `clock.parse_stored`.
            moment = moment.replace(tzinfo=legacy_zone)
        return moment.astimezone(timezone.utc)
    raise ValueError(f"unknown kind {kind!r}")


def _canonical(kind: str, value) -> str:
    """One value in the form both sides are compared in."""
    if value is None:
        return "∅"
    if kind == kinds.INSTANT:
        return value.astimezone(timezone.utc).isoformat()
    if kind == kinds.JSON:
        return json.dumps(json.loads(value), sort_keys=True, ensure_ascii=False)
    if kind == kinds.DOUBLE:
        return repr(float(value))
    return repr(value)


def _fingerprint(rows) -> str:
    """Order-independent: the digest of the sorted canonical rows."""
    digest = hashlib.sha256()
    for row in sorted(rows):
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# --- the source ---------------------------------------------------------------

def open_source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise Refusal(f"source not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _source_columns(conn) -> dict[str, list[str]]:
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {table: [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            for table in tables}


def load_order(conn) -> list[str]:
    """Every mapped table, each after the tables its foreign keys name."""
    depends = {}
    for table in kinds.COLUMN_KINDS:
        depends[table] = {row[2] for row in conn.execute(
            f'PRAGMA foreign_key_list("{table}")')} - {table}
    order, done = [], set()
    while len(order) < len(depends):
        ready = sorted(t for t in depends if t not in done and depends[t] <= done)
        if not ready:
            raise Refusal("the source's foreign keys form a cycle")
        order += ready
        done |= set(ready)
    return order


def _select(table: str) -> str:
    columns = ", ".join(f'"{column}"' for column in kinds.COLUMN_KINDS[table])
    if table == "reward_vouchers":
        return f'SELECT {columns}, rowid FROM "{table}" ORDER BY rowid'
    return f'SELECT {columns} FROM "{table}"'


def source_rows(conn, table: str, legacy_zone):
    """Every row converted, with `insertion_order` appended for vouchers."""
    columns = list(kinds.COLUMN_KINDS[table].items())
    for raw in conn.execute(_select(table)):
        row = [convert(kind, value, legacy_zone)
               for (_, kind), value in zip(columns, raw)]
        if table == "reward_vouchers":
            row.append(raw[-1])
        yield row


def preflight_source(conn) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SOURCE_SCHEMA_VERSION:
        raise Refusal(f"the source is at SQLite schema {version}, not "
                      f"{SOURCE_SCHEMA_VERSION}; upgrade to the last 2.20 release first")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise Refusal(f"the source fails integrity_check: {integrity}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise Refusal(f"the source has {len(violations)} foreign key violation(s), "
                      f"e.g. {violations[0]}")
    columns = _source_columns(conn)
    expected = {table: list(cols) for table, cols in kinds.COLUMN_KINDS.items()}
    leftover_tables = set(columns) & LEFTOVER_TABLES
    if set(columns) - leftover_tables != set(expected):
        raise Refusal("the source's tables differ from the map: "
                      f"unmapped {sorted(set(columns) - set(expected) - leftover_tables)}, "
                      f"missing {sorted(set(expected) - set(columns))}")
    for table, cols in expected.items():
        leftovers = {column for column in columns[table]
                     if (table, column) in LEFTOVER_COLUMNS}
        if set(columns[table]) - leftovers != set(cols):
            raise Refusal(f"{table}'s columns differ from the map: "
                          f"unmapped {sorted(set(columns[table]) - set(cols) - leftovers)}, "
                          f"missing {sorted(set(cols) - set(columns[table]))}")
        for column in sorted(leftovers):
            held = conn.execute(f'SELECT COUNT(*) FROM "{table}" '
                                f'WHERE "{column}" IS NOT NULL').fetchone()[0]
            if held:
                raise Refusal(f"the leftover column {table}.{column} holds {held} "
                              "value(s); it is left behind only while empty")
    if leftover_tables:
        _check_economy_settings(conn)


def _check_economy_settings(conn) -> None:
    """Every old `economy_settings` value equals the installation-wide value
    that replaced it, so leaving the table behind loses nothing."""
    rewards = dict(conn.execute(
        "SELECT activity_id, coin_reward FROM rewards WHERE guild_id = 0"))
    prices = dict(conn.execute(
        "SELECT item_id, price FROM shop_prices WHERE guild_id = 0"))
    for key, value in conn.execute("SELECT setting_key, setting_value FROM economy_settings"):
        if key.startswith("reward_"):
            current = rewards.get(key[len("reward_"):])
        elif key.startswith("shop_") and key.endswith("_price"):
            current = prices.get(key[len("shop_"):-len("_price")])
        else:
            current = None
        if current != value:
            raise Refusal(f"economy_settings.{key} is {value!r}, but the value that "
                          f"replaced it is {current!r}; it is left behind only while "
                          "nothing would be lost")


def census(conn, legacy_zone) -> dict[str, int]:
    """Convert every value without writing it. Returns the row count per table;
    raises `Refusal` listing the values that will not convert."""
    problems, counts = [], {}
    for table, columns in kinds.COLUMN_KINDS.items():
        names = list(columns)
        count = 0
        for raw in conn.execute(_select(table)):
            count += 1
            for name, value in zip(names, raw):
                try:
                    convert(columns[name], value, legacy_zone)
                except ValueError as exc:
                    problems.append(f"{table}.{name}: {exc}")
        counts[table] = count
    if problems:
        shown = "\n  ".join(problems[:PROBLEMS_SHOWN])
        raise Refusal(f"{len(problems)} value(s) will not convert:\n  {shown}")
    return counts


# --- the target ---------------------------------------------------------------

def preflight_target(dsn: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            server = conn.info.server_version
            tables = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'").fetchone()[0]
            provider, locale = conn.execute(
                "SELECT datlocprovider::text, datlocale FROM pg_database "
                "WHERE datname = current_database()").fetchone()
    except psycopg.Error as exc:
        raise Refusal(f"cannot reach the target: {exc}") from exc
    if server < MINIMUM_SERVER_VERSION:
        raise Refusal(f"the target is PostgreSQL {server}; 18 or newer is required")
    if tables:
        raise Refusal(f"the target holds {tables} table(s); copy into an empty "
                      "database, never over one")
    if (provider, locale) != ("b", "C.UTF-8"):
        raise Refusal(f"the target's locale is {provider}/{locale}, not builtin "
                      "C.UTF-8; create it as docs/installation.md §5 says")


def _target_columns(table: str) -> list[str]:
    columns = list(kinds.COLUMN_KINDS[table])
    columns += list(kinds.POSTGRES_ONLY_COLUMNS.get(table, {}))
    return columns


def load(conn, source, order: list[str], legacy_zone) -> dict[str, int]:
    """The baseline, then every table by COPY; returns rows copied per table."""
    conn.execute(BASELINE.read_text(encoding="utf-8"))
    copied = {}
    for table in order:
        columns = _target_columns(table)
        statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)))
        count = 0
        with conn.cursor() as cursor, cursor.copy(statement) as copy:
            for row in source_rows(source, table, legacy_zone):
                copy.write_row(row)
                count += 1
        copied[table] = count
    identities = [(table, column)
                  for table in order for column in _target_columns(table)
                  if {**kinds.COLUMN_KINDS[table],
                      **kinds.POSTGRES_ONLY_COLUMNS.get(table, {})}[column] == kinds.IDENTITY]
    for table, column in identities:
        # Past the highest id copied, so a new row never reuses one -- the
        # promise AUTOINCREMENT made.
        conn.execute(sql.SQL(
            "SELECT setval(pg_get_serial_sequence(%s, %s), "
            "COALESCE((SELECT MAX({}) FROM {}), 0) + 1, false)").format(
                sql.Identifier(column), sql.Identifier(table)), (table, column))
    return copied


# --- the proof ----------------------------------------------------------------

def _source_canonical(source, table, legacy_zone):
    columns = list(kinds.COLUMN_KINDS[table].values())
    extra = [kinds.BIGINT] if table == "reward_vouchers" else []
    for row in source_rows(source, table, legacy_zone):
        yield "|".join(_canonical(kind, value) for kind, value in zip(columns + extra, row))


def _target_canonical(conn, table):
    names = _target_columns(table)
    mapped = {**kinds.COLUMN_KINDS[table], **kinds.POSTGRES_ONLY_COLUMNS.get(table, {})}
    statement = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(map(sql.Identifier, names)), sql.Identifier(table))
    for row in conn.execute(statement):
        yield "|".join(_canonical(mapped[name], value) for name, value in zip(names, row))


def _balances(rows) -> dict:
    return {tuple(key): balance for *key, balance in rows}


def prove(conn, source, order: list[str], legacy_zone) -> list[str]:
    """Every difference between source and target; empty means proven."""
    problems = []
    for table in order:
        source_rows_canonical = list(_source_canonical(source, table, legacy_zone))
        target_rows_canonical = list(_target_canonical(conn, table))
        if len(source_rows_canonical) != len(target_rows_canonical):
            problems.append(f"{table}: {len(source_rows_canonical)} rows in SQLite, "
                            f"{len(target_rows_canonical)} in PostgreSQL")
        elif _fingerprint(source_rows_canonical) != _fingerprint(target_rows_canonical):
            problems.append(f"{table}: the rows differ (fingerprint mismatch)")
    for table, key in (("users", "user_id"), ("scoped_accounts", "scope_type, scope_id, user_id")):
        source_map = _balances(source.execute(f"SELECT {key}, balance FROM {table}"))
        target_map = _balances(conn.execute(f"SELECT {key}, balance FROM {table}"))
        if source_map != target_map:
            problems.append(f"{table}: the per-member balance map differs")
        if sum(v or 0 for v in source_map.values()) != sum(v or 0 for v in target_map.values()):
            problems.append(f"{table}: the balance total differs")
    return problems


# --- the run ------------------------------------------------------------------

def run(source_path: Path, target: str, zone_name: str, dry_run: bool, out=print) -> int:
    started = time.monotonic()
    try:
        legacy_zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        out(f"REFUSED: --legacy-timezone {zone_name!r} is not a time zone")
        return 2
    try:
        source = open_source(source_path)
        try:
            preflight_source(source)
            counts = census(source, legacy_zone)
            order = load_order(source)
            preflight_target(target)
        except Refusal:
            source.close()
            raise
    except Refusal as refusal:
        out(f"REFUSED: {refusal}")
        return 2
    out(f"preflight passed: {sum(counts.values())} rows in {len(counts)} tables, "
        f"every value converts; legacy timestamps read as {zone_name}")
    if dry_run:
        for table in order:
            out(f"  {table}: {counts[table]}")
        out("dry run: nothing written")
        source.close()
        return 0
    with source, psycopg.connect(target) as conn:
        copied = load(conn, source, order, legacy_zone)
        problems = prove(conn, source, order, legacy_zone)
        if problems:
            conn.rollback()
            out("PROOF FAILED, nothing committed:")
            for problem in problems:
                out(f"  {problem}")
            return 1
        conn.commit()
    source.close()
    out(f"copied and proven: {sum(copied.values())} rows in {len(copied)} tables, "
        f"every row and balance identical ({time.monotonic() - started:.1f} s)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True,
                        help="the 2.20 SQLite database, opened read-only")
    parser.add_argument("--target", default="",
                        help="an empty PostgreSQL database (default: libpq's PG* env)")
    parser.add_argument("--legacy-timezone", required=True,
                        help="the zone the host wrote offset-less timestamps in, "
                             "e.g. Europe/Budapest")
    parser.add_argument("--dry-run", action="store_true",
                        help="preflight and census only; write nothing")
    arguments = parser.parse_args(argv)
    return run(arguments.source, arguments.target, arguments.legacy_timezone,
               arguments.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
