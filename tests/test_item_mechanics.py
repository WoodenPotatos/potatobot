"""A guild's own numbers for the built-in item mechanics.

`ItemDefinition.value` is the shipped default; `shop_item_values` is what a guild
puts in its place. Three things make that safe rather than a way to print coins,
and each is asserted here.

**Every knob has a declared bound, and the bound has a reason.** Each of these
numbers sits on a game whose house edge is 2% by design, so a generous value is
not cosmetic. An out-of-bounds value is *refused*, never clamped: applying half
of what an operator asked for is how a game quietly starts paying more than it
takes.

**The declaration is the only copy.** Two of these numbers used to be literals in
`database.py` that merely duplicated the catalog — a lockpick's 0.15 and a
drill's 0.25 — so a configurable value would have silently done nothing for
either. The absence of those literals is asserted against the source.

**Only scalars are configurable.** The four "keeps the better of two" items are
deliberately absent: their count lives in the caller, which pre-rolls both
outcomes and passes them in, so parameterising it is a signature change across
three resolvers rather than a value lookup.
"""

import asyncio
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import database
import item_catalog
import settings_cache
from settings_registry import SETTING_DEFINITIONS, validate_setting_value


class MechanicDeclarationTests(unittest.TestCase):
    def test_every_shipped_value_is_inside_its_own_bounds(self):
        """A bound tightened below the default would make every guild's shipped
        item silently out of range, and `mechanic_value` would ignore an
        override it should have accepted."""
        for key, parameter in item_catalog.MECHANIC_PARAMETERS.items():
            with self.subTest(item=key):
                shipped = item_catalog.ITEM_DEFINITIONS[key].value
                self.assertIsNotNone(shipped, "a mechanic with no default")
                self.assertGreaterEqual(shipped, parameter.minimum)
                self.assertLessEqual(shipped, parameter.maximum)

    def test_every_parameterised_item_is_a_real_catalog_item(self):
        for key in item_catalog.MECHANIC_PARAMETERS:
            self.assertIn(key, item_catalog.ITEM_DEFINITIONS)

    def test_each_bound_records_which_direction_favours_the_player(self):
        """Whoever reviews a future bound needs to know which way is dangerous."""
        for key, parameter in item_catalog.MECHANIC_PARAMETERS.items():
            with self.subTest(item=key):
                self.assertIn(parameter.player_favours, {"higher", "lower"})
                self.assertTrue(parameter.unit)

    def test_the_best_of_two_items_are_not_configurable(self):
        """Their count lives in the caller, so a value here would do nothing."""
        for key in ("loaded_die", "stacked_deck", "lucky_charm", "marked_card"):
            self.assertNotIn(key, item_catalog.MECHANIC_PARAMETERS)
            self.assertIsNone(item_catalog.mechanic_value(key, {key: 9}))

    def test_no_mechanic_number_is_still_a_literal_in_the_model(self):
        """The regression that would disable the whole feature silently.

        A lockpick's bonus and a drill's exposure were hard-coded, duplicating
        the catalog, so an override would have changed the declaration and
        nothing else. Comments are stripped before matching, so a comment
        explaining the old literal does not read as the literal being back.
        """
        source = open(os.path.join(ROOT, "database.py"), encoding="utf-8").read()
        code = "\n".join(line for line in source.split("\n")
                         if not line.strip().startswith("#"))
        self.assertNotIn("0.15 if inventory_lockpick", code)
        self.assertNotIn("protected * 0.25", code)
        # And the day-gap grace is derived rather than fixed at three.
        self.assertNotIn("day_gap == 3", code)


class MechanicValueTests(unittest.TestCase):
    def test_an_override_inside_the_bounds_wins(self):
        self.assertEqual(
            220, item_catalog.mechanic_value("parachute", {"parachute": 220}))

    def test_an_override_outside_the_bounds_is_ignored_not_clamped(self):
        """It can only arrive from a row written before a bound tightened, and
        applying part of an operator's intention is worse than none of it."""
        for value in (99, 245, 10000):
            with self.subTest(value=value):
                self.assertEqual(
                    195, item_catalog.mechanic_value("parachute",
                                                     {"parachute": value}))

    def test_a_nonsense_override_falls_back_to_the_shipped_value(self):
        for value in ("high", None, True, [], {}):
            with self.subTest(value=value):
                self.assertEqual(
                    0.15,
                    item_catalog.mechanic_value("lockpick", {"lockpick": value}))

    def test_no_overrides_reads_the_shipped_value(self):
        for key, definition in item_catalog.ITEM_DEFINITIONS.items():
            with self.subTest(item=key):
                self.assertEqual(definition.value,
                                 item_catalog.mechanic_value(key, {})
                                 if key in item_catalog.MECHANIC_PARAMETERS
                                 else definition.value)


