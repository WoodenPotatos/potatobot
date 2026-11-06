"""The PostgreSQL engine's lifecycle: waiting for the server, bringing a
database to the latest schema, the backup before a migration, and the session
behaviour every accessor relies on (core/database.py, 2.21)."""

import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402

from core import database  # noqa: E402


class WaitForDatabaseTests(unittest.TestCase):

    def test_a_reachable_server_returns_at_once(self):
        with patch.object(database, "DSN", pg_harness.server_dsn()):
            database.wait_for_database(timeout=5)

    def test_an_unreachable_server_fails_loudly_and_soon(self):
        with patch.object(database, "DSN", "postgresql://potatobot@127.0.0.1:1/x"), \
                self.assertLogs("PotatoBot.Database", level="WARNING"):
            with self.assertRaises(database.DatabaseOperationError):
                database.wait_for_database(timeout=1)


class InitializeDatabaseTests(unittest.TestCase):

    def setUp(self):
        self.original_dsn = database.DSN
        self.addCleanup(setattr, database, "DSN", self.original_dsn)

    def _empty_database(self):
        database.DSN = pg_harness.create_database("init", baseline=False)

    def test_an_empty_database_gets_the_baseline_and_a_second_start_changes_nothing(self):
        self._empty_database()
        database.initialize_database()
        self.assertEqual(database.LATEST_SCHEMA_VERSION, database.get_schema_version())
        with database.get_connection() as conn:
            seeded = conn.execute("SELECT COUNT(*) FROM shop_prices WHERE guild_id = 0"
                                  ).fetchone()[0]
        database.initialize_database()
        with database.get_connection() as conn:
            self.assertEqual(seeded, conn.execute(
                "SELECT COUNT(*) FROM shop_prices WHERE guild_id = 0").fetchone()[0])
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM schema_migrations").fetchone()[0])
        self.assertGreater(seeded, 0)

    def test_a_newer_schema_is_refused(self):
        self._empty_database()
        database.initialize_database()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO schema_migrations (version, applied_at) "
                         "VALUES (%s, now())", (database.LATEST_SCHEMA_VERSION + 1,))
        with self.assertRaises(database.DatabaseOperationError):
            database.initialize_database()

    def test_tables_without_a_schema_record_are_refused_not_guessed_at(self):
        self._empty_database()
        with psycopg.connect(database.DSN, autocommit=True) as conn:
            conn.execute("CREATE TABLE users (user_id bigint PRIMARY KEY)")
        with self.assertRaises(database.DatabaseOperationError):
            database.initialize_database()


class PreMigrationBackupTests(unittest.TestCase):
    """A migration on a database holding data takes a `pg_dump` first, and is
    refused without one. A stand-in `pg_dump` exercises the plumbing; neither
    this host nor CI has a version-18 client, and the deployment's image does."""

    def setUp(self):
        self.original_dsn = database.DSN
        self.addCleanup(setattr, database, "DSN", self.original_dsn)
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        self.schema = self.work / "schema"
        self.schema.mkdir()
        shutil.copy(Path(database.SCHEMA_DIR) / "0026_baseline.sql", self.schema)
        database.DSN = pg_harness.create_database("backup")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id) VALUES (1)")

    def _with_migration(self):
        (self.schema / "9998_test_column.sql").write_text(
            "ALTER TABLE users ADD COLUMN test_marker bigint;\n", encoding="utf-8")
        return patch.multiple(database, SCHEMA_DIR=str(self.schema),
                              LATEST_SCHEMA_VERSION=9998)

    def _fake_pg_dump(self, exit_code):
        bin_dir = self.work / "bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "pg_dump"
        script.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            printf 'PGDMP-stand-in'
            exit {exit_code}
            """), encoding="utf-8")
        script.chmod(0o755)
        return patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})

    def test_the_backup_is_taken_owner_only_before_the_migration(self):
        with self._with_migration(), self._fake_pg_dump(0), \
                patch.object(database, "DATA_DIR", str(self.work)):
            database.initialize_database()
        dumps = list((self.work / "backups").glob("potatobot-v26-*.dump"))
        self.assertEqual(1, len(dumps))
        self.assertEqual(0o600, stat.S_IMODE(dumps[0].stat().st_mode))
        self.assertEqual(b"PGDMP-stand-in", dumps[0].read_bytes())
        with database.get_connection() as conn:
            self.assertEqual(9998, conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0])

    def test_a_failed_backup_refuses_the_migration_and_leaves_no_file(self):
        with self._with_migration(), self._fake_pg_dump(1), \
                patch.object(database, "DATA_DIR", str(self.work)):
            with self.assertRaises(database.DatabaseOperationError):
                database.initialize_database()
        self.assertEqual([], list((self.work / "backups").glob("*.dump")))
        with database.get_connection() as conn:
            self.assertEqual(26, conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0])
            columns = {row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users'")}
        self.assertNotIn("test_marker", columns)


class SessionBehaviourTests(unittest.TestCase):

    def setUp(self):
        pg_harness.fresh_database()
        database.initialize_database()

    def test_a_python_bool_is_stored_as_the_flag_sqlite_stored(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO guilds (guild_id, active, first_seen_at, last_seen_at) "
                         "VALUES (1, %s, now(), now())", (True,))
            self.assertEqual(1, conn.execute(
                "SELECT active FROM guilds WHERE guild_id = 1").fetchone()[0])

    def test_instants_come_back_in_utc(self):
        with database.get_connection() as conn:
            moment = conn.execute(
                "SELECT '2026-07-01T14:00:00+02:00'::timestamptz").fetchone()[0]
        self.assertEqual(12, moment.hour)
        self.assertEqual(timezone.utc.utcoffset(None), moment.utcoffset())


if __name__ == "__main__":
    unittest.main()


class FreshDatabaseTests(unittest.TestCase):
    """`pg_harness.fresh_database` is every database test's starting point, so
    a reset that leaves a row or an advanced identity behind makes tests depend
    on the order they run in, and passes alone what fails in the full suite."""

    def setUp(self):
        self.original_dsn = database.DSN

    def tearDown(self):
        database.DSN = self.original_dsn

    def test_every_table_is_emptied_and_every_identity_restarted(self):
        dsn = pg_harness.fresh_database()
        database.initialize_database()
        database.register_guild(10, "Harness Guild")
        database.record_warning(20, 30, "reason", "2026-11-02T12:00:00+00:00", 10, "spam")
        with psycopg.connect(dsn, autocommit=True) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'schema_migrations'")]
            sequences = [row[0] for row in conn.execute(
                "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")]
            for name in sequences:
                conn.execute("SELECT nextval(%s)", (name,))
            filled = [t for t in tables if conn.execute(
                sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(
                    sql.Identifier(t))).fetchone()[0]]
        # Prove the check can fail: the setup above must leave rows behind.
        self.assertGreaterEqual(len(filled), 3, filled)
        self.assertTrue(sequences)

        pg_harness.fresh_database()

        with psycopg.connect(dsn, autocommit=True) as conn:
            for table in tables:
                count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(table))).fetchone()[0]
                self.assertEqual(count, 0, f"{table} kept {count} row(s)")
            advanced = [row[0] for row in conn.execute(
                "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' "
                "AND last_value IS NOT NULL")]
            self.assertEqual(advanced, [])
            self.assertTrue(conn.execute(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations)").fetchone()[0],
                "schema_migrations must survive, or initialize_database rebuilds")
