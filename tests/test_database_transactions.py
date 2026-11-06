import sys
import ast
import concurrent.futures
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from core import database
from core.clock import parse_stored

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class TransactionTests(unittest.TestCase):
    GUILD = 10  # the guild whose wallet every test below uses

    def setUp(self):
        # Always redirect the shared database module before destructive setup.
        # Test modules are imported together, so setting an environment variable
        # at import time is unsafe when another test imported ``database`` first.
        self.original_path = database.DATA_DIR
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (guild_id, user_id, balance) VALUES (%s, 1, 100)",
                (self.GUILD,))

    def tearDown(self):
        database.DATA_DIR = self.original_path
        self.temp_dir.cleanup()

    @staticmethod
    def run_parallel(callable_, count=8):
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(lambda _: callable_(), range(count)))

    def test_wager_cannot_spend_one_balance_more_than_once(self):
        results = self.run_parallel(lambda: database.reserve_wager(self.GUILD, 1, 40))
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 20)

    def test_instant_wager_reserves_and_settles_atomically(self):
        results = self.run_parallel(
            lambda: database.resolve_instant_wager(self.GUILD, 1, 40, loss_inc=1)
        )
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 20)

    def test_reserved_wager_payout_preserves_net_game_result(self):
        self.assertEqual(database.reserve_wager(self.GUILD, 1, 10), 90)
        result = database.settle_wager(self.GUILD, 1, credit=20, win_inc=1)
        self.assertEqual(result["stats"][0], 110)

    def test_interactive_wager_settles_exactly_once(self):
        reservation = database.begin_interactive_wager("wager-1", self.GUILD, 1, "mines", 20)
        self.assertEqual(reservation["balance"], 80)
        # No item asked for, so none was spent.
        self.assertFalse(reservation["consumed"])
        results = self.run_parallel(
            lambda: database.resolve_interactive_wager(
                "wager-1", 1, credit=40, win_inc=1, outcome="cashout"
            )
        )
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 120)

    def test_restart_recovery_refunds_pending_wager_once(self):
        database.begin_interactive_wager("wager-2", self.GUILD, 1, "blackjack", 30)
        first = database.refund_pending_wagers()
        second = database.refund_pending_wagers()
        self.assertEqual(first, {"count": 1, "amount": 30})
        self.assertEqual(second, {"count": 0, "amount": 0})
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 100)

    def test_double_down_increases_recoverable_stake(self):
        database.begin_interactive_wager("wager-3", self.GUILD, 1, "blackjack", 20)
        self.assertEqual(database.increase_interactive_wager("wager-3", 1, 20), 60)
        recovered = database.refund_pending_wagers()
        self.assertEqual(recovered, {"count": 1, "amount": 40})
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 100)

    def test_periodic_reward_is_atomic_and_guild_local(self):
        results = self.run_parallel(
            lambda: database.claim_periodic_reward(self.GUILD, 1, "server_boost", 50, 30)
        )
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 150)
        self.assertIsNotNone(
            database.claim_periodic_reward(11, 1, "server_boost", 50, 30)
        )
        # Wallets are per guild (schema 27): the second guild's claim pays that
        # guild's own wallet, which starts empty, and leaves this one alone.
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 150)
        self.assertEqual(database.get_user_balance(11, 1), 50)

    def test_transfer_is_atomic_and_preserves_total_balance(self):
        results = self.run_parallel(lambda: database.transfer_balance(self.GUILD, 1, 2, 40))
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 20)
        self.assertEqual(database.get_user_balance(self.GUILD, 2), 180)

    def test_batch_award_updates_every_member_once(self):
        count = database.apply_batch_balance(self.GUILD, [1, 2, 2, 3], 50)
        self.assertEqual(count, 3)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 150)
        self.assertEqual(database.get_user_balance(self.GUILD, 2), 150)
        self.assertEqual(database.get_user_balance(self.GUILD, 3), 150)

    def test_batch_rewards_apply_balance_and_xp_once(self):
        results = database.apply_batch_user_deltas(self.GUILD, [(1, 5, 20), (2, 10, 40)])
        self.assertEqual(results[1]["stats"][:3], (105, 20, 2))
        self.assertEqual(results[2]["stats"][:3], (110, 40, 3))
        self.assertTrue(results[1]["xp_changed"])

    def test_daily_reward_can_only_be_claimed_once(self):
        now = datetime.now(timezone.utc).isoformat()
        results = self.run_parallel(
            lambda: database.claim_timed_reward(
                self.GUILD, 1, "last_daily", now, 500, 50, once_per_day=True
            )
        )
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 600)

    def test_everydle_reward_and_streak_are_claimed_once(self):
        now = datetime.now(timezone.utc).isoformat()
        results = self.run_parallel(
            lambda: database.claim_everydle_reward(
                self.GUILD, 1, "last_valdle", now, 500, 100
            )
        )
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 605)
        with database.get_connection() as conn:
            streak = conn.execute(
                "SELECT streak_count FROM users WHERE guild_id = %s AND user_id = 1",
                (self.GUILD,)
            ).fetchone()[0]
        self.assertEqual(streak, 1)

    def test_multiple_everydle_modes_do_not_increment_streak_twice(self):
        now = datetime.now(timezone.utc).isoformat()
        first = database.claim_everydle_reward(self.GUILD, 1, "last_valdle", now, 500, 100)
        second = database.claim_everydle_reward(self.GUILD, 1, "last_dbdle_killer", now, 500, 100)
        self.assertTrue(first["claimed"])
        self.assertTrue(second["claimed"])
        with database.get_connection() as conn:
            streak = conn.execute(
                "SELECT streak_count FROM users WHERE guild_id = %s AND user_id = 1",
                (self.GUILD,)
            ).fetchone()[0]
        self.assertEqual(streak, 1)

    def test_robbery_cooldown_and_transfer_are_atomic(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET balance = 10000 WHERE guild_id = %s AND user_id = 1",
                         (self.GUILD,))
            conn.execute("INSERT INTO users (guild_id, user_id, balance) VALUES (%s, 2, 10000)",
                         (self.GUILD,))
        now = datetime.now(timezone.utc).isoformat()
        results = self.run_parallel(
            lambda: database.resolve_robbery(self.GUILD, 1, 2, now, 1.0, 1.0, 0.0, 0.10)
        )
        self.assertEqual(sum(result["resolved"] for result in results), 1)
        self.assertEqual(database.get_user_balance(self.GUILD, 1) + database.get_user_balance(self.GUILD, 2), 20000)

    def test_critical_write_failures_are_raised(self):
        with pg_harness.unreachable_database():
            with self.assertLogs("PotatoBot.Database", level="ERROR"):
                with self.assertRaises(database.DatabaseOperationError):
                    database.reserve_wager(self.GUILD, 1, 10)

    def test_lockpick_purchase_is_atomic(self):
        """A lockpick is a stackable inventory item, so concurrent buys must each
        either debit and grant one, or do neither — never one without the other."""
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET balance = 200 WHERE guild_id = %s AND user_id = 1",
                         (self.GUILD,))
        results = self.run_parallel(
            lambda: database.purchase_inventory_item(self.GUILD, 1, 80, "lockpick")
        )
        bought = sum(result["purchased"] for result in results)
        self.assertEqual(bought, 2)
        self.assertEqual(database.get_user_balance(self.GUILD, 1), 200 - bought * 80)
        self.assertEqual(database.get_user_inventory(self.GUILD, 1)["lockpick"], bought)

    def test_cooldown_reset_uses_current_schema(self):
        now = datetime.now(timezone.utc).isoformat()
        with database.get_connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET last_daily = %s, last_loldle_easy = %s, last_dbdle_killer = %s
                WHERE guild_id = %s AND user_id = 1
                """,
                (now, now, now, self.GUILD),
            )
        database.reset_user_cooldowns(self.GUILD, 1)
        with database.get_connection() as conn:
            values = conn.execute(
                "SELECT last_daily, last_loldle_easy, last_dbdle_killer FROM users "
                "WHERE guild_id = %s AND user_id = 1", (self.GUILD,)
            ).fetchone()
        self.assertEqual(values, (None, None, None))


if __name__ == "__main__":
    unittest.main()


class EverydleStreakPayoutTests(unittest.TestCase):
    """The bonus must follow the streak the member is shown.

    `claim_everydle_reward` computed it from `streak_count + 1` — the streak
    *before* this claim — while returning `new_streak` to the caller, so the
    embed and the payout disagreed. Reported as "I lost my streak, it says 1
    again, and I was paid more than last time".

    Two branches were wrong and only one was noticed. Nothing caught either,
    because **no test anywhere asserted a payout**: the existing everydle tests
    check `streak_count` and item consumption. That gap is the reason a wrong
    variable survived, so these assert coins.
    """

    GUILD = 1
    BASE_COIN = 5000
    BASE_XP = 100

    def setUp(self):
        self.original_path = database.DATA_DIR
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        self.day = datetime(2026, 9, 1, 12, 0)

    def tearDown(self):
        database.DATA_DIR = self.original_path
        self.temp_dir.cleanup()

    def claim(self, offset_days=0, column="last_valdle"):
        when = (self.day + timedelta(days=offset_days)).isoformat()
        return database.claim_everydle_reward(
            self.GUILD, 1, column, when, self.BASE_COIN, self.BASE_XP)

    def expected(self, streak):
        return int(self.BASE_COIN * (1.0 + min(streak, 100) / 100.0))

    def test_the_payout_matches_the_streak_that_is_shown(self):
        for offset, streak in ((0, 1), (1, 2), (2, 3)):
            with self.subTest(day=offset + 1):
                result = self.claim(offset)
                self.assertEqual(streak, result["streak"])
                self.assertEqual(self.expected(streak), result["reward"])

    def test_a_reset_streak_pays_a_reset_bonus(self):
        """The reported bug. A week's gap shows streak 1, so it must pay
        streak 1 — it used to pay the lapsed streak's bonus."""
        self.claim(0)
        self.claim(1)
        self.claim(2)                       # streak 3
        result = self.claim(12)             # ten days later
        self.assertEqual(1, result["streak"])
        self.assertEqual(self.expected(1), result["reward"])

    def test_a_second_game_the_same_day_pays_its_own_streak(self):
        """Never reported, same cause. `streak_count` and `last_streak_update`
        are shared across every Everydle game, so a second game the same day
        takes `day_gap == 0` and does not advance the streak — but the reward
        added one anyway."""
        first = self.claim(0)
        second = self.claim(0, column="last_dbdle_killer")
        self.assertEqual(first["streak"], second["streak"])
        self.assertEqual(first["reward"], second["reward"])

    def test_the_bonus_is_capped(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (guild_id, user_id) VALUES (%s, 1) "
                "ON CONFLICT DO NOTHING", (self.GUILD,))
            conn.execute(
                "UPDATE users SET streak_count = 150, last_streak_update = %s "
                "WHERE guild_id = %s AND user_id = 1",
                ((self.day - timedelta(days=1)).isoformat(), self.GUILD))
        result = self.claim(0)
        self.assertEqual(151, result["streak"])
        self.assertEqual(self.expected(100), result["reward"],
                         "the cap applies to the payout, not only to the count")


