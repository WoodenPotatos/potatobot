"""Record or compare a PostgreSQL database fingerprint.

The backlog requires a schema version and table counts before and after every
migration, and a migration must rewrite no row. A fingerprint records, per
table, the row count, the columns and a content hash computed in the server,
so a rehearsal can prove that data survived and was left alone.

Usage:
    python scripts/db_snapshot.py record > before.json          # libpq's PG* env
    python scripts/db_snapshot.py record "dbname=rehearsal" > after.json
    python scripts/db_snapshot.py compare before.json after.json

`compare` exits non-zero when a table lost rows or columns, disappeared, or had
its rows rewritten, which is what makes it usable as a gate in a rehearsal
script or a release checklist. The fingerprint names the database and host,
never a password.
"""

import argparse
import json
import sys
from pathlib import Path

import psycopg
from psycopg import sql


def record(dsn: str = "") -> dict:
    """Fingerprint a database without modifying it.

    One `REPEATABLE READ, READ ONLY` transaction, so every table is read at the
    same instant and the snapshot can never be the thing that changes the data.
    A table's content hash is order-independent: the md5 of its rows' sorted
    md5s, over each row's text form.
    """
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        tables = [row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name")]
        version = None
        if "schema_migrations" in tables:
            version = conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        counts, columns, hashes = {}, {}, {}
        for table in tables:
            name = sql.Identifier(table)
            counts[table] = conn.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(name)).fetchone()[0]
            columns[table] = [row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY column_name", (table,))]
            hashes[table] = conn.execute(sql.SQL(
                "SELECT md5(COALESCE(string_agg(h, '' ORDER BY h), '')) "
                "FROM (SELECT md5(t::text) AS h FROM {} AS t) AS rows"
            ).format(name)).fetchone()[0]
        return {
            "database": conn.info.dbname,
            "host": conn.info.host,
            "server_version": conn.info.server_version,
            "schema_version": version,
            "table_counts": counts,
            "table_columns": columns,
            "table_hashes": hashes,
        }


def compare(before: dict, after: dict) -> int:
    """Report what changed between two snapshots. Returns a process exit code."""
    problems = []
    notes = []

    if before["schema_version"] != after["schema_version"]:
        notes.append(
            f"schema version {before['schema_version']} -> {after['schema_version']}"
        )

    added = sorted(set(after["table_counts"]) - set(before["table_counts"]))
    removed = sorted(set(before["table_counts"]) - set(after["table_counts"]))
    if added:
        notes.append(f"tables added: {', '.join(added)}")
    if removed:
        problems.append(f"tables removed: {', '.join(removed)}")

    for table in sorted(set(before["table_counts"]) & set(after["table_counts"])):
        old, new = before["table_counts"][table], after["table_counts"][table]
        if table == "schema_migrations" and new > old:
            notes.append(f"{table}: {new - old} migration(s) recorded")
        elif old != new:
            problems.append(f"{table}: {old} -> {new} rows")
        old_columns = set(before["table_columns"].get(table, []))
        new_columns = set(after["table_columns"].get(table, []))
        if new_columns - old_columns:
            notes.append(f"{table}: columns added {', '.join(sorted(new_columns - old_columns))}")
        if old_columns - new_columns:
            problems.append(f"{table}: columns removed {', '.join(sorted(old_columns - new_columns))}")
        if table == "schema_migrations":
            continue  # a migration adds its own row, noted above
        if old_columns == new_columns and old == new \
                and before["table_hashes"][table] != after["table_hashes"][table]:
            # Same rows, same columns, different content: something rewrote
            # rows, which a migration must not do.
            problems.append(f"{table}: rows rewritten (content hash changed)")

    for note in notes:
        print(f"  note    {note}")
    for problem in problems:
        print(f"  PROBLEM {problem}")
    if not notes and not problems:
        print("  identical")

    print(
        f"\n{'FAILED' if problems else 'OK'}: "
        f"{len(problems)} problem(s), {len(notes)} expected change(s)"
    )
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    record_parser = subcommands.add_parser("record", help="print a snapshot as JSON")
    record_parser.add_argument(
        "dsn", nargs="?", default="",
        help="a libpq connection string; empty uses the PG* environment")

    compare_parser = subcommands.add_parser("compare", help="diff two snapshots")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)

    arguments = parser.parse_args()
    if arguments.command == "record":
        json.dump(record(arguments.dsn), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    return compare(
        json.loads(arguments.before.read_text(encoding="utf-8")),
        json.loads(arguments.after.read_text(encoding="utf-8")),
    )


if __name__ == "__main__":
    raise SystemExit(main())
