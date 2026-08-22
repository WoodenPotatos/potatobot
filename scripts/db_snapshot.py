"""Record or compare a SQLite database fingerprint.

The backlog requires a checksum, integrity result, schema version and table
counts before and after every migration. Nothing else in the repository produced
those, so a rehearsal had no way to prove that data survived.

Usage:
    python scripts/db_snapshot.py record economy.db > before.json
    python scripts/db_snapshot.py record economy.db > after.json
    python scripts/db_snapshot.py compare before.json after.json

`compare` exits non-zero when any table's row count changed, which is what makes
it usable as a gate in a rehearsal script or a release checklist.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def record(path: Path) -> dict:
    """Fingerprint a database without modifying it."""
    if not path.exists():
        raise SystemExit(f"database not found: {path}")

    # Read-only so a snapshot can never be the thing that changes the file.
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [
            list(row) for row in conn.execute("PRAGMA foreign_key_check")
        ]
        tables = sorted(
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
        counts = {}
        columns = {}
        for table in tables:
            counts[table] = conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            columns[table] = sorted(
                row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
            )

    return {
        "path": str(path),
        "schema_version": version,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
        "file_sha256": file_checksum(path),
        "table_counts": counts,
        "table_columns": columns,
    }


def compare(before: dict, after: dict) -> int:
    """Report what changed between two snapshots. Returns a process exit code."""
    problems = []
    notes = []

    if after["integrity_check"] != "ok":
        problems.append(f"integrity_check is {after['integrity_check']!r}")
    if after["foreign_key_violations"]:
        problems.append(
            f"{len(after['foreign_key_violations'])} foreign key violations"
        )

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
        if old != new:
            problems.append(f"{table}: {old} -> {new} rows")
        new_columns = sorted(
            set(after["table_columns"].get(table, []))
            - set(before["table_columns"].get(table, []))
        )
        lost_columns = sorted(
            set(before["table_columns"].get(table, []))
            - set(after["table_columns"].get(table, []))
        )
        if new_columns:
            notes.append(f"{table}: columns added {', '.join(new_columns)}")
        if lost_columns:
            problems.append(f"{table}: columns removed {', '.join(lost_columns)}")

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
    record_parser.add_argument("database", type=Path)

    compare_parser = subcommands.add_parser("compare", help="diff two snapshots")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)

    arguments = parser.parse_args()
    if arguments.command == "record":
        json.dump(record(arguments.database), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    return compare(
        json.loads(arguments.before.read_text(encoding="utf-8")),
        json.loads(arguments.after.read_text(encoding="utf-8")),
    )


if __name__ == "__main__":
    raise SystemExit(main())