class AmountBoundTests(unittest.TestCase):
    """An amount past MAX_AMOUNT is refused with an answer, not an OverflowError.

    Past 2**63 a Python int cannot be bound by `sqlite3` at all; the failure was
    `OverflowError`, which no `except sqlite3.Error` handler catches, so the
    command died and the member saw nothing. The bound sits far below that.
    """

    HUGE = 10**19
    GUILD = 1

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        database.register_guild(1, "Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (guild_id, user_id, balance) "
                         "VALUES (%s, 1, 500)", (self.GUILD,))
            conn.execute("INSERT INTO users (guild_id, user_id, balance) "
                         "VALUES (%s, 2, 500)", (self.GUILD,))

    def tearDown(self):
        database.DATA_DIR = self.original_path
        self.temp_dir.cleanup()

    def test_an_instant_stake_past_the_bound_is_refused_not_raised(self):
        self.assertIsNone(database.resolve_instant_wager(self.GUILD, 1, self.HUGE))
        self.assertIsNone(database.resolve_dice_wager(1, 1, self.HUGE, 1, 1, 1))
        self.assertIsNone(database.resolve_wheel_wager(1, 1, self.HUGE))
        self.assertIsNone(database.resolve_slots_wager(1, 1, self.HUGE))

    def test_an_interactive_reservation_past_the_bound_is_refused(self):
        self.assertIsNone(database.begin_interactive_wager("w", 1, 1, "bj", self.HUGE))

    def test_a_transfer_past_the_bound_is_refused(self):
        self.assertIsNone(database.transfer_balance(self.GUILD, 1, 2, self.HUGE))
        self.assertEqual(500, database.get_user_balance(self.GUILD, 2))

    def test_a_raw_delta_past_the_bound_names_the_reason(self):
        with self.assertRaises(database.ValidationError) as caught:
            database.apply_user_delta(self.GUILD, 1, self.HUGE)
        self.assertEqual("amount_out_of_range", caught.exception.reason)
        self.assertEqual(500, database.get_user_balance(self.GUILD, 1))

    def test_the_bound_is_generous_enough_for_any_real_balance(self):
        result = database.apply_user_delta(self.GUILD, 1, database.MAX_AMOUNT)
        self.assertEqual(500 + database.MAX_AMOUNT, result["stats"][0])



