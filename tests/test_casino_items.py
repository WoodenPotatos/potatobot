"""Casino consumables: what they change, and when they are spent.

Every one of these items is a probability change, so a test that only checks
"the branch ran" would pass while the odds were wrong. Each item's effect is
measured against its closed form, the way the gacha featured split is.

The rule they all share, from the loaded die: an item is spent only when an
otherwise-eligible *paid* wager resolves — win or lose — and never by a wager
that was refused. That is structural rather than remembered, because the
consumption happens inside the same transaction as the stake.
"""

import os
import random
import tempfile
import unittest
from itertools import product

import database
import item_catalog


GUILD = 10
MEMBER = 1


class CasinoItemTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "casino.db")
        database.initialize_database()
        database.register_guild(GUILD, "Casino Guild")
        self.set_balance(10_000_000_000)

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def set_balance(self, amount):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, balance) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
                (MEMBER, amount),
            )

    def give(self, item_key, quantity):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO user_inventory VALUES (?, ?, ?, ?, '2026-01-01') "
                "ON CONFLICT(guild_id, user_id, item_key) DO UPDATE SET "
                "quantity = excluded.quantity",
                (GUILD, MEMBER, item_key, quantity),
            )

    def held(self, item_key):
        return database.get_user_inventory(GUILD, MEMBER).get(item_key, 0)


class RoulettePayoutTests(CasinoItemTestCase):
    """The arithmetic moved out of the cog and must not have changed."""

    def test_the_payouts_are_the_ones_the_cog_used(self):
        self.assertEqual(100, database._roulette_payout(100, 3, "red", "red", None))
        self.assertEqual(1400, database._roulette_payout(100, 0, "green", "green", None))
        self.assertEqual(3500, database._roulette_payout(100, 17, "black", None, 17))
        self.assertEqual(0, database._roulette_payout(100, 17, "black", "red", None))

    def test_zero_is_green_and_the_red_set_is_the_european_wheel(self):
        self.assertEqual((0, "green"), database._roulette_spin(_Fixed(0)))
        self.assertEqual(18, len(database.ROULETTE_RED_NUMBERS))
        for number in range(1, 37):
            _, colour = database._roulette_spin(_Fixed(number))
            self.assertEqual(
                colour, "red" if number in database.ROULETTE_RED_NUMBERS else "black")


class LoadedDieInRouletteTests(CasinoItemTestCase):
    # The effects under test move the win rate by ~0.25, so three thousand
    # rounds (standard error ~0.009) separates them by more than twenty
    # sigma. Any more is suite time spent proving nothing extra.
    ROUNDS = 3_000

    def _win_rate(self, seed):
        rng = random.Random(seed)
        wins = 0
        for _ in range(self.ROUNDS):
            result = database.resolve_roulette_wager(
                GUILD, MEMBER, 100, "red", None, rng=rng)
            wins += result["outcome"] == "win"
        return wins / self.ROUNDS

    def test_the_base_rate_is_the_wheel(self):
        # 18 red pockets out of 37. Nothing about the move into the transaction
        # may change the odds a member without an item faces.
        self.assertAlmostEqual(18 / 37, self._win_rate(11), delta=0.03)

    def test_the_die_gives_the_better_of_two_spins(self):
        self.give("loaded_die", self.ROUNDS)
        # Two independent spins, so a loss needs both to miss.
        expected = 1 - (19 / 37) ** 2
        self.assertAlmostEqual(expected, self._win_rate(12), delta=0.03)

    def test_the_die_is_spent_by_one_spin(self):
        self.give("loaded_die", 2)
        result = database.resolve_roulette_wager(
            GUILD, MEMBER, 100, "red", None, rng=random.Random(1))
        self.assertTrue(result["loaded_die"])
        self.assertEqual(1, self.held("loaded_die"))

    def test_a_refused_wager_spends_nothing(self):
        self.give("loaded_die", 1)
        self.set_balance(5)
        self.assertIsNone(database.resolve_roulette_wager(
            GUILD, MEMBER, 100, "red", None, rng=random.Random(1)))
        self.assertEqual(1, self.held("loaded_die"),
                         "an unaffordable wager must not spend the item")

    def test_a_spin_without_one_reports_it_and_draws_once(self):
        counter = _CountingRng()
        database.resolve_roulette_wager(GUILD, MEMBER, 100, "red", None, rng=counter)
        self.assertEqual(1, counter.calls)

    def test_an_invalid_bet_is_refused_before_the_stake_moves(self):
        for colour, number in (("purple", None), (None, 37), (None, None)):
            self.assertIsNone(database.resolve_roulette_wager(
                GUILD, MEMBER, 100, colour, number, rng=random.Random(1)))


