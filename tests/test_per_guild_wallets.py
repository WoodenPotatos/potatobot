"""Schema 27 (3.0.0): one wallet per member per guild.

Two halves. The migration: the four required shapes (clean, upgrade,
idempotent, data-preserving), provenance that refuses rather than guesses, and
the coin-conservation proof todo.md section 3 asks for — the supply, the row
count and the per-member balance map, because a sum alone is conserved by a
duplicated row. Then the behaviour the key change exists for: wallets that do
not leak between guilds, a new account's coins only once, and every path that
finds money by a wager's id paying the wager's own guild.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402
from core import database  # noqa: E402
from core.clock import utc_now  # noqa: E402
import test_privacy_workflows  # noqa: E402

# Short synthetic ids: never a real guild or member.
GUILD_A = 101
GUILD_B = 202
GUILD_C = 303


def coin_supply():
    """The one definition, borrowed from the privacy tests (todo.md section 3)."""
    # The module, not the class, is imported: a test class in this namespace
    # would be collected and run a second time from here.
    return test_privacy_workflows.PrivacyWorkflowTests.coin_supply(None)


def balance_map():
    with database.get_connection() as conn:
        return {row[0]: row[1] for row in conn.execute(
            "SELECT user_id, balance FROM users ORDER BY user_id")}


def schema_version():
    with database.get_connection() as conn:
        return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]


def users_columns():
    with database.get_connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'users'")}


def primary_key(table):
    with database.get_connection() as conn:
        return conn.execute(
            "SELECT array_agg(a.attname ORDER BY array_position(i.indkey, a.attnum)) "
            "FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = %s::regclass AND i.indisprimary", (table,)
        ).fetchone()[0]


class SchemaTwentySevenMigrationTests(unittest.TestCase):
    """A database at schema 26 holding the live shape of data, migrated."""

    def setUp(self):
        self.original_dsn = database.DSN
        self.addCleanup(setattr, database, "DSN", self.original_dsn)
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        database.DSN = pg_harness.create_database("wallets")
        # Neither this host nor CI has a version-18 pg_dump; the plumbing is
        # PreMigrationBackupTests' concern, so a stand-in answers here.
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "pg_dump"
        fake.write_text(textwrap.dedent("""\
            #!/bin/sh
            printf 'PGDMP-stand-in'
            """), encoding="utf-8")
        fake.chmod(0o755)
        env = patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("POTATOBOT_LEGACY_GUILD_ID", None)
        data_dir = patch.object(database, "DATA_DIR", str(self.work))
        data_dir.start()
        self.addCleanup(data_dir.stop)

    def seed_schema_26(self, active_guilds=(GUILD_A,), marker_guild=None):
        now = utc_now().isoformat()
        with database.get_connection() as conn:
            for guild_id in active_guilds:
                conn.execute(
                    "INSERT INTO guilds (guild_id, display_name, active, first_seen_at, "
                    "last_seen_at) VALUES (%s, 'g', 1, %s, %s)", (guild_id, now, now))
            if marker_guild is not None:
                conn.execute(
                    "INSERT INTO scope_adoptions (adoption_key, guild_id, user_count, "
                    "adopted_at) VALUES (%s, %s, 3, %s)",
                    (f"legacy-users-v1:{marker_guild}", marker_guild, now))
            # Ordinary members, a rich one, a broke one, and an erased member's
            # tombstone: every row has to arrive, none twice.
            conn.execute(
                "INSERT INTO users (user_id, balance, xp, level, streak_count, last_daily, "
                "last_active, protected_reserve) VALUES "
                "(11, 100, 0, 1, 0, NULL, %s, 0), "
                "(12, 987654321, 4000, 21, 9, %s, %s, 5000), "
                "(13, 0, 15, 2, 0, NULL, NULL, 0), "
                "(-1, 250, 0, 1, 0, NULL, NULL, 0)",
                (now, now, now))
            conn.execute(
                "INSERT INTO casino_earnings_window (user_id, window_started_at, "
                "window_total) VALUES (12, %s, 4200)", (now,))
            conn.execute(
                "INSERT INTO casino_wagers (wager_id, guild_id, user_id, game_key, stake, "
                "status, created_at) VALUES ('w-pending', %s, 12, 'blackjack', 300, "
                "'pending', %s)", (GUILD_A, now))
            conn.commit()

    def test_a_clean_database_reaches_27_with_the_composite_keys(self):
        database.initialize_database()
        self.assertEqual(27, schema_version())
        self.assertEqual(["guild_id", "user_id"], primary_key("users"))
        self.assertEqual(["guild_id", "user_id"], primary_key("casino_earnings_window"))

    def test_an_upgrade_conserves_every_coin_and_attributes_every_row(self):
        self.seed_schema_26()
        supply, rows, per_member = coin_supply(), len(balance_map()), balance_map()
        database.initialize_database()
        self.assertEqual(27, schema_version())
        # Three assertions together: a duplicated row conserves the sum of nothing.
        self.assertEqual(supply, coin_supply())
        self.assertEqual(rows, len(balance_map()))
        self.assertEqual(per_member, balance_map())
        with database.get_connection() as conn:
            self.assertEqual({GUILD_A}, {row[0] for row in conn.execute(
                "SELECT DISTINCT guild_id FROM users")})
            self.assertEqual((GUILD_A, 4200), conn.execute(
                "SELECT guild_id, window_total FROM casino_earnings_window "
                "WHERE user_id = 12").fetchone())
            # The rest of the row came across untouched.
            self.assertEqual((4000, 21, 9, 5000), conn.execute(
                "SELECT xp, level, streak_count, protected_reserve FROM users "
                "WHERE user_id = 12").fetchone())
        self.assertEqual(["guild_id", "user_id"], primary_key("users"))

    def test_a_second_start_changes_nothing(self):
        self.seed_schema_26()
        database.initialize_database()
        before = (coin_supply(), balance_map(), schema_version())
        database.initialize_database()
        self.assertEqual(before, (coin_supply(), balance_map(), schema_version()))

    def test_the_configured_guild_wins_over_the_guilds_table(self):
        self.seed_schema_26(active_guilds=(GUILD_A,))
        with patch.dict(os.environ, {"POTATOBOT_LEGACY_GUILD_ID": str(GUILD_C)}):
            database.initialize_database()
        with database.get_connection() as conn:
            self.assertEqual({GUILD_C}, {row[0] for row in conn.execute(
                "SELECT DISTINCT guild_id FROM users")})

    def test_the_adoption_marker_answers_when_several_guilds_are_active(self):
        self.seed_schema_26(active_guilds=(GUILD_A, GUILD_B), marker_guild=GUILD_B)
        database.initialize_database()
        with database.get_connection() as conn:
            self.assertEqual({GUILD_B}, {row[0] for row in conn.execute(
                "SELECT DISTINCT guild_id FROM users")})

    def test_an_unattributable_database_is_refused_and_left_at_26(self):
        self.seed_schema_26(active_guilds=(GUILD_A, GUILD_B))
        per_member = balance_map()
        with self.assertRaises(database.DatabaseOperationError):
            database.initialize_database()
        self.assertEqual(26, schema_version())
        self.assertNotIn("guild_id", users_columns())
        self.assertEqual(per_member, balance_map())

    def test_zero_is_not_a_guild_to_park_wallets_in(self):
        # The local dashboard sets POTATOBOT_LEGACY_GUILD_ID=0 as a guard;
        # it must fall through to the other sources, never become guild 0.
        self.seed_schema_26(active_guilds=(GUILD_A,))
        with patch.dict(os.environ, {"POTATOBOT_LEGACY_GUILD_ID": "0"}):
            database.initialize_database()
        with database.get_connection() as conn:
            self.assertEqual({GUILD_A}, {row[0] for row in conn.execute(
                "SELECT DISTINCT guild_id FROM users")})

    def test_an_empty_database_migrates_without_any_provenance(self):
        database.initialize_database()  # no guilds, no users: nothing to attribute
        self.assertEqual(27, schema_version())


class PerGuildWalletTests(unittest.TestCase):

    def setUp(self):
        self.original_dsn = database.DSN
        self.addCleanup(setattr, database, "DSN", self.original_dsn)
        database.DSN = pg_harness.fresh_database()
        database.initialize_database()

    def wager(self, wager_id, guild_id, user_id, stake):
        result = database.begin_interactive_wager(
            wager_id, guild_id, user_id, "blackjack", stake)
        self.assertIsNotNone(result)
        return result

    def test_a_new_account_is_paid_once_in_its_first_guild(self):
        database.apply_user_delta(GUILD_A, 7, 0)
        database.apply_user_delta(GUILD_B, 7, 0)
        self.assertEqual(database.NEW_ACCOUNT_BALANCE, database.get_user_balance(GUILD_A, 7))
        self.assertEqual(0, database.get_user_balance(GUILD_B, 7))

    def test_a_wallet_does_not_leak_into_another_guild(self):
        database.apply_user_delta(GUILD_A, 7, 500, 40)
        database.claim_timed_reward(GUILD_A, 7, "last_daily", utc_now().isoformat(),
                                    50, 5, once_per_day=True)
        self.assertEqual(0, database.get_user_balance(GUILD_B, 7))
        self.assertIsNone(database.get_cooldown(GUILD_B, 7, "last_daily"))
        # /daily is per guild: guild A's claim does not block guild B's.
        claim = database.claim_timed_reward(GUILD_B, 7, "last_daily",
                                            utc_now().isoformat(), 50, 5,
                                            once_per_day=True)
        self.assertTrue(claim["claimed"])
        self.assertEqual(50, database.get_user_balance(GUILD_B, 7))

    def test_a_falsy_guild_never_gets_a_wallet(self):
        # A programming error, raised as one rather than wrapped.
        with self.assertRaises(ValueError):
            database.apply_user_delta(0, 7, 100)
        with database.get_connection() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def test_a_transfer_moves_coins_inside_one_guild(self):
        database.apply_user_delta(GUILD_A, 7, 0)
        database.apply_user_delta(GUILD_B, 8, 0)  # 8's first guild is B
        database.apply_user_delta(GUILD_A, 8, 0)
        database.transfer_balance(GUILD_A, 7, 8, 30)
        self.assertEqual(70, database.get_user_balance(GUILD_A, 7))
        self.assertEqual(30, database.get_user_balance(GUILD_A, 8))
        self.assertEqual(100, database.get_user_balance(GUILD_B, 8))

    def test_a_wager_settles_and_refunds_into_its_own_guild(self):
        for guild in (GUILD_A, GUILD_B):
            database.apply_user_delta(guild, 7, 1000)
        supply = coin_supply()
        self.wager("w1", GUILD_B, 7, 200)
        self.assertTrue(database.refund_interactive_wager("w1"))
        self.wager("w2", GUILD_B, 7, 200)
        database.resolve_interactive_wager("w2", 7, credit=400, win_inc=1)
        self.wager("w3", GUILD_B, 7, 100)
        self.assertEqual(1, database.refund_pending_wagers()["count"])
        self.assertEqual(1100, database.get_user_balance(GUILD_A, 7))
        self.assertEqual(1200, database.get_user_balance(GUILD_B, 7))
        self.assertEqual(supply + 200, coin_supply())

    def test_a_double_down_debits_the_wagers_guild(self):
        # 7's first guild is A (100 + 1000); B starts at 0 + 1000.
        for guild in (GUILD_A, GUILD_B):
            database.apply_user_delta(guild, 7, 1000)
        self.wager("w1", GUILD_B, 7, 100)
        database.increase_interactive_wager("w1", 7, 100)
        self.assertEqual(1100, database.get_user_balance(GUILD_A, 7))
        self.assertEqual(800, database.get_user_balance(GUILD_B, 7))

    def test_erasure_returns_each_pending_stake_to_its_own_guild(self):
        database.apply_user_delta(GUILD_A, 7, 1000)
        database.apply_user_delta(GUILD_B, 7, 1000)
        self.wager("w1", GUILD_B, 7, 300)
        supply = coin_supply()
        receipt = database.anonymize_user(7, 1, GUILD_A, "test")
        tombstone = receipt["tombstone_id"]
        self.assertEqual(supply, coin_supply())
        self.assertEqual(1100, database.get_user_balance(GUILD_A, tombstone))
        self.assertEqual(1000, database.get_user_balance(GUILD_B, tombstone))

    def test_the_casino_window_counts_per_guild(self):
        limit = (3600, 500)
        with database.get_connection() as conn:
            first = database._apply_stats_locked(conn, GUILD_A, 7, 500,
                                                 casino_rate_limit=limit)
            other = database._apply_stats_locked(conn, GUILD_B, 7, 500,
                                                 casino_rate_limit=limit)
            again = database._apply_stats_locked(conn, GUILD_A, 7, 500,
                                                 casino_rate_limit=limit)
            conn.commit()
        self.assertFalse(first["casino_rate_limited"])
        self.assertFalse(other["casino_rate_limited"])
        self.assertTrue(again["casino_rate_limited"])

    def test_a_victim_new_to_the_guild_reads_as_poor_not_as_new(self):
        database.apply_user_delta(GUILD_A, 7, 10_000)
        database.apply_user_delta(GUILD_B, 8, 10_000)  # rich, but in B only
        result = database.resolve_robbery(GUILD_A, 7, 8, utc_now().isoformat(),
                                          0.5, 1.0, 0.0, 0.05)
        # "poor", never "missing_user": the refusal must not reveal that the
        # victim has no wallet here. It rolls back, wallet creation included.
        self.assertEqual({"resolved": False, "reason": "victim_poor"}, result)
        self.assertEqual(10_100, database.get_user_balance(GUILD_B, 8))

    def test_leaderboards_rank_one_guilds_wallets(self):
        database.apply_user_delta(GUILD_A, 7, 0, 900)
        database.apply_user_delta(GUILD_A, 8, 0, 100)
        database.apply_user_delta(GUILD_B, 8, 0, 5000)
        members = [7, 8]
        self.assertEqual([7, 8], [row[0] for row in database.get_top_levels(GUILD_A, members)])
        self.assertEqual(1, database.get_user_rank(GUILD_A, 900, members))
        self.assertEqual(7, database.get_top_xp_user(GUILD_A, members))
        self.assertEqual(8, database.get_top_xp_user(GUILD_B, members))

    def test_retention_asks_once_per_member_and_only_when_inactive_everywhere(self):
        old = (utc_now() - timedelta(days=400)).isoformat()
        recent = utc_now().isoformat()
        for guild in (GUILD_A, GUILD_B):
            database.apply_user_delta(guild, 7, 0)
            database.apply_user_delta(guild, 8, 0)
        database.update_last_active(GUILD_A, 7, old)
        database.update_last_active(GUILD_B, 7, old)
        database.update_last_active(GUILD_A, 8, old)
        database.update_last_active(GUILD_B, 8, recent)
        cutoff = (utc_now() - timedelta(days=365)).isoformat()
        self.assertEqual([7], database.get_retention_candidates(cutoff))


if __name__ == "__main__":
    unittest.main()
