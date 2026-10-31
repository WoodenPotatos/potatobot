"""scripts/offsite_backup.py: the offsite copy, database-only.

A prior attempt (`backup.sh`, deleted 2026-05-20) copied the whole project
tree via `rclone`, which would have swept `.env` into the destination. These
tests pin the two things that make the replacement safe: the snapshot is a
real, consistent copy of the data (not a placeholder), and the tool never
touches anything but the one database file it was given.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import offsite_backup


class BackupSnapshotTests(unittest.TestCase):
    def test_the_snapshot_holds_the_same_rows_as_the_source(self):
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            destination = Path(workdir) / "snapshot.db"

            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER)")
                conn.execute("INSERT INTO users VALUES (1, 500)")
                conn.commit()

            offsite_backup.backup_snapshot(source, destination)

            with sqlite3.connect(destination) as conn:
                rows = conn.execute("SELECT user_id, balance FROM users").fetchall()
            self.assertEqual([(1, 500)], rows)

    def test_the_source_is_opened_explicitly_read_only(self):
        """The deployed unit runs under `ProtectSystem=strict`, which makes
        the filesystem read-only to it except explicitly listed paths.
        sqlite3's default open mode is read-write-create, which fails with
        "unable to open database file" against a read-only mount even though
        nothing here ever writes to the source -- confirmed live against the
        actual deployed unit. A permission-based local repro of the mount
        being read-only does not reliably reproduce that failure (SQLite's
        own read/write-fallback behavior differs from a real EROFS mount),
        so this pins the fix structurally instead: the source must be opened
        through a `file:` URI with `mode=ro`, in `uri` mode -- never the
        default positional open sqlite3.connect(path) would otherwise use."""
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            destination = Path(workdir) / "snapshot.db"
            sqlite3.connect(source).close()

            real_connect = sqlite3.connect
            calls = []

            def spy_connect(*args, **kwargs):
                calls.append((args, kwargs))
                return real_connect(*args, **kwargs)

            with patch("scripts.offsite_backup.sqlite3.connect", side_effect=spy_connect):
                offsite_backup.backup_snapshot(source, destination)

            source_call = calls[0]
            self.assertTrue(source_call[1].get("uri") or
                             (len(source_call[0]) > 1 and source_call[0][1]),
                             "the source connection must be opened with uri=True")
            arg = source_call[0][0]
            self.assertTrue(arg.startswith("file:"), arg)
            self.assertIn("mode=ro", arg)

    def test_the_snapshot_is_independent_of_the_source_afterward(self):
        """A later write to the source must not appear in an already-taken
        snapshot -- it is a copy, not a live view."""
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            destination = Path(workdir) / "snapshot.db"

            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
                conn.commit()

            offsite_backup.backup_snapshot(source, destination)

            with sqlite3.connect(source) as conn:
                conn.execute("INSERT INTO users VALUES (99)")
                conn.commit()

            with sqlite3.connect(destination) as conn:
                rows = conn.execute("SELECT user_id FROM users").fetchall()
            self.assertEqual([], rows)


def _stamp(days_ago: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")


class LocalBackupPruneTests(unittest.TestCase):
    """The pre-migration copies beside the database are the other half of
    retention: the Privacy Policy promises they go 90 days after the migration
    they protect, and this flag is what keeps that promise. Everything here
    pins what it must *not* delete."""

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.root = Path(self.workdir.name)
        self.database = self.root / "economy.db"
        sqlite3.connect(self.database).close()
        self.old = self.root / f"economy.db.backup-v9-{_stamp(200)}"
        self.recent = self.root / f"economy.db.backup-v25-{_stamp(10)}"
        self.undatable = self.root / "economy.db.backup-v5-garbage"
        self.other = self.root / f"other.db.backup-v2-{_stamp(200)}"
        self.wal = self.root / "economy.db-wal"
        for path in (self.old, self.recent, self.undatable, self.other, self.wal):
            path.write_bytes(b"x")
        # A symlink wearing an old backup's name, pointing at the live file.
        self.link = self.root / f"economy.db.backup-v1-{_stamp(400)}"
        self.link.symlink_to(self.database)

    def tearDown(self):
        self.workdir.cleanup()

    def test_only_this_databases_dated_copies_older_than_the_window_are_due(self):
        due = offsite_backup.local_backups_due(self.database, 90)
        self.assertEqual([self.old], due)

    def test_the_default_of_zero_keeps_every_copy(self):
        self.assertEqual([], offsite_backup.local_backups_due(self.database, 0))
        self.assertEqual([], offsite_backup.local_backups_due(self.database, -1))

    def test_the_name_decides_not_the_modification_time(self):
        """A copy resets mtime; the podman cutover did. A fresh mtime on an old
        name is still old, and an old mtime on a recent name is still recent."""
        import os
        import time
        ancient = time.time() - 400 * 86400
        os.utime(self.recent, (ancient, ancient))
        os.utime(self.old, None)
        self.assertEqual([self.old], offsite_backup.local_backups_due(self.database, 90))

    def test_prune_deletes_the_due_copies_and_nothing_else(self):
        removed = offsite_backup.prune_local_backups(self.database, 90)
        self.assertEqual([self.old], removed)
        self.assertFalse(self.old.exists())
        for kept in (self.database, self.recent, self.undatable, self.other, self.wal):
            self.assertTrue(kept.exists(), kept.name)
        self.assertTrue(self.link.is_symlink())
        self.assertTrue(self.database.exists(), "the symlink's target is the live file")

    def test_the_cutoff_is_exact_to_the_day(self):
        from datetime import datetime
        now = datetime(2026, 10, 31, 12, 0, 0)
        boundary = self.root / "economy.db.backup-v10-20260802-120000"
        just_inside = self.root / "economy.db.backup-v11-20260802-115959"
        boundary.write_bytes(b"x")
        just_inside.write_bytes(b"x")
        due = offsite_backup.local_backups_due(self.database, 90, now=now)
        self.assertIn(just_inside, due)
        self.assertNotIn(boundary, due)


class MainTests(unittest.TestCase):
    def test_the_local_prune_runs_only_after_the_offsite_copy_is_verified(self):
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            sqlite3.connect(source).close()
            old = Path(workdir) / f"source.db.backup-v3-{_stamp(200)}"
            old.write_bytes(b"x")

            import subprocess

            def failing_check(args, check):
                if args[1] == "check":
                    raise subprocess.CalledProcessError(1, args)

            with patch("scripts.offsite_backup.shutil.which", return_value="/usr/bin/rclone"), \
                 patch("scripts.offsite_backup.subprocess.run", side_effect=failing_check):
                with self.assertRaises(subprocess.CalledProcessError):
                    offsite_backup.main([str(source), "r:", "--prune-local-backups-days", "90"])
            self.assertTrue(old.exists(), "a failed verification must not cost a local copy")

            with patch("scripts.offsite_backup.shutil.which", return_value="/usr/bin/rclone"), \
                 patch("scripts.offsite_backup.subprocess.run"):
                offsite_backup.main([str(source), "r:", "--prune-local-backups-days", "90"])
            self.assertFalse(old.exists())

    def test_without_the_flag_main_leaves_local_copies_alone(self):
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            sqlite3.connect(source).close()
            old = Path(workdir) / f"source.db.backup-v3-{_stamp(200)}"
            old.write_bytes(b"x")
            with patch("scripts.offsite_backup.shutil.which", return_value="/usr/bin/rclone"), \
                 patch("scripts.offsite_backup.subprocess.run"):
                offsite_backup.main([str(source), "r:"])
            self.assertTrue(old.exists())

    def test_refuses_a_database_that_does_not_exist(self):
        with tempfile.TemporaryDirectory() as workdir:
            missing = Path(workdir) / "nope.db"
            with self.assertRaises(SystemExit):
                offsite_backup.main([str(missing), "gdrivebot:PotatoBotBackups"])

    def test_refuses_when_rclone_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            sqlite3.connect(source).close()
            with patch("scripts.offsite_backup.shutil.which", return_value=None):
                with self.assertRaises(SystemExit):
                    offsite_backup.main([str(source), "gdrivebot:PotatoBotBackups"])

    def test_uploads_verifies_then_prunes_with_the_given_remote_and_retention(self):
        """The whole project tree is never in scope: only the one snapshot
        file this run produced is ever named in the upload call."""
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            sqlite3.connect(source).close()

            with patch("scripts.offsite_backup.shutil.which", return_value="/usr/bin/rclone"), \
                 patch("scripts.offsite_backup.subprocess.run") as mocked_run:
                offsite_backup.main([
                    str(source), "gdrivebot:PotatoBotBackups", "--retention-days", "7",
                ])

            self.assertEqual(3, mocked_run.call_count)
            upload_call, check_call, prune_call = mocked_run.call_args_list

            upload_args = upload_call.args[0]
            self.assertEqual("rclone", upload_args[0])
            self.assertEqual("copy", upload_args[1])
            self.assertTrue(upload_args[2].endswith(".db"))
            self.assertEqual("gdrivebot:PotatoBotBackups", upload_args[3])

            # The check names this run's snapshot alone, and compares bytes:
            # a crypt remote has no plaintext hashes to compare.
            check_args = check_call.args[0]
            self.assertEqual(["rclone", "check"], check_args[:2])
            self.assertEqual("gdrivebot:PotatoBotBackups", check_args[3])
            self.assertIn("--download", check_args)
            self.assertIn("--one-way", check_args)
            self.assertEqual(Path(upload_args[2]).name,
                             check_args[check_args.index("--include") + 1])
            self.assertEqual(str(Path(upload_args[2]).parent), check_args[2])

            prune_args = prune_call.args[0]
            self.assertEqual(["rclone", "delete", "gdrivebot:PotatoBotBackups",
                              "--min-age", "7d"], prune_args)

    def test_a_failed_check_stops_the_run_before_the_prune(self):
        """An upload that did not land must never be followed by a prune."""
        import subprocess
        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "source.db"
            sqlite3.connect(source).close()

            def fake_run(args, check):
                if args[1] == "check":
                    raise subprocess.CalledProcessError(1, args)

            with patch("scripts.offsite_backup.shutil.which", return_value="/usr/bin/rclone"), \
                 patch("scripts.offsite_backup.subprocess.run", side_effect=fake_run) as mocked_run:
                with self.assertRaises(subprocess.CalledProcessError):
                    offsite_backup.main([str(source), "gdrivebot:PotatoBotBackups"])

            self.assertNotIn("delete", [call.args[0][1] for call in mocked_run.call_args_list])


if __name__ == "__main__":
    unittest.main()
