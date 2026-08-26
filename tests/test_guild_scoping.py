"""Behaviour tests for the schema 8 guild dimension.

These cover what the migration tests cannot: that a second guild actually sees
its own values, and that a leaderboard ranks only the members who are present.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import database


class GuildScopingTests(unittest.TestCase):
    def setUp(self):
        self.original_path = database.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(111, "First")
        database.register_guild(222, "Second")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    # -- prices and rewards ------------------------------------------------

    def test_a_guild_without_an_override_reads_the_installation_default(self):
        self.assertEqual(
            database.get_shop_price(111, "premium", 0),
            database.SHOP_DEFAULTS["premium"],
        )
        prices = database.get_shop_prices(111)
        self.assertEqual(prices, database.SHOP_DEFAULTS)

    def test_an_override_shadows_the_default_for_that_guild_only(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO shop_prices (guild_id, item_id, price) VALUES (111, ?, 7)",
                ("premium",),
            )
        self.assertEqual(database.get_shop_price(111, "premium", 0), 7)
        self.assertEqual(
            database.get_shop_price(222, "premium", 0),
            database.SHOP_DEFAULTS["premium"],
        )
        # The bulk read must resolve the same way as the single read.
        self.assertEqual(database.get_shop_prices(111)["premium"], 7)
        self.assertEqual(
            database.get_shop_prices(222)["premium"],
            database.SHOP_DEFAULTS["premium"],
        )

    def test_reward_overrides_are_per_guild(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO rewards (guild_id, activity_id, coin_reward, xp_reward) "
                "VALUES (111, 'daily_normal', 1, 2)"
            )
        self.assertEqual(database.get_reward(111, "daily_normal", 0, 0), (1, 2))
        self.assertEqual(
            database.get_reward(222, "daily_normal", 0, 0),
            database.REWARD_DEFAULTS["daily_normal"],
        )

    def test_negative_override_falls_back_to_the_supplied_default(self):
        """A nonsense stored value must not become a negative payout."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO rewards (guild_id, activity_id, coin_reward, xp_reward) "
                "VALUES (111, 'daily_normal', -5, 2)"
            )
            conn.execute(
                "INSERT INTO shop_prices (guild_id, item_id, price) "
                "VALUES (111, 'premium', -1)"
            )
        self.assertEqual(database.get_reward(111, "daily_normal", 50, 60), (50, 60))
        self.assertEqual(database.get_shop_price(111, "premium", 99), 99)

    # -- voice preferences -------------------------------------------------

    def test_voice_preferences_do_not_leak_between_guilds(self):
        database.set_voice_name(111, 7, "first room")
        database.set_voice_limit(111, 7, 4)
        self.assertEqual(database.get_voice_settings(111, 7)[0], "first room")
        # Nothing saved in the second guild and no legacy default row exists.
        self.assertIsNone(database.get_voice_settings(222, 7))

        database.set_voice_name(222, 7, "second room")
        self.assertEqual(database.get_voice_settings(111, 7)[0], "first room")
        self.assertEqual(database.get_voice_settings(222, 7)[0], "second room")

    def test_a_legacy_voice_row_still_applies_until_a_guild_saves_its_own(self):
        """Pre-schema-8 rows migrate to guild_id 0, so they must keep working."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO voice_settings (guild_id, user_id, channel_name) "
                "VALUES (0, 7, 'legacy room')"
            )
        self.assertEqual(database.get_voice_settings(111, 7)[0], "legacy room")
        database.set_voice_name(111, 7, "scoped room")
        self.assertEqual(database.get_voice_settings(111, 7)[0], "scoped room")
        # The other guild still inherits the legacy default.
        self.assertEqual(database.get_voice_settings(222, 7)[0], "legacy room")

    def test_voice_permissions_are_scoped_with_a_legacy_fallback(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO voice_permissions "
                "(guild_id, owner_id, target_id, is_allowed) VALUES (0, 7, 8, 1)"
            )
        self.assertEqual(database.get_voice_permissions(111, 7), [(8, 1)])
        database.set_voice_permission(111, 7, 9, 0)
        # A guild that has made its own decisions no longer inherits the default.
        self.assertEqual(database.get_voice_permissions(111, 7), [(9, 0)])
        self.assertEqual(database.get_voice_permissions(222, 7), [(8, 1)])

    # -- leaderboards ------------------------------------------------------

    def _seed_users(self):
        with database.get_connection() as conn:
            for user_id, balance, xp, streak in (
                (1, 100, 10, 3), (2, 500, 50, 9), (3, 900, 90, 1),
            ):
                conn.execute(
                    "INSERT INTO users (user_id, balance, xp, streak_count) "
                    "VALUES (?, ?, ?, ?)", (user_id, balance, xp, streak),
                )

    def test_leaderboards_only_rank_the_members_of_the_calling_guild(self):
        self._seed_users()
        self.assertEqual(
            [row[0] for row in database.get_top_levels([1, 2], 10)], [2, 1]
        )
        self.assertEqual(
            [row[0] for row in database.get_top_balances([1, 3], 10)], [3, 1]
        )
        self.assertEqual(
            [row[0] for row in database.get_top_streaks([1, 2], 10)], [2, 1]
        )
        self.assertEqual(database.get_top_xp_user([1, 2]), 2)
        self.assertEqual(database.get_top_xp_user([1]), 1)

    def test_rank_counts_only_fellow_members(self):
        self._seed_users()
        # User 1 is last of three installation-wide but second of two here.
        self.assertEqual(database.get_user_rank(10, [1, 2]), 2)
        self.assertEqual(database.get_user_rank(10, [1]), 1)

    def test_an_empty_guild_ranks_nobody_rather_than_everybody(self):
        self._seed_users()
        self.assertEqual(database.get_top_levels([], 10), [])
        self.assertEqual(database.get_top_balances([], 10), [])
        self.assertIsNone(database.get_top_xp_user([]))
        self.assertEqual(database.get_user_rank(10, []), 1)

    def test_a_member_list_beyond_one_chunk_is_ranked_correctly(self):
        """The id list is chunked for SQLite, so the merge must not lose the top."""
        with database.get_connection() as conn:
            for user_id in range(1, 2001):
                conn.execute(
                    "INSERT INTO users (user_id, balance, xp) VALUES (?, ?, ?)",
                    (user_id, user_id, user_id),
                )
        member_ids = list(range(1, 2001))
        self.assertGreater(len(member_ids), database._MEMBER_ID_CHUNK)
        self.assertEqual(
            [row[0] for row in database.get_top_levels(member_ids, 3)],
            [2000, 1999, 1998],
        )
        self.assertEqual(database.get_top_xp_user(member_ids), 2000)
        self.assertEqual(database.get_user_rank(1998, member_ids), 3)

    # -- rentals -----------------------------------------------------------

    def test_rental_cleanup_never_sees_another_guilds_asset(self):
        database.add_rented_item("emoji", "1", "2030-01-01T00:00:00", 111)
        database.add_rented_item("emoji", "2", "2030-01-01T00:00:00", 222)
        first = {row[2] for row in database.get_all_rentals(111)}
        second = {row[2] for row in database.get_all_rentals(222)}
        self.assertEqual(first, {"1"})
        self.assertEqual(second, {"2"})

    def test_a_legacy_rental_without_provenance_is_offered_to_the_caller(self):
        """Guessing an owner is forbidden, so an unattributed row is returned to
        whichever guild is cleaning up; only that guild can own the asset."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO rented_items (item_type, discord_item_id, expires_at) "
                "VALUES ('emoji', '3', '2030-01-01T00:00:00')"
            )
        self.assertEqual({row[2] for row in database.get_all_rentals(111)}, {"3"})
        self.assertEqual({row[2] for row in database.get_all_rentals(222)}, {"3"})


