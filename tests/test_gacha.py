import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

import database


class QueueRng:
    def __init__(self, *points):
        self.points = list(points)

    def randrange(self, stop):
        point = self.points.pop(0) if self.points else 0
        return min(point, stop - 1)


class GachaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "gacha.db")
        database.initialize_database()
        database.register_guild(10, "Gacha Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 1000000)")
            conn.execute("INSERT INTO users (user_id, balance) VALUES (2, 100000)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_default_banner_matches_approved_rates(self):
        banner = database.get_gacha_banner(10)
        self.assertEqual(banner["config"]["tiers"], {"3": 97800, "4": 1600, "5": 600})
        self.assertEqual(banner["config"]["soft_pity_start"], 75)
        self.assertEqual(banner["config"]["soft_pity_multiplier"], 3)
        self.assertEqual(banner["config"]["four_star_guarantee_interval"], 10)
        self.assertEqual(banner["config"]["hard_pity"], 100)

    def test_soft_pity_triples_rare_tiers_after_pull_75(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity "
                "(guild_id, user_id, banner_key, pulls_since_five_star, "
                "pulls_toward_four_star, updated_at) VALUES (10, 1, 'standard', 75, 0, ?)",
                (datetime.now().isoformat(),),
            )
        result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(94000, 0))
        self.assertTrue(result["results"][0]["soft_pity"])
        self.assertEqual(result["results"][0]["rarity"], 4)

    def test_hard_pity_guarantees_five_star_and_resets(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity "
                "(guild_id, user_id, banner_key, pulls_since_five_star, "
                "pulls_toward_four_star, updated_at) VALUES (10, 1, 'standard', 99, 0, ?)",
                (datetime.now().isoformat(),),
            )
        result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(0))
        self.assertTrue(result["results"][0]["hard_pity"])
        self.assertEqual(result["results"][0]["rarity"], 5)
        self.assertEqual(result["pity"], 0)

    def test_ten_pull_is_all_or_nothing(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET balance = 49999 WHERE user_id = 1")
        result = database.perform_gacha_pulls(10, 1, 10, rng=QueueRng())
        self.assertFalse(result["purchased"])
        self.assertEqual(database.get_user_balance(1), 49999)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM gacha_pulls").fetchone()[0], 0)

    def test_duplicate_vault_grants_configured_compensation(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET protected_reserve = 500000 WHERE user_id = 1")
            conn.execute(
                "INSERT INTO gacha_pity "
                "(guild_id, user_id, banner_key, pulls_since_five_star, "
                "pulls_toward_four_star, updated_at) VALUES (10, 1, 'standard', 99, 0, ?)",
                (datetime.now().isoformat(),),
            )
        before = database.get_user_balance(1)
        result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(0))
        reward = result["results"][0]
        # The banner awards vaults under the shop's item keys, so the same key
        # always means the same protected reserve whichever system granted it.
        self.assertEqual(reward["key"], "big_vault")
        self.assertEqual(reward["duplicate_compensation"], 50000)
        self.assertEqual(database.get_user_balance(1), before - 5000 + 50000)

    def test_glove_exposes_one_quarter_of_protected_reserve_and_is_consumed(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE users SET protected_reserve = 100000 WHERE user_id = 2")
            conn.execute(
                "INSERT INTO user_inventory VALUES (10, 1, 'vault_glove', 1, ?)",
                (datetime.now().isoformat(),),
            )
        result = database.resolve_robbery(
            1, 2, datetime.now().isoformat(), 1.0, 1.0, 0.0, 0.10, 10
        )
        self.assertEqual(result["amount"], 2500)
        self.assertTrue(result["consumed_glove"])
        self.assertEqual(database.get_user_inventory(10, 1), {})

    def test_every_tenth_pull_is_four_star_or_higher(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity "
                "(guild_id, user_id, banner_key, pulls_since_five_star, "
                "pulls_toward_four_star, updated_at) VALUES (10, 1, 'standard', 0, 9, ?)",
                (datetime.now().isoformat(),),
            )
        result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(0, 0))
        reward = result["results"][0]
        self.assertEqual(reward["rarity"], 4)
        self.assertTrue(reward["four_star_guarantee"])
        self.assertEqual(result["four_star_counter"], 0)

    def test_tenth_pull_never_downgrades_a_five_star(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity "
                "(guild_id, user_id, banner_key, pulls_since_five_star, "
                "pulls_toward_four_star, updated_at) VALUES (10, 1, 'standard', 0, 9, ?)",
                (datetime.now().isoformat(),),
            )
        result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(99999, 0))
        self.assertEqual(result["results"][0]["rarity"], 5)
        self.assertTrue(result["results"][0]["four_star_guarantee"])

    def test_loaded_die_is_consumed_only_by_valid_paid_dice_game(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO user_inventory VALUES (10, 1, 'loaded_die', 1, ?)",
                (datetime.now().isoformat(),),
            )
        self.assertIsNone(database.resolve_dice_wager(10, 1, 2000000, 2, 6, 5))
        self.assertEqual(database.get_user_inventory(10, 1)["loaded_die"], 1)
        result = database.resolve_dice_wager(10, 1, 100, 2, 6, 5)
        self.assertTrue(result["loaded_die"])
        self.assertEqual(result["player_roll"], 6)
        self.assertEqual(result["outcome"], "win")
        self.assertEqual(database.get_user_inventory(10, 1), {})

    def test_asset_voucher_timer_starts_at_fulfillment(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO reward_vouchers "
                "(voucher_id, guild_id, user_id, reward_key, duration_days, acquired_at) "
                "VALUES ('voucher', 10, 1, 'sticker_30d', 30, ?)",
                (datetime.now().isoformat(),),
            )
        redeemed = database.redeem_voucher(10, 1, "voucher")
        self.assertEqual(redeemed["kind"], "fulfillment")
        with database.get_connection() as conn:
            self.assertIsNone(conn.execute(
                "SELECT expires_at FROM reward_vouchers WHERE voucher_id = 'voucher'"
            ).fetchone()[0])
        fulfilled = database.fulfill_voucher_request(
            10, redeemed["request_id"], 999, "123456"
        )
        self.assertTrue(fulfilled["fulfilled"])
        self.assertIsNotNone(fulfilled["expires_at"])
        with database.get_connection() as conn:
            source_type = conn.execute(
                "SELECT source_type FROM reward_vouchers WHERE voucher_id = 'voucher'"
            ).fetchone()[0]
        self.assertEqual(source_type, "gacha")

    def test_premium_extension_can_be_rolled_back_without_losing_prior_time(self):
        acquired = datetime.now().isoformat()
        with database.get_connection() as conn:
            conn.executemany(
                "INSERT INTO reward_vouchers "
                "(voucher_id, guild_id, user_id, reward_key, duration_days, acquired_at) "
                "VALUES (?, 10, 1, 'premium_30d', 30, ?)",
                [("premium-a", acquired), ("premium-b", acquired)],
            )
        first = database.redeem_voucher(10, 1, "premium-a")
        second = database.redeem_voucher(10, 1, "premium-b")
        self.assertGreater(second["expires_at"], first["expires_at"])
        self.assertTrue(database.rollback_premium_redemption(10, 1, "premium-b"))
        with database.get_connection() as conn:
            expiry = conn.execute(
                "SELECT expires_at FROM timed_entitlements WHERE guild_id = 10 "
                "AND user_id = 1 AND entitlement_key = 'premium'"
            ).fetchone()[0]
            status = conn.execute(
                "SELECT status FROM reward_vouchers WHERE voucher_id = 'premium-b'"
            ).fetchone()[0]
        self.assertEqual(expiry, first["expires_at"])
        self.assertEqual(status, "available")

    def test_safe_custom_consumable_purchase_uses_inventory(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO shop_item_definitions "
                "(guild_id, item_key, template_type, enabled, price, config_json, updated_at) "
                "VALUES (10, 'glove_shop', 'consumable', 1, 1000, "
                "'{\"item_key\": \"vault_glove\"}', ?)",
                (datetime.now().isoformat(),),
            )
        result = database.purchase_custom_shop_item(10, 1, "glove_shop")
        self.assertTrue(result["purchased"])
        self.assertEqual(database.get_user_inventory(10, 1)["vault_glove"], 1)

    def test_custom_shop_asset_voucher_keeps_shop_ownership(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO shop_item_definitions "
                "(guild_id, item_key, template_type, enabled, price, config_json, updated_at) "
                "VALUES (10, 'custom_emoji', 'fulfillment_voucher', 1, 1000, "
                "'{\"asset_type\": \"emoji\", \"duration_days\": 30}', ?)",
                (datetime.now().isoformat(),),
            )
        purchased = database.purchase_custom_shop_item(10, 1, "custom_emoji")
        self.assertTrue(purchased["purchased"])
        with database.get_connection() as conn:
            source_type = conn.execute(
                "SELECT source_type FROM reward_vouchers WHERE voucher_id = ?",
                (purchased["voucher_id"],),
            ).fetchone()[0]
        self.assertEqual(source_type, "shop")

    def test_banner_rejects_soft_pity_expansion_over_one_hundred_percent(self):
        config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        # Valid base weights, but tripling both rare tiers cannot fit in 100%.
        config_value["tiers"] = {"3": 20000, "4": 40000, "5": 40000}
        with self.assertRaises(ValueError):
            database.set_gacha_banner(10, 999, True, config_value, 0)

    def test_banner_accepts_the_largest_expansion_that_still_fits(self):
        config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        multiplier = config_value["soft_pity_multiplier"]
        rare_total = 100000 // multiplier
        config_value["tiers"] = {
            "3": 100000 - rare_total, "4": rare_total - 1, "5": 1,
        }
        saved = database.set_gacha_banner(10, 999, True, config_value, 0)
        self.assertEqual(saved["config"]["tiers"]["5"], 1)

    def test_soft_pity_boundary_pull_settles_with_the_saved_banner(self):
        config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        # Rare total 33333, which triples to exactly 99999 under soft pity.
        config_value["tiers"] = {"3": 66667, "4": 33332, "5": 1}
        database.set_gacha_banner(10, 999, True, config_value, 0)
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity "
                "(guild_id, user_id, banner_key, pulls_since_five_star, "
                "pulls_toward_four_star, updated_at) VALUES (10, 1, 'standard', 75, 0, ?)",
                (datetime.now().isoformat(),),
            )
        result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(0, 0))
        self.assertTrue(result["purchased"])
        self.assertTrue(result["results"][0]["soft_pity"])

    def test_custom_role_rollback_refunds_the_amount_actually_charged(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO shop_item_definitions "
                "(guild_id, item_key, template_type, enabled, price, config_json, updated_at) "
                "VALUES (10, 'vip_role', 'fixed_role', 1, 1000, "
                "'{\"role_id\": 555}', ?)",
                (datetime.now().isoformat(),),
            )
        before = database.get_user_balance(1)
        purchase = database.purchase_custom_shop_item(10, 1, "vip_role")
        self.assertEqual(purchase["price"], 1000)
        self.assertEqual(database.get_user_balance(1), before - 1000)

        # An administrator raising the price between debit and rollback must not
        # change what the compensating refund returns.
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE shop_item_definitions SET price = 9000 "
                "WHERE guild_id = 10 AND item_key = 'vip_role'"
            )
        database.rollback_custom_role_purchase(
            10, 1, purchase["price"], purchase["template_type"], 555,
            purchase["config"].get("duration_days"),
        )
        self.assertEqual(database.get_user_balance(1), before)

    def test_timed_role_rollback_restores_the_previous_expiry(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO shop_item_definitions "
                "(guild_id, item_key, template_type, enabled, price, config_json, updated_at) "
                "VALUES (10, 'timed_vip', 'timed_role', 1, 1000, "
                "'{\"role_id\": 777, \"duration_days\": 30}', ?)",
                (datetime.now().isoformat(),),
            )
        first = database.purchase_custom_shop_item(10, 1, "timed_vip")
        second = database.purchase_custom_shop_item(10, 1, "timed_vip")
        self.assertEqual(second["template_type"], "timed_role")
        database.rollback_custom_role_purchase(
            10, 1, second["price"], second["template_type"], 777,
            second["config"]["duration_days"],
        )
        with database.get_connection() as conn:
            expires_at = conn.execute(
                "SELECT expires_at FROM timed_entitlements WHERE guild_id = 10 "
                "AND user_id = 1 AND entitlement_key = 'role:777' AND status = 'active'"
            ).fetchone()[0]
        self.assertEqual(expires_at, first["expires_at"])

    def test_a_slow_action_keeps_its_lease_and_is_not_posted_twice(self):
        """Re-queueing on elapsed time alone made a slow multi-section publish
        run a second time, after its Discord sends had already happened."""
        action_id = database.queue_control_action(10, 999, "publish_managed", {})
        claimed = database.claim_control_action()
        self.assertEqual(claimed["action_id"], action_id)

        # Simulate a worker that has been running longer than the old five-minute
        # window but is still alive and renewing its lease.
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE control_actions SET started_at = '2000-01-01T00:00:00+00:00' "
                "WHERE action_id = ?", (action_id,),
            )
        database.renew_control_action_lease(action_id)
        self.assertIsNone(database.claim_control_action())

        # Once the lease lapses the action becomes recoverable again.
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE control_actions SET lease_expires_at = '2000-01-01T00:00:00+00:00' "
                "WHERE action_id = ?", (action_id,),
            )
        recovered = database.claim_control_action()
        self.assertEqual(recovered["action_id"], action_id)

    def test_ticket_claimer_survives_a_restart(self):
        database.add_ticket(500, 20, 10, "support")
        self.assertIsNone(database.get_ticket_claimer(500))
        database.set_ticket_claimer(500, 77)
        self.assertEqual(database.get_ticket_claimer(500), 77)
        # A second ticket must not inherit the first one's claimer.
        database.add_ticket(501, 21, 10, "support")
        self.assertIsNone(database.get_ticket_claimer(501))

    def test_disabled_reward_is_never_drawn_but_keeps_its_weight(self):
        config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        # Disable everything in the 3-star pool except the lockpick, which has a
        # small weight; a disabled row must not be reachable regardless.
        for entry in config_value["rewards"]["3"]:
            entry["enabled"] = entry["key"] == "lockpick"
        database.set_gacha_banner(10, 999, True, config_value, 0)

        drawn = set()
        for _ in range(30):
            result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(0, 0))
            outcome = result["results"][0]
            if outcome["rarity"] == 3:
                drawn.add(outcome["key"])
        self.assertEqual(drawn, {"lockpick"})

        # The disabled rows are still stored, so re-enabling restores them.
        stored = database.get_gacha_banner(10)["config"]["rewards"]["3"]
        self.assertEqual(len(stored), len(database.DEFAULT_GACHA_CONFIG["rewards"]["3"]))

    def test_a_tier_cannot_have_every_reward_disabled(self):
        config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        for entry in config_value["rewards"]["5"]:
            entry["enabled"] = False
        with self.assertRaises(database.ValidationError) as caught:
            database.set_gacha_banner(10, 999, True, config_value, 0)
        self.assertEqual(caught.exception.reason, "gacha_tier_all_disabled")

    def test_banners_without_the_enabled_flag_stay_valid(self):
        """Older stored banners predate the flag; absent means enabled."""
        config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        for tier in config_value["rewards"].values():
            for entry in tier:
                self.assertNotIn("enabled", entry)
        saved = database.set_gacha_banner(10, 999, True, config_value, 0)
        self.assertTrue(saved["enabled"])
        result = database.perform_gacha_pulls(10, 1, 1, rng=QueueRng(0, 0))
        self.assertTrue(result["purchased"])

    def test_reward_enabled_flag_must_be_boolean(self):
        config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        config_value["rewards"]["3"][0]["enabled"] = "yes"
        with self.assertRaises(database.ValidationError) as caught:
            database.set_gacha_banner(10, 999, True, config_value, 0)
        self.assertEqual(caught.exception.reason, "gacha_reward_enabled")

    def test_control_action_is_claimed_and_completed_once(self):
        action_id = database.queue_control_action(
            10, 999, "publish_managed",
            {"kind": "embed", "menu_key": "notice", "channel_id": 2}
        )
        claimed = database.claim_control_action()
        self.assertEqual(claimed["action_id"], action_id)
        self.assertIsNone(database.claim_control_action())
        database.finish_control_action(action_id, True)
        with database.get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM control_actions WHERE action_id = ?", (action_id,)
            ).fetchone()[0]
        self.assertEqual(status, "completed")