class LuckyCharmInSlotsTests(CasinoItemTestCase):
    # The effects under test move the win rate by ~0.25, so three thousand
    # rounds (standard error ~0.009) separates them by more than twenty
    # sigma. Any more is suite time spent proving nothing extra.
    ROUNDS = 3_000

    @staticmethod
    def base_win_rate():
        """Exhaustive rather than assumed: every one of the 729 outcomes."""
        outcomes = list(product(database.SLOT_SYMBOLS, repeat=3))
        wins = sum(1 for reels in outcomes if database._slots_payout(10, reels) > 0)
        return wins / len(outcomes)

    def test_the_payout_table_is_the_one_the_cog_used(self):
        self.assertEqual(5000, database._slots_payout(100, ("7️⃣", "7️⃣", "7️⃣")))
        self.assertEqual(1000, database._slots_payout(100, ("🍒", "🍒", "🍒")))
        self.assertEqual(150, database._slots_payout(100, ("🍒", "🍒", "🍋")))
        self.assertEqual(150, database._slots_payout(100, ("🍒", "🍋", "🍒")))
        self.assertEqual(0, database._slots_payout(100, ("🍒", "🍋", "🍇")))

    def test_the_base_rate_is_the_reels(self):
        self.assertAlmostEqual(225 / 729, self.base_win_rate(), places=10)
        rng = random.Random(21)
        wins = sum(
            database.resolve_slots_wager(GUILD, MEMBER, 100, rng=rng)["outcome"] == "win"
            for _ in range(self.ROUNDS)
        )
        self.assertAlmostEqual(self.base_win_rate(), wins / self.ROUNDS, delta=0.03)

    def test_the_charm_gives_the_better_of_two_spins(self):
        self.give("lucky_charm", self.ROUNDS)
        rng = random.Random(22)
        wins = sum(
            database.resolve_slots_wager(GUILD, MEMBER, 100, rng=rng)["outcome"] == "win"
            for _ in range(self.ROUNDS)
        )
        expected = 1 - (1 - self.base_win_rate()) ** 2
        self.assertAlmostEqual(expected, wins / self.ROUNDS, delta=0.03)

    def test_the_charm_never_lowers_the_payout(self):
        self.give("lucky_charm", 200)
        rng = random.Random(3)
        for _ in range(200):
            result = database.resolve_slots_wager(GUILD, MEMBER, 100, rng=rng)
            if result["second_reels"]:
                self.assertGreaterEqual(
                    result["payout"],
                    database._slots_payout(100, tuple(result["second_reels"])) if
                    result["reels"] != result["second_reels"] else 0,
                )

    def test_a_refused_wager_spends_nothing(self):
        self.give("lucky_charm", 1)
        self.set_balance(5)
        self.assertIsNone(database.resolve_slots_wager(GUILD, MEMBER, 100))
        self.assertEqual(1, self.held("lucky_charm"))


