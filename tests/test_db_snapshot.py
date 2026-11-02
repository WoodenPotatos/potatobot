"""scripts/db_snapshot.py: the fingerprint a rehearsal compares before and
after a migration. It has to notice lost rows, lost columns, a dropped table and
rows rewritten in place, and say nothing about a column a migration added."""

import contextlib
import io
import os
import sys
import unittest

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402

from scripts import db_snapshot  # noqa: E402


class SnapshotTests(unittest.TestCase):

    def setUp(self):
        self.dsn = pg_harness.create_database("snapshot")
        with psycopg.connect(self.dsn) as conn:
            conn.execute("INSERT INTO guilds (guild_id, display_name, first_seen_at, "
                         "last_seen_at) VALUES (1, 'One', now(), now()), "
                         "(2, 'Two', now(), now())")

    def _compare(self, before, after):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = db_snapshot.compare(before, after)
        return code, out.getvalue()

    def _change(self, statement):
        with psycopg.connect(self.dsn) as conn:
            conn.execute(statement)

    def test_an_untouched_database_compares_identical(self):
        first = db_snapshot.record(self.dsn)
        code, output = self._compare(first, db_snapshot.record(self.dsn))
        self.assertEqual(0, code)
        self.assertIn("identical", output)
        self.assertEqual(26, first["schema_version"])
        self.assertEqual(2, first["table_counts"]["guilds"])

    def test_a_lost_row_fails(self):
        before = db_snapshot.record(self.dsn)
        self._change("DELETE FROM guilds WHERE guild_id = 2")
        code, output = self._compare(before, db_snapshot.record(self.dsn))
        self.assertEqual(1, code)
        self.assertIn("guilds: 2 -> 1 rows", output)

    def test_a_row_rewritten_in_place_fails(self):
        before = db_snapshot.record(self.dsn)
        self._change("UPDATE guilds SET display_name = 'Renamed' WHERE guild_id = 1")
        code, output = self._compare(before, db_snapshot.record(self.dsn))
        self.assertEqual(1, code)
        self.assertIn("guilds: rows rewritten", output)

    def test_an_added_column_is_a_note_not_a_problem(self):
        before = db_snapshot.record(self.dsn)
        self._change("ALTER TABLE guilds ADD COLUMN test_marker bigint")
        code, output = self._compare(before, db_snapshot.record(self.dsn))
        self.assertEqual(0, code)
        self.assertIn("guilds: columns added test_marker", output)

    def test_a_dropped_column_and_a_dropped_table_fail(self):
        before = db_snapshot.record(self.dsn)
        self._change("ALTER TABLE guilds DROP COLUMN display_name")
        self._change("DROP TABLE server_config")
        code, output = self._compare(before, db_snapshot.record(self.dsn))
        self.assertEqual(1, code)
        self.assertIn("columns removed display_name", output)
        self.assertIn("tables removed: server_config", output)

    def test_a_recorded_migration_is_a_note_and_a_lost_one_a_problem(self):
        before = db_snapshot.record(self.dsn)
        self._change("INSERT INTO schema_migrations (version, applied_at) VALUES (27, now())")
        migrated = db_snapshot.record(self.dsn)
        code, output = self._compare(before, migrated)
        self.assertEqual(0, code)
        self.assertIn("schema_migrations: 1 migration(s) recorded", output)
        code, output = self._compare(migrated, before)
        self.assertEqual(1, code)
        self.assertIn("schema_migrations: 2 -> 1 rows", output)

    def test_the_fingerprint_names_no_password(self):
        snapshot = db_snapshot.record(self.dsn)
        self.assertEqual({"database", "host", "server_version", "schema_version",
                          "table_counts", "table_columns", "table_hashes"}, set(snapshot))
        self.assertNotIn("password", str(snapshot).lower())


if __name__ == "__main__":
    unittest.main()
