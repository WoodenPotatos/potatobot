import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

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


class FeaturedSplitTests(unittest.TestCase):
    """The 50/50: a rare pull is either the banner's featured reward or one from
    the standard banner's pool, and losing guarantees the next one of that tier.

    Every property here is about *identity*. The tier is decided before any of
    this runs, so soft pity, hard pity and the tenth-pull floor are untouched —
    ``test_a_banner_without_a_featured_reward_consumes_no_extra_randomness`` is
    what keeps that honest, because the whole existing suite drives a queue RNG
    whose points are consumed in call order.
    """

    FEATURED_KEY = "premium_30d"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "gacha.db")
        database.initialize_database()
        database.register_guild(10, "Gacha Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 100000000)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    # -- fixtures ------------------------------------------------------------

    def _event_config(self, split=50, featured_four=False):
        config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        config["featured_split"] = split
        for entry in config["rewards"]["5"]:
            if entry["key"] == self.FEATURED_KEY:
                entry["featured"] = True
        if featured_four:
            config["rewards"]["4"][0]["featured"] = True
        return config

    def _make_event_banner(self, split=50, featured_four=False, standard=True):
        if standard:
            database.set_gacha_banner(
                10, 1, True, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
                0, banner_key="standard")
        database.create_gacha_banner(
            10, 1, "event", "Event", self._event_config(split, featured_four),
            enabled=True)

    def _pull_five_star(self, rng_points, banner_key="event"):
        """Force one 5-star through hard pity, which consumes no tier RNG."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity (guild_id, user_id, banner_key, "
                "pulls_since_five_star, pulls_toward_four_star, updated_at) "
                "VALUES (10, 1, ?, 99, 0, ?) "
                "ON CONFLICT(guild_id, user_id, banner_key) DO UPDATE SET "
                "pulls_since_five_star = 99",
                (banner_key, datetime.now().isoformat()),
            )
        return database.perform_gacha_pulls(
            10, 1, 1, banner_key=banner_key, rng=QueueRng(*rng_points))

    def _guarantees(self, banner_key="event"):
        with database.get_connection() as conn:
            return conn.execute(
                "SELECT guaranteed_featured_five, guaranteed_featured_four "
                "FROM gacha_pity WHERE guild_id = 10 AND user_id = 1 "
                "AND banner_key = ?", (banner_key,)
            ).fetchone()

    # -- winning and losing --------------------------------------------------

    def test_winning_the_split_awards_the_featured_reward(self):
        self._make_event_banner()
        # First draw is the split roll; 0 < 50 wins.
        result = self._pull_five_star([0])
        reward = result["results"][0]
        self.assertEqual(self.FEATURED_KEY, reward["key"])
        self.assertTrue(reward["featured"])
        self.assertFalse(reward["featured_guaranteed"])
        self.assertEqual((0, 0), self._guarantees())

    def test_losing_the_split_draws_the_standard_pool_and_sets_the_guarantee(self):
        self._make_event_banner()
        # 99 >= 50 loses; the second point picks from the standard 5-star pool.
        result = self._pull_five_star([99, 0])
        reward = result["results"][0]
        self.assertNotEqual(self.FEATURED_KEY, reward["key"])
        self.assertFalse(reward["featured"])
        self.assertTrue(reward["guarantee_held"])
        self.assertEqual(1, self._guarantees()[0])

    def test_a_held_guarantee_awards_the_featured_reward_and_is_spent(self):
        self._make_event_banner()
        self._pull_five_star([99, 0])
        self.assertEqual(1, self._guarantees()[0])
        # No split roll is consumed on a guaranteed pull.
        result = self._pull_five_star([])
        reward = result["results"][0]
        self.assertEqual(self.FEATURED_KEY, reward["key"])
        self.assertTrue(reward["featured"])
        self.assertTrue(reward["featured_guaranteed"])
        self.assertEqual(0, self._guarantees()[0], "the guarantee must be spent")

    def test_the_guarantee_is_recorded_on_the_pull_row(self):
        self._make_event_banner()
        self._pull_five_star([99, 0])
        self._pull_five_star([])
        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT featured, featured_guaranteed FROM gacha_pulls "
                "WHERE banner_key = 'event' ORDER BY pull_id"
            ).fetchall()
        self.assertEqual([(0, 0), (1, 1)], rows)

    # -- the two tiers are independent ---------------------------------------

    def test_losing_the_five_star_split_does_not_arm_the_four_star_guarantee(self):
        self._make_event_banner(featured_four=True)
        self._pull_five_star([99, 0])
        five, four = self._guarantees()
        self.assertEqual(1, five)
        self.assertEqual(0, four, "the tiers guarantee independently")

    def test_the_four_star_tier_splits_on_its_own_counter(self):
        self._make_event_banner(featured_four=True)
        featured_four_key = self._event_config(featured_four=True)["rewards"]["4"][0]["key"]
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity (guild_id, user_id, banner_key, "
                "pulls_since_five_star, pulls_toward_four_star, updated_at) "
                "VALUES (10, 1, 'event', 0, 9, ?)", (datetime.now().isoformat(),))
        # Tier draw, then the split roll: the tenth-pull floor forces a 4-star.
        result = database.perform_gacha_pulls(
            10, 1, 1, banner_key="event", rng=QueueRng(0, 0))
        reward = result["results"][0]
        self.assertEqual(4, reward["rarity"])
        self.assertEqual(featured_four_key, reward["key"])
        self.assertTrue(reward["featured"])
        self.assertEqual(0, self._guarantees()[0], "the 5-star tier is untouched")

    # -- when there is no split ----------------------------------------------

    def test_a_disabled_standard_banner_removes_the_split_entirely(self):
        self._make_event_banner()
        database.set_gacha_banner(
            10, 1, False, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
            1, banner_key="standard")
        # With no split, the first draw picks from the banner's own tier-5 table.
        result = self._pull_five_star([0])
        reward = result["results"][0]
        self.assertFalse(reward["featured"])
        self.assertEqual(database.DEFAULT_GACHA_CONFIG["rewards"]["5"][0]["key"],
                         reward["key"])
        self.assertEqual((0, 0), self._guarantees())

    def test_an_absent_standard_banner_still_splits_against_the_shipped_pool(self):
        # A guild that has never pulled standard has no row for it, and the read
        # accessors already synthesise it as enabled. It must not be created here.
        self._make_event_banner(standard=False)
        result = self._pull_five_star([99, 0])
        self.assertFalse(result["results"][0]["featured"])
        self.assertTrue(result["results"][0]["guarantee_held"])
        with database.get_connection() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM gacha_banners WHERE guild_id = 10 "
                "AND banner_key = 'standard'").fetchone(),
                "another banner's pull must not create the standard row")

    def test_the_standard_banner_itself_never_splits(self):
        database.set_gacha_banner(
            10, 1, True, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
            0, banner_key="standard")
        result = self._pull_five_star([0], banner_key="standard")
        self.assertFalse(result["results"][0]["featured"])

    def test_a_banner_without_a_featured_reward_consumes_no_extra_randomness(self):
        """The guard on every existing probability test in this file."""
        database.set_gacha_banner(
            10, 1, True, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
            0, banner_key="standard")
        counter = _CountingRng()
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pity (guild_id, user_id, banner_key, "
                "pulls_since_five_star, pulls_toward_four_star, updated_at) "
                "VALUES (10, 1, 'standard', 0, 0, ?)", (datetime.now().isoformat(),))
        database.perform_gacha_pulls(10, 1, 10, rng=counter)
        # One tier draw plus one reward draw per pull, and nothing else.
        self.assertEqual(20, counter.calls)

    # -- the split as a proportion -------------------------------------------

    def test_a_split_of_one_hundred_always_awards_the_featured_reward(self):
        self._make_event_banner(split=100)
        for point in (0, 50, 99):
            result = self._pull_five_star([point])
            self.assertEqual(self.FEATURED_KEY, result["results"][0]["key"])

    def test_a_split_of_zero_always_loses(self):
        self._make_event_banner(split=0)
        result = self._pull_five_star([0])
        self.assertNotEqual(self.FEATURED_KEY, result["results"][0]["key"])
        self.assertEqual(1, self._guarantees()[0])

    # -- a lose/win pair inside one batch ------------------------------------

    def test_a_guarantee_earned_and_spent_inside_one_ten_pull_settles_once(self):
        """The guarantee lives in a loop variable and is written once, after the
        loop, exactly like pity and the four-star counter."""
        self._make_event_banner()
        config = self._event_config()
        # Force every pull to a 5-star so both branches run inside one batch.
        config["tiers"] = {"3": 0, "4": 0, "5": 100000}
        config["soft_pity_multiplier"] = 1
        database.set_gacha_banner(10, 1, True, config, 1, banner_key="event")
        # pull 1: tier draw, split roll loses. pull 2: tier draw, guarantee spent.
        result = database.perform_gacha_pulls(
            10, 1, 10, banner_key="event", rng=QueueRng(0, 99, 0))
        first, second = result["results"][0], result["results"][1]
        self.assertFalse(first["featured"])
        self.assertTrue(second["featured"])
        self.assertTrue(second["featured_guaranteed"])

    # -- validation ----------------------------------------------------------

    def test_the_standard_banner_cannot_feature_a_reward(self):
        with self.assertRaises(database.ValidationError) as caught:
            database.set_gacha_banner(10, 1, True, self._event_config(), 0,
                                      banner_key="standard")
        self.assertEqual("gacha_featured_on_standard", caught.exception.reason)

    def test_a_tier_cannot_feature_two_rewards(self):
        config = self._event_config()
        config["rewards"]["5"][0]["featured"] = True
        config["rewards"]["5"][1]["featured"] = True
        with self.assertRaises(database.ValidationError) as caught:
            database.create_gacha_banner(10, 1, "two", "Two", config)
        self.assertEqual("gacha_featured_duplicate", caught.exception.reason)

    def test_the_three_star_tier_cannot_feature_a_reward(self):
        config = self._event_config()
        config["rewards"]["3"][0]["featured"] = True
        with self.assertRaises(database.ValidationError) as caught:
            database.create_gacha_banner(10, 1, "low", "Low", config)
        self.assertEqual("gacha_featured_tier", caught.exception.reason)

    def test_a_featured_reward_cannot_be_disabled(self):
        config = self._event_config()
        for entry in config["rewards"]["5"]:
            if entry.get("featured"):
                entry["enabled"] = False
        with self.assertRaises(database.ValidationError) as caught:
            database.create_gacha_banner(10, 1, "off", "Off", config)
        self.assertEqual("gacha_featured_disabled", caught.exception.reason)

    def test_the_featured_flag_must_be_boolean(self):
        config = self._event_config()
        config["rewards"]["5"][0]["featured"] = "yes"
        with self.assertRaises(database.ValidationError) as caught:
            database.create_gacha_banner(10, 1, "bad", "Bad", config)
        self.assertEqual("gacha_reward_featured", caught.exception.reason)

    def test_the_split_is_range_checked(self):
        for split in (-1, 101):
            config = self._event_config(split=split)
            with self.assertRaises(database.ValidationError) as caught:
                database.create_gacha_banner(10, 1, f"s{abs(split)}", "S", config)
            self.assertEqual("gacha_featured_split_range", caught.exception.reason)

    def test_a_banner_stored_before_schema_fourteen_gains_the_default_split(self):
        legacy = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        del legacy["featured_split"]
        validated = database._validated_gacha_config(legacy)
        self.assertEqual(50, validated["featured_split"])

    def test_a_banner_stored_before_the_split_still_reports_one_when_read(self):
        """Found against the deployment's own banners, not a fixture.

        The read accessors return the *stored* config, so a banner saved before
        `featured_split` existed has no such key — and the dashboard builds one
        number input per scalar, so an absent one renders empty and comes back as
        Number('') === 0, which means "always lose the split". Both of the live
        installation's banners were in exactly that state.
        """
        legacy = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        del legacy["featured_split"]
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_banners (guild_id, banner_key, enabled, "
                "config_json, revision, updated_at) VALUES (10, 'old', 1, ?, 3, ?)",
                (json.dumps(legacy), datetime.now().isoformat()),
            )
        listed = {banner["banner_key"]: banner
                  for banner in database.list_gacha_banners(10)}
        self.assertEqual(50, listed["old"]["config"]["featured_split"])
        self.assertEqual(
            50, database.get_gacha_banner(10, "old")["config"]["featured_split"])

    def test_no_shipped_reward_is_featured(self):
        """`missing_shipped_rewards` matches on the key alone and copies these
        dicts into an operator's table, so a shipped flag could never be
        reconciled away — the same reason `enabled` is absent from them."""
        for entries in database.DEFAULT_GACHA_CONFIG["rewards"].values():
            for entry in entries:
                self.assertNotIn("featured", entry)


class FeaturedSplitRateTests(unittest.TestCase):
    """The long-run featured rate, against its closed form.

    The unit tests above drive a queue RNG and prove each branch in isolation.
    This one runs the real loop against a seeded Mersenne Twister and checks the
    rate the mechanic actually produces, which is the only thing that catches a
    guarantee that is set but never spent, or spent twice.

    The closed form, with split *s* and the featured reward *not* in the standard
    pool: a third of rare pulls are guaranteed ones, because a non-guaranteed
    pull arms the guarantee with probability (1 - s) and a guaranteed pull clears
    it, so the guaranteed fraction x satisfies x = (1 - x)(1 - s). At s = 0.5
    that is x = 1/3, and the featured share is x + (1 - x)s = 2/3.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "gacha.db")
        database.initialize_database()
        database.register_guild(10, "Gacha Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 100000000000)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_the_featured_share_matches_the_closed_form(self):
        import random

        # A curated standard pool: the featured key is removed from it, which is
        # the arrangement the closed form above describes and the one the
        # dashboard's warning nudges an operator towards.
        featured_key = "premium_30d"
        standard = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        standard["rewards"]["5"] = [entry for entry in standard["rewards"]["5"]
                                    if entry["key"] != featured_key]
        database.set_gacha_banner(10, 1, True, standard, 0, banner_key="standard")

        event = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        # Force every pull to a 5-star so the sample is all decisions and no
        # waiting: the split is orthogonal to the tier, so this measures only it.
        event["tiers"] = {"3": 0, "4": 0, "5": 100000}
        event["soft_pity_multiplier"] = 1
        for entry in event["rewards"]["5"]:
            if entry["key"] == featured_key:
                entry["featured"] = True
        database.create_gacha_banner(10, 1, "event", "Event", event, enabled=True)

        rng = random.Random(1234)
        featured = total = 0
        owed = False
        for _ in range(300):
            result = database.perform_gacha_pulls(
                10, 1, 10, banner_key="event", rng=rng)
            for pull in result["results"]:
                total += 1
                if pull["key"] == featured_key:
                    featured += 1
                # A held guarantee must be honoured by the very next rare pull.
                if owed:
                    self.assertEqual(featured_key, pull["key"],
                                     "a held guarantee was not honoured")
                    self.assertTrue(pull["featured_guaranteed"])
                owed = pull["guarantee_held"]

        self.assertEqual(3000, total)
        share = featured / total
        # 2/3 exactly; 3000 samples put the 4-sigma band well inside 3 points.
        self.assertAlmostEqual(2 / 3, share, delta=0.03,
                               msg=f"featured share was {share:.3f}, expected ~0.667")

    def test_a_loss_can_award_the_featured_reward_when_it_is_in_the_standard_pool(self):
        """The consequence of not excluding it, pinned so it stays a known trade.

        The operator curates the standard pool; the code does not second-guess
        them. While the featured key is still in it the real rate is 0.733 rather
        than 0.667, which is what the dashboard warns about.
        """
        import random

        featured_key = "premium_30d"
        database.set_gacha_banner(
            10, 1, True, json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG)),
            0, banner_key="standard")
        event = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        event["tiers"] = {"3": 0, "4": 0, "5": 100000}
        event["soft_pity_multiplier"] = 1
        for entry in event["rewards"]["5"]:
            if entry["key"] == featured_key:
                entry["featured"] = True
        database.create_gacha_banner(10, 1, "event", "Event", event, enabled=True)

        rng = random.Random(99)
        featured = total = 0
        for _ in range(300):
            for pull in database.perform_gacha_pulls(
                    10, 1, 10, banner_key="event", rng=rng)["results"]:
                total += 1
                featured += pull["key"] == featured_key
        # x = 1/3 guaranteed, and a non-guaranteed pull hits it at 0.5 + 0.5/5.
        self.assertAlmostEqual(0.7333, featured / total, delta=0.03)