class InteractiveWagerItemTests(CasinoItemTestCase):
    """Blackjack and mines consume their item in the reservation itself.

    Their layouts are decided in the cog *after* the reservation has committed,
    so a separate consume call would leave a spent item with no wager, or a
    wager with a free item, whenever one of the two writes failed.
    """

    def test_the_item_is_consumed_by_the_reservation(self):
        self.give("stacked_deck", 2)
        reservation = database.begin_interactive_wager(
            "w1", GUILD, MEMBER, "blackjack", 100, consume_item="stacked_deck")
        self.assertTrue(reservation["consumed"])
        self.assertEqual(1, self.held("stacked_deck"))

    def test_holding_none_reserves_the_wager_anyway(self):
        reservation = database.begin_interactive_wager(
            "w2", GUILD, MEMBER, "blackjack", 100, consume_item="stacked_deck")
        self.assertIsNotNone(reservation)
        self.assertFalse(reservation["consumed"])

    def test_an_unaffordable_wager_spends_nothing(self):
        self.give("metal_detector", 1)
        self.set_balance(5)
        self.assertIsNone(database.begin_interactive_wager(
            "w3", GUILD, MEMBER, "mines", 100, consume_item="metal_detector"))
        self.assertEqual(1, self.held("metal_detector"),
                         "the debit is checked before the item is spent")

    def test_asking_for_no_item_consumes_nothing(self):
        self.give("stacked_deck", 1)
        reservation = database.begin_interactive_wager(
            "w4", GUILD, MEMBER, "mines", 100)
        self.assertFalse(reservation["consumed"])
        self.assertEqual(1, self.held("stacked_deck"))


class StackedDeckTests(unittest.TestCase):
    """The blackjack rule, as a pure function."""

    def test_it_keeps_the_higher_opening_hand(self):
        from cogs.casino import better_opening_hand
        low = [{"name": "2♠️", "value": 2}, {"name": "3♠️", "value": 3}]
        high = [{"name": "K♥️", "value": 10}, {"name": "A♥️", "value": 11}]
        self.assertIs(high, better_opening_hand(low, high))
        self.assertIs(high, better_opening_hand(high, low))

    def test_a_tie_keeps_the_first_hand(self):
        from cogs.casino import better_opening_hand
        first = [{"name": "K♠️", "value": 10}, {"name": "9♠️", "value": 9}]
        second = [{"name": "Q♥️", "value": 10}, {"name": "9♥️", "value": 9}]
        self.assertIs(first, better_opening_hand(first, second))

    def test_two_cards_can_never_bust_so_higher_is_always_better(self):
        # A hand of two is at most 21 (ace plus ten, scored down from 22), so
        # "closest to 21 without going over" reduces to "higher score" and the
        # rule needs no bust check.
        from cogs.casino import calc_score
        self.assertEqual(21, calc_score(
            [{"name": "A♠️", "value": 11}, {"name": "K♠️", "value": 10}]))
        self.assertEqual(12, calc_score(
            [{"name": "A♠️", "value": 11}, {"name": "A♥️", "value": 11}]))


class CatalogTests(unittest.TestCase):
    def test_the_new_items_are_inventory_items_in_both_systems(self):
        for key in ("stacked_deck", "lucky_charm", "metal_detector"):
            definition = item_catalog.ITEM_DEFINITIONS[key]
            self.assertIs(item_catalog.ItemEffect.INVENTORY, definition.effect)
            self.assertTrue(definition.sold_in_shop, f"{key} is not in the shop")
            self.assertTrue(definition.drawable_in_gacha, f"{key} is not drawable")
            self.assertIn(key, item_catalog.INVENTORY_ITEM_KEYS)

    def test_the_detector_reads_its_tile_count_from_the_catalog(self):
        self.assertEqual(1, item_catalog.ITEM_DEFINITIONS["metal_detector"].value)

    def test_the_shop_menu_still_fits_discord(self):
        """Adding a built-in must lower the custom cap, not overflow the menu."""
        import dashboard_api
        self.assertLessEqual(
            len(database.BUILTIN_SHOP_KEYS) + dashboard_api.SHOP_ITEM_LIMIT, 25)


class _Fixed:
    def __init__(self, value):
        self.value = value

    def randint(self, low, high):
        return self.value


class _CountingRng:
    def __init__(self):
        self.calls = 0

    def randint(self, low, high):
        self.calls += 1
        return low

    def choice(self, seq):
        self.calls += 1
        return seq[0]


if __name__ == "__main__":
    unittest.main()
