"""A voucher says what it is for, instead of it being guessed from its key.

`redeem_voucher` used to do `asset_type = reward_key.split("_", 1)[0]` and refuse
anything not beginning `emoji`, `sticker` or `sound`. That is why one of a
guild's own voucher items could never be a banner reward — and why a key that
happened to begin `emoji_` would have worked by accident, which is worse than a
clean refusal.

`reward_vouchers.subject` states it instead, written when the voucher is granted.
**NULL means "derive from the key"**, so every voucher that predates the column
redeems down exactly the path it always did — the same third state
`gacha_banners.display_name` and `warnings.tag` carry.

The role case rests on one thing worth stating: the gacha never makes a Discord
call inside a pull, because the grant runs in the pull's transaction. That is why
premium has always been a *voucher* rather than a reward the pull hands over, and
it is why a custom role is one too rather than a new reward kind.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import database

GUILD = 1
MEMBER = 7
ROLE = 1420070400000000009


class VoucherSubjectTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "vouchers.db")
        database.initialize_database()
        database.register_guild(GUILD, "Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def add_voucher(self, voucher_id, reward_key, subject=None, days=30,
                    status="available"):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO reward_vouchers (voucher_id, guild_id, user_id, "
                "reward_key, source_type, duration_days, subject, status, "
                "acquired_at) VALUES (?, ?, ?, ?, 'gacha', ?, ?, ?, '2026-01-01')",
                (voucher_id, GUILD, MEMBER, reward_key, days, subject, status))
            conn.commit()

    def custom_role_item(self, key="vip_month", days=30):
        database.create_shop_item_definition(GUILD, 42, {
            "item_key": key, "template_type": "timed_role", "category": None,
            "enabled": False, "price": 1,
            "config": {"role_id": ROLE, "duration_days": days},
            "text": {"name": "VIP month", "description": "d"}})

    def custom_asset_item(self, key="my_sticker", asset="sticker", days=90):
        database.create_shop_item_definition(GUILD, 42, {
            "item_key": key, "template_type": "fulfillment_voucher",
            "category": None, "enabled": False, "price": 1,
            "config": {"asset_type": asset, "duration_days": days},
            "text": {"name": "Own sticker", "description": "d"}})

    # --- what gets written at grant time -------------------------------------

    def subject_written_for(self, key):
        with database.get_connection() as conn:
            return database._voucher_subject_for(conn, GUILD, key)

    def test_a_builtin_key_writes_no_subject(self):
        """So a shipped voucher behaves byte-for-byte as it always has."""
        for key in ("emoji_30d", "sticker_180d", "premium_30d"):
            with self.subTest(key=key):
                self.assertIsNone(self.subject_written_for(key))

    def test_a_custom_asset_item_writes_its_configured_type(self):
        """Not the first segment of its key, which is the whole bug."""
        self.custom_asset_item(key="totally_unlike_a_key", asset="sticker")
        self.assertEqual("sticker",
                         self.subject_written_for("totally_unlike_a_key"))

    def test_a_custom_timed_role_writes_the_entitlement_key(self):
        """`role:<id>` is the key the shop already writes for a custom timed
        role, so a redeemed one expires through the pass that already revokes
        anything starting with it."""
        self.custom_role_item()
        self.assertEqual(f"role:{ROLE}", self.subject_written_for("vip_month"))

    def test_a_key_naming_no_item_writes_no_subject(self):
        self.assertIsNone(self.subject_written_for("nothing_here"))

    # --- redemption ----------------------------------------------------------

    def test_a_legacy_voucher_still_redeems_by_its_key(self):
        """Every voucher granted before the column exists reads NULL."""
        self.add_voucher("old", "emoji_30d")
        result = database.redeem_voucher(GUILD, MEMBER, "old")
        self.assertTrue(result["redeemed"])
        self.assertEqual("fulfillment", result["kind"])
        self.assertEqual("emoji", result["asset_type"])

    def test_a_legacy_premium_voucher_is_untouched(self):
        self.add_voucher("old", "premium_30d")
        self.assertEqual("premium",
                         database.redeem_voucher(GUILD, MEMBER, "old")["kind"])

    def test_a_custom_asset_voucher_redeems_as_its_stored_type(self):
        self.add_voucher("v", "totally_unlike_a_key", subject="sticker")
        result = database.redeem_voucher(GUILD, MEMBER, "v")
        self.assertEqual("sticker", result["asset_type"])
        with database.get_connection() as conn:
            stored = conn.execute(
                "SELECT asset_type FROM fulfillment_requests").fetchone()[0]
        self.assertEqual("sticker", stored)

    def test_a_role_voucher_records_the_entitlement_and_stops(self):
        """It must not touch Discord: this runs inside the transaction."""
        self.add_voucher("v", "vip_month", subject=f"role:{ROLE}")
        result = database.redeem_voucher(GUILD, MEMBER, "v")
        self.assertEqual("role", result["kind"])
        self.assertEqual(ROLE, result["role_id"])
        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT entitlement_key, status FROM timed_entitlements"
            ).fetchall()
        self.assertEqual([(f"role:{ROLE}", "active")], rows)

    def test_a_second_role_voucher_extends_rather_than_stacks(self):
        self.add_voucher("a", "vip_month", subject=f"role:{ROLE}", days=30)
        self.add_voucher("b", "vip_month", subject=f"role:{ROLE}", days=30)
        first = database.redeem_voucher(GUILD, MEMBER, "a")["expires_at"]
        second = database.redeem_voucher(GUILD, MEMBER, "b")["expires_at"]
        self.assertGreater(second, first)
        with database.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM timed_entitlements").fetchone()[0]
        self.assertEqual(1, count, "a second voucher created a second grant")

    def test_a_malformed_subject_refuses_rather_than_raising(self):
        """This is reached from a command callback, where a raise is a dead
        command rather than a message."""
        self.add_voucher("v", "x", subject="role:not-a-number")
        self.assertEqual({"redeemed": False, "reason": "not_redeemable"},
                         database.redeem_voucher(GUILD, MEMBER, "v"))

    def test_redeeming_twice_is_refused(self):
        self.add_voucher("v", "vip_month", subject=f"role:{ROLE}")
        database.redeem_voucher(GUILD, MEMBER, "v")
        self.assertEqual(
            {"redeemed": False, "reason": "already_redeemed"},
            database.redeem_voucher(GUILD, MEMBER, "v"))

    # --- the failure path ----------------------------------------------------

    def test_a_rollback_leaves_the_voucher_unredeemed(self):
        """A pull cannot be refunded — it has already spent pity and coins — so
        a redemption Discord refuses must leave the voucher usable."""
        self.add_voucher("v", "vip_month", subject=f"role:{ROLE}")
        result = database.redeem_voucher(GUILD, MEMBER, "v")
        self.assertTrue(database.rollback_voucher_redemption(
            GUILD, MEMBER, "v", result["entitlement_key"]))
        with database.get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM reward_vouchers WHERE voucher_id = 'v'"
            ).fetchone()[0]
            grants = conn.execute(
                "SELECT COUNT(*) FROM timed_entitlements").fetchone()[0]
        self.assertEqual("available", status)
        self.assertEqual(0, grants)

    def test_the_premium_rollback_still_works_through_the_shared_one(self):
        self.add_voucher("v", "premium_30d")
        database.redeem_voucher(GUILD, MEMBER, "v")
        self.assertTrue(
            database.rollback_premium_redemption(GUILD, MEMBER, "v"))
        with database.get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM reward_vouchers WHERE voucher_id = 'v'"
            ).fetchone()[0]
        self.assertEqual("available", status)

    # --- what happens afterwards ---------------------------------------------

    def test_the_voucher_outlives_the_item_that_produced_it(self):
        """The subject is written at grant time precisely so deleting the item
        does not strand every voucher it ever produced."""
        self.custom_role_item()
        with database.get_connection() as conn:
            subject = database._voucher_subject_for(conn, GUILD, "vip_month")
        self.add_voucher("v", "vip_month", subject=subject)
        database.delete_shop_item_definition(GUILD, 42, "vip_month", 1)
        self.assertEqual("role",
                         database.redeem_voucher(GUILD, MEMBER, "v")["kind"])

    def test_a_gacha_sourced_role_still_expires(self):
        """`cogs/shop.py`'s expiry pass matches `role:` with **no source
        filter**, which is what makes a gacha-granted role revocable at all. If
        that ever gains one, a role won from a banner becomes permanent."""
        self.add_voucher("v", "vip_month", subject=f"role:{ROLE}")
        database.redeem_voucher(GUILD, MEMBER, "v")
        with database.get_connection() as conn:
            conn.execute("UPDATE timed_entitlements SET expires_at = '2020-01-01'")
            conn.commit()
        expired = database.get_expired_entitlements("2026-12-31T00:00:00+00:00")
        keys = [entry["entitlement_key"] for entry in expired]
        self.assertIn(f"role:{ROLE}", keys)
        self.assertEqual("gacha", expired[0]["source_type"])

    def test_revoking_it_reads_the_role_from_the_key_not_the_item_id(self):
        """A `role:<id>` entitlement leaves `discord_item_id` NULL, and reading
        it raised a `TypeError` straight past the Discord handler once — which
        aborted a member's erasure entirely. The branch must take the id out of
        the key."""
        import inspect
        from cogs.gacha import revoke_entitlement

        source = inspect.getsource(revoke_entitlement)
        # Comments stripped: the branch carries one explaining that it must not
        # read `discord_item_id`, and left in it reads as the column being read.
        code = "\n".join(line for line in source.split("\n")
                         if not line.strip().startswith("#"))
        role_branch = code[code.index('kind.startswith("role:")'):]
        cut = role_branch.find("elif")
        role_branch = role_branch[:cut] if cut != -1 else role_branch
        self.assertIn('kind.split(":", 1)[1]', role_branch)
        self.assertNotIn("discord_item_id", role_branch)


if __name__ == "__main__":
    unittest.main()