class _CountingRng:
    def __init__(self):
        self.calls = 0

    def randrange(self, stop):
        self.calls += 1
        return 0


class FeaturedChanceFormulaTests(unittest.TestCase):
    """The dashboard's displayed chance, checked against the mechanic's own odds.

    `weight / Σweight` is simply not the probability once a tier splits: the
    featured row takes the split and the rest share what is left in proportion to
    the *standard banner's* weights. A column labelled "chance within the tier"
    showing the old number would be wrong on exactly the banners an operator most
    wants to check, so the formula is pinned in the language it is written in.
    """

    def test_the_displayed_chance_formula_holds(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        script = Path(__file__).parent / "js" / "featured_chance.js"
        result = subprocess.run([node, str(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode,
                         f"{result.stdout}\n{result.stderr}")
        self.assertIn("ok", result.stdout)

    def test_the_real_formula_is_still_the_one_the_node_test_transcribes(self):
        """The Node test holds a transcription, so this checks the original is
        still shaped the way it was transcribed from."""
        source = (Path(__file__).parent.parent / "dashboard" / "script.js").read_text(
            encoding="utf-8")
        self.assertIn("const share = (100 - split) * (match.weight / poolTotal);",
                      source)
        self.assertIn("function standardPoolFor(tier)", source)


class PityHistoryTests(unittest.TestCase):
    """What `/pity` reads. `gacha_pulls` has recorded this since schema 5 and
    nothing has ever read it, so these accessors are the first consumers."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "gacha.db")
        database.initialize_database()
        database.register_guild(10, "Gacha Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 100000000000)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _all_five_star_banner(self):
        config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        config["tiers"] = {"3": 0, "4": 0, "5": 100000}
        config["soft_pity_multiplier"] = 1
        database.set_gacha_banner(10, 1, True, config, 0, banner_key="standard")

    def test_the_pity_shown_is_the_one_the_pull_landed_at(self):
        """`pity_before` is the pity *before* the pull, so the count it hit is
        one more. Off by one here would be invisible and wrong forever."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pulls (guild_id, user_id, banner_key, "
                "banner_revision, rarity, reward_key, reward_json, pity_before, "
                "soft_pity, hard_pity, created_at) "
                "VALUES (10, 1, 'standard', 1, 5, 'big_vault', '{}', 89, 1, 0, ?)",
                (datetime.now().isoformat(),),
            )
        entry = database.get_five_star_history(10, 1)[0]
        self.assertEqual(90, entry["pity"], "the 90th pull, not the 89th")

    def test_it_returns_only_five_stars_newest_first(self):
        self._all_five_star_banner()
        database.perform_gacha_pulls(10, 1, 10, rng=QueueRng())
        # A 3-star that must not appear.
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pulls (guild_id, user_id, banner_key, "
                "banner_revision, rarity, reward_key, reward_json, pity_before, "
                "soft_pity, hard_pity, created_at) "
                "VALUES (10, 1, 'standard', 1, 3, 'coins_250', '{}', 0, 0, 0, ?)",
                (datetime.now().isoformat(),),
            )
            newest = conn.execute(
                "SELECT reward_key FROM gacha_pulls WHERE rarity = 5 "
                "ORDER BY pull_id DESC LIMIT 1").fetchone()[0]
        history = database.get_five_star_history(10, 1, 5)
        self.assertEqual(5, len(history))
        self.assertEqual(newest, history[0]["reward_key"])
        self.assertTrue(all(entry["reward_key"] != "coins_250" for entry in history))

    def test_the_limit_is_bounded_on_both_sides(self):
        self._all_five_star_banner()
        database.perform_gacha_pulls(10, 1, 10, rng=QueueRng())
        self.assertEqual(1, len(database.get_five_star_history(10, 1, 0)))
        self.assertEqual(10, len(database.get_five_star_history(10, 1, 1000)))

    def test_a_member_who_has_never_pulled_reads_as_zero(self):
        pity = database.get_gacha_pity(10, 999)
        self.assertEqual(0, pity["pity"])
        self.assertEqual(0, pity["total_pulls"])
        self.assertFalse(pity["guaranteed_featured_five"])
        self.assertEqual([], database.get_five_star_history(10, 999))

    def test_history_is_scoped_to_the_guild(self):
        database.register_guild(20, "Other")
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pulls (guild_id, user_id, banner_key, "
                "banner_revision, rarity, reward_key, reward_json, pity_before, "
                "soft_pity, hard_pity, created_at) "
                "VALUES (20, 1, 'standard', 1, 5, 'big_vault', '{}', 5, 0, 0, ?)",
                (datetime.now().isoformat(),),
            )
        self.assertEqual([], database.get_five_star_history(10, 1))
        self.assertEqual(1, len(database.get_five_star_history(20, 1)))
