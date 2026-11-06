"""scripts/rehearse_migration.py: every refusal fires before anything is
written, and the migration step reaches the target the way the bot reaches its
database. (An end-to-end run needs version-18 `pg_dump` and `pg_restore`, which
the bot's image carries and neither this host nor CI does.)"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402

from scripts import rehearse_migration  # noqa: E402


class PreflightTests(unittest.TestCase):

    def setUp(self):
        self.source = pg_harness.create_database("rehearse_source")
        self.target = pg_harness.create_database("rehearse_target", baseline=False)
        bin_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bin_dir, True)
        for tool in ("pg_dump", "pg_restore"):
            (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (bin_dir / tool).chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def _preflight(self, source, target, path=None):
        with patch.dict(os.environ, {"PATH": path or self.path}):
            rehearse_migration.preflight(source, target)

    def test_an_empty_scratch_target_passes(self):
        self._preflight(self.source, self.target)

    def test_the_source_as_its_own_target_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "the target is the source"):
            self._preflight(self.source, self.source)

    def test_a_target_that_holds_tables_is_refused(self):
        with psycopg.connect(self.target, autocommit=True) as conn:
            conn.execute("CREATE TABLE leftover (id bigint)")
        with self.assertRaisesRegex(SystemExit, "not empty"):
            self._preflight(self.source, self.target)

    def test_missing_postgresql_tools_are_refused(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        with self.assertRaisesRegex(SystemExit, "pg_dump is not on PATH"):
            self._preflight(self.source, self.target, path=empty)

    def test_an_unreachable_database_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "cannot reach a database"):
            self._preflight(self.source, "postgresql://potatobot@127.0.0.1:1/x")


class TargetEnvironmentTests(unittest.TestCase):

    def test_the_target_reaches_update_db_through_libpq_variables_only(self):
        with patch.dict(os.environ, {"PGDATABASE": "live", "PGHOST": "live-host",
                                     "PGPASSWORD": "live-secret"}):
            environment = rehearse_migration.target_environment(
                "host=127.0.0.1 port=55432 dbname=rehearsal user=potatobot password=x",
                Path("/tmp/rehearsal"))
        self.assertEqual("rehearsal", environment["PGDATABASE"])
        self.assertEqual("127.0.0.1", environment["PGHOST"])
        self.assertEqual("55432", environment["PGPORT"])
        self.assertEqual("x", environment["PGPASSWORD"])

    def test_no_live_variable_survives_into_the_migration(self):
        with patch.dict(os.environ, {"PGPASSWORD": "live-secret", "PGSSLMODE": "require"}):
            environment = rehearse_migration.target_environment(
                "dbname=rehearsal", Path("/tmp/rehearsal"))
        self.assertNotIn("PGPASSWORD", environment)
        self.assertNotIn("PGSSLMODE", environment)

    def test_the_bots_own_pre_migration_dump_lands_in_the_rehearsal(self):
        with patch.dict(os.environ, {"POTATOBOT_DATA_DIR": "/data"}):
            environment = rehearse_migration.target_environment(
                "dbname=rehearsal", Path("/tmp/rehearsal"))
        self.assertEqual("/tmp/rehearsal", environment["POTATOBOT_DATA_DIR"])


if __name__ == "__main__":
    unittest.main()
