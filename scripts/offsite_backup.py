"""Copy a consistent database dump off the host, and prune old copies.

Every backup this repository already took lived on the single production
host -- the pre-migration dump, all of it. This is the offsite half: the
database only, never the project tree. A prior attempt (`backup.sh`, deleted
2026-05-20) copied the whole tree via `rclone`, which would have swept `.env`
-- live credentials -- into the destination. This copies exactly one file,
the dump it just took.

The dump is `pg_dump --format=custom`, a consistent snapshot taken inside one
transaction, so a live writer never produces a torn copy. Before upload,
`pg_restore --list` reads the archive's table of contents, so a truncated or
corrupt dump is refused rather than shipped. Each run uploads, then downloads
that copy back and compares it, and only then prunes. Point it at an `rclone
crypt` remote so the copy is encrypted before it leaves the host; the script
itself is the same either way.

Usage:
    python scripts/offsite_backup.py gdrivecrypt:
    python scripts/offsite_backup.py gdrivecrypt: --retention-days 30
    python scripts/offsite_backup.py gdrivecrypt: \\
        --dump-command "podman exec potatobot-db pg_dump --format=custom -U potatobot potatobot" \\
        --restore-command "podman exec -i potatobot-db pg_restore" \\
        --backups-dir /var/lib/potatobot/bot/backups --prune-local-backups-days 90

By default the dump is `pg_dump` against libpq's PG* environment; the deployed
unit runs both PostgreSQL tools inside the database's own container, which has
no published port. A command writes the dump to its standard output.

`--prune-local-backups-days` is the other half of retention: the dumps
`initialize_database` takes before each migration (`potatobot-v<n>-<stamp>.dump`
in `--backups-dir`), and the SQLite pre-migration copies of 2.20 and earlier
(`economy.db.backup-v<n>-<stamp>` beside `--legacy-sqlite`), stay on the host as
the rollback artefact, and nothing else removes them. The Privacy Policy
(`docs/legal/PRIVACY.md` §7) promises they go 90 days after the migration they
protect; passing the flag is what keeps that promise. It runs only after this
night's offsite copy has been verified, decides by the date in the file's
*name* (a copy resets the modification time, and the podman cutover did exactly
that), and skips any file it cannot date. Off by default, so a self-hoster who
never read this keeps every copy.

Requires `rclone` on PATH and a remote already configured with `rclone config`
-- this script only invokes it, it does not set one up.
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# What `database._pre_migration_backup` names a dump:
# `potatobot-v<schema version>-<YYYYMMDD-HHMMSS>.dump`, and what 2.20's
# SQLite migrations named a copy: `<database file>.backup-v<n>-<stamp>`. Both
# stamps are host-local time (the one `datetime.now()` the project allows,
# being a name).
DUMP_NAME = re.compile(r"^potatobot-v(?P<version>\d+)-(?P<stamp>\d{8}-\d{6})\.dump$")
LEGACY_NAME = re.compile(
    r"^(?P<stem>.+)\.backup-v(?P<version>\d+)-(?P<stamp>\d{8}-\d{6})$")


def take_dump(command: list[str], destination: Path) -> None:
    """Run the dump command into a new owner-only file, or raise.

    `O_EXCL` refuses a file already there and 0600 holds whatever umask the
    caller has: the dump is every member's data. A failed or empty dump leaves
    no file behind.
    """
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        completed = subprocess.run(command, stdout=output, check=False)
    if completed.returncode != 0 or destination.stat().st_size == 0:
        destination.unlink()
        raise SystemExit(f"the dump failed (exit {completed.returncode})")


def check_dump(command: list[str], dump: Path) -> None:
    """Read the archive's table of contents, or raise: a dump `pg_restore`
    cannot list is a dump nobody can restore. The file goes in on standard
    input, so the command may run in another container."""
    with dump.open("rb") as archive:
        completed = subprocess.run([*command, "--list"], stdin=archive,
                                   stdout=subprocess.DEVNULL, check=False)
    if completed.returncode != 0:
        raise SystemExit("the dump is not a readable archive; nothing was uploaded")


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
    file included checks this run's dump alone, not the older copies beside
    it. Raises on a mismatch or a missing file, which stops the run before
    `prune`: a night whose upload did not land never deletes the copies that
    did.
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


def _dated(candidate: Path, pattern: re.Pattern, stem: str | None = None):
    """The stamp a pre-migration copy's name carries, or None."""
    if candidate.is_symlink() or not candidate.is_file():
        return None
    match = pattern.match(candidate.name)
    if match is None or (stem is not None and match.group("stem") != stem):
        return None
    try:
        return datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def local_backups_due(backups_dir: Path | None, legacy_sqlite: Path | None,
                      max_age_days: int, now: datetime | None = None) -> list[Path]:
    """The pre-migration copies older than the window.

    Only a regular file named exactly as the bot names its dumps in
    `backups_dir`, or as 2.20 named copies of `legacy_sqlite` beside it, with a
    parseable stamp, qualifies; the stamp decides, never the modification
    time. A symlink, another database's copies, the WAL sidecars and a name
    that will not parse are all left alone: a file you cannot date is a file
    you do not delete. `max_age_days <= 0` means the feature is off.
    """
    if max_age_days <= 0:
        return []
    cutoff = (now or datetime.now()) - timedelta(days=max_age_days)
    candidates = []
    if backups_dir is not None and backups_dir.is_dir():
        candidates += [(path, _dated(path, DUMP_NAME))
                       for path in sorted(backups_dir.glob("potatobot-v*.dump"))]
    if legacy_sqlite is not None:
        candidates += [(path, _dated(path, LEGACY_NAME, legacy_sqlite.name))
                       for path in sorted(legacy_sqlite.parent.glob(
                           f"{legacy_sqlite.name}.backup-v*"))]
    return [path for path, taken in candidates if taken is not None and taken < cutoff]


