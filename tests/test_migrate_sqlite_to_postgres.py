"""scripts/migrate_sqlite_to_postgres.py: the one-time copy from 2.20's SQLite.

The source is built from `tests/fixtures/sqlite_schema_26.sql`, the schema
2.20's own code created, seeded with every case the copy has to get right:
offset-less legacy timestamps from both sides of a clock change, aware ones,
flags, doubles, JSON, NULLs, Hungarian text, snowflakes past 2**53, a negative
tombstone id, and vouchers whose insertion order differs from their key order.
Every refusal has to leave the target as it found it.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402

from core import database  # noqa: E402
from scripts import migrate_sqlite_to_postgres as copy_tool  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sqlite_schema_26.sql"
BIG = 1420070400000000042  # above 2**53, so a float round trip would show
ZONE = "Europe/Budapest"


def build_source(path: Path, version: int = 26) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(FIXTURE.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("INSERT INTO guilds (guild_id, display_name, active, first_seen_at, "
                     "last_seen_at) VALUES (?, 'Krumpli Klub ő', 1, "
                     "'2026-01-01T10:00:00+00:00', '2026-10-31T10:00:00+00:00')", (BIG,))
        conn.executemany(
            "INSERT INTO users (user_id, balance, xp, level, rob_bonus, last_daily, "
            "last_active, inactive_warned) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(BIG, 5000, 120, 3, 0.25, "2026-07-01T14:00:00",       # summer, naive
              "2026-10-31T12:00:00+00:00", 0),
             (7, 100, 0, 1, 0.0, "2026-01-15T14:00:00",             # winter, naive
              "2026-01-15T14:00:00", 1),
             (-3, 900, 0, 1, None, None, None, 0)])                 # a tombstone
        conn.execute("INSERT INTO warnings (user_id, mod_id, reason, date, guild_id, tag) "
                     "VALUES (?, 9, 'csúnya szó', '2026-10-30T08:00:00+00:00', ?, 'spam')",
                     (BIG, BIG))
        conn.execute("INSERT INTO settings_audit (guild_id, actor_id, action, target_key, "
                     "old_value_json, new_value_json, created_at) VALUES "
                     "(?, 9, 'setting.update', 'k', NULL, '{\"b\": [1, 2], \"a\": \"x\"}', "
                     "'2026-10-30T08:00:00+00:00')", (BIG,))
        # Inserted in z, a, m order with one instant: insertion order is the tie.
        for voucher in ("zz", "aa", "mm"):
            conn.execute("INSERT INTO reward_vouchers (voucher_id, guild_id, user_id, "
                         "reward_key, duration_days, acquired_at) VALUES "
                         "(?, ?, ?, 'premium_30d', 30, '2026-10-31T09:00:00+00:00')",
                         (voucher, BIG, BIG))


class CopyTests(unittest.TestCase):

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.source = self.work / "economy.db"
        build_source(self.source)
        self.target = pg_harness.create_database("copy_target", baseline=False)
        self.output = []
        self.original_dsn = database.DSN
        self.addCleanup(setattr, database, "DSN", self.original_dsn)

    def run_copy(self, **overrides):
        arguments = dict(source_path=self.source, target=self.target, zone_name=ZONE,
                         dry_run=False, out=self.output.append)
        arguments.update(overrides)
        return copy_tool.run(**arguments)

    def target_tables(self):
        with psycopg.connect(self.target) as conn:
            return conn.execute("SELECT count(*) FROM information_schema.tables "
                                "WHERE table_schema = 'public'").fetchone()[0]

    def test_a_copy_is_proven_and_the_bot_reads_it(self):
        self.assertEqual(0, self.run_copy(), self.output)
        self.assertIn("copied and proven", self.output[-1])
        database.DSN = self.target
        database.initialize_database()   # finds schema 26 and applies nothing
        self.assertEqual(26, database.get_schema_version())
        self.assertEqual(5000, database.get_user_balance(BIG))
        self.assertEqual(900, database.get_user_balance(-3))
        vouchers = database.get_user_vouchers(BIG, BIG)
        self.assertEqual(["zz", "aa", "mm"], [v["voucher_id"] for v in vouchers])
        # A new row continues past the copied ids.
        new = database.record_warning(7, 9, "x", database.utc_now().isoformat(), BIG, "spam")
        self.assertGreater(new["warning_id"], 1)

    def test_legacy_timestamps_are_read_as_host_local_time(self):
        self.assertEqual(0, self.run_copy(), self.output)
        with psycopg.connect(self.target) as conn:
            daily = dict(conn.execute("SELECT user_id, last_daily FROM users "
                                      "WHERE last_daily IS NOT NULL").fetchall())
        # 14:00 in Budapest is 12:00 UTC in summer and 13:00 UTC in winter.
        self.assertEqual(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc), daily[BIG])
        self.assertEqual(datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc), daily[7])

    def test_a_dry_run_writes_nothing(self):
        self.assertEqual(0, self.run_copy(dry_run=True), self.output)
        self.assertEqual(0, self.target_tables())

    def _refused(self, needle, **overrides):
        code = self.run_copy(**overrides)
        self.assertEqual(2, code, self.output)
        self.assertIn(needle, self.output[-1])
        self.assertEqual(0, self.target_tables(), "a refusal must not write")

    def test_a_source_at_another_version_is_refused(self):
        build_source(other := self.work / "old.db", version=25)
        self._refused("schema 25", source_path=other)

    def test_a_target_that_is_not_empty_is_refused(self):
        with psycopg.connect(self.target, autocommit=True) as conn:
            conn.execute("CREATE TABLE leftover (id bigint)")
        code = self.run_copy()
        self.assertEqual(2, code)
        self.assertIn("holds 1 table", self.output[-1])

    def test_a_target_with_another_locale_is_refused(self):
        with psycopg.connect(pg_harness.server_dsn(), autocommit=True) as admin:
            name = f"potatobot_test_{os.getpid()}_libc"
            admin.execute(f"DROP DATABASE IF EXISTS {name}")
            admin.execute(f"CREATE DATABASE {name} TEMPLATE template0 "
                          "LOCALE_PROVIDER libc LOCALE 'C'")
        self.addCleanup(self._drop, name)
        dsn = psycopg.conninfo.make_conninfo(pg_harness.server_dsn(), dbname=name)
        code = self.run_copy(target=dsn)
        self.assertEqual(2, code)
        self.assertIn("not builtin C.UTF-8", self.output[-1])

    def _drop(self, name):
        with psycopg.connect(pg_harness.server_dsn(), autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")

    def test_a_value_that_will_not_convert_is_refused_with_its_place(self):
        with sqlite3.connect(self.source) as conn:   # SQLite takes text in an INTEGER column
            conn.execute("UPDATE users SET xp = 'lots' WHERE user_id = 7")
        self._refused("users.xp: not an integer")

    def test_an_unparseable_timestamp_is_refused(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute("UPDATE users SET last_daily = 'daily-marker' WHERE user_id = 7")
        self._refused("users.last_daily: not a timestamp")

    def test_a_foreign_key_violation_is_refused(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute("INSERT INTO realm_guilds (realm_id, guild_id, status) "
                         "VALUES (999, ?, 'pending')", (BIG,))
        self._refused("foreign key violation")

    def test_an_unmapped_column_is_refused(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute("ALTER TABLE users ADD COLUMN surprise TEXT")
        self._refused("unmapped ['surprise']")

    def test_a_zone_that_does_not_exist_is_refused(self):
        self._refused("not a time zone", zone_name="Europe/Atlantis")

    def _add_leftovers(self, price=5000):
        """What an upgraded installation carries and a fresh one never had."""
        with sqlite3.connect(self.source) as conn:
            conn.execute("ALTER TABLE active_channels ADD COLUMN faction_group TEXT")
            conn.execute("ALTER TABLE users ADD COLUMN last_dbdle TEXT")
            conn.execute("ALTER TABLE users ADD COLUMN last_loldle TEXT")
            conn.execute("CREATE TABLE economy_settings (setting_key TEXT PRIMARY KEY, "
                         "setting_value INTEGER)")
            conn.execute("INSERT INTO shop_prices (guild_id, item_id, price) "
                         "VALUES (0, 'lockpick', 5000)")
            conn.execute("INSERT INTO rewards (guild_id, activity_id, coin_reward, "
                         "xp_reward) VALUES (0, 'valdle', 5000, 10)")
            conn.executemany("INSERT INTO economy_settings VALUES (?, ?)",
                             [("shop_lockpick_price", price), ("reward_valdle", 5000)])

    def test_the_known_leftovers_stay_behind_when_they_hold_nothing_new(self):
        self._add_leftovers()
        self.assertEqual(0, self.run_copy(), self.output)
        with psycopg.connect(self.target) as conn:
            self.assertIsNone(conn.execute(
                "SELECT to_regclass('public.economy_settings')").fetchone()[0])

    def test_a_leftover_column_holding_a_value_is_refused(self):
        self._add_leftovers()
        with sqlite3.connect(self.source) as conn:
            conn.execute("UPDATE users SET last_loldle = '2026-01-01' WHERE user_id = 7")
        self._refused("users.last_loldle holds 1 value")

    def test_an_old_economy_value_that_differs_is_refused(self):
        self._add_leftovers(price=4999)
        self._refused("economy_settings.shop_lockpick_price is 4999")

    def test_a_failed_proof_commits_nothing(self):
        real_load = copy_tool.load

        def load_then_corrupt(conn, *args, **kwargs):
            copied = real_load(conn, *args, **kwargs)
            conn.execute("UPDATE users SET balance = balance + 1 WHERE user_id = 7")
            return copied

        with patch.object(copy_tool, "load", load_then_corrupt):
            self.assertEqual(1, self.run_copy())
        self.assertIn("PROOF FAILED", "\n".join(self.output))
        self.assertIn("users", "\n".join(self.output))
        self.assertEqual(0, self.target_tables(), "a failed proof must roll back")


if __name__ == "__main__":
    unittest.main()
