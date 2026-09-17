"""Export, anonymising erasure and retention.

``docs/privacy.md`` requires that deletion preserve the minimum audit record,
anonymise references where possible, and never silently alter financial totals.
These tests pin each of those three properties.
"""

import json
import re
import os
import sqlite3
import tempfile
import unittest

from core import database


class PrivacyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.original_path = database.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(111, "Guild")
        self.seed()

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def seed(self):
        """One row in every table the subject can appear in."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, balance, xp, last_active, streak_count, "
                "last_daily) VALUES (7, 5000, 120, '2020-01-01T00:00:00+00:00', 4, 'x')"
            )
            conn.execute("INSERT INTO users (user_id, balance) VALUES (8, 100)")
            conn.execute(
                "INSERT INTO scoped_accounts (scope_type, scope_id, user_id, balance, "
                "xp, level, created_at, updated_at) "
                "VALUES ('guild', 111, 7, 5000, 120, 4, 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO casino_wagers (wager_id, guild_id, user_id, game_key, "
                "stake, status, created_at) VALUES ('w1', 111, 7, 'bj', 250, 'pending', 'now')"
            )
            conn.execute(
                "INSERT INTO casino_wagers (wager_id, guild_id, user_id, game_key, "
                "stake, status, created_at) VALUES ('w2', 111, 7, 'bj', 10, 'settled', 'now')"
            )
            conn.execute(
                "INSERT INTO reward_claims (guild_id, user_id, reward_key, claimed_at) "
                "VALUES (111, 7, 'boost', 'now')"
            )
            # Quantity zero must still be exported: it is a record of a past holding.
            conn.execute(
                "INSERT INTO user_inventory (guild_id, user_id, item_key, quantity, "
                "updated_at) VALUES (111, 7, 'lockpick', 0, 'now')"
            )
            conn.execute(
                "INSERT INTO gacha_pity (guild_id, user_id, banner_key, "
                "pulls_since_five_star, pulls_toward_four_star, updated_at) "
                "VALUES (111, 7, 'standard', 3, 1, 'now')"
            )
            conn.execute(
                "INSERT INTO gacha_pulls (guild_id, user_id, banner_key, "
                "banner_revision, rarity, reward_key, reward_json, pity_before, "
                "soft_pity, hard_pity, four_star_guarantee, created_at) "
                "VALUES (111, 7, 'standard', 1, 5, 'premium_30d', '{}', 3, 0, 0, 0, 'now')"
            )
            conn.execute(
                "INSERT INTO reward_vouchers (voucher_id, guild_id, user_id, "
                "reward_key, duration_days, status, acquired_at) "
                "VALUES ('v1', 111, 7, 'premium_30d', 30, 'active', 'now')"
            )
            conn.execute(
                "INSERT INTO timed_entitlements (entitlement_id, guild_id, user_id, "
                "entitlement_key, starts_at, expires_at, source_voucher_id, "
                "discord_item_id, status) VALUES (1, 111, 7, 'premium', 'now', "
                "'2099-01-01T00:00:00+00:00', 'v1', NULL, 'active')"
            )
            conn.execute(
                "INSERT INTO fulfillment_requests (voucher_id, guild_id, user_id, "
                "asset_type, status, created_at) VALUES ('v1', 111, 7, 'emoji', 'open', 'now')"
            )
            # The subject is the moderator on someone else's warning.
            conn.execute(
                "INSERT INTO warnings (user_id, mod_id, reason, date, guild_id) "
                "VALUES (7, 8, 'spam', 'now', 111)"
            )
            conn.execute(
                "INSERT INTO warnings (user_id, mod_id, reason, date, guild_id) "
                "VALUES (8, 7, 'other', 'now', 111)"
            )
            conn.execute(
                "INSERT INTO tickets (channel_id, opener_id, created_at, guild_id, "
                "claimer_id) VALUES (900, 7, 'now', 111, 8)"
            )
            conn.execute(
                "INSERT INTO tickets (channel_id, opener_id, created_at, guild_id, "
                "claimer_id) VALUES (901, 8, 'now', 111, 7)"
            )
            conn.execute(
                "INSERT INTO voice_settings (guild_id, user_id, channel_name) "
                "VALUES (111, 7, 'room')"
            )
            # The subject is the target of someone else's block list.
            conn.execute(
                "INSERT INTO voice_permissions (guild_id, owner_id, target_id, "
                "is_allowed) VALUES (111, 8, 7, 0)"
            )
            conn.execute(
                "INSERT INTO active_channels (channel_id, owner_id, guild_id) "
                "VALUES (950, 7, 111)"
            )
            conn.execute(
                "INSERT INTO user_identities (user_id, first_seen_at, last_seen_at) "
                "VALUES (7, 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO user_sharing_preferences (user_id, guild_id, category, "
                "opted_out, updated_at) VALUES (7, 111, 'economy', 1, 'now')"
            )
            conn.execute(
                "INSERT INTO activity_events (user_id, origin_guild_id, category, "
                "event_type, created_at) VALUES (7, 111, 'economy', 'seed', 'now')"
            )
            conn.execute(
                "INSERT INTO rented_items (item_type, discord_item_id, expires_at, "
                "guild_id) VALUES ('premium', NULL, '2099-01-01', 111)"
            )
            conn.execute(
                "INSERT INTO settings_audit (guild_id, actor_id, action, target_key, "
                "created_at) VALUES (111, 7, 'setting.update', 'language', 'now')"
            )

    def coin_supply(self):
        """Balances plus every stake still owed back to a player."""
        with database.get_connection() as conn:
            balances = conn.execute("SELECT COALESCE(SUM(balance), 0) FROM users").fetchone()[0]
            owed = conn.execute(
                "SELECT COALESCE(SUM(stake), 0) FROM casino_wagers WHERE status = 'pending'"
            ).fetchone()[0]
        return balances + owed

    # -- export ------------------------------------------------------------

    def test_export_covers_every_table_the_subject_appears_in(self):
        export = database.export_user_data(7)
        populated = {table for table, rows in export["tables"].items() if rows}
        expected = {table for table, _, _ in database.SUBJECT_TABLES}
        expected.add("rented_items")
        self.assertEqual(expected, populated)

    def test_export_includes_rows_where_the_subject_is_not_the_owner(self):
        """A subject can appear as a moderator, a claimer or a block target."""
        tables = database.export_user_data(7)["tables"]
        self.assertEqual(
            {row["user_id"] for row in tables["warnings"]}, {7, 8}
        )
        self.assertEqual(
            {row["channel_id"] for row in tables["tickets"]}, {900, 901}
        )
        self.assertEqual(tables["voice_permissions"][0]["target_id"], 7)

    def test_export_keeps_rows_the_gameplay_readers_filter_out(self):
        """get_user_inventory hides zero quantities; an export must not."""
        tables = database.export_user_data(7)["tables"]
        self.assertEqual(tables["user_inventory"][0]["quantity"], 0)
        self.assertEqual(database.get_user_inventory(111, 7), {})

    def test_export_is_json_serialisable_and_names_its_schema(self):
        export = database.export_user_data(7)
        json.dumps(export)
        self.assertEqual(export["schema_version"], database.LATEST_SCHEMA_VERSION)
        self.assertEqual(export["user_id"], "7")
        self.assertEqual(export["truncated_tables"], [])

    def test_export_marks_a_table_it_had_to_truncate(self):
        with database.get_connection() as conn:
            for index in range(database.EXPORT_ROW_LIMIT + 5):
                conn.execute(
                    "INSERT INTO activity_events (user_id, origin_guild_id, category, "
                    "event_type, created_at) VALUES (7, 111, 'economy', ?, 'now')",
                    (f"bulk-{index}",),
                )
        export = database.export_user_data(7)
        self.assertIn("activity_events", export["truncated_tables"])
        self.assertEqual(
            len(export["tables"]["activity_events"]), database.EXPORT_ROW_LIMIT
        )

    def test_export_of_an_unknown_member_is_empty_rather_than_an_error(self):
        export = database.export_user_data(999999)
        self.assertEqual([], [t for t, rows in export["tables"].items() if rows])

    # -- erasure: financial integrity --------------------------------------

    def test_erasure_leaves_the_coin_supply_untouched(self):
        before = self.coin_supply()
        database.anonymize_user(7, actor_id=8, guild_id=111)
        self.assertEqual(before, self.coin_supply())

    def test_a_pending_wager_is_refunded_rather_than_forgotten(self):
        """The stake had already left the balance; deleting the row would lose it."""
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        self.assertEqual(receipt["refunded_wagers"], {"count": 1, "amount": 250})
        with database.get_connection() as conn:
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (receipt["tombstone_id"],),
            ).fetchone()[0]
            statuses = {
                row[0] for row in conn.execute(
                    "SELECT status FROM casino_wagers WHERE user_id = ?",
                    (receipt["tombstone_id"],),
                )
            }
        self.assertEqual(balance, 5250)
        self.assertEqual(statuses, {"refunded", "settled"})

    def test_the_economy_row_survives_under_the_tombstone(self):
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        self.assertLess(receipt["tombstone_id"], 0)
        self.assertEqual(receipt["retained_rows"]["users"], 1)
        self.assertEqual(receipt["retained_rows"]["scoped_accounts"], 1)
        with database.get_connection() as conn:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM users WHERE user_id = 7").fetchone()
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM scoped_accounts WHERE user_id = 7"
                ).fetchone()
            )

    def test_the_repeat_payout_guard_survives_the_erasure(self):
        """Deleting reward_claims would let a returning member be paid twice."""
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        with database.get_connection() as conn:
            claims = conn.execute(
                "SELECT COUNT(*) FROM reward_claims WHERE user_id = ?",
                (receipt["tombstone_id"],),
            ).fetchone()[0]
        self.assertEqual(claims, 1)

    def test_behavioural_columns_are_blanked_on_the_retained_row(self):
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT last_daily, streak_count, last_active, xp FROM users "
                "WHERE user_id = ?", (receipt["tombstone_id"],)
            ).fetchone()
        # Cooldowns, streaks and activity go; the economy figures stay.
        self.assertEqual(row, (None, 0, None, 120))

    # -- erasure: deletion and attribution ---------------------------------

    def test_every_personal_table_is_emptied_of_the_subject(self):
        database.anonymize_user(7, actor_id=8, guild_id=111)
        with database.get_connection() as conn:
            for table, clause, bindings in database.ERASE_DELETE_ORDER:
                with self.subTest(table=table):
                    remaining = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {clause}", (7,) * bindings
                    ).fetchone()[0]
                    self.assertEqual(remaining, 0)

    def test_deletion_order_respects_the_voucher_foreign_keys(self):
        """timed_entitlements and fulfillment_requests reference reward_vouchers
        with no ON DELETE action, so a wrong order raises IntegrityError."""
        with database.get_connection() as conn:
            self.assertEqual(
                conn.execute("PRAGMA foreign_keys").fetchone()[0], 1
            )
        database.anonymize_user(7, actor_id=8, guild_id=111)  # must not raise
        with database.get_connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM reward_vouchers").fetchone()[0], 0
            )

    def test_other_members_rows_survive_with_the_subject_dereferenced(self):
        database.anonymize_user(7, actor_id=8, guild_id=111)
        with database.get_connection() as conn:
            warning = conn.execute(
                "SELECT user_id, mod_id FROM warnings WHERE user_id = 8"
            ).fetchone()
            ticket = conn.execute(
                "SELECT opener_id, claimer_id FROM tickets WHERE channel_id = 901"
            ).fetchone()
        self.assertEqual(warning, (8, None))
        self.assertEqual(ticket, (8, None))

    def test_not_null_actor_columns_point_at_the_tombstone(self):
        """settings_audit.actor_id cannot be nulled, so the row must be re-keyed
        rather than deleted or left naming the erased member."""
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        with database.get_connection() as conn:
            actors = {
                row[0] for row in conn.execute(
                    "SELECT actor_id FROM settings_audit WHERE action = 'setting.update'"
                )
            }
        self.assertEqual(actors, {receipt["tombstone_id"]})

    # -- erasure: the audit record -----------------------------------------

    def test_the_erasure_audit_row_never_names_the_erased_member(self):
        """The audit feed is readable by any guild administrator."""
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        entries = [
            entry for entry in database.get_settings_audit(111)
            if entry["action"] == "user.erase"
        ]
        self.assertEqual(len(entries), 1)
        payload = json.dumps(entries[0])
        self.assertIn(str(receipt["tombstone_id"]), payload)
        self.assertNotIn('"7"', payload)
        self.assertNotIn(": 7", payload)
        self.assertEqual(entries[0]["target_key"],
                         f"tombstone:{receipt['tombstone_id']}")

    def test_an_earlier_warning_deletion_payload_is_scrubbed(self):
        """remove_warning embeds the subject's id and the staff reason text."""
        warning_id = database.get_warnings(7, 111)[0][0]
        database.remove_warning(warning_id, 7, 111, 8)
        stored = json.dumps(database.get_settings_audit(111))
        self.assertIn("spam", stored)

        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        entry = next(
            entry for entry in database.get_settings_audit(111)
            if entry["action"] == "warning.delete"
        )
        self.assertEqual(entry["old_value"]["user_id"], receipt["tombstone_id"])
        self.assertIsNone(entry["old_value"]["reason"])
        self.assertTrue(entry["old_value"]["erased"])
        self.assertEqual(receipt["audit_payloads_scrubbed"], 1)

    # -- erasure: identifiers ----------------------------------------------

    def test_two_erasures_get_distinct_tombstones(self):
        first = database.anonymize_user(7, actor_id=8, guild_id=111)
        second = database.anonymize_user(8, actor_id=7, guild_id=111)
        self.assertNotEqual(first["tombstone_id"], second["tombstone_id"])
        with database.get_connection() as conn:
            ids = {row[0] for row in conn.execute("SELECT user_id FROM users")}
        self.assertEqual(ids, {first["tombstone_id"], second["tombstone_id"]})

    def test_a_returning_member_starts_from_a_fresh_account(self):
        database.anonymize_user(7, actor_id=8, guild_id=111)
        database.apply_user_delta(7, balance_change=0)
        self.assertEqual(database.get_user_balance(7), 100)

    def test_erasure_refuses_a_non_snowflake_subject(self):
        for subject in (0, -1):
            with self.subTest(subject=subject):
                with self.assertRaises(database.ValidationError):
                    database.anonymize_user(subject, actor_id=8, guild_id=111)

    def test_a_tombstone_is_never_ranked(self):
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        tombstone = receipt["tombstone_id"]
        member_ids = [tombstone, 8]
        self.assertEqual(
            [row[0] for row in database.get_top_balances(member_ids, 10)], [8]
        )
        self.assertEqual(
            [row[0] for row in database.get_top_levels(member_ids, 10)], [8]
        )
        self.assertEqual(database.get_top_xp_user(member_ids), 8)
        self.assertEqual(database.get_user_rank(0, member_ids), 1)

    # -- entitlements and retention ----------------------------------------

    def test_active_grants_are_reported_before_they_are_erased(self):
        grants = database.get_active_entitlements_for_user(7)
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["entitlement_key"], "premium")
        self.assertEqual(grants[0]["guild_id"], 111)

    def test_retention_selects_only_members_with_stale_recorded_activity(self):
        self.assertEqual(
            database.get_retention_candidates("2021-01-01T00:00:00+00:00"), [7]
        )
        # User 8 has never recorded activity, so absence of a timestamp must not
        # be read as absence of a member.
        self.assertEqual(
            database.get_retention_candidates("2019-01-01T00:00:00+00:00"), []
        )

    def test_retention_never_selects_a_tombstone(self):
        receipt = database.anonymize_user(7, actor_id=8, guild_id=111)
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE users SET last_active = '2020-01-01T00:00:00+00:00' "
                "WHERE user_id = ?", (receipt["tombstone_id"],)
            )
        self.assertEqual(
            database.get_retention_candidates("2021-01-01T00:00:00+00:00"), []
        )

    def test_retention_batch_size_is_honoured(self):
        with database.get_connection() as conn:
            for user_id in range(100, 140):
                conn.execute(
                    "INSERT INTO users (user_id, last_active) "
                    "VALUES (?, '2020-01-01T00:00:00+00:00')", (user_id,)
                )
        self.assertEqual(
            len(database.get_retention_candidates("2021-01-01T00:00:00+00:00", 25)), 25
        )


