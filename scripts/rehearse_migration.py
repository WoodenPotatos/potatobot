"""Rehearse a schema migration on a copy of a deployed database.

Never point this at the live file. It copies the database, snapshots it, migrates
the copy, snapshots again, and reports what changed. The copy and both snapshots
are kept so the same pair can be compared again later, and so the pre-migration
copy doubles as the rollback artefact.

Usage:
    python scripts/rehearse_migration.py /opt/potatobot/economy.db
    python scripts/rehearse_migration.py economy.db --workdir /tmp/rehearsal

Exits non-zero if the migration fails or if any table's row count changed, so it
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

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.db_snapshot import compare, record  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="the deployed database to rehearse")
    parser.add_argument(
        "--workdir", type=Path, default=None,
        help="where to place the copy and snapshots (default: ./migration-rehearsal-<stamp>)",
    )
    arguments = parser.parse_args()

    source = arguments.database.resolve()
    if not source.exists():
        raise SystemExit(f"database not found: {source}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = (arguments.workdir or Path.cwd() / f"migration-rehearsal-{stamp}").resolve()
    if workdir == source.parent:
        raise SystemExit("choose a workdir outside the deployed database's directory")
    workdir.mkdir(parents=True, exist_ok=True)

    copy = workdir / source.name
    print(f"1. copying {source} -> {copy}")
    shutil.copy2(source, copy)
    # WAL and shared-memory sidecars carry committed data that has not been
    # checkpointed yet; without them the copy can be missing recent writes.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{source}{suffix}")
        if sidecar.exists():
            shutil.copy2(sidecar, Path(f"{copy}{suffix}"))
            print(f"   copied sidecar {sidecar.name}")

    before_path = workdir / "before.json"
    after_path = workdir / "after.json"

    print("2. snapshotting before migration")
    before = record(copy)
    before_path.write_text(json.dumps(before, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"   schema {before['schema_version']}, "
          f"integrity {before['integrity_check']}, "
          f"{sum(before['table_counts'].values())} rows across "
          f"{len(before['table_counts'])} tables")

    print("3. migrating the copy")
    # update_db.py runs the identical initialization path the bot uses.
    environment = dict(os.environ, POTATOBOT_DB_PATH=str(copy))
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

    print("4. snapshotting after migration")
    after = record(copy)
    after_path.write_text(json.dumps(after, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")

    print("5. comparing")
    exit_code = compare(before, after)

    print(f"\nartefacts kept in {workdir}")
    print(f"  pre-migration copy (rollback artefact): {copy}")
    print(f"  snapshots: {before_path.name}, {after_path.name}")
    if exit_code == 0:
        print("\nRehearsal succeeded. Back up the live database before applying this "
              "for real, and keep the pre-migration copy until the acceptance run passes.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
