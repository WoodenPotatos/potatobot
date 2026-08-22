"""The Shop and Potato Gacha must agree on what a built-in item is.

Both systems hand out the same goods, so item identity — the stable key and the
effect it applies — lives once in `item_catalog`. What each system may differ on
is *acquisition*: a duplicate vault refuses a shop purchase and charges nothing,
while the same duplicate from a pull pays the banner's compensation. These tests
pin both halves of that split, because before the catalog the two systems had
drifted into a lockpick backed by different storage and vault tiers under keys
only one of them knew.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import database
import item_catalog
from settings_registry import SETTING_DEFINITIONS


class SharedItemIdentityTests(unittest.TestCase):
    """Static consistency between the catalog and everything derived from it."""

    def test_gacha_item_and_vault_rewards_are_catalog_items(self):
        drawable = {
            entry["key"]
            for tier in database.DEFAULT_GACHA_CONFIG["rewards"].values()
            for entry in tier
            if entry["kind"] in {"item", "vault"}
        }
        unknown = sorted(drawable - set(item_catalog.ITEM_DEFINITIONS))
        self.assertEqual(
            [], unknown,
            "a banner may only award items the shared catalog defines",
        )

    def test_gacha_vault_rewards_award_the_catalog_reserve(self):
        for tier in database.DEFAULT_GACHA_CONFIG["rewards"].values():
            for entry in tier:
                if entry["kind"] != "vault":
                    continue
                definition = item_catalog.VAULT_ITEMS[entry["key"]]
                self.assertEqual(
                    entry["amount"], definition.value,
                    f"{entry['key']} must protect the same reserve the shop sells",
                )

    def test_a_banner_cannot_redefine_a_catalog_vault(self):
        config = database._validated_gacha_config(database.DEFAULT_GACHA_CONFIG)
        config = {**config, "rewards": {**config["rewards"]}}
        config["rewards"]["5"] = [
            {"key": "big_vault", "kind": "vault", "amount": 1, "weight": 1},
        ]
        with self.assertRaises(database.ValidationError) as caught:
            database._validated_gacha_config(config)
        self.assertEqual(caught.exception.reason, "gacha_vault_amount_mismatch")

    def test_a_reward_key_must_be_a_usable_identifier(self):
        """The key names a locale entry and is written into every pull row, so an
        empty or oddly shaped one would persist as unrenderable history."""
        for bad_key in ("", "Med Vault", "med-vault", "x" * 65):
            with self.subTest(key=bad_key):
                config = database._validated_gacha_config(database.DEFAULT_GACHA_CONFIG)
                config = {**config, "rewards": {**config["rewards"]}}
                config["rewards"]["3"] = [
                    {"key": bad_key, "kind": "coins", "amount": 1, "weight": 1},
                ]
                with self.assertRaises(database.ValidationError) as caught:
                    database._validated_gacha_config(config)
                self.assertEqual(caught.exception.reason, "gacha_reward_key_invalid")

    def test_a_tier_cannot_list_one_reward_twice(self):
        config = database._validated_gacha_config(database.DEFAULT_GACHA_CONFIG)
        config = {**config, "rewards": {**config["rewards"]}}
        config["rewards"]["3"] = [
            {"key": "loaded_die", "kind": "item", "amount": 1, "weight": 1},
            {"key": "loaded_die", "kind": "item", "amount": 1, "weight": 9},
        ]
        with self.assertRaises(database.ValidationError) as caught:
            database._validated_gacha_config(config)
        self.assertEqual(caught.exception.reason, "gacha_reward_duplicate")

    def test_shop_defaults_and_builtin_keys_derive_from_the_catalog(self):
        self.assertEqual(database.SHOP_DEFAULTS, item_catalog.shop_default_prices())
        self.assertEqual(set(database.BUILTIN_SHOP_KEYS), set(item_catalog.SHOP_ITEMS))

    def test_every_shop_item_has_a_price_setting(self):
        expected = {f"shop_price_{key}" for key in item_catalog.SHOP_ITEMS}
        actual = {key for key in SETTING_DEFINITIONS if key.startswith("shop_price_")}
        self.assertEqual(expected, actual)

    def test_the_two_new_items_are_sold_and_drawable(self):
        for key in ("loaded_die", "vault_glove"):
            definition = item_catalog.ITEM_DEFINITIONS[key]
            self.assertTrue(definition.sold_in_shop, f"{key} must be purchasable")
            self.assertTrue(definition.drawable_in_gacha, f"{key} must be pullable")

    def test_shop_menu_cannot_exceed_the_discord_select_limit(self):
        import dashboard_api
        from cogs.shop import SELECT_OPTION_LIMIT

        self.assertLessEqual(
            len(database.BUILTIN_SHOP_KEYS) + dashboard_api.SHOP_ITEM_LIMIT,
            SELECT_OPTION_LIMIT,
        )


class SharedItemPurchaseTests(unittest.TestCase):
    """Runtime behaviour: same storage from either system, different rules."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "shop.db")
        database.initialize_database()
        database.register_guild(10, "Shop Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 1000000)")
            conn.execute("INSERT INTO users (user_id, balance) VALUES (2, 100000)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_consumables_stack_instead_of_being_refused(self):
        database.purchase_inventory_item(10, 1, 10000, "loaded_die")
        result = database.purchase_inventory_item(10, 1, 10000, "loaded_die")
        self.assertTrue(result["purchased"])
        self.assertEqual(database.get_user_inventory(10, 1)["loaded_die"], 2)
        self.assertEqual(database.get_user_balance(1), 1000000 - 20000)

    def test_a_bought_lockpick_and_a_pulled_one_share_one_row(self):
        database.purchase_inventory_item(10, 1, 5000, "lockpick")
        with database.get_connection() as conn:
            database._grant_gacha_reward_locked(
                conn, 10, 1,
                {"key": "lockpick", "kind": "item", "amount": 1},
                database.DEFAULT_GACHA_CONFIG, datetime.now().isoformat(),
            )
        # One row, not one per source: the systems grant the same object.
        self.assertEqual(database.get_user_inventory(10, 1), {"lockpick": 2})

    def test_a_purchase_that_cannot_be_afforded_grants_nothing(self):
        result = database.purchase_inventory_item(10, 2, 500000, "vault_glove")
        self.assertFalse(result["purchased"])
        self.assertEqual(result["reason"], "insufficient_funds")
        self.assertEqual(database.get_user_balance(2), 100000)
        self.assertEqual(database.get_user_inventory(10, 2), {})

    def test_only_catalog_inventory_items_can_be_purchased(self):
        for key in ("big_vault", "premium", "not_an_item"):
            with self.assertRaises(ValueError):
                database.purchase_inventory_item(10, 1, 100, key)

    def test_a_purchase_reports_the_price_it_actually_charged(self):
        result = database.purchase_inventory_item(10, 1, 7500, "lockpick")
        self.assertEqual(result["price"], 7500)
        self.assertEqual(result["balance"], 1000000 - 7500)

    def test_duplicate_vault_refuses_a_purchase_but_compensates_a_pull(self):
        reserve = item_catalog.VAULT_ITEMS["big_vault"].value
        price = 250000
        database.purchase_upgrade(1, price, "vault", reserve)
        self.assertEqual(database.get_user_balance(1), 1000000 - price)

        # The shop's rule: nothing is charged for something already owned.
        before = database.get_user_balance(1)
        refused = database.purchase_upgrade(1, price, "vault", reserve)
        self.assertFalse(refused["purchased"])
        self.assertEqual(refused["reason"], "already_owned")
        self.assertEqual(database.get_user_balance(1), before)

        # The gacha's rule: a duplicate pays the configured percentage instead.
        with database.get_connection() as conn:
            granted = database._grant_gacha_reward_locked(
                conn, 10, 1,
                {"key": "big_vault", "kind": "vault", "amount": reserve},
                database.DEFAULT_GACHA_CONFIG, datetime.now().isoformat(),
            )
        self.assertEqual(granted["duplicate_compensation"], reserve // 10)
        self.assertEqual(database.get_user_balance(1), before + reserve // 10)

    def test_the_same_vault_key_protects_the_same_reserve_from_either_system(self):
        definition = item_catalog.VAULT_ITEMS["med_vault"]
        database.purchase_upgrade(1, 0, "vault", definition.value)
        with database.get_connection() as conn:
            bought = conn.execute(
                "SELECT protected_reserve FROM users WHERE user_id = 1"
            ).fetchone()[0]
            database._grant_gacha_reward_locked(
                conn, 10, 2,
                {"key": "med_vault", "kind": "vault", "amount": definition.value},
                database.DEFAULT_GACHA_CONFIG, datetime.now().isoformat(),
            )
            pulled = conn.execute(
                "SELECT protected_reserve FROM users WHERE user_id = 2"
            ).fetchone()[0]
        self.assertEqual(bought, definition.value)
        self.assertEqual(pulled, definition.value)


class LegacyLockpickTests(unittest.TestCase):
    """The column-backed lockpick is a finite compatibility path, not a mechanic.

    It was never migrated into inventory because ``users.rob_bonus`` has no guild
    dimension and CLAUDE.md forbids guessing a legacy row's provenance, so it has
    to keep working for whoever already bought one and then drain itself.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "legacy.db")
        database.initialize_database()
        database.register_guild(10, "Legacy Guild")
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, balance, rob_bonus) VALUES (1, 100000, 0.15)"
            )
            conn.execute("INSERT INTO users (user_id, balance) VALUES (2, 100000)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_a_legacy_bonus_still_applies_and_is_then_cleared(self):
        # chance_roll 0.14 succeeds only with the stored +0.15 on top of 0.0.
        result = database.resolve_robbery(
            1, 2, datetime.now().isoformat(), 0.0, 1.0, 0.14, 0.10, 10
        )
        self.assertTrue(result["resolved"])
        self.assertTrue(result["won"])
        self.assertTrue(result["consumed_lockpick"])
        with database.get_connection() as conn:
            remaining = conn.execute(
                "SELECT rob_bonus FROM users WHERE user_id = 1"
            ).fetchone()[0]
        self.assertEqual(remaining, 0.0)

    def test_nothing_writes_the_legacy_column_any_more(self):
        with self.assertRaises(ValueError):
            database.purchase_upgrade(1, 100, "lockpick", 0.15)
        for removed in ("buy_lockpick", "break_lockpick", "get_user_rob_bonus"):
            self.assertFalse(hasattr(database, removed),
                             f"{removed} was removed with the column-backed lockpick")


if __name__ == "__main__":
    unittest.main()