class ErasureColumnCoverageTests(unittest.TestCase):
    """Every column that can hold a member is erased, re-keyed, scrubbed or named.

    `SubjectTableCoverageTests` below asks whether a *table* is declared; this
    asks about *columns*, in both directions, because the two gaps this closes
    were columns on tables that were otherwise fine: `lfg_posts.host_id` and
    `joined_json`, `minigame_state.last_user_id`, and the erasure action's own
    `payload_json`. All three arrived after the privacy work and nothing noticed.

    Classification is by column *name*, deliberately. A new table gets a new
    row here only if its member column is called something new, and then the
    test fails and says so -- which is the point. An allowlist of 95 specific
    columns would be maintained by nobody.
    """

    # Columns that hold a member's id. Each (table, column) carrying one of
    # these names must appear in an erase list or a scrub declaration.
    MEMBER_COLUMNS = {
        "user_id", "owner_id", "target_id", "opener_id", "claimer_id", "mod_id",
        "host_id", "last_user_id", "actor_id", "completed_by", "approved_by",
        "created_by", "updated_by",
    }
    # `_id`/`_by` columns that are not members, with why. A name absent from
    # both sets is a finding.
    NOT_MEMBERS = {
        "guild_id": "a guild", "origin_guild_id": "a guild",
        "channel_id": "a channel", "message_id": "a message",
        "role_id": "a role", "game_role_id": "a role",
        "realm_id": "a realm", "scope_id": "a guild or 0 for the instance",
        # Surrogate primary keys.
        "event_id": "row id", "action_id": "row id", "document_id": "row id",
        "request_id": "row id", "pull_id": "row id", "entry_id": "row id",
        "audit_id": "row id", "entitlement_id": "row id",
        "response_id": "row id",
    }
    # JSON columns that never carry a member id, with why. The ones that can
    # are declared in `database.ERASE_SCRUBBED_JSON` and rewritten there.
    JSON_WITHOUT_MEMBERS = {
        ("activity_events", "metadata_json"): "the row is deleted with the member",
        ("casino_wagers", "resolution_json"): "outcome and credit only",
        ("dashboard_documents", "content_json"): "operator-authored embed text",
        ("gacha_banners", "config_json"): "the reward table",
        ("gacha_pulls", "reward_json"): "the row is deleted with the member",
        ("guild_settings", "value_json"): "operator configuration; `ignored_users` "
            "may name a member and is the operator's to edit",
        ("instance_settings", "value_json"): "installation-wide values",
        ("managed_messages", "options_json"): "button labels and sections",
        ("settings_audit", "new_value_json"): "settings values and the erasure "
            "receipt, which names the tombstone only",
        ("shop_item_definitions", "config_json"): "role ids and amounts",
    }

    def setUp(self):
        self.original_path = database.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _columns(self):
        with sqlite3.connect(database.DB_PATH) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                for _, name, ctype, *_ in conn.execute(f"PRAGMA table_info({table})"):
                    yield table, name, (ctype or "").upper()

    def _covered_member_columns(self):
        covered = set()
        for table, clause, _ in database.ERASE_DELETE_ORDER:
            for column in re.findall(r"(\w+) = \?", clause):
                covered.add((table, column))
        for table in database.ERASE_REKEY_SUBJECT:
            covered.add((table, "user_id"))
        covered.update(database.ERASE_NULL_ACTOR)
        covered.update(database.ERASE_REKEY_ACTOR)
        return covered

    def test_every_member_column_is_reached_by_the_erasure(self):
        covered = self._covered_member_columns()
        missing = sorted(
            f"{table}.{name}" for table, name, ctype in self._columns()
            if "INT" in ctype and name in self.MEMBER_COLUMNS
            and (table, name) not in covered)
        self.assertEqual([], missing)

    def test_every_id_column_is_classified(self):
        """A new id-shaped column has to be called a member or not, by name."""
        unknown = sorted(
            f"{table}.{name}" for table, name, ctype in self._columns()
            if "INT" in ctype and (name.endswith("_id") or name.endswith("_by"))
            and name not in self.MEMBER_COLUMNS and name not in self.NOT_MEMBERS)
        self.assertEqual([], unknown)

    def test_every_json_column_is_scrubbed_or_explained(self):
        scrubbed = set(database.ERASE_SCRUBBED_JSON)
        unexplained = sorted(
            f"{table}.{name}" for table, name, _ in self._columns()
            if name.endswith("_json")
            and (table, name) not in scrubbed
            and (table, name) not in self.JSON_WITHOUT_MEMBERS)
        self.assertEqual([], unexplained)

    def test_every_declared_column_exists(self):
        """The other direction: a list naming a column that is gone is dead."""
        real = {(table, name) for table, name, _ in self._columns()}
        declared = self._covered_member_columns() | set(database.ERASE_SCRUBBED_JSON)
        self.assertEqual(set(), declared - real)


