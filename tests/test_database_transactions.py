import concurrent.futures
import os
import tempfile
import unittest
from datetime import datetime

import database


USERS_SCHEMA = """
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 100,
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    bj_wins INTEGER DEFAULT 0,
    bj_losses INTEGER DEFAULT 0,
    last_daily TEXT,
    last_job TEXT,
    rob_bonus REAL DEFAULT 0.0,
    rob_defense REAL DEFAULT 1.0,
    vault_protection REAL DEFAULT 0.0,
    bodyguard_until TEXT,
    last_rob TEXT,
    last_loldle_easy TEXT,
    last_loldle_medium TEXT,
    last_loldle_hard TEXT,
    last_valdle TEXT,
    last_genshindle TEXT,
    last_dbdle_killer TEXT,
    last_dbdle_survivor TEXT,
    last_dbdle_perk TEXT,
    streak_count INTEGER DEFAULT 0,
    last_streak_update TEXT,
    last_active TEXT,
    inactive_warned INTEGER DEFAULT 0,
    rules_read_time INTEGER
)
"""


class TransactionTests(unittest.TestCase):
    def setUp(self):
        # Always redirect the shared database module before destructive setup.
        # Test modules are imported together, so setting an environment variable
        # at import time is unsafe when another test imported ``database`` first.
        self.original_path = database.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        with database.get_connection() as conn:
            conn.execute(USERS_SCHEMA)
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 100)")
        database.initialize_database()

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    @staticmethod
    def run_parallel(callable_, count=8):
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(lambda _: callable_(), range(count)))

    def test_wager_cannot_spend_one_balance_more_than_once(self):
        results = self.run_parallel(lambda: database.reserve_wager(1, 40))
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(database.get_user_balance(1), 20)

    def test_instant_wager_reserves_and_settles_atomically(self):
        results = self.run_parallel(
            lambda: database.resolve_instant_wager(1, 40, loss_inc=1)
        )
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(database.get_user_balance(1), 20)

    def test_reserved_wager_payout_preserves_net_game_result(self):
        self.assertEqual(database.reserve_wager(1, 10), 90)
        result = database.settle_wager(1, credit=20, win_inc=1)
        self.assertEqual(result["stats"][0], 110)

    def test_interactive_wager_settles_exactly_once(self):
        reservation = database.begin_interactive_wager("wager-1", 10, 1, "mines", 20)
        self.assertEqual(reservation["balance"], 80)
        # No item asked for, so none was spent.
        self.assertFalse(reservation["consumed"])
        results = self.run_parallel(
            lambda: database.resolve_interactive_wager(
                "wager-1", 1, credit=40, win_inc=1, outcome="cashout"
            )
        )
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(database.get_user_balance(1), 120)

    def test_restart_recovery_refunds_pending_wager_once(self):
        database.begin_interactive_wager("wager-2", 10, 1, "blackjack", 30)
        first = database.refund_pending_wagers()
        second = database.refund_pending_wagers()
        self.assertEqual(first, {"count": 1, "amount": 30})
        self.assertEqual(second, {"count": 0, "amount": 0})
        self.assertEqual(database.get_user_balance(1), 100)

    def test_double_down_increases_recoverable_stake(self):
        database.begin_interactive_wager("wager-3", 10, 1, "blackjack", 20)
        self.assertEqual(database.increase_interactive_wager("wager-3", 1, 20), 60)
        recovered = database.refund_pending_wagers()
        self.assertEqual(recovered, {"count": 1, "amount": 40})
        self.assertEqual(database.get_user_balance(1), 100)

    def test_periodic_reward_is_atomic_and_guild_local(self):
        results = self.run_parallel(
            lambda: database.claim_periodic_reward(10, 1, "server_boost", 50, 30)
        )
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(database.get_user_balance(1), 150)
        self.assertIsNotNone(
            database.claim_periodic_reward(11, 1, "server_boost", 50, 30)
        )
        self.assertEqual(database.get_user_balance(1), 200)

    def test_transfer_is_atomic_and_preserves_total_balance(self):
        results = self.run_parallel(lambda: database.transfer_balance(1, 2, 40))
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(database.get_user_balance(1), 20)
        self.assertEqual(database.get_user_balance(2), 180)

    def test_batch_award_updates_every_member_once(self):
        count = database.apply_batch_balance([1, 2, 2, 3], 50)
        self.assertEqual(count, 3)
        self.assertEqual(database.get_user_balance(1), 150)
        self.assertEqual(database.get_user_balance(2), 150)
        self.assertEqual(database.get_user_balance(3), 150)

    def test_batch_rewards_apply_balance_and_xp_once(self):
        results = database.apply_batch_user_deltas([(1, 5, 20), (2, 10, 40)])
        self.assertEqual(results[1]["stats"][:3], (105, 20, 2))
        self.assertEqual(results[2]["stats"][:3], (110, 40, 3))
        self.assertTrue(results[1]["xp_changed"])

    def test_daily_reward_can_only_be_claimed_once(self):
        now = datetime.now().isoformat()
        results = self.run_parallel(
            lambda: database.claim_timed_reward(
                1, "last_daily", now, 500, 50, once_per_day=True
            )
        )
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        self.assertEqual(database.get_user_balance(1), 600)

    def test_everydle_reward_and_streak_are_claimed_once(self):
        now = datetime.now().isoformat()
        results = self.run_parallel(
            lambda: database.claim_everydle_reward(
                1, "last_valdle", now, 500, 100
            )
        )
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        self.assertEqual(database.get_user_balance(1), 605)
        with database.get_connection() as conn:
            streak = conn.execute(
                "SELECT streak_count FROM users WHERE user_id = 1"
            ).fetchone()[0]
        self.assertEqual(streak, 1)

    def test_multiple_everydle_modes_do_not_increment_streak_twice(self):
        now = datetime.now().isoformat()
        first = database.claim_everydle_reward(1, "last_valdle", now, 500, 100)
        second = database.claim_everydle_reward(1, "last_dbdle_killer", now, 500, 100)
        self.assertTrue(first["claimed"])
        self.assertTrue(second["claimed"])
        with database.get_connection() as conn:
            streak = conn.execute(
                "SELECT streak_count FROM users WHERE user_id = 1"
            ).fetchone()[0]
        self.assertEqual(streak, 1)

    def test_robbery_cooldown_and_transfer_are_atomic(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET balance = 10000 WHERE user_id = 1")
            conn.execute("INSERT INTO users (user_id, balance) VALUES (2, 10000)")
        now = datetime.now().isoformat()
        results = self.run_parallel(
            lambda: database.resolve_robbery(1, 2, now, 1.0, 1.0, 0.0, 0.10)
        )
        self.assertEqual(sum(result["resolved"] for result in results), 1)
        self.assertEqual(database.get_user_balance(1) + database.get_user_balance(2), 20000)

    def test_critical_write_failures_are_raised(self):
        test_path = database.DB_PATH
        database.DB_PATH = self.temp_dir.name
        try:
            with self.assertLogs("PotatoBot.Database", level="ERROR"):
                with self.assertRaises(database.DatabaseOperationError):
                    database.reserve_wager(1, 10)
        finally:
            database.DB_PATH = test_path

    def test_lockpick_purchase_is_atomic(self):
        """A lockpick is a stackable inventory item, so concurrent buys must each
        either debit and grant one, or do neither — never one without the other."""
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET balance = 200 WHERE user_id = 1")
        results = self.run_parallel(
            lambda: database.purchase_inventory_item(10, 1, 80, "lockpick")
        )
        bought = sum(result["purchased"] for result in results)
        self.assertEqual(bought, 2)
        self.assertEqual(database.get_user_balance(1), 200 - bought * 80)
        self.assertEqual(database.get_user_inventory(10, 1)["lockpick"], bought)

    def test_cooldown_reset_uses_current_schema(self):
        now = datetime.now().isoformat()
        with database.get_connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET last_daily = ?, last_loldle_easy = ?, last_dbdle_killer = ?
                WHERE user_id = 1
                """,
                (now, now, now),
            )
        database.reset_user_cooldowns(1)
        with database.get_connection() as conn:
            values = conn.execute(
                "SELECT last_daily, last_loldle_easy, last_dbdle_killer FROM users WHERE user_id = 1"
            ).fetchone()
        self.assertEqual(values, (None, None, None))


if __name__ == "__main__":
    unittest.main()