class LegacyProvenanceTests(unittest.TestCase):
    """A row that carries no guild must not count against every guild.

    Pre-schema-8 warnings have no provenance. Counting them everywhere meant one
    installation's history could push a member over a threshold in a guild it
    never happened in — and a threshold can ban.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        for guild_id, name in ((111, "first"), (222, "second")):
            database.register_guild(guild_id, name)

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def add_legacy_warning(self, user_id):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO warnings (user_id, mod_id, reason, date, guild_id, tag) "
                "VALUES (?, 9, 'legacy', '2026-01-01T00:00:00', NULL, NULL)",
                (user_id,))

    def test_a_legacy_warning_counts_toward_no_threshold(self):
        self.add_legacy_warning(7)
        record = database.record_warning(7, 9, "new one",
                                        "2026-08-26T00:00:00", 111, "general")
        self.assertEqual(1, record["total"],
                         "only the warning filed in this guild may count")
        self.assertEqual(1, record["tag_count"])
        self.assertEqual(0, database.get_warning_count(7, 222))

    def test_a_legacy_warning_is_still_visible_and_removable(self):
        """Counting nowhere must not mean disappearing: invisible history is a
        worse failure than an uncounted row."""
        self.add_legacy_warning(7)
        listed = database.get_warnings(7, 111)
        self.assertEqual(1, len(listed), f"the row must still be listed: {listed}")
        warning_id = listed[0][0]
        self.assertIsNotNone(
            database.remove_warning(warning_id, 7, 111, 9),
            "a legacy row must stay clearable by the guild it counts against")

    def test_the_repair_attributes_them_when_there_is_one_guild(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE guilds SET active = 0 WHERE guild_id = 222")
        self.add_legacy_warning(7)
        with database.get_connection() as conn:
            self.assertEqual(1, database.repair_warning_provenance(conn))
        record = database.record_warning(7, 9, "new one",
                                        "2026-08-26T00:00:00", 111, "general")
        self.assertEqual(2, record["total"],
                         "once attributed, the legacy row counts here")

    def test_the_repair_refuses_to_guess_with_two_guilds(self):
        """Guessing would move somebody's moderation history into a guild it
        never happened in."""
        self.add_legacy_warning(7)
        with database.get_connection() as conn:
            self.assertEqual(0, database.repair_warning_provenance(conn))
            remaining = conn.execute(
                "SELECT COUNT(*) FROM warnings WHERE guild_id IS NULL").fetchone()[0]
        self.assertEqual(1, remaining)

    def test_the_repair_is_idempotent(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE guilds SET active = 0 WHERE guild_id = 222")
        self.add_legacy_warning(7)
        with database.get_connection() as conn:
            self.assertEqual(1, database.repair_warning_provenance(conn))
            self.assertEqual(0, database.repair_warning_provenance(conn))


class RentalCleanupCogTests(unittest.IsolatedAsyncioTestCase):
    """The cog half of rental cleanup, which the query-level test cannot see.

    `get_all_rentals` correctly hands an unattributed rental to every guild — its
    own docstring says no guild deletes another's asset "because `delete_rental`
    is only reached for an asset this guild owns". The cog made that false: a
    missing emoji raised nothing, control fell through, and the row was deleted
    while the asset lived on in the guild that owned it.
    """

    async def asyncSetUp(self):
        from cogs.shop import Shop
        self.deleted = []
        self.shop = Shop.__new__(Shop)          # no bot, no cog machinery needed

        async def fake_run(function, *args):
            if function is database.delete_rental:
                self.deleted.append(args[0])

        self.patcher = patch.object(database, "run", fake_run)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def guild(self, *, has_asset):
        guild = MagicMock()
        asset = None
        if has_asset:
            asset = MagicMock()
            asset.delete = AsyncMock()      # awaited by the cog
        guild.get_emoji.return_value = asset
        return guild

    def expired(self, guild_id):
        past = (datetime.now() - timedelta(days=1)).isoformat()
        return [(42, "emoji", 1420070400000000001, past, guild_id)]

    async def test_an_unattributed_rental_is_kept_when_the_asset_is_elsewhere(self):
        await self.shop._expire_guild_rentals(self.guild(has_asset=False),
                                              self.expired(None))
        self.assertEqual([], self.deleted,
                         "the row is the only record of a live rental")

    async def test_an_unattributed_rental_is_cleared_by_the_guild_that_has_it(self):
        await self.shop._expire_guild_rentals(self.guild(has_asset=True),
                                              self.expired(None))
        self.assertEqual([42], self.deleted)

    async def test_an_attributed_rental_is_cleared_even_if_the_asset_is_gone(self):
        """The ordinary case: somebody deleted the emoji by hand."""
        await self.shop._expire_guild_rentals(self.guild(has_asset=False),
                                              self.expired(111))
        self.assertEqual([42], self.deleted)

    async def test_a_transient_discord_error_keeps_the_row_for_the_next_pass(self):
        guild = MagicMock()
        guild.get_emoji.side_effect = RuntimeError("discord had a moment")
        await self.shop._expire_guild_rentals(guild, self.expired(111))
        self.assertEqual([], self.deleted,
                         "an unknown failure is not evidence the asset is gone")


if __name__ == "__main__":
    unittest.main()