class GachaBannerTests(unittest.TestCase):
    """A guild may run several banners. The key stays the stable identifier that
    pull history and pity reference; the name is only what an operator reads."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "banners.db")
        database.initialize_database()
        database.register_guild(10, "Gacha Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 1000000)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_a_guild_with_no_stored_banner_still_lists_the_default(self):
        banners = database.list_gacha_banners(10)
        self.assertEqual(1, len(banners))
        self.assertTrue(banners[0]["is_default"])
        self.assertEqual(database.DEFAULT_GACHA_BANNER_KEY, banners[0]["banner_key"])
        self.assertEqual(0, banners[0]["revision"])

    def test_created_banner_starts_disabled_and_lists_default_first(self):
        created = database.create_gacha_banner(
            10, 99, "summer", "Summer Banner",
            json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
        )
        self.assertFalse(created["enabled"])
        keys = [banner["banner_key"] for banner in database.list_gacha_banners(10)]
        self.assertEqual([database.DEFAULT_GACHA_BANNER_KEY, "summer"], keys)

    def test_a_duplicate_key_and_an_invalid_key_are_both_refused(self):
        database.create_gacha_banner(
            10, 99, "summer", "Summer", dict(database.DEFAULT_GACHA_CONFIG)
        )
        with self.assertRaises(database.ValidationError) as duplicate:
            database.create_gacha_banner(
                10, 99, "summer", "Again", dict(database.DEFAULT_GACHA_CONFIG)
            )
        self.assertEqual("gacha_banner_exists", duplicate.exception.reason)
        with self.assertRaises(database.ValidationError) as bad_key:
            database.create_gacha_banner(
                10, 99, "Summer Banner!", "Name",
                dict(database.DEFAULT_GACHA_CONFIG),
            )
        self.assertEqual("gacha_banner_key_invalid", bad_key.exception.reason)
        with self.assertRaises(database.ValidationError) as blank:
            database.create_gacha_banner(
                10, 99, "winter", "   ", dict(database.DEFAULT_GACHA_CONFIG)
            )
        self.assertEqual("gacha_banner_name_invalid", blank.exception.reason)

    def test_pulling_an_unknown_banner_creates_nothing_and_charges_nothing(self):
        """The banner argument is user supplied, so a typo must not conjure a
        default-priced banner the operator never configured."""
        result = database.perform_gacha_pulls(10, 1, 1, "does_not_exist")
        self.assertFalse(result["purchased"])
        self.assertEqual("banner_unknown", result["reason"])
        with database.get_connection() as conn:
            self.assertEqual(
                0,
                conn.execute("SELECT COUNT(*) FROM gacha_banners").fetchone()[0],
            )
            self.assertEqual(
                1000000,
                conn.execute(
                    "SELECT balance FROM users WHERE user_id = 1"
                ).fetchone()[0],
            )

    def test_saving_an_unknown_banner_is_refused_rather_than_creating_it(self):
        with self.assertRaises(database.ValidationError) as error:
            database.set_gacha_banner(
                10, 99, True, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
                0, banner_key="typo",
            )
        self.assertEqual("gacha_banner_unknown", error.exception.reason)

    def test_saving_rewards_does_not_silently_rename_the_banner(self):
        database.create_gacha_banner(
            10, 99, "summer", "Summer", dict(database.DEFAULT_GACHA_CONFIG)
        )
        saved = database.set_gacha_banner(
            10, 99, True, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
            1, banner_key="summer",
        )
        self.assertEqual("Summer", saved["display_name"])
        renamed = database.set_gacha_banner(
            10, 99, True, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
            2, banner_key="summer", display_name="Summer Rerun",
        )
        self.assertEqual("Summer Rerun", renamed["display_name"])

    def test_pity_is_tracked_per_banner(self):
        database.create_gacha_banner(
            10, 99, "summer", "Summer", dict(database.DEFAULT_GACHA_CONFIG),
            enabled=True,
        )
        database.perform_gacha_pulls(10, 1, 1, "summer")
        database.perform_gacha_pulls(10, 1, 1)
        with database.get_connection() as conn:
            rows = dict(conn.execute(
                "SELECT banner_key, pulls_toward_four_star FROM gacha_pity "
                "WHERE guild_id = 10 AND user_id = 1"
            ).fetchall())
        self.assertEqual({"summer": 1, database.DEFAULT_GACHA_BANNER_KEY: 1}, rows)

    def test_the_default_banner_cannot_be_deleted(self):
        with self.assertRaises(database.ValidationError) as error:
            database.delete_gacha_banner(
                10, 99, database.DEFAULT_GACHA_BANNER_KEY, 0
            )
        self.assertEqual("gacha_banner_default_undeletable", error.exception.reason)

    def test_deleting_a_banner_keeps_its_immutable_pull_history(self):
        database.create_gacha_banner(
            10, 99, "summer", "Summer", dict(database.DEFAULT_GACHA_CONFIG),
            enabled=True,
        )
        database.perform_gacha_pulls(10, 1, 1, "summer")
        with self.assertRaises(database.RevisionConflictError):
            database.delete_gacha_banner(10, 99, "summer", 99)
        database.delete_gacha_banner(10, 99, "summer", 1)
        with database.get_connection() as conn:
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM gacha_pulls WHERE banner_key = 'summer'"
                ).fetchone()[0],
            )
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM gacha_banners WHERE banner_key = 'summer'"
            ).fetchone())
        actions = {row["action"] for row in database.get_settings_audit(10)}
        self.assertIn("gacha.create", actions)
        self.assertIn("gacha.delete", actions)

    def test_a_pre_schema_nine_banner_renders_as_its_key(self):
        """An existing banner has no name, so the key has to stand in for it."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_banners (guild_id, banner_key, display_name, "
                "enabled, config_json, revision, updated_at) "
                "VALUES (10, 'legacy', NULL, 1, ?, 1, '2026-01-01T00:00:00+00:00')",
                (json.dumps(database.DEFAULT_GACHA_CONFIG, sort_keys=True),),
            )
            conn.commit()
        banner = database.get_gacha_banner(10, "legacy")
        self.assertEqual("legacy", banner["display_name"])