class WriteTransactionTests(unittest.TestCase):
    """`get_connection` begins every non-read transaction itself, at once.

    Taking the write lock at the start rather than at the first write is what
    lets a read-then-write body act on what it read. On PostgreSQL the lock is
    a transaction-scoped advisory lock (`_begin_write_transaction`); it is
    checked here from an independent session, which is what a second process
    -- a dashboard run as its own service -- would be.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        self.outsider = psycopg.connect(database.DSN, autocommit=True)
        self.addCleanup(self.outsider.close)

    def tearDown(self):
        database.DATA_DIR = self.original_path
        self.temp_dir.cleanup()

    def _another_session_can_write(self) -> bool:
        with self.outsider.transaction():
            return self.outsider.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (database._WRITE_LOCK_KEY,)).fetchone()[0]

    def test_a_write_connection_holds_the_write_lock_before_any_statement(self):
        with database.get_connection():
            self.assertFalse(self._another_session_can_write())
        self.assertTrue(self._another_session_can_write())

    def test_a_read_connection_takes_no_lock_and_cannot_write(self):
        previous = getattr(database._DB_EXECUTION, "mode", None)
        database._DB_EXECUTION.mode = "read"
        try:
            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                with database.get_connection() as conn:
                    self.assertTrue(self._another_session_can_write())
                    conn.execute("INSERT INTO guilds (guild_id, first_seen_at, last_seen_at) "
                                 "VALUES (1, now(), now())")
        finally:
            database._DB_EXECUTION.mode = previous

    def test_a_refused_begin_is_a_database_operation_error(self):
        closed = psycopg.connect(database.DSN)
        closed.close()
        with self.assertRaises(database.DatabaseOperationError):
            database._begin_write_transaction(closed)


def _call_name(node):
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", None)


class TransactionShapeTests(unittest.TestCase):
    """Source-level guards the PostgreSQL move depends on."""

    SOURCES = ("core/database.py", "dashboard_api.py")

    @staticmethod
    def _tree(path):
        return ast.parse((ROOT / path).read_text(encoding="utf-8"))

    @staticmethod
    def _docstrings(tree):
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                body = node.body
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)):
                    found.add(id(body[0].value))
        return found

    def test_only_the_helper_begins_a_transaction(self):
        offenders = []
        for path in self.SOURCES:
            tree = self._tree(path)
            docstrings = self._docstrings(tree)
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if function.name == "_begin_write_transaction":
                    continue
                for node in ast.walk(function):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and id(node) not in docstrings
                            and node.value.lstrip().upper().startswith("BEGIN")):
                        offenders.append(f"{path}:{node.lineno} {function.name}")
        self.assertEqual([], offenders)

    def test_no_connection_is_opened_inside_an_open_one(self):
        """A nested connection would wait behind its own caller's write lock:
        the busy timeout on SQLite, forever on a PostgreSQL advisory lock."""
        database_tree = self._tree("core/database.py")
        functions = {node.name: node for node in ast.walk(database_tree)
                     if isinstance(node, ast.FunctionDef)}

        def opens(name, seen=frozenset()):
            if name in seen or name not in functions:
                return False
            for node in ast.walk(functions[name]):
                if isinstance(node, ast.Call):
                    called = _call_name(node)
                    if called == "get_connection" or (
                            called and opens(called, seen | {name})):
                        return True
            return False

        self.assertTrue(opens("get_user_balance"))  # the probe can fire
        nested = []
        for path in self.SOURCES:
            for function in ast.walk(self._tree(path)):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for block in ast.walk(function):
                    if not (isinstance(block, ast.With) and any(
                            isinstance(item.context_expr, ast.Call)
                            and _call_name(item.context_expr) == "get_connection"
                            for item in block.items)):
                        continue
                    body = ast.Module(body=block.body, type_ignores=[])
                    for node in ast.walk(body):
                        if (isinstance(node, ast.Call)
                                and _call_name(node) != "get_connection"
                                and opens(_call_name(node))):
                            nested.append(f"{path}:{node.lineno} {function.name} -> "
                                          f"{_call_name(node)}")
        self.assertEqual([], nested)

    def test_handlers_catch_the_engine_neutral_names(self):
        """`EngineError` and `IntegrityError` are the driver's exceptions under
        names the PostgreSQL move can repoint in one place."""
        def mentions_sqlite3(node):
            return any(isinstance(n, ast.Name) and n.id == "sqlite3"
                       for n in ast.walk(node))

        offenders = []
        for handler in ast.walk(self._tree("core/database.py")):
            if (isinstance(handler, ast.ExceptHandler) and handler.type is not None
                    and mentions_sqlite3(handler.type)):
                offenders.append(f"core/database.py:{handler.lineno}")
        outside = [path.relative_to(ROOT) for pattern in ("core/*.py", "cogs/*.py")
                   for path in sorted(ROOT.glob(pattern))]
        outside += [Path(name) for name in ("main.py", "update_db.py", "dashboard_api.py")]
        for path in outside:
            if str(path) == "core/database.py":
                continue
            tree = self._tree(str(path))
            for node in ast.walk(tree):
                imported = (isinstance(node, ast.Import)
                            and any(a.name == "sqlite3" for a in node.names)) or (
                    isinstance(node, ast.ImportFrom) and node.module == "sqlite3")
                if imported or (isinstance(node, ast.Name) and node.id == "sqlite3"):
                    offenders.append(f"{path}:{node.lineno}")
        self.assertEqual([], offenders)

    def test_no_sqlite_only_conflict_clause(self):
        """`INSERT OR IGNORE`/`OR REPLACE` exist only in SQLite; the portable
        spelling is `ON CONFLICT … DO NOTHING`/`DO UPDATE`."""
        pattern = re.compile(r"\bINSERT\s+OR\b|\bREPLACE\s+INTO\b", re.IGNORECASE)
        offenders = []
        for path in self.SOURCES:
            tree = self._tree(path)
            docstrings = self._docstrings(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings and pattern.search(node.value)):
                    offenders.append(f"{path}:{node.lineno}")
        self.assertEqual([], offenders)

    def test_no_lastrowid(self):
        """`cursor.lastrowid` is SQLite's; an insert reports its id with
        `RETURNING`, which PostgreSQL understands too."""
        offenders = [f"{path}:{node.lineno}" for path in self.SOURCES
                     for node in ast.walk(self._tree(path))
                     if isinstance(node, ast.Attribute) and node.attr == "lastrowid"]
        self.assertEqual([], offenders)

    def test_no_engine_json_functions(self):
        """SQLite's `json_each`/`json_extract`/`json_valid` have no PostgreSQL
        spelling in common; a JSON column is matched in Python."""
        pattern = re.compile(r"\bjson_(each|extract|valid|tree|array_length)\s*\(",
                             re.IGNORECASE)
        offenders = []
        for path in self.SOURCES:
            tree = self._tree(path)
            docstrings = self._docstrings(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings and pattern.search(node.value)):
                    offenders.append(f"{path}:{node.lineno}")
        self.assertEqual([], offenders)

    #: Every SQL `SUM`/`AVG`, by function. PostgreSQL returns `numeric` (a
    #: Decimal) for either over integers, and has no `SUM` over a comparison, so
    #: a new one is reviewed and its result converted before it joins this list.
    REVIEWED_SUM_AVG = {"get_top_gacha_luck"}

    def test_every_sum_and_avg_is_reviewed(self):
        pattern = re.compile(r"\b(SUM|AVG|TOTAL)\s*\(", re.IGNORECASE)
        found = set()
        for path in self.SOURCES:
            tree = self._tree(path)
            docstrings = self._docstrings(tree)
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and id(node) not in docstrings and pattern.search(node.value)):
                        found.add(function.name)
        self.assertEqual(self.REVIEWED_SUM_AVG, found)

    #: Every function whose SQL holds an `ORDER BY`. PostgreSQL leaves ties in
    #: no particular order and puts NULL last in ascending order where SQLite
    #: puts it first, so each was checked to sort on a unique key, or given the
    #: tiebreaker and NULL placement that reproduce SQLite's (2.20.7a). A new one
    #: is reviewed the same way before it joins this list.
    REVIEWED_ORDER_BY = {
        "get_active_guilds", "get_realms",
        "get_voice_settings", "get_top_levels", "get_top_balances",
        "get_top_streaks", "get_top_gacha_luck", "get_top_xp_user",
        "get_shop_price", "get_shop_prices", "get_reward", "get_config_id",
        "_audit_rows", "get_access_invalidations_since", "get_work_responses",
        "adopt_work_tier", "get_expired_lfg_posts", "get_five_star_history",
        "get_user_inventory", "get_user_vouchers", "_extend_timed_entitlement",
        "rollback_voucher_redemption", "get_fulfillment_requests",
        "get_active_entitlements", "get_shop_item_definitions",
        "purchase_custom_shop_item", "rollback_custom_role_purchase",
        "list_managed_messages", "list_dashboard_documents",
        "claim_control_action", "get_retention_candidates",
    }

    def test_every_order_by_is_reviewed(self):
        found = set()
        for path in self.SOURCES:
            tree = self._tree(path)
            docstrings = self._docstrings(tree)
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and id(node) not in docstrings
                            and re.search(r"\bORDER\s+BY\b", node.value, re.IGNORECASE)):
                        found.add(function.name)
        self.assertEqual(self.REVIEWED_ORDER_BY, found)

    #: SQL fragments a function assembles from constants in its own body and
    #: interpolates: a WHERE clause, a column list, a SET list. Each was read;
    #: every identifier inside one goes through `sql_identifier` where it is
    #: built. A new one is reviewed the same way before it joins.
    REVIEWED_SQL_FRAGMENTS = {
        ("get_warning_count", "clause"), ("record_warning", "scope"),
        ("record_warning", "clause"), ("set_guild_settings", "where"),
        ("export_user_data", "clause"), ("anonymize_user", "clause"),
        ("_blank_personal_columns_locked", "cooldowns"),
        ("adopt_legacy_database", "select_columns"),
        ("adopt_legacy_database", "insert_columns"),
    }
    def test_every_interpolated_identifier_is_checked(self):
        sql = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA)\b")
        offenders = []
        for path in self.SOURCES:
            tree = self._tree(path)
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if not isinstance(node, ast.JoinedStr):
                        continue
                    text = "".join(v.value for v in node.values
                                   if isinstance(v, ast.Constant))
                    if not sql.search(text):
                        continue
                    for value in node.values:
                        if not isinstance(value, ast.FormattedValue):
                            continue
                        expr = value.value
                        if isinstance(expr, ast.Call) and _call_name(expr) == "sql_identifier":
                            continue
                        if isinstance(expr, ast.Name) and (
                                expr.id == "placeholders"
                                or (function.name, expr.id) in self.REVIEWED_SQL_FRAGMENTS):
                            continue
                        offenders.append(f"{path}:{node.lineno} {function.name}: "
                                         f"{ast.unparse(expr)}")
        self.assertEqual([], offenders)


class TicketReplacementTests(unittest.TestCase):
    """A non-support ticket on a channel that already has a row replaces it
    completely, as SQLite's `INSERT OR REPLACE` did by deleting the row first;
    a support ticket stays a plain insert the partial index can refuse."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()

    def tearDown(self):
        database.DATA_DIR = self.original_path
        self.temp_dir.cleanup()

    def test_the_column_set_the_replacement_resets_is_complete(self):
        """`add_ticket` names every column outside its insert in the update;
        a new column has to join that list, and this fails until it does."""
        with database.get_connection() as conn:
            columns = {row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'tickets'")}
        self.assertEqual(
            {"channel_id", "opener_id", "created_at", "guild_id", "ticket_type",
             "claimer_id"},
            columns,
        )

    def test_a_stale_row_is_replaced_and_its_claimer_cleared(self):
        database.add_ticket(500, 11, guild_id=1, ticket_type="purchase")
        database.set_ticket_claimer(500, 77)
        database.add_ticket(500, 22, guild_id=2, ticket_type="report")
        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT opener_id, guild_id, ticket_type, claimer_id FROM tickets "
                "WHERE channel_id = 500").fetchall()
        self.assertEqual([(22, 2, "report", None)], rows)

    def test_a_second_support_ticket_for_one_member_is_still_refused(self):
        database.add_ticket(600, 11, guild_id=1, ticket_type="support")
        with self.assertRaises(database.DatabaseOperationError):
            database.add_ticket(601, 11, guild_id=1, ticket_type="support")