class ErasureReachesLateTablesTests(unittest.TestCase):
    """The three columns that escaped, exercised rather than listed."""

    def setUp(self):
        self.original_path = database.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(111, "Guild")
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (7, 10)")
            conn.execute("INSERT INTO users (user_id, balance) VALUES (8, 10)")
        # 7 hosts one post and has joined 8's post.
        database.create_lfg_post(111, 1001, 5, host_id=7, needed=0)
        database.create_lfg_post(111, 1002, 5, host_id=8, needed=0)
        self.assertIn("joined", database.join_lfg_post(111, 1002, 7))
        # 7 took the last counting turn.
        database.advance_minigame(111, "counting", "1", 7, "")
        # And an operator queued 7's erasure through the outbox.
        self.action_id = database.queue_control_action(
            111, 42, "erase_member", {"user_id": 7})

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_the_hosted_post_goes_and_the_joined_post_forgets_them(self):
        receipt = database.anonymize_user(7, actor_id=42, guild_id=111)
        self.assertEqual(1, receipt["deleted_rows"].get("lfg_posts"))
        self.assertEqual(1, receipt["lfg_parties_left"])
        self.assertIsNone(database.get_lfg_post(111, 1001))
        self.assertEqual([], database.get_lfg_post(111, 1002)["joined"])

    def test_the_last_turn_is_forgotten_and_the_chain_plays_on(self):
        database.anonymize_user(7, actor_id=42, guild_id=111)
        state = database.get_minigame_state(111, "counting")
        self.assertIsNone(state["last_user_id"])
        self.assertEqual("1", state["value"])
        self.assertIsNotNone(database.advance_minigame(111, "counting", "2", 8, "1"))

    def test_the_erasure_action_no_longer_names_the_member(self):
        receipt = database.anonymize_user(7, actor_id=42, guild_id=111)
        self.assertEqual(1, receipt["control_action_payloads_scrubbed"])
        with sqlite3.connect(database.DB_PATH) as conn:
            payload = json.loads(conn.execute(
                "SELECT payload_json FROM control_actions WHERE action_id = ?",
                (self.action_id,)).fetchone()[0])
        self.assertEqual(receipt["tombstone_id"], payload["user_id"])

    def test_a_longer_id_containing_this_one_is_left_alone(self):
        """The LIKE prefilter is a prefilter; membership decides."""
        database.create_lfg_post(111, 1003, 5, host_id=8, needed=0)
        self.assertIn("joined", database.join_lfg_post(111, 1003, 1007))
        database.anonymize_user(7, actor_id=42, guild_id=111)
        self.assertEqual([1007], database.get_lfg_post(111, 1003)["joined"])


