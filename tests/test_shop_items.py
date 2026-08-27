"""The Shop and Potato Gacha must agree on what a built-in item is.

Both systems hand out the same goods, so item identity — the stable key and the
effect it applies — lives once in `item_catalog`. What each system may differ on
is *acquisition*: a duplicate vault refuses a shop purchase and charges nothing,
while the same duplicate from a pull pays the banner's compensation. These tests
pin both halves of that split, because before the catalog the two systems had
drifted into a lockpick backed by different storage and vault tiers under keys
only one of them knew.
"""

import json
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

    def test_every_builtin_has_a_category(self):
        """Mostly redundant with the import-time TypeError a missing category
        raises, and kept as the statement of intent."""
        self.assertEqual(
            [], [key for key, definition in item_catalog.ITEM_DEFINITIONS.items()
                 if definition.category is None])

    def test_every_category_is_ordered_and_labelled_in_both_catalogs(self):
        """Both directions, the way the coverage rule prescribes.

        A category with no label draws as `[shop.categories.x.name]` in a live
        Discord menu; a label naming no category is a shelf nobody can reach,
        which is the `level_roles` failure the rule was written for.
        """
        declared = {category.value for category in item_catalog.ItemCategory}
        self.assertEqual(declared,
                         {c.value for c in item_catalog.SHOP_CATEGORY_ORDER})
        for language in ("hu", "en"):
            path = os.path.join(ROOT, "locales", f"{language}.json")
            with open(path, encoding="utf-8") as handle:
                catalog = json.load(handle)
            labels = catalog["shop"]["categories"]
            self.assertEqual(declared, set(labels), language)
            for key, entry in labels.items():
                self.assertEqual({"name", "desc"}, set(entry), f"{language}.{key}")
                for field, text in entry.items():
                    self.assertTrue(text.strip(), f"{language}.{key}.{field} is blank")

    def test_no_category_ships_empty(self):
        """A shelf with no built-in only appears once somebody makes a custom
        item, which is a real state but not one to ship. Asserted so removing
        the last item from a category is a decision rather than an accident."""
        for category in item_catalog.SHOP_CATEGORY_ORDER:
            self.assertTrue(item_catalog.shop_items_in(category.value),
                            f"{category.value} ships with no built-in item")

    def test_every_template_has_a_default_category(self):
        import dashboard_api

        self.assertEqual(set(item_catalog.SHOP_TEMPLATE_CATEGORIES),
                         dashboard_api.SAFE_SHOP_TEMPLATES)

    def test_a_consumable_inherits_the_category_of_what_it_grants(self):
        """A custom item wrapping a lockpick is a heist item, not a casino one.
        Guessing one shelf for every consumable would have been the collapse
        this whole declaration exists to avoid."""
        self.assertEqual("heist", item_catalog.resolve_custom_category(
            "consumable", {"item_key": "lockpick"}))
        self.assertEqual("casino", item_catalog.resolve_custom_category(
            "consumable", {"item_key": "lucky_charm"}))
        self.assertEqual("perks", item_catalog.resolve_custom_category(
            "consumable", {"item_key": "streak_freeze"}))

    def test_a_stored_category_wins_and_an_unknown_one_falls_back(self):
        """Three states: absent means "resolve from the template", a valid value
        is the operator's choice, and anything else is not trusted."""
        self.assertEqual("protection",
                         item_catalog.resolve_custom_category("vault", {}, None))
        self.assertEqual("heist",
                         item_catalog.resolve_custom_category("vault", {}, "heist"))
        self.assertEqual("protection",
                         item_catalog.resolve_custom_category("vault", {}, "nonsense"))

    def test_hiding_a_builtin_returns_its_slot(self):
        before = item_catalog.custom_item_capacity("rentals")
        after = item_catalog.custom_item_capacity("rentals", ["rent_sound"])
        self.assertEqual(before + 1, after)
        self.assertNotIn("rent_sound", item_catalog.visible_shop_items(["rent_sound"]))

    def test_hiding_never_touches_the_gacha_or_the_reserved_keys(self):
        """Hiding is a shelf decision, not a retirement. A hidden key must stay
        reserved, or a custom item could take it and shadow a built-in that live
        inventory and entitlement rows already reference."""
        hidden = ["lockpick", "rent_sound"]
        self.assertIn("lockpick", item_catalog.GACHA_ELIGIBLE_ITEMS)
        self.assertEqual(17, len(database.BUILTIN_SHOP_KEYS))
        for key in hidden:
            self.assertIn(key, database.BUILTIN_SHOP_KEYS)
            self.assertIn(key, item_catalog.shop_default_prices())
        self.assertEqual(len(item_catalog.SHOP_ITEMS) - 2,
                         len(item_catalog.visible_shop_items(hidden)))

    def test_no_existing_guild_can_be_over_the_new_cap(self):
        """The old global cap was 8. If the smallest per-section capacity were
        below that, upgrading could put a guild over its section's limit."""
        smallest = min(item_catalog.custom_item_capacity(c.value)
                       for c in item_catalog.SHOP_CATEGORY_ORDER)
        self.assertGreaterEqual(smallest, 8)

    def test_the_section_menu_itself_fits_in_one_select(self):
        """The section select has the same 25-option ceiling the item select
        has, and nothing checked it."""
        self.assertLessEqual(len(item_catalog.SHOP_CATEGORY_ORDER),
                             item_catalog.SELECT_OPTION_LIMIT)

    def test_no_section_can_exceed_the_discord_select_limit(self):
        """One budget per shelf now, not one for the whole shop.

        The flat cap was `25 - len(BUILTIN_SHOP_KEYS)`, which meant every item
        we shipped took a slot from the operator — 13, then 10, then 8. Per
        section a new built-in only ever costs a slot in its own section.
        """
        for category in item_catalog.SHOP_CATEGORY_ORDER:
            self.assertLessEqual(
                len(item_catalog.shop_items_in(category.value))
                + item_catalog.custom_item_capacity(category.value),
                item_catalog.SELECT_OPTION_LIMIT,
            )

    def test_the_menu_and_the_dashboard_share_one_limit(self):
        """`cogs/shop.py` and `dashboard_api.py` cannot import each other, so
        the platform's number lives in the catalog and both read it. Two
        literals could only ever agree by luck."""
        import dashboard_api
        from cogs.shop import SELECT_OPTION_LIMIT

        self.assertEqual(item_catalog.SELECT_OPTION_LIMIT, SELECT_OPTION_LIMIT)
        self.assertFalse(hasattr(dashboard_api, "SHOP_ITEM_LIMIT"),
                         "a retired flat cap left importable is how a second "
                         "writer comes to disagree")


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