class ReturnedIdTests(unittest.TestCase):
    """Every insert that reports its new id reads it from `RETURNING`, which
    PostgreSQL shares, rather than SQLite's `cursor.lastrowid`. Each id must
    name the row that call wrote -- two calls apiece, so a stale or shared id
    cannot pass by coincidence."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        database.register_guild(1, "Guild")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(setattr, database, "DATA_DIR", self.original_path)

    def _value(self, sql, key):
        with database.get_connection() as conn:
            row = conn.execute(sql, (key,)).fetchone()
        return row[0] if row else None

    def test_create_realm(self):
        ids = [database.create_realm(name, 9) for name in ("First realm", "Second realm")]
        self.assertEqual(["First realm", "Second realm"],
                         [self._value("SELECT name FROM realms WHERE realm_id = %s", i)
                          for i in ids])

    def test_record_warning(self):
        ids = [database.record_warning(5, 9, reason, "2026-10-31", 1, "general")["warning_id"]
               for reason in ("first", "second")]
        self.assertEqual(["first", "second"],
                         [self._value("SELECT reason FROM warnings WHERE id = %s", i)
                          for i in ids])

    def test_create_work_response(self):
        ids = [database.create_work_response(1, 9, "normal", message)["response_id"]
               for message in ("Dug {earnings}.", "Found {earnings}.")]
        self.assertEqual(
            ["Dug {earnings}.", "Found {earnings}."],
            [self._value("SELECT message FROM work_responses WHERE response_id = %s", i)
             for i in ids])

    def test_adopt_work_tier_maps_each_shipped_row_to_its_copy(self):
        with database.get_connection() as conn:
            shipped = dict(conn.execute(
                "SELECT response_id, message FROM work_responses "
                "WHERE guild_id = %s AND tier = 'normal'",
                (database.WORK_DEFAULT_GUILD_ID,)).fetchall())
            adopted = database.adopt_work_tier(conn, 1, 9, "normal")
        self.assertGreater(len(shipped), 1)
        self.assertEqual(set(shipped), set(adopted))
        for shipped_id, copy_id in adopted.items():
            self.assertNotEqual(shipped_id, copy_id)
            self.assertEqual(
                shipped[shipped_id],
                self._value("SELECT message FROM work_responses "
                            "WHERE guild_id = 1 AND response_id = %s", copy_id))

    def test_grant_manual_entitlement(self):
        ids = [database.grant_manual_entitlement(1, 5, "emoji", item, 7, 9)["entitlement_id"]
               for item in ("111", "222")]
        self.assertEqual(
            ["111", "222"],
            [self._value("SELECT discord_item_id FROM timed_entitlements "
                         "WHERE entitlement_id = %s", i) for i in ids])

    def test_save_dashboard_document(self):
        ids = [database.save_dashboard_document(1, 9, "embed", name, {"n": name})["document_id"]
               for name in ("Alpha", "Beta")]
        self.assertEqual(["Alpha", "Beta"],
                         [self._value("SELECT name FROM dashboard_documents "
                                      "WHERE document_id = %s", i) for i in ids])

    def test_queue_control_action(self):
        ids = [database.queue_control_action(1, 9, "test_announcement", {"n": n})
               for n in (1, 2)]
        self.assertEqual(['{"n": 1}', '{"n": 2}'],
                         [self._value("SELECT payload_json FROM control_actions "
                                      "WHERE action_id = %s", i) for i in ids])


class SubjectJsonMatchTests(unittest.TestCase):
    """A member's id inside a JSON column is matched exactly, in Python.

    The SQL only narrows the rows by the id's digits, so every case here that a
    LIKE alone would get wrong -- a longer id containing the member's, a string
    spelling of it, a malformed payload -- has to come out as it did when
    SQLite's JSON functions decided (todo.md §2, docs/subsystems/database.md).
    """

    MEMBER = 123
    NEAR_MISSES = (1234, 9123)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(setattr, database, "DATA_DIR", self.original_path)

    def _lfg(self, message_id, joined_json, host_id=900):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO lfg_posts (guild_id, message_id, channel_id, host_id, "
                "game_text, needed, joined_json, created_at) "
                "VALUES (1, %s, 2, %s, 'game', 3, %s, '2026-10-31T00:00:00+00:00')",
                (message_id, host_id, joined_json))

    def _audit(self, action, payload):
        with database.get_connection() as conn:
            return conn.execute(
                "INSERT INTO settings_audit (guild_id, actor_id, action, target_key, "
                "old_value_json, created_at) VALUES (1, 900, %s, 'k', %s, "
                "'2026-10-31T00:00:00+00:00') RETURNING audit_id",
                (action, payload)).fetchone()[0]

    def _action(self, payload_json):
        with database.get_connection() as conn:
            return conn.execute(
                "INSERT INTO control_actions (guild_id, actor_id, action_type, "
                "payload_json, created_at) VALUES (1, 900, 'erase_member', %s, "
                "'2026-10-31T00:00:00+00:00') RETURNING action_id",
                (payload_json,)).fetchone()[0]

    def _exported(self, table, key):
        return {row[key] for row in database.export_user_data(self.MEMBER)["tables"][table]}

    def test_a_joined_party_is_exported_and_near_misses_are_not(self):
        self._lfg(1, json.dumps([900, self.MEMBER]))
        self._lfg(2, json.dumps(list(self.NEAR_MISSES)))
        self._lfg(3, json.dumps([str(self.MEMBER)]))
        self._lfg(4, f"[{self.MEMBER}")  # malformed: no row, and no error
        self._lfg(5, "[]", host_id=self.MEMBER)
        self.assertEqual({1, 5}, self._exported("lfg_posts", "message_id"))

    def test_a_removed_warning_names_its_subject_and_its_moderator(self):
        as_moderator = self._audit("warning.delete",
                                   json.dumps({"user_id": 7, "moderator_id": self.MEMBER}))
        as_subject = self._audit("warning.delete", json.dumps({"user_id": self.MEMBER}))
        self._audit("warning.delete", json.dumps({"user_id": self.NEAR_MISSES[0]}))
        self._audit("warning.delete", json.dumps({"user_id": str(self.MEMBER)}))
        self._audit("feature.toggle", json.dumps({"user_id": self.MEMBER}))
        self._audit("warning.delete", f'{{"user_id": {self.MEMBER}')
        self.assertEqual({as_moderator, as_subject},
                         self._exported("settings_audit", "audit_id"))

    def test_an_outbox_payload_about_the_member_is_exported_and_scrubbed_alone(self):
        about = self._action(json.dumps({"user_id": self.MEMBER}))
        near = [self._action(json.dumps({"user_id": other})) for other in self.NEAR_MISSES]
        spelled = self._action(json.dumps({"user_id": str(self.MEMBER)}))
        malformed = self._action(f'{{"user_id": {self.MEMBER}')
        self.assertEqual({about}, self._exported("control_actions", "action_id"))
        with database.get_connection() as conn:
            scrubbed = database._scrub_control_action_payloads_locked(conn, self.MEMBER, -1)
            payloads = dict(conn.execute(
                "SELECT action_id, payload_json FROM control_actions").fetchall())
        self.assertEqual(1, scrubbed)
        self.assertEqual({"user_id": -1}, json.loads(payloads[about]))
        for untouched, user in zip(near, self.NEAR_MISSES):
            self.assertEqual({"user_id": user}, json.loads(payloads[untouched]))
        self.assertEqual({"user_id": str(self.MEMBER)}, json.loads(payloads[spelled]))
        self.assertEqual(f'{{"user_id": {self.MEMBER}', payloads[malformed])


class AggregateResultTests(unittest.TestCase):
    """An aggregate's Python type is the same on either engine."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(setattr, database, "DATA_DIR", self.original_path)

    def test_the_luck_average_is_a_float(self):
        with database.get_connection() as conn:
            for pity in (1, 2, 4):
                conn.execute(
                    "INSERT INTO gacha_pulls (guild_id, user_id, banner_key, "
                    "banner_revision, rarity, reward_key, reward_json, pity_before, "
                    "soft_pity, hard_pity, four_star_guarantee, featured, "
                    "featured_guaranteed, created_at) VALUES "
                    "(1, 5, %s, 1, 5, 'test_reward', '{}', %s, 0, 0, 0, 0, 0, "
                    "'2026-01-01T00:00:00+00:00')",
                    (database.DEFAULT_GACHA_BANNER_KEY, pity))
        [(user_id, average, count)] = database.get_top_gacha_luck(1, [5])
        self.assertEqual((5, 3), (user_id, count))
        self.assertIs(float, type(average))
        self.assertAlmostEqual(10 / 3, average)  # pity_before + 1: 2, 3, 5


