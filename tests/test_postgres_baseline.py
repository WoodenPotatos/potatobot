"""The PostgreSQL baseline (`core/schema/0026_baseline.sql`) against the
column map it was generated from (`core/schema_types.py`).

The baseline is the source of truth once SQLite's migration chain is gone, so
what it creates is checked column by column on a real server: a column the
map names and the baseline lacks, or a type that differs, is a column the
copy would drop or mistype.
"""

import sys
import unittest
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg_harness  # noqa: E402

from core import database, schema_types  # noqa: E402

# What information_schema reports for each kind.
REPORTED = {
    schema_types.IDENTITY: "bigint",
    schema_types.BIGINT: "bigint",
    schema_types.FLAG: "smallint",
    schema_types.DOUBLE: "double precision",
    schema_types.TEXT: "text",
    schema_types.JSON: "text",
    schema_types.INSTANT: "timestamp with time zone",
}


class BaselineSchemaTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # The baseline and every migration after it: the map has to describe
        # the schema a database actually reaches, not only where it started.
        cls.dsn = pg_harness.create_database("baseline", baseline=False)
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            for version, path in database.schema_files():
                with conn.transaction():
                    conn.execute(Path(path).read_text(encoding="utf-8"))
                    if version != database.BASELINE_VERSION:
                        conn.execute("INSERT INTO schema_migrations (version, applied_at) "
                                     "VALUES (%s, now())", (version,))
        with psycopg.connect(cls.dsn) as conn:
            cls.columns = {}
            for table, column, data_type, identity in conn.execute(
                    "SELECT table_name, column_name, data_type, is_identity "
                    "FROM information_schema.columns WHERE table_schema = 'public'"):
                cls.columns.setdefault(table, {})[column] = (data_type, identity)
            cls.database = conn.execute(
                "SELECT datlocprovider, datlocale, pg_encoding_to_char(encoding) "
                "FROM pg_database WHERE datname = current_database()").fetchone()
            cls.migrations = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version").fetchall()

    def expected(self):
        expected = {table: dict(columns) for table, columns
                    in schema_types.COLUMN_KINDS.items()}
        for table, columns in schema_types.POSTGRES_ONLY_COLUMNS.items():
            expected[table].update(columns)
        return expected

    def test_the_baseline_has_exactly_the_mapped_tables_and_columns(self):
        expected = self.expected()
        actual = {t: c for t, c in self.columns.items() if t != "schema_migrations"}
        self.assertEqual(set(expected), set(actual))
        for table, columns in expected.items():
            with self.subTest(table=table):
                self.assertEqual(set(columns), set(actual[table]))

    def test_every_column_has_its_kinds_type(self):
        for table, columns in self.expected().items():
            for column, kind in columns.items():
                with self.subTest(column=f"{table}.{column}"):
                    data_type, identity = self.columns[table][column]
                    self.assertEqual(REPORTED[kind], data_type)
                    self.assertEqual(kind == schema_types.IDENTITY, identity == "YES")

    def test_every_schema_file_is_recorded(self):
        self.assertEqual([(version,) for version, _ in database.schema_files()],
                         self.migrations)

    def test_the_server_sorts_text_the_way_sqlite_did(self):
        """The built-in C.UTF-8 locale orders by code point, which is SQLite's
        byte order for UTF-8 text; a glibc locale would not."""
        self.assertEqual(("b", "C.UTF-8", "UTF8"), self.database)
        with psycopg.connect(self.dsn) as conn:
            ordered = [row[0] for row in conn.execute(
                "SELECT x FROM (VALUES ('a'), ('B'), ('é'), ('Z'), ('_'), ('ő')) v(x) "
                "ORDER BY x")]
        self.assertEqual(["B", "Z", "_", "a", "é", "ő"], ordered)


if __name__ == "__main__":
    unittest.main()
