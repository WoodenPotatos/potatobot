"""scripts/offsite_backup.py: the offsite copy, database-only.

A prior attempt (`backup.sh`, deleted 2026-05-20) copied the whole project
tree via `rclone`, which would have swept `.env` into the destination. These
tests pin what makes the replacement safe: the dump is a real, readable
archive before anything leaves the host, only that one file is ever uploaded,
nothing is pruned on the strength of a copy that did not verify, and the local
prune deletes only what it can date.

The PostgreSQL tools are stand-in scripts on a temporary PATH, so the real
`take_dump` and `check_dump` run; only `rclone` is intercepted.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import offsite_backup  # noqa: E402

_real_run = subprocess.run


def _stamp(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")


class StandInTools:
    """`pg_dump` and `pg_restore` stand-ins, and an `rclone` that is recorded."""

    def __init__(self, test, dump_exit=0, dump_output="PGDMP-stand-in", restore_exit=0,
                 failing_rclone=None):
        self.workdir = tempfile.TemporaryDirectory()
        test.addCleanup(self.workdir.cleanup)
        self.bin = Path(self.workdir.name) / "bin"
        self.bin.mkdir()
        self._script("pg_dump", f"printf '%s' '{dump_output}'\nexit {dump_exit}\n")
        self._script("pg_restore", f"cat > /dev/null\nexit {restore_exit}\n")
        self._script("rclone", "exit 0\n")
        self.failing_rclone = failing_rclone
        self.rclone_calls = []
        self.uploaded = None

    def _script(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def run(self, args, *pargs, **kwargs):
        if args[0] == "rclone":
            self.rclone_calls.append(args)
            if args[1] == "copy":
                self.uploaded = Path(args[2]).read_bytes()
            if args[1] == self.failing_rclone:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        return _real_run(args, *pargs, **kwargs)

    def patched(self):
        path = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        return [patch.dict(os.environ, {"PATH": path}),
                patch("scripts.offsite_backup.subprocess.run", side_effect=self.run)]


def run_main(tools, argv):
    patches = tools.patched()
    for item in patches:
        item.start()
    try:
        return offsite_backup.main(argv)
    finally:
        for item in reversed(patches):
            item.stop()


class DumpTests(unittest.TestCase):

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.dump = Path(self.workdir.name) / "potatobot.dump"

    def test_the_dump_is_owner_only_and_holds_what_the_command_wrote(self):
        offsite_backup.take_dump(["sh", "-c", "printf PGDMP-data"], self.dump)
        self.assertEqual(b"PGDMP-data", self.dump.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(self.dump.stat().st_mode))

    def test_a_failed_or_empty_dump_raises_and_leaves_no_file(self):
        for command in (["sh", "-c", "printf partial; exit 3"], ["sh", "-c", "exit 0"]):
            with self.subTest(command=command[-1]):
                with self.assertRaises(SystemExit):
                    offsite_backup.take_dump(command, self.dump)
                self.assertFalse(self.dump.exists())

    def test_an_unreadable_archive_is_refused(self):
        self.dump.write_bytes(b"not an archive")
        with self.assertRaisesRegex(SystemExit, "not a readable archive"):
            offsite_backup.check_dump(["sh", "-c", "cat > /dev/null; exit 1"], self.dump)

    def test_the_archive_reaches_pg_restore_on_standard_input(self):
        self.dump.write_bytes(b"PGDMP-data")
        seen = Path(self.workdir.name) / "seen"
        offsite_backup.check_dump(["sh", "-c", f'cat > "{seen}"; exit 0', "--"], self.dump)
        self.assertEqual(b"PGDMP-data", seen.read_bytes())


class LocalBackupPruneTests(unittest.TestCase):
    """The pre-migration copies are the other half of retention: the Privacy
    Policy promises they go 90 days after the migration they protect, and this
    flag is what keeps that promise. Everything here pins what it must *not*
    delete."""

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        root = Path(self.workdir.name)
        self.backups = root / "backups"
        self.backups.mkdir()
        self.old_dump = self.backups / f"potatobot-v27-{_stamp(200)}.dump"
        self.recent_dump = self.backups / f"potatobot-v28-{_stamp(10)}.dump"
        self.undatable_dump = self.backups / "potatobot-v29-garbage.dump"
        self.foreign = self.backups / f"other-v27-{_stamp(200)}.dump"
        self.legacy = root / "economy.db"
        self.legacy.write_bytes(b"x")
        self.old_copy = root / f"economy.db.backup-v9-{_stamp(200)}"
        self.recent_copy = root / f"economy.db.backup-v25-{_stamp(10)}"
        self.other_db = root / f"other.db.backup-v2-{_stamp(200)}"
        self.wal = root / "economy.db-wal"
        for path in (self.old_dump, self.recent_dump, self.undatable_dump, self.foreign,
                     self.old_copy, self.recent_copy, self.other_db, self.wal):
            path.write_bytes(b"x")
        # Symlinks wearing old names, pointing at the live files.
        self.dump_link = self.backups / f"potatobot-v1-{_stamp(400)}.dump"
        self.dump_link.symlink_to(self.recent_dump)
        self.copy_link = root / f"economy.db.backup-v1-{_stamp(400)}"
        self.copy_link.symlink_to(self.legacy)

    def test_only_dated_copies_older_than_the_window_are_due(self):
        due = offsite_backup.local_backups_due(self.backups, self.legacy, 90)
        self.assertEqual([self.old_dump, self.old_copy], due)

    def test_either_kind_can_be_pruned_alone(self):
        self.assertEqual([self.old_dump],
                         offsite_backup.local_backups_due(self.backups, None, 90))
        self.assertEqual([self.old_copy],
                         offsite_backup.local_backups_due(None, self.legacy, 90))

    def test_the_default_of_zero_keeps_every_copy(self):
        self.assertEqual([], offsite_backup.local_backups_due(self.backups, self.legacy, 0))

    def test_the_name_decides_not_the_modification_time(self):
        long_ago = (datetime.now() - timedelta(days=400)).timestamp()
        os.utime(self.recent_dump, (long_ago, long_ago))
        now = datetime.now().timestamp()
        os.utime(self.old_dump, (now, now))
        self.assertEqual([self.old_dump, self.old_copy],
                         offsite_backup.local_backups_due(self.backups, self.legacy, 90))

    def test_prune_deletes_the_due_copies_and_nothing_else(self):
        removed = offsite_backup.prune_local_backups(self.backups, self.legacy, 90)
        self.assertEqual([self.old_dump, self.old_copy], removed)
        for kept in (self.recent_dump, self.undatable_dump, self.foreign, self.legacy,
                     self.recent_copy, self.other_db, self.wal, self.dump_link,
                     self.copy_link):
            self.assertTrue(kept.exists() or kept.is_symlink(), kept.name)

    def test_the_cutoff_is_exact_to_the_day(self):
        now = datetime(2026, 11, 1, 12, 0, 0)
        exactly = self.backups / "potatobot-v30-20260803-120000.dump"
        just_inside = self.backups / "potatobot-v31-20260803-120001.dump"
        for path in (exactly, just_inside):
            path.write_bytes(b"x")
        due = offsite_backup.local_backups_due(self.backups, None, 90, now=now)
        self.assertNotIn(exactly, due)
        self.assertNotIn(just_inside, due)
        self.assertIn(self.old_dump, due)


class MainTests(unittest.TestCase):

    def test_dumps_checks_uploads_verifies_then_prunes_with_the_given_remote(self):
        """The whole project tree is never in scope: only the one dump this
        run produced is ever named in the upload call."""
        tools = StandInTools(self)
        run_main(tools, ["gdrivecrypt:PotatoBot", "--retention-days", "7"])
        upload, check, prune = tools.rclone_calls
        self.assertEqual(["rclone", "copy"], upload[:2])
        self.assertTrue(upload[2].endswith(".dump"))
        self.assertEqual("gdrivecrypt:PotatoBot", upload[3])
        self.assertEqual(b"PGDMP-stand-in", tools.uploaded)
        # The check names this run's dump alone, and compares bytes: a crypt
        # remote has no plaintext hashes to compare.
        self.assertEqual(["rclone", "check"], check[:2])
        self.assertIn("--download", check)
        self.assertIn("--one-way", check)
        self.assertEqual(Path(upload[2]).name, check[check.index("--include") + 1])
        self.assertEqual(["rclone", "delete", "gdrivecrypt:PotatoBot", "--min-age", "7d"],
                         prune)

    def test_a_failed_check_stops_the_run_before_any_prune(self):
        tools = StandInTools(self, failing_rclone="check")
        backups = Path(tools.workdir.name) / "backups"
        backups.mkdir()
        old = backups / f"potatobot-v27-{_stamp(200)}.dump"
        old.write_bytes(b"x")
        with self.assertRaises(subprocess.CalledProcessError):
            run_main(tools, ["r:", "--backups-dir", str(backups),
                             "--prune-local-backups-days", "90"])
        self.assertNotIn("delete", [call[1] for call in tools.rclone_calls])
        self.assertTrue(old.exists(), "a failed verification must not cost a local copy")

    def test_the_local_prune_runs_after_a_verified_copy(self):
        tools = StandInTools(self)
        backups = Path(tools.workdir.name) / "backups"
        backups.mkdir()
        old = backups / f"potatobot-v27-{_stamp(200)}.dump"
        old.write_bytes(b"x")
        run_main(tools, ["r:", "--backups-dir", str(backups)])
        self.assertTrue(old.exists(), "without the flag local copies stay")
        run_main(tools, ["r:", "--backups-dir", str(backups),
                         "--prune-local-backups-days", "90"])
        self.assertFalse(old.exists())

    def test_a_failed_or_unreadable_dump_uploads_nothing(self):
        for kwargs in ({"dump_exit": 1}, {"restore_exit": 1}):
            with self.subTest(**kwargs):
                tools = StandInTools(self, **kwargs)
                with self.assertRaises(SystemExit):
                    run_main(tools, ["r:"])
                self.assertEqual([], tools.rclone_calls)

    def test_the_dump_is_staged_in_the_named_workdir(self):
        """Where the database is encrypted at rest, the plaintext dump must not
        be staged on the plain disk: it lands under `--workdir`, and nothing is
        left there afterwards."""
        tools = StandInTools(self)
        staging = Path(tools.workdir.name) / "offsite-tmp"
        staging.mkdir()
        run_main(tools, ["r:", "--workdir", str(staging)])
        upload = tools.rclone_calls[0]
        self.assertEqual(staging, Path(upload[2]).parent.parent)
        self.assertEqual([], list(staging.iterdir()))

    def test_a_missing_or_symlinked_workdir_is_refused_before_anything_runs(self):
        tools = StandInTools(self)
        missing = Path(tools.workdir.name) / "no-such-dir"
        link = Path(tools.workdir.name) / "link"
        link.symlink_to(tools.workdir.name)
        for path in (missing, link):
            with self.subTest(path=path.name), \
                    patch("scripts.offsite_backup.take_dump") as dump:
                with self.assertRaisesRegex(SystemExit, "not an existing directory"):
                    offsite_backup.main(["r:", "--workdir", str(path)])
                dump.assert_not_called()
        self.assertEqual([], tools.rclone_calls)
        self.assertFalse(missing.exists(), "a missing workdir must not be created")

    def test_a_missing_tool_is_refused_before_anything_runs(self):
        tools = StandInTools(self)
        (tools.bin / "rclone").unlink()
        with patch.dict(os.environ, {"PATH": str(tools.bin)}), \
                patch("scripts.offsite_backup.subprocess.run") as mocked:
            with self.assertRaisesRegex(SystemExit, "rclone not found"):
                offsite_backup.main(["r:"])
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
