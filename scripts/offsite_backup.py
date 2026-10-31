"""Copy a consistent database snapshot off the host, and prune old copies.

Every backup this repository already took lived on the single production
host — the pre-migration copy, the manual `.backup` procedure in
`docs/installation.md`, all of it. This is the offsite half: the database
only, never the project tree. A prior attempt (`backup.sh`, deleted
2026-05-20) copied the whole tree via `rclone`, which would have swept
`.env` — live credentials — into the destination. This copies exactly one
file.

The snapshot itself is taken through SQLite's own online-backup API, the
same guarantee `docs/installation.md`'s manual `.backup` procedure relies
on, so a live writer never produces a torn copy.

Each run uploads, then downloads that copy back and compares it, and only
then prunes. Point it at an `rclone crypt` remote so the copy is encrypted
before it leaves the host; the script itself is the same either way.

Usage:
    python scripts/offsite_backup.py economy.db gdrivecrypt:
    python scripts/offsite_backup.py economy.db gdrivecrypt: --retention-days 30
    python scripts/offsite_backup.py economy.db gdrivecrypt: --prune-local-backups-days 90

`--prune-local-backups-days` is the other half of retention: the copies
`initialize_database` takes before each migration (`economy.db.backup-v<n>-
<stamp>`) stay beside the live database as the rollback artefact, and nothing
else ever removes them -- 21 full copies of every member's balance had
accumulated by 2026-10-31. The Privacy Policy (`docs/legal/PRIVACY.md` §7)
promises they go 90 days after the migration they protect; passing the flag
is what keeps that promise. It runs only after this night's offsite copy has
been verified, decides by the date in the file's *name* (a copy resets the
modification time, and the podman cutover did exactly that), and skips any
file it cannot date. Off by default, so a self-hoster who never read this
keeps every copy.

Requires `rclone` on PATH and a remote already configured with `rclone
config` — this script only invokes it, it does not set one up.
"""

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# What `database.initialize_database` names a pre-migration copy:
# `<database file name>.backup-v<schema version>-<YYYYMMDD-HHMMSS>`, the stamp
# in host-local time (the one `datetime.now()` the project allows, being a name).
BACKUP_NAME = re.compile(
    r"^(?P<stem>.+)\.backup-v(?P<version>\d+)-(?P<stamp>\d{8}-\d{6})$")


def backup_snapshot(source: Path, destination: Path) -> None:
    """A WAL-safe, consistent copy via SQLite's own online backup API.

    The source is opened explicitly read-only (a `file:` URI with
    `mode=ro`), not sqlite3's default read-write-create mode: the deployed
    unit runs under `ProtectSystem=strict`, which makes the whole
    filesystem read-only to it except explicitly listed paths, and a
    read-write open against a read-only mount fails with "unable to open
    database file" even though every operation here only ever reads from
    the source.
    """
    # `with conn:` only wraps the transaction (commit/rollback on exit) --
    # sqlite3.Connection does not close itself there, so both are closed
    # explicitly rather than left for garbage collection.
    source_uri = f"file:{source}?mode=ro"
    src = sqlite3.connect(source_uri, uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def upload(snapshot: Path, remote: str) -> None:
    subprocess.run(
        ["rclone", "copy", str(snapshot), remote, "--checksum"],
        check=True,
    )


def verify(snapshot: Path, remote: str) -> None:
    """Download the uploaded copy and compare it byte for byte.

    `--download` rather than a hash comparison, because an `rclone crypt`
    remote stores no plaintext hashes, and the plain `check` it would
    otherwise fall back to compares sizes only. `--one-way` with the one
    file included checks this run's snapshot alone, not the older copies
    beside it. Raises on a mismatch or a missing file, which stops the run
    before `prune`: a night whose upload did not land never deletes the
    copies that did.
    """
    subprocess.run(
        ["rclone", "check", str(snapshot.parent), remote,
         "--one-way", "--download", "--include", snapshot.name],
        check=True,
    )


def prune(remote: str, retention_days: int) -> None:
    subprocess.run(
        ["rclone", "delete", remote, "--min-age", f"{retention_days}d"],
        check=True,
    )


def local_backups_due(database: Path, max_age_days: int,
                      now: datetime | None = None) -> list[Path]:
    """The pre-migration copies beside `database` older than the window.

    Only a regular file in the same directory, named exactly for this database
    with a parseable stamp, qualifies; the stamp decides, never the modification
    time. A symlink, another database's copies, the WAL sidecars and a name that
    will not parse are all left alone: a file you cannot date is a file you do
    not delete. `max_age_days <= 0` means the feature is off.
    """
    if max_age_days <= 0:
        return []
    cutoff = (now or datetime.now()) - timedelta(days=max_age_days)
    due = []
    for candidate in sorted(database.parent.glob(f"{database.name}.backup-v*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        match = BACKUP_NAME.match(candidate.name)
        if match is None or match.group("stem") != database.name:
            continue
        try:
            taken = datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if taken < cutoff:
            due.append(candidate)
    return due


def prune_local_backups(database: Path, max_age_days: int,
                        now: datetime | None = None) -> list[Path]:
    """Delete what `local_backups_due` names, and return it."""
    due = local_backups_due(database, max_age_days, now)
    for path in due:
        path.unlink()
    return due


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("database", type=Path, help="the live database to back up")
    parser.add_argument("remote", help="an rclone remote:path, e.g. gdrivecrypt:")
    parser.add_argument("--retention-days", type=int, default=30,
                         help="delete remote copies older than this many days (default: 30)")
    parser.add_argument("--prune-local-backups-days", type=int, default=0,
                         help="also delete the pre-migration copies beside the database "
                              "whose name-stamp is older than this many days, after the "
                              "offsite copy has been verified (default: 0, keep them all)")
    arguments = parser.parse_args(argv)

    source = arguments.database.resolve()
    if not source.exists():
        raise SystemExit(f"database not found: {source}")

    if shutil.which("rclone") is None:
        raise SystemExit("rclone not found on PATH")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as workdir:
        snapshot = Path(workdir) / f"{source.stem}-{stamp}.db"
        backup_snapshot(source, snapshot)
        upload(snapshot, arguments.remote)
        verify(snapshot, arguments.remote)

    prune(arguments.remote, arguments.retention_days)
    # Only now: a night whose upload did not land has already raised above, so
    # a local copy is never removed on the strength of an offsite copy that is
    # not there.
    for removed in prune_local_backups(source, arguments.prune_local_backups_days):
        print(f"Removed pre-migration backup {removed.name} (older than "
              f"{arguments.prune_local_backups_days} days)")

    print(f"Offsite backup finished at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
