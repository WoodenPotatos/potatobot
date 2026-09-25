"""LeaderboardView: the one view behind /leaderboard's three types.

Before the consolidation, LvlsView, RanksView and /topstreak's inline embed
each had no test coverage at all -- these lock in the behaviour the merge
must preserve: each type queries and renders correctly, and each type's
distinct empty-state message is what actually gets sent when nobody
qualifies.
"""

import os
import tempfile
import unittest

from core import database
from cogs.utils import t
from cogs.profiles import LeaderboardView


GUILD = 4242


class _FakeMember:
    def __init__(self, member_id, display_name, premium_since=None):
        self.id = member_id
        self.display_name = display_name
        self.bot = False
        self.premium_since = premium_since


class _FakeGuild:
    def __init__(self, guild_id, members):
        self.id = guild_id
        self.members = members
        self.icon = None

    def get_member(self, user_id):
        for member in self.members:
            if member.id == user_id:
                return member
        return None


class LeaderboardViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "leaderboard.db")
        database.initialize_database()
        database.register_guild(GUILD, "Leaderboard Guild")
        self.alice = _FakeMember(1, "Alice")
        self.bob = _FakeMember(2, "Bob")
        self.guild = _FakeGuild(GUILD, [self.alice, self.bob])

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def seed(self, user_id, balance=0, xp=0, streak_count=0):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, balance, xp, streak_count) "
                "VALUES (?, ?, ?, ?)", (user_id, balance, xp, streak_count),
            )

    def seed_five_star(self, user_id, pity_before, banner_key=None):
        """One recorded 5-star pull, the only shape `get_top_gacha_luck` reads."""
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO gacha_pulls (guild_id, user_id, banner_key, "
                "banner_revision, rarity, reward_key, reward_json, pity_before, "
                "soft_pity, hard_pity, four_star_guarantee, featured, "
                "featured_guaranteed, created_at) VALUES "
                "(?, ?, ?, 1, 5, 'test_reward', '{}', ?, 0, 0, 0, 0, 0, "
                "'2026-01-01T00:00:00+00:00')",
                (GUILD, user_id, banner_key or database.DEFAULT_GACHA_BANNER_KEY,
                 pity_before),
            )

    async def test_wealth_renders_seeded_balances(self):
        self.seed(self.alice.id, balance=500)
        self.seed(self.bob.id, balance=100)
        embed = await LeaderboardView(self.guild, "wealth").generate_embed()
        self.assertIsNotNone(embed)
        self.assertEqual(t("profiles.ranks_title"), embed.title)
        self.assertIn("Alice", embed.description)

    async def test_levels_renders_seeded_xp(self):
        self.seed(self.alice.id, xp=100)
        embed = await LeaderboardView(self.guild, "levels").generate_embed()
        self.assertIsNotNone(embed)
        self.assertEqual(t("profiles.lvls_title"), embed.title)
        self.assertIn("Alice", embed.description)

    async def test_streaks_renders_seeded_streaks(self):
        self.seed(self.alice.id, streak_count=5)
        embed = await LeaderboardView(self.guild, "streaks").generate_embed()
        self.assertIsNotNone(embed)
        self.assertEqual(t("profiles.topstreak_title"), embed.title)
        self.assertIn("Alice", embed.description)

    async def test_a_streak_of_zero_does_not_qualify(self):
        """get_top_streaks only ranks positive streaks -- a seeded row with
        streak_count=0 must not appear, and with nobody else qualifying the
        board is empty."""
        self.seed(self.alice.id, streak_count=0)
        embed = await LeaderboardView(self.guild, "streaks").generate_embed()
        self.assertIsNone(embed)

    async def test_each_type_has_its_own_empty_message_when_nobody_qualifies(self):
        cases = (
            ("wealth", "profiles.leaderboard_empty"),
            ("levels", "profiles.no_levels_stored"),
            ("streaks", "profiles.no_streaks_yet"),
            ("pity", "profiles.no_pity_data"),
        )
        for leaderboard_type, expected_key in cases:
            with self.subTest(leaderboard_type=leaderboard_type):
                view = LeaderboardView(self.guild, leaderboard_type)
                embed = await view.generate_embed()
                self.assertIsNone(embed)
                self.assertEqual(t(expected_key), view.empty_message())

    async def test_pity_renders_seeded_luck(self):
        """Alice's three 5-stars average pity 1 -- about as lucky as it gets --
        so she ranks and the board names her."""
        self.seed_five_star(self.alice.id, pity_before=0)
        self.seed_five_star(self.alice.id, pity_before=1)
        self.seed_five_star(self.alice.id, pity_before=1)
        embed = await LeaderboardView(self.guild, "pity").generate_embed()
        self.assertIsNotNone(embed)
        self.assertEqual(t("profiles.pity_leaderboard_title"), embed.title)
        self.assertIn("Alice", embed.description)

    async def test_below_the_minimum_five_star_count_does_not_qualify(self):
        """Two 5-stars is one short of the floor -- one early lucky pull must
        not top the board on no evidence."""
        self.seed_five_star(self.alice.id, pity_before=0)
        self.seed_five_star(self.alice.id, pity_before=0)
        embed = await LeaderboardView(self.guild, "pity").generate_embed()
        self.assertIsNone(embed)

    async def test_pity_ranks_by_average_luck_not_pull_count(self):
        """Bob has more 5-stars than Alice but a worse average, so Alice, the
        luckier puller, ranks first."""
        self.seed_five_star(self.alice.id, pity_before=0)
        self.seed_five_star(self.alice.id, pity_before=0)
        self.seed_five_star(self.alice.id, pity_before=1)
        self.seed_five_star(self.bob.id, pity_before=74)
        self.seed_five_star(self.bob.id, pity_before=74)
        self.seed_five_star(self.bob.id, pity_before=74)
        embed = await LeaderboardView(self.guild, "pity").generate_embed()
        self.assertIsNotNone(embed)
        self.assertLess(embed.description.index("Alice"), embed.description.index("Bob"))

    async def test_a_departed_member_drops_off_the_pity_board(self):
        """Matches the other three leaderboards: ranking is filtered to
        current guild membership, even though `gacha_pulls` already carries
        its own real `guild_id`."""
        self.seed_five_star(999, pity_before=0)
        self.seed_five_star(999, pity_before=0)
        self.seed_five_star(999, pity_before=0)
        embed = await LeaderboardView(self.guild, "pity").generate_embed()
        self.assertIsNone(embed)

    async def test_a_different_banner_does_not_count(self):
        self.seed_five_star(self.alice.id, pity_before=0, banner_key="other")
        self.seed_five_star(self.alice.id, pity_before=0, banner_key="other")
        self.seed_five_star(self.alice.id, pity_before=0, banner_key="other")
        embed = await LeaderboardView(self.guild, "pity").generate_embed()
        self.assertIsNone(embed)


if __name__ == "__main__":
    unittest.main()
