"""Rehearse a schema migration on a copy of a deployed PostgreSQL database.

Never point the target at the live database. It dumps the source, restores the
dump into an empty scratch database you name, snapshots it, migrates it through
`update_db.py` (the path the bot itself takes), snapshots again and reports what
changed. The dump and both snapshots are kept, so the same pair can be compared
again later and the dump doubles as the rollback artefact.

The target is created by you, not by this tool -- an empty database on a
throwaway server, or one the deployment's role owns -- so the tool needs no
right to create databases and cannot create one in the wrong place.

Usage:
    python scripts/rehearse_migration.py --target "host=127.0.0.1 port=55432 dbname=rehearsal"
    python scripts/rehearse_migration.py --source "dbname=potatobot" --target "..." --workdir /tmp/r

The source defaults to libpq's PG* environment. Needs `pg_dump` and `pg_restore`
at least as new as the server (the bot's image carries them). Exits non-zero if
a refusal fires, the migration fails or any table lost or rewrote rows, so it
can gate a deployment.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.db_snapshot import compare, record  # noqa: E402

# libpq connection parameters and the environment variables that carry them,
# so the migration subprocess reaches the target the way the bot reaches its
# database, with no connection string in its arguments.
_PG_ENVIRONMENT = {"host": "PGHOST", "port": "PGPORT", "dbname": "PGDATABASE",
                   "user": "PGUSER", "password": "PGPASSWORD",
                   "sslmode": "PGSSLMODE", "passfile": "PGPASSFILE"}


def _identity(dsn: str) -> tuple:
    """Where a connection string points, for telling two databases apart."""
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        info = conn.info
        return (info.host, info.port, info.dbname)


def preflight(source: str, target: str) -> None:
    """Every refusal, before anything is written."""
    for tool in ("pg_dump", "pg_restore"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is not on PATH (run this inside the bot's image)")
    try:
        source_identity = _identity(source)
        target_identity = _identity(target)
    except psycopg.Error as exc:
        raise SystemExit(f"cannot reach a database: {exc}") from exc
    if source_identity == target_identity:
        raise SystemExit("the target is the source; rehearse on a scratch database")
    with psycopg.connect(target) as conn:
        tables = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'").fetchone()[0]
    if tables:
        raise SystemExit("the target database is not empty; give an empty scratch database")


def target_environment(target: str, workdir: Path) -> dict:
    """The environment `update_db.py` migrates the target in: libpq pointed at
    the target, and the data directory at this rehearsal's workdir, because the
    bot dumps the database before migrating it into that directory -- never
    the live backups beside the real database."""
    environment = {key: value for key, value in os.environ.items()
                   if key not in _PG_ENVIRONMENT.values()}
    for key, value in conninfo_to_dict(target).items():
        if key in _PG_ENVIRONMENT and value is not None:
            environment[_PG_ENVIRONMENT[key]] = str(value)
    environment["POTATOBOT_DATA_DIR"] = str(workdir)
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="",
                        help="the deployed database (default: libpq's PG* environment)")
    parser.add_argument("--target", required=True,
                        help="an empty scratch database to restore into and migrate")
    parser.add_argument(
        "--workdir", type=Path, default=None,
        help="where to keep the dump and snapshots (default: ./migration-rehearsal-<stamp>)",
    )
    arguments = parser.parse_args()

    preflight(arguments.source, arguments.target)

    # Local time on purpose: a directory name an operator reads.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = (arguments.workdir or Path.cwd() / f"migration-rehearsal-{stamp}").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    dump = workdir / "source.dump"

    print(f"1. dumping the source -> {dump}")
    # Owner-only before pg_dump writes a byte: the dump is every member's data.
    descriptor = os.open(dump, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        dumped = subprocess.run(
            ["pg_dump", "--format=custom", "--dbname", arguments.source],
            stdout=output, stderr=subprocess.PIPE, check=False)
    if dumped.returncode != 0:
        print(f"   dump FAILED:\n{dumped.stderr.decode(errors='replace').strip()}")
        return 1

    print("2. restoring into the target")
    restored = subprocess.run(
        ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
         "--dbname", arguments.target, str(dump)],
        capture_output=True, text=True, check=False)
    if restored.returncode != 0:
        print(f"   restore FAILED:\n{restored.stderr.strip()}")
        return 1

    before_path = workdir / "before.json"
    after_path = workdir / "after.json"

    print("3. snapshotting before migration")
    before = record(arguments.target)
    before_path.write_text(json.dumps(before, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"   schema {before['schema_version']}, "
          f"{sum(before['table_counts'].values())} rows across "
          f"{len(before['table_counts'])} tables")

    print("4. migrating the target")
    environment = target_environment(arguments.target, workdir)
    result = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "update_db.py")],
        env=environment, cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, check=False,
    )
    if result.stdout.strip():
        print(f"   {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"   migration FAILED:\n{result.stderr.strip()}")
        return 1

    print("5. snapshotting after migration")
    after = record(arguments.target)
    after_path.write_text(json.dumps(after, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")

    print("6. comparing")
    exit_code = compare(before, after)

    print(f"\nartefacts kept in {workdir}")
    print(f"  pre-migration dump (rollback artefact): {dump}")
    print(f"  the bot's own pre-migration dump: {workdir / 'backups'}")
    print(f"  snapshots: {before_path.name}, {after_path.name}")
    if exit_code == 0:
        print("\nRehearsal succeeded. The bot takes its own dump before migrating; keep "
              "this one too until the acceptance run passes.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