class MechanicSettingTests(unittest.TestCase):
    def definition(self):
        return SETTING_DEFINITIONS["shop_item_values"]

    def test_a_valid_map_is_stored(self):
        self.assertEqual(
            {"parachute": 220, "metal_detector": 2},
            validate_setting_value(self.definition(),
                                   {"parachute": 220, "metal_detector": 2}))

    def test_a_value_equal_to_the_default_is_dropped(self):
        """"Reset it to normal" must leave no row behind, or a later change to
        the shipped default could never reach that guild."""
        self.assertEqual({}, validate_setting_value(
            self.definition(), {"parachute": 195, "lockpick": 0.15}))

    def test_an_integral_value_is_stored_as_an_integer(self):
        """A browser sending 3.0 for a tile count must not read back as a
        change nobody made."""
        stored = validate_setting_value(self.definition(), {"metal_detector": 3.0})
        self.assertEqual({"metal_detector": 3}, stored)
        self.assertIsInstance(stored["metal_detector"], int)

    def test_out_of_bounds_is_refused_with_the_bounds_named(self):
        with self.assertRaises(ValueError) as caught:
            validate_setting_value(self.definition(), {"metal_detector": 12})
        self.assertIn("3", str(caught.exception))

    def test_an_item_with_no_configurable_mechanic_is_refused(self):
        with self.assertRaises(ValueError):
            validate_setting_value(self.definition(), {"loaded_die": 5})
        with self.assertRaises(ValueError):
            validate_setting_value(self.definition(), {"premium": 1})

    def test_a_non_number_is_refused(self):
        for value in ("2", None, True, [2]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_setting_value(self.definition(),
                                           {"metal_detector": value})


class MechanicReachesTheGameTests(unittest.TestCase):
    """The point of the whole thing: the number an operator types is the number
    the game plays with."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "mechanics.db")
        database.initialize_database()
        database.register_guild(1, "Guild")
        settings_cache.invalidate()

    def tearDown(self):
        database.DB_PATH = self.original_path
        settings_cache.invalidate()
        self.temp_dir.cleanup()

    def override(self, values):
        database.set_guild_settings(
            1, 42, [{"key": "shop_item_values", "value": values, "revision": 0}])
        asyncio.run(settings_cache.refresh([1], force=True))

    def test_a_drill_exposes_the_share_the_operator_chose(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance, protected_reserve) "
                         "VALUES (1, 100000, 80000)")
            conn.execute("INSERT INTO users (user_id, balance) VALUES (2, 5000)")
            conn.execute(
                "INSERT INTO user_inventory (guild_id, user_id, item_key, "
                "quantity, updated_at) VALUES (1, 2, 'vault_glove', 1, '2026-01-01')")
            conn.commit()
        self.override({"vault_glove": 0.50})
        result = database.resolve_robbery(
            2, 1, "2026-01-01T00:00:00+00:00", 0.9, 1.0, 0.0, 1.0, guild_id=1)
        # 20,000 accessible plus half of the 80,000 reserve. The shipped 0.25
        # would have exposed 20,000 and taken 40,000.
        self.assertEqual(60000, result["amount"])

    def test_the_shipped_share_applies_with_no_override(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance, protected_reserve) "
                         "VALUES (1, 100000, 80000)")
            conn.execute("INSERT INTO users (user_id, balance) VALUES (2, 5000)")
            conn.execute(
                "INSERT INTO user_inventory (guild_id, user_id, item_key, "
                "quantity, updated_at) VALUES (1, 2, 'vault_glove', 1, '2026-01-01')")
            conn.commit()
        asyncio.run(settings_cache.refresh([1], force=True))
        result = database.resolve_robbery(
            2, 1, "2026-01-01T00:00:00+00:00", 0.9, 1.0, 0.0, 1.0, guild_id=1)
        self.assertEqual(40000, result["amount"])

    def test_an_unreadable_setting_falls_back_rather_than_raising(self):
        """A mechanic reverting to its shipped number is a game that still
        works; an exception inside a settlement transaction is not."""
        original = settings_cache.setting
        settings_cache.setting = lambda *_: (_ for _ in ()).throw(
            RuntimeError("boom"))
        try:
            self.assertEqual({}, database.guild_item_values(1))
        finally:
            settings_cache.setting = original


if __name__ == "__main__":
    unittest.main()
