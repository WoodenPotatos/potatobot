"""The casino earnings rate limit: an anti-exploit throttle, not a game.

Both thresholds ship at the inert 0 -- a shipped non-zero default here would
bake one installation's own economy scaling into every copy of the bot,
exactly the rule that forbids a shipped reward naming a fixed coin amount
(see the `warn_threshold_*` settings for the same shape). The throttle clamps
a casino payout to whatever budget remains in the rolling window rather than
refusing it outright, so a shown win is never clawed back to zero.
"""

import os
import tempfile
import unittest

from core import database
from core import feature_access
from core import settings_cache
from core import settings_registry

GUILD = 10
MEMBER = 1


class _FixedRng:
    """Always lands the wheel on its richest segment (2000 == 20x)."""

    def randrange(self, n):
        return n - 1

    def choice(self, seq):
        return seq[0]


class RateLimitRegistryTests(unittest.TestCase):
    def test_feature_ships_disabled_and_depends_on_casino(self):
        definition = settings_registry.FEATURE_DEFINITIONS["casino_rate_limit"]
        self.assertFalse(definition.default)
        self.assertEqual(("casino",), tuple(definition.dependencies))
        self.assertEqual("casino", definition.parent)

    def test_both_thresholds_default_to_the_inert_zero(self):
        """A shipped non-zero default would be this installation's own economy
        scaling, baked into every copy of the bot."""
        for key in ("casino_rate_limit_window_seconds", "casino_rate_limit_max_coins"):
            with self.subTest(key=key):
                self.assertEqual(0, settings_registry.SETTING_DEFINITIONS[key].default)

    def test_the_settings_are_owned_by_the_feature_they_configure(self):
        for key in ("casino_rate_limit_window_seconds", "casino_rate_limit_max_coins",
                    "casino_rate_limit_alert_channel"):
            with self.subTest(key=key):
                self.assertEqual(
                    "casino_rate_limit",
                    settings_registry.SETTING_DEFINITIONS[key].owner_feature)


class RateLimitDatabaseBehaviorTests(unittest.TestCase):
    """`_apply_stats_locked`'s opt-in clamp, exercised through a real resolver
    rather than called directly -- `resolve_wheel_wager` is the simplest of
    the six and needs no items or multi-step settlement."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "casino.db")
        database.initialize_database()
        database.register_guild(GUILD, "Casino Guild")
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, balance) VALUES (?, ?)",
                (MEMBER, 10_000_000_000),
            )

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _window_row(self):
        with database.get_connection() as conn:
            return conn.execute(
                "SELECT window_started_at, window_total FROM casino_earnings_window "
                "WHERE user_id = ?", (MEMBER,),
            ).fetchone()

    def test_no_rate_limit_argument_touches_no_window_row(self):
        result = database.resolve_wheel_wager(GUILD, MEMBER, 100, rng=_FixedRng())
        self.assertEqual(2000, result["multiplier"])
        self.assertEqual(2000, result["payout"])
        self.assertIsNone(self._window_row())
        self.assertFalse(result["casino_rate_limited"])

    def test_a_win_within_budget_is_credited_in_full(self):
        result = database.resolve_wheel_wager(
            GUILD, MEMBER, 100, rng=_FixedRng(), casino_rate_limit=(300, 10_000))
        self.assertEqual(2000, result["payout"])  # 100 stake * 20x
        self.assertFalse(result["casino_rate_limited"])
        _, window_total = self._window_row()
        self.assertEqual(2000, window_total)

    def test_a_win_crossing_the_threshold_is_clamped_not_refused(self):
        result = database.resolve_wheel_wager(
            GUILD, MEMBER, 100, rng=_FixedRng(), casino_rate_limit=(300, 500))
        self.assertTrue(result["casino_rate_limited"])
        self.assertEqual(500, result["payout"],
                         "the credit must clamp to the remaining budget, not refuse")
        _, window_total = self._window_row()
        self.assertEqual(500, window_total)

    def test_a_second_win_in_the_same_window_sees_the_first_ones_total(self):
        database.resolve_wheel_wager(
            GUILD, MEMBER, 100, rng=_FixedRng(), casino_rate_limit=(300, 2500))
        second = database.resolve_wheel_wager(
            GUILD, MEMBER, 100, rng=_FixedRng(), casino_rate_limit=(300, 2500))
        self.assertTrue(second["casino_rate_limited"])
        self.assertEqual(500, second["payout"],
                         "2000 + 2000 would be 4000 against a 2500 budget: "
                         "only 500 is left")

    def test_an_expired_window_starts_fresh_rather_than_sliding(self):
        database.resolve_wheel_wager(
            GUILD, MEMBER, 100, rng=_FixedRng(), casino_rate_limit=(300, 2000))
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE casino_earnings_window SET window_started_at = ? "
                "WHERE user_id = ?",
                ("2000-01-01T00:00:00+00:00", MEMBER),
            )
        result = database.resolve_wheel_wager(
            GUILD, MEMBER, 100, rng=_FixedRng(), casino_rate_limit=(300, 2000))
        self.assertFalse(result["casino_rate_limited"],
                         "an expired window must reset, not keep accumulating")
        self.assertEqual(2000, result["payout"])


class RateLimitConfigResolutionTests(unittest.TestCase):
    """`cogs.casino._casino_rate_limit_config`: both the feature and both
    thresholds must be set before this ever returns anything."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "casino.db")
        database.initialize_database()
        database.register_guild(GUILD, "Casino Guild")

    def tearDown(self):
        settings_cache.invalidate()
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _set(self, window, max_coins):
        settings_cache.apply_changes(GUILD, {
            "casino_rate_limit_window_seconds": {"value": window},
            "casino_rate_limit_max_coins": {"value": max_coins},
        })

    def test_feature_off_returns_none_even_if_both_thresholds_are_set(self):
        from cogs.casino import _casino_rate_limit_config

        feature_access.seed_cached_feature(GUILD, "casino_rate_limit", False)
        self._set(300, 10_000)
        self.assertIsNone(_casino_rate_limit_config(GUILD))

    def test_either_threshold_left_at_zero_returns_none(self):
        from cogs.casino import _casino_rate_limit_config

        feature_access.seed_cached_feature(GUILD, "casino_rate_limit", True)
        self._set(0, 10_000)
        self.assertIsNone(_casino_rate_limit_config(GUILD))
        self._set(300, 0)
        self.assertIsNone(_casino_rate_limit_config(GUILD))

    def test_feature_on_and_both_thresholds_set_returns_the_pair(self):
        from cogs.casino import _casino_rate_limit_config

        feature_access.seed_cached_feature(GUILD, "casino_rate_limit", True)
        self._set(300, 10_000)
        self.assertEqual((300, 10_000), _casino_rate_limit_config(GUILD))


if __name__ == "__main__":
    unittest.main()