class SubjectTableCoverageTests(unittest.TestCase):
    """A new per-user table must be added to the privacy tables, not just created."""

    SUBJECT_COLUMNS = {"user_id", "opener_id", "owner_id", "target_id", "mod_id",
                       "claimer_id", "completed_by", "actor_id", "created_by",
                       "updated_by", "approved_by"}

    def setUp(self):
        self.original_path = database.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_every_table_holding_a_person_is_declared_somewhere(self):
        declared = (
            {table for table, _, _ in database.SUBJECT_TABLES}
            | {table for table, _, _ in database.ERASE_DELETE_ORDER}
            | set(database.ERASE_REKEY_SUBJECT)
            | {table for table, _ in database.ERASE_NULL_ACTOR}
            | {table for table, _ in database.ERASE_REKEY_ACTOR}
        )
        with sqlite3.connect(database.DB_PATH) as conn:
            tables = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
            undeclared = [
                table for table in tables
                if table not in declared
                and self.SUBJECT_COLUMNS & {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
            ]
        self.assertEqual([], undeclared)

    def test_the_delete_order_lists_children_before_their_parents(self):
        order = [table for table, _, _ in database.ERASE_DELETE_ORDER]
        self.assertLess(
            order.index("fulfillment_requests"), order.index("reward_vouchers")
        )
        self.assertLess(
            order.index("timed_entitlements"), order.index("reward_vouchers")
        )

    def test_no_table_is_both_deleted_and_retained(self):
        deleted = {table for table, _, _ in database.ERASE_DELETE_ORDER}
        self.assertEqual(set(), deleted & set(database.ERASE_REKEY_SUBJECT))


if __name__ == "__main__":
    unittest.main()


class EntitlementRevocationTests(unittest.IsolatedAsyncioTestCase):
    """The Discord half of an erasure must not be able to abort the database half.

    ``revoke_entitlement`` used to fall through to ``fetch_soundboard_sound`` for
    any key it did not recognise, reading ``discord_item_id``. A custom-shop
    ``role:<id>`` entitlement leaves that column NULL, so ``int(None)`` raised a
    TypeError past the ``except discord.HTTPException`` — and every caller is an
    erasure path, so the member asked to be erased and was not.
    """

    def setUp(self):
        import cogs.gacha as gacha
        self.gacha = gacha
        self.guild = _RevocationGuild()

    def _entitlement(self, key, item_id=None, entitlement_id=1):
        return {
            "entitlement_id": entitlement_id,
            "guild_id": 111,
            "user_id": 7,
            "entitlement_key": key,
            "discord_item_id": item_id,
            "status": "active",
        }

    async def test_custom_shop_role_is_revoked_rather_than_raising(self):
        member = _RevocationMember()
        role = object()
        self.guild.member = member
        self.guild.roles[4242] = role
        ok = await self.gacha.revoke_entitlement(
            self.guild, self._entitlement("role:4242"))
        self.assertTrue(ok)
        self.assertEqual([role], member.removed)

    async def test_unknown_key_reports_success_rather_than_blocking_erasure(self):
        # Nothing here knows how to reach it, and returning False would make the
        # retention sweep retry the same row every day forever.
        ok = await self.gacha.revoke_entitlement(
            self.guild, self._entitlement("something_new"))
        self.assertTrue(ok)

    async def test_asset_entitlement_without_a_discord_id_does_not_raise(self):
        ok = await self.gacha.revoke_entitlement(
            self.guild, self._entitlement("sticker", item_id=None))
        self.assertTrue(ok)

    async def test_a_type_error_is_caught_and_reported_as_a_failure(self):
        # Belt and braces: the handler catches TypeError/ValueError too, so a
        # future malformed row is a False rather than an aborted erasure.
        ok = await self.gacha.revoke_entitlement(
            self.guild, self._entitlement("emoji", item_id="not-an-id"))
        self.assertFalse(ok)


class _RevocationMember:
    def __init__(self):
        self.removed = []
        self.roles = []

    async def remove_roles(self, role, reason=None):
        self.removed.append(role)


class _RevocationGuild:
    id = 111

    def __init__(self):
        self.member = None
        self.roles = {}

    def get_member(self, user_id):
        return self.member

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def get_emoji(self, emoji_id):
        return None