if __name__ == "__main__":
    unittest.main()


class BannerRewardReconciliationTests(unittest.TestCase):
    """A stored banner is frozen at the shipped rewards of the day it was saved.

    Nothing ever reconciled the two, so a reward added to `DEFAULT_GACHA_CONFIG`
    could never reach a banner already in use — which is how `streak_freeze`
    reached the live installation's shop and its shipped 4-star tier while being
    unobtainable from the banner that guild actually pulls on.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "gacha.db")
        database.initialize_database()
        database.register_guild(42, "Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _saved_without(self, key):
        config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        for tier, entries in config["rewards"].items():
            config["rewards"][tier] = [e for e in entries if e["key"] != key]
        standard = next(b for b in database.list_gacha_banners(42)
                        if b["banner_key"] == "standard")
        database.set_gacha_banner(42, 7, True, config, standard["revision"],
                                  "standard")
        return database.get_gacha_banner(42, "standard")

    def test_a_reward_absent_from_a_stored_banner_is_reported(self):
        banner = self._saved_without("streak_freeze")
        missing = database.missing_shipped_rewards(banner["config"])
        found = [entry["key"] for entries in missing.values() for entry in entries]
        self.assertEqual(["streak_freeze"], found)

    def test_a_banner_holding_everything_reports_nothing(self):
        self.assertEqual(
            {}, database.missing_shipped_rewards(
                {"rewards": database.shipped_reward_table()}))

    def test_adding_the_missing_rewards_produces_a_valid_config(self):
        """The reconciliation has to survive the validator, or it is useless."""
        banner = self._saved_without("streak_freeze")
        config = banner["config"]
        for tier, entries in database.missing_shipped_rewards(config).items():
            config["rewards"][tier].extend(entries)
        # Saving it is the real test: the validator rejects a duplicate key, and
        # a vault whose amount disagrees with the catalog.
        database.set_gacha_banner(42, 7, True, config, banner["revision"],
                                  "standard")
        after = database.get_gacha_banner(42, "standard")
        self.assertEqual({}, database.missing_shipped_rewards(after["config"]))

    def test_it_matches_on_the_key_alone(self):
        """A key is what a pull row records, so two entries with one key would
        double its odds and make the displayed chance a lie."""
        banner = self._saved_without("streak_freeze")
        config = banner["config"]
        # Present under the same key but with a different weight: still present.
        config["rewards"]["4"].append(
            {"key": "streak_freeze", "kind": "item", "amount": 1, "weight": 99})
        self.assertEqual({}, database.missing_shipped_rewards(config))

    def test_a_new_banner_starts_with_one_placeholder_per_tier(self):
        """Copying eighteen shipped rewards means the first thing an operator
        does with a new banner is prune it. It cannot be literally empty, because
        a tier can still be rolled and must have something to award."""
        config = database.new_banner_config()
        for tier in ("3", "4", "5"):
            with self.subTest(tier=tier):
                self.assertEqual(1, len(config["rewards"][tier]))
        # The scalars are still the shipped starting points.
        self.assertEqual(database.DEFAULT_GACHA_CONFIG["cost"], config["cost"])
        self.assertEqual(database.DEFAULT_GACHA_CONFIG["tiers"], config["tiers"])
        # And it validates, so a new banner is saveable straight away.
        database.create_gacha_banner(42, 7, "summer", "Summer", config)
        created = database.get_gacha_banner(42, "summer")
        self.assertFalse(created["enabled"], "a new banner starts disabled")