class OrderingContractTests(unittest.TestCase):
    """Orders the code relies on, written the way SQLite already produced them.

    On SQLite these pass with or without the explicit tiebreakers, because they
    encode what its sorter did anyway; they become the check when the suite runs
    on PostgreSQL, which promises neither the tie order nor SQLite's NULL
    placement (todo.md §2, Phase 2).
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
        database.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(setattr, database, "DATA_DIR", self.original_path)

    GUILD = 1

    def _users(self, rows):
        with database.get_connection() as conn:
            conn.cursor().executemany(
                "INSERT INTO users (guild_id, user_id, xp, balance) "
                "VALUES (%s, %s, %s, %s)",
                [(self.GUILD, *row) for row in rows])

    def test_leaderboard_ties_rank_by_id_and_null_is_never_first(self):
        # SQLite put NULL last in a descending sort; PostgreSQL puts it first,
        # which would rank a NULL above everyone and break the merge's sort.
        self._users([(9, 5, 5), (3, 5, 5), (7, 5, 5), (1, 8, 8), (5, None, None)])
        members = [1, 3, 5, 7, 9]
        self.assertEqual([1, 3, 7], [row[0] for row in database.get_top_levels(self.GUILD, members, 3)])
        self.assertEqual([1, 3, 7], [row[0] for row in database.get_top_balances(self.GUILD, members, 3)])

    def test_the_top_xp_tie_goes_to_the_lower_id(self):
        self._users([(7, 10, 0), (3, 10, 0), (4, None, 0)])
        self.assertEqual(3, database.get_top_xp_user(self.GUILD, [3, 4, 7]))

    def test_a_guild_without_a_name_lists_first(self):
        for guild_id, name in ((30, "Beta"), (10, "Alpha"), (20, None)):
            database.register_guild(guild_id, name)
        with database.get_connection() as conn:
            conn.execute("UPDATE guilds SET display_name = NULL WHERE guild_id = 20")
        expected = ["20", "10", "30"]
        self.assertEqual(expected, [g["id"] for g in database.get_active_guilds()])
        self.assertEqual(expected,
                         [g["id"] for g in database.get_active_guilds([10, 20, 30])])

    def test_two_banners_of_one_name_list_by_key(self):
        config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        for key in ("zeta", "alpha"):
            database.create_gacha_banner(10, 99, key, "Same Name", config)
        keys = [banner["banner_key"] for banner in database.list_gacha_banners(10)]
        self.assertEqual([database.DEFAULT_GACHA_BANNER_KEY, "alpha", "zeta"], keys)

    def test_an_expiry_tie_extends_the_older_grant(self):
        first = database.grant_manual_entitlement(1, 5, "emoji", "111", 7, 9)["entitlement_id"]
        second = database.grant_manual_entitlement(1, 5, "emoji", "222", 7, 9)["entitlement_id"]
        with database.get_connection() as conn:
            conn.execute("UPDATE timed_entitlements SET expires_at = "
                         "'2030-01-01T00:00:00+00:00'")
            now = datetime(2026, 10, 31, tzinfo=timezone.utc)
            database._extend_timed_entitlement(conn, 1, 5, "emoji", 1, now, None,
                                               now.isoformat())
            expiries = dict(conn.execute(
                "SELECT entitlement_id, expires_at FROM timed_entitlements").fetchall())
        self.assertEqual(datetime(2030, 1, 2, tzinfo=timezone.utc),
                         parse_stored(expiries[min(first, second)]))
        self.assertEqual(datetime(2030, 1, 1, tzinfo=timezone.utc),
                         parse_stored(expiries[max(first, second)]))

    def test_a_member_with_no_recorded_value_is_not_ranked(self):
        """A NULL reaching `_ranked_members`' merge compared None with an int,
        and the caught TypeError emptied the whole board. Such a member is left
        off it instead (2.20.8a)."""
        with database.get_connection() as conn:
            conn.cursor().executemany(
                "INSERT INTO users (guild_id, user_id, xp, balance, streak_count) "
                "VALUES (%s, %s, %s, %s, %s)",
                [(self.GUILD, 1, 5, 50, 2), (self.GUILD, 2, None, None, None)])
        self.assertEqual([1], [row[0] for row in database.get_top_levels(self.GUILD, [1, 2], 10)])
        self.assertEqual([1], [row[0] for row in database.get_top_balances(self.GUILD, [1, 2], 10)])
        self.assertEqual([1], [row[0] for row in database.get_top_streaks(self.GUILD, [1, 2], 10)])
        self.assertEqual(1, database.get_top_xp_user(self.GUILD, [1, 2]))


class BannerScheduleInstantTests(unittest.TestCase):
    """A banner's window compares instants, so stored text and the `datetime`
    PostgreSQL returns order alike, and so do two offsets of one moment."""

    def test_text_and_datetime_bounds_give_the_same_status(self):
        start = "2026-11-01T00:00:00+00:00"
        end = "2026-11-08T00:00:00+00:00"
        for bounds in ((start, end), (parse_stored(start), parse_stored(end))):
            status = database._banner_pull_status
            self.assertEqual("scheduled", status(True, *bounds, "2026-10-31T23:59:59+00:00"))
            self.assertEqual("live", status(True, *bounds, "2026-11-01T00:00:00+00:00"))
            self.assertEqual("expired", status(True, *bounds, "2026-11-08T00:00:00+00:00"))
            self.assertEqual("disabled", status(False, *bounds, "2026-11-02T00:00:00+00:00"))

    def test_an_offset_now_is_compared_as_the_instant_it_names(self):
        # 00:30 at +01:00 is 23:30 UTC the evening before: not yet started.
        self.assertEqual("scheduled", database._banner_pull_status(
            True, "2026-11-01T00:00:00+00:00", None, "2026-11-01T00:30:00+01:00"))


class SqlIdentifierTests(unittest.TestCase):
    """The one gate a table or column name passes before it is interpolated."""

    def test_a_plain_lowercase_identifier_passes_unchanged(self):
        for name in ("users", "last_valdle", "guild_settings", "_x9"):
            self.assertIs(name, database.sql_identifier(name))

    def test_anything_else_is_refused(self):
        for name in ("Users", "users; DROP TABLE users", "users--", '"users"',
                     "last valdle", "9lives", "", None, 7, "users\n"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                database.sql_identifier(name)


class SchemaTypeMapTests(unittest.TestCase):
    """`core/schema_types.py` beyond what `BaselineSchemaTests` checks against
    the PostgreSQL schema: the naming rule, and that what the code writes into
    an INSTANT column is an instant. (Its SQLite-side checks went with SQLite's
    schema, and the SQLite type table with the archived file.)"""

    def test_every_json_column_says_so_in_its_name(self):
        from core import schema_types
        for table, columns in schema_types.COLUMN_KINDS.items():
            for column, kind in columns.items():
                with self.subTest(column=f"{table}.{column}"):
                    self.assertEqual(column.endswith("_json"), kind == schema_types.JSON)

    def test_what_the_code_writes_into_an_instant_column_parses(self):
        """Every instant the accessors exercised by this suite file leaves
        behind reads back through `parse_stored`, the copy's own reader."""
        from core import schema_types
        with tempfile.TemporaryDirectory() as directory:
            original = database.DATA_DIR
            database.DATA_DIR = directory
            pg_harness.fresh_database()
            try:
                database.initialize_database()
                database.register_guild(1, "Guild")
                database.create_realm("A realm", 9)
                database.record_warning(5, 9, "spam", database.utc_now().isoformat(), 1,
                                        "general")
                database.create_work_response(1, 9, "normal", "Dug {earnings}.")
                database.grant_manual_entitlement(1, 5, "emoji", "111", 7, 9)
                database.save_dashboard_document(1, 9, "embed", "Doc", {"n": 1})
                database.queue_control_action(1, 9, "test_announcement", {"n": 1})
                database.add_ticket(500, 11, guild_id=1, ticket_type="purchase")
                checked = 0
                with database.get_connection() as conn:
                    for table, columns in schema_types.COLUMN_KINDS.items():
                        for column, kind in columns.items():
                            if kind != schema_types.INSTANT:
                                continue
                            for (value,) in conn.execute(
                                    f"SELECT {column} FROM {table} "
                                    f"WHERE {column} IS NOT NULL"):
                                parse_stored(value)
                                checked += 1
            finally:
                database.DATA_DIR = original
        self.assertGreater(checked, 10)