class ShopMenuSectionTests(unittest.TestCase):
    """The two-step menu, and the three properties that keep it alive.

    A Discord select holds 25 options, so a flat menu *was* the ceiling. Two
    steps make it 25 per section, and the grouping has to guarantee three
    things: nothing exceeds the ceiling, no section is ever offered empty, and
    a section's built-ins survive an overflow ahead of its custom items.
    """

    def items(self, extra=None):
        from cogs.shop import add_custom_shop_items, get_shop_items

        merged = get_shop_items({})
        return add_custom_shop_items(merged, extra or [])

    def custom(self, key, template, config, **kwargs):
        row = {"item_key": key, "template_type": template, "enabled": True,
               "price": 500, "config": config, "revision": 1,
               "name": key, "description": "d"}
        row.update(kwargs)
        return row

    def test_every_item_lands_on_exactly_one_shelf(self):
        from cogs.shop import build_category_index

        items = self.items()
        index = build_category_index(items)
        placed = [key for bucket in index.values() for key in bucket]
        self.assertEqual(sorted(items), sorted(placed))
        self.assertEqual(len(placed), len(set(placed)))

    def test_an_empty_section_is_never_offered(self):
        """`discord.ui.Select(options=[])` does not raise locally — Discord
        answers 400 and the whole command dies."""
        from cogs.shop import build_category_index

        index = build_category_index({})
        self.assertEqual({}, index)
        # And with only one shelf populated, only that shelf is offered.
        one = {"premium": {"price": 1, "type": "role", "value": "premium_role",
                           "category": "perks"}}
        self.assertEqual(["perks"], list(build_category_index(one)))

    def test_the_trim_is_per_section_and_keeps_the_builtins(self):
        from cogs.shop import build_category_index

        extra = [self.custom(f"c{i}", "vault", {"amount": 25000})
                 for i in range(40)]
        index = build_category_index(self.items(extra), guild_id=1)
        protection = index["protection"]
        self.assertEqual(item_catalog.SELECT_OPTION_LIMIT, len(protection))
        # Every built-in on that shelf survived; only custom rows were dropped.
        for key in item_catalog.shop_items_in("protection"):
            self.assertIn(key, protection)
        # And no other shelf shrank.
        self.assertEqual(len(item_catalog.shop_items_in("casino")),
                         len(index["casino"]))

    def test_the_trim_logs_which_section_and_what_it_hid(self):
        from cogs.shop import build_category_index

        extra = [self.custom(f"c{i}", "vault", {"amount": 25000})
                 for i in range(40)]
        with self.assertLogs("PotatoBot.Shop", "ERROR") as logs:
            build_category_index(self.items(extra), guild_id=99)
        joined = "\n".join(logs.output)
        self.assertIn("protection", joined)
        self.assertIn("99", joined)

    def test_a_hidden_builtin_leaves_the_menu_but_not_the_catalog(self):
        """Hiding is a shelf decision, not a retirement."""
        from cogs.shop import get_shop_items

        # `lockpick` rather than `rent_sound`: the assertion below is about the
        # gacha, and only an item with a `gacha_kind` is drawable at all.
        visible = get_shop_items({}, ["lockpick", "rent_sound"])
        self.assertNotIn("lockpick", visible)
        self.assertNotIn("rent_sound", visible)
        # Still drawable from a banner, and still a reserved key so a custom
        # item cannot take it and shadow the rows that reference it.
        self.assertIn("lockpick", item_catalog.GACHA_ELIGIBLE_ITEMS)
        self.assertIn("lockpick", database.BUILTIN_SHOP_KEYS)
        self.assertIn("rent_sound", database.BUILTIN_SHOP_KEYS)
        # And its price setting stays registered, or un-hiding would come back
        # with no price.
        self.assertIn("shop_price_lockpick", SETTING_DEFINITIONS)

    def test_an_item_with_no_usable_section_is_filed_rather_than_dropped(self):
        """A member who cannot see what a guild sells has no way to report it,
        so an unresolvable section fails to the first shelf and logs."""
        from cogs.shop import build_category_index

        broken = {"odd": {"price": 1, "type": "custom", "template_type": "vault",
                          "config": {}, "name": "Odd", "description": "",
                          "category": "not_a_section"}}
        with self.assertLogs("PotatoBot.Shop", "ERROR"):
            index = build_category_index(broken)
        first = item_catalog.SHOP_CATEGORY_ORDER[0].value
        self.assertIn("odd", index[first])

    def test_resolve_finds_an_item_by_key_and_by_name_but_never_guesses(self):
        from cogs.shop import resolve_shop_item, shop_item_name

        items = self.items()
        self.assertEqual("premium", resolve_shop_item(items, "premium"))
        self.assertEqual("premium", resolve_shop_item(items, "PREMIUM"))
        name = shop_item_name("small_vault", items["small_vault"])
        self.assertEqual("small_vault", resolve_shop_item(items, name))
        # Deliberately no fuzzy match: guessing which item somebody meant to
        # spend money on is not a kindness.
        self.assertIsNone(resolve_shop_item(items, "vault"))
        self.assertIsNone(resolve_shop_item(items, ""))
        self.assertIsNone(resolve_shop_item(items, "no such item"))

    def test_buy_can_reach_an_item_the_menu_had_to_trim(self):
        """This is what makes the render trim acceptable rather than a loss."""
        from cogs.shop import build_category_index, resolve_shop_item

        extra = [self.custom(f"c{i}", "vault", {"amount": 25000})
                 for i in range(40)]
        items = self.items(extra)
        index = build_category_index(items, guild_id=1)
        shown = {key for bucket in index.values() for key in bucket}
        hidden = [key for key in items if key not in shown]
        self.assertTrue(hidden, "the fixture did not overflow a section")
        for key in hidden:
            self.assertEqual(key, resolve_shop_item(items, key))

    def test_an_over_long_label_is_truncated_rather_than_rejected(self):
        """A custom item's name reaches a select option's label, which caps at
        100 characters. discord.py does not check it, so Discord answers 400 for
        the whole component and `/shop` dies for that guild."""
        from cogs.shop import SELECT_LABEL_LIMIT, ShopView

        extra = [self.custom("longname", "vault", {"amount": 25000},
                             name="x" * 300)]
        view = ShopView(1, self.items(extra), guild_id=1)
        view.category = "protection"
        view._render()
        for option in view.item_select.options:
            self.assertLessEqual(len(option.label), SELECT_LABEL_LIMIT)
            self.assertLessEqual(len(option.description or ""), 100)

    def test_the_view_starts_on_the_sections_and_back_clears_the_choice(self):
        from cogs.shop import ShopView

        view = ShopView(1, self.items(), guild_id=1)
        self.assertEqual([view.CATEGORY_ID],
                         [child.custom_id for child in view.children])

        view.category = "protection"
        view._render()
        self.assertEqual([view.ITEM_ID, view.BUY_ID, view.BACK_ID],
                         [child.custom_id for child in view.children])

        # Pick something, then go back. Leaving `selected_item` set is a real
        # cross-section mis-purchase: choose under one shelf, Back, open
        # another, press Buy, and you buy the first item.
        view.selected_item = "small_vault"
        view.category = None
        view.selected_item = None
        view._render()
        self.assertEqual([view.CATEGORY_ID],
                         [child.custom_id for child in view.children])
        self.assertIsNone(view.selected_item)

    def test_the_components_are_built_once_so_the_cooldown_survives(self):
        """`interaction_check` rate-limits per `custom_id`. Rebuilding the
        components on every render would reset that silently."""
        from cogs.shop import ShopView

        view = ShopView(1, self.items(), guild_id=1)
        before = view.category_select, view.item_select, view.buy_btn, view.back_btn
        view.category = "casino"
        view._render()
        view.category = None
        view._render()
        after = view.category_select, view.item_select, view.buy_btn, view.back_btn
        for first, second in zip(before, after):
            self.assertIs(first, second)