def prune_local_backups(backups_dir: Path | None, legacy_sqlite: Path | None,
                        max_age_days: int, now: datetime | None = None) -> list[Path]:
    """Delete what `local_backups_due` names, and return it."""
    due = local_backups_due(backups_dir, legacy_sqlite, max_age_days, now)
    for path in due:
        path.unlink()
    return due


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("remote", help="an rclone remote:path, e.g. gdrivecrypt:")
    parser.add_argument("--dump-command", default="pg_dump --format=custom",
                        help="writes a custom-format dump to stdout "
                             "(default: pg_dump against libpq's PG* environment)")
    parser.add_argument("--restore-command", default="pg_restore",
                        help="pg_restore, reading an archive on stdin (default: pg_restore)")
    parser.add_argument("--retention-days", type=int, default=30,
                         help="delete remote copies older than this many days (default: 30)")
    parser.add_argument("--backups-dir", type=Path, default=None,
                        help="where the bot writes its pre-migration dumps "
                             "(the data directory's backups/)")
    parser.add_argument("--legacy-sqlite", type=Path, default=None,
                        help="the SQLite database 2.20 ran on, whose pre-migration "
                             "copies beside it are pruned too")
    parser.add_argument("--prune-local-backups-days", type=int, default=0,
                         help="also delete the pre-migration copies whose name-stamp "
                              "is older than this many days, after the offsite copy "
                              "has been verified (default: 0, keep them all)")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="an existing directory to stage the night's dump in "
                             "(default: the system temporary directory)")
    arguments = parser.parse_args(argv)

    # The dump is every member's data in plaintext, and so is the copy the
    # check downloads back. Where the database sits on an encrypted volume,
    # staging them in /tmp puts a nightly plaintext copy on the plain disk
    # (deleted, but its blocks stay), so the deployed unit names a directory
    # inside the volume. Refused rather than created: a mistyped path must not
    # quietly become a directory on the wrong disk, and TMPDIR is no
    # substitute, because tempfile falls back to /tmp in silence.
    workdir_parent = arguments.workdir
    if workdir_parent is not None and (
            workdir_parent.is_symlink() or not workdir_parent.is_dir()):
        raise SystemExit(f"--workdir {workdir_parent} is not an existing directory")

    dump_command = shlex.split(arguments.dump_command)
    restore_command = shlex.split(arguments.restore_command)
    for tool in ("rclone", dump_command[0], restore_command[0]):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(dir=workdir_parent) as workdir:
        snapshot = Path(workdir) / f"potatobot-{stamp}.dump"
        take_dump(dump_command, snapshot)
        check_dump(restore_command, snapshot)
        upload(snapshot, arguments.remote)
        verify(snapshot, arguments.remote)

    prune(arguments.remote, arguments.retention_days)
    # Only now: a night whose upload did not land has already raised above, so
    # a local copy is never removed on the strength of an offsite copy that is
    # not there.
    for removed in prune_local_backups(arguments.backups_dir, arguments.legacy_sqlite,
                                       arguments.prune_local_backups_days):
        print(f"Removed pre-migration backup {removed.name} (older than "
              f"{arguments.prune_local_backups_days} days)")

    print(f"Offsite backup finished at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
