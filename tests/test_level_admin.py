"""Correcting a member's level by hand.

A member lost hours of voice XP to a rule nobody could see, and nothing in the
bot could give it back — the reward paths are the only writers of XP. These are
the two commands that can, and the invariant that shapes both of them:

**Level is derived from XP and never stored independently.** Every write
recomputes it, so a command that set `users.level` would be undone by the
member's next message. `/setlevel` therefore writes the level's XP floor, and
`/givexp` writes a delta and lets the level fall where it falls.
"""

import ast
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import database


def strip_comments(source: str) -> str:
    """Source without comment lines.

    A comment explaining why a formula is *not* written inline otherwise reads
    as the formula being written inline — this has bitten twice before.
    """
    return "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))


class LevelCurveTests(unittest.TestCase):
    """One definition of the curve, and its inverse."""

    def test_the_floor_and_the_level_are_inverses(self):
        for level in range(1, 201):
            with self.subTest(level=level):
                self.assertEqual(level,
                                 database.level_for_xp(database.xp_for_level(level)))

    def test_level_one_starts_at_zero(self):
        self.assertEqual(0, database.xp_for_level(1))
        self.assertEqual(1, database.level_for_xp(0))

    def test_the_floor_is_the_lowest_xp_for_its_level(self):
        """One XP below a floor must be the level beneath it, or `/setlevel`
        would land a member one short of what it promised."""
        for level in range(2, 60):
            floor = database.xp_for_level(level)
            self.assertEqual(level, database.level_for_xp(floor))
            self.assertEqual(level - 1, database.level_for_xp(floor - 1))

    def test_negative_and_absurd_input_do_not_raise(self):
        """`level_for_xp` runs inside the reward path, where raising would
        swallow whatever the member was doing."""
        self.assertEqual(1, database.level_for_xp(-500))
        self.assertEqual(0, database.xp_for_level(0))
        self.assertEqual(0, database.xp_for_level(-3))

    def test_the_formula_is_written_once(self):
        """It was inline in two places and about to be inline in a third."""
        with open(os.path.join(ROOT, "core", "database.py"), encoding="utf-8") as handle:
            source = strip_comments(handle.read())
        self.assertEqual(
            1, len(re.findall(r"math\.sqrt", source)),
            "the level curve must exist only in `level_for_xp`")


class SetUserExperienceTests(unittest.TestCase):
    """The absolute write both commands rest on."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, "levels.db")
        database.initialize_database()

    def tearDown(self):
        database.DB_PATH = self.original
        self.temp.cleanup()

    def test_it_sets_the_xp_and_the_level_that_follows(self):
        result = database.set_user_experience(7, database.xp_for_level(12))
        self.assertEqual(1210, result["stats"][1])
        self.assertEqual(12, result["stats"][2])
        self.assertEqual(1, result["old_level"])
        self.assertEqual(0, result["old_xp"])

    def test_the_level_column_matches_the_xp_that_was_written(self):
        """The whole reason this writes XP: a level that disagreed with the XP
        would be corrected away by the member's next message."""
        database.set_user_experience(7, 1210)
        with database.get_connection() as conn:
            xp, level = conn.execute(
                "SELECT xp, level FROM users WHERE user_id = 7").fetchone()
        self.assertEqual(1210, xp)
        self.assertEqual(database.level_for_xp(xp), level)

    def test_a_negative_target_is_clamped_to_zero(self):
        database.set_user_experience(7, 500)
        result = database.set_user_experience(7, -100)
        self.assertEqual(0, result["stats"][1])
        self.assertEqual(1, result["stats"][2])
        self.assertEqual(500, result["old_xp"])

    def test_it_creates_a_member_who_has_never_been_seen(self):
        result = database.set_user_experience(999, 40)
        self.assertEqual(3, result["stats"][2])
        self.assertTrue(database.get_user_profile(999))

    def test_it_leaves_the_balance_alone(self):
        """Correcting a level must not touch somebody's money."""
        database.apply_user_delta(7, 250, 0)
        before = database.get_user_profile(7)[2]
        result = database.set_user_experience(7, 1210)
        self.assertEqual(before, result["stats"][0])
        self.assertEqual(before, database.get_user_profile(7)[2])

    def test_setting_the_same_xp_reports_no_change(self):
        database.set_user_experience(7, 1210)
        self.assertFalse(database.set_user_experience(7, 1210)["xp_changed"])

    def test_it_is_not_classified_as_a_read(self):
        """`run_read_sync` refuses anything outside `READ_ONLY_OPERATIONS`, and a
        writer listed there would run on a `query_only` connection."""
        self.assertNotIn("set_user_experience", database.READ_ONLY_OPERATIONS)


class DeltaReportsOldExperienceTests(unittest.TestCase):
    """`old_xp` comes from the writer, not from the caller's arithmetic."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, "levels.db")
        database.initialize_database()

    def tearDown(self):
        database.DB_PATH = self.original
        self.temp.cleanup()

    def test_a_clamped_negative_delta_still_reports_the_real_old_value(self):
        """Subtracting the delta back off the new total is wrong exactly when
        the change hit the floor: 0 - (-500) is 500, and they had 100."""
        database.apply_user_delta(7, 0, 100)
        result = database.apply_user_delta(7, 0, -500)
        self.assertEqual(100, result["old_xp"])
        self.assertEqual(0, result["stats"][1])


class Role:
    def __init__(self, role_id, name="role"):
        self.id = role_id
        self.name = name


class LevelRoleReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """The half that could not exist before.

    `check_level_roles` only ran on a promotion and only ever added, so lowering
    a member left the old level role on — which makes a punishment no punishment
    — and a member dropped below every milestone kept a role outright.
    """

    MILESTONES = {5: 501, 10: 1001, 20: 2001}

    def member(self, *roles):
        return SimpleNamespace(
            id=7, roles=list(roles),
            guild=SimpleNamespace(
                id=99,
                get_role=lambda rid: Role(rid),
                roles=[Role(rid) for rid in self.MILESTONES.values()],
            ),
            add_roles=AsyncMock(), remove_roles=AsyncMock(),
        )

    async def reconcile(self, member, level):
        import cogs.utils as utils

        with patch.object(utils, "guild_setting",
                          AsyncMock(return_value=dict(self.MILESTONES))):
            await utils.reconcile_level_roles(member, level)

    async def test_a_promotion_grants_the_role_and_clears_the_lower_one(self):
        member = self.member(Role(501))
        await self.reconcile(member, 10)
        member.remove_roles.assert_awaited_once()
        self.assertEqual([501], [r.id for r in member.remove_roles.await_args.args])
        self.assertEqual(1001, member.add_roles.await_args.args[0].id)

    async def test_a_demotion_drops_back_to_the_lower_role(self):
        member = self.member(Role(1001))
        await self.reconcile(member, 5)
        self.assertEqual([1001], [r.id for r in member.remove_roles.await_args.args])
        self.assertEqual(501, member.add_roles.await_args.args[0].id)

    async def test_a_demotion_below_every_milestone_removes_them_all(self):
        """Unreachable before: nothing ever ran with a level below the lowest
        milestone, so the member simply kept the role."""
        member = self.member(Role(501))
        await self.reconcile(member, 2)
        self.assertEqual([501], [r.id for r in member.remove_roles.await_args.args])
        member.add_roles.assert_not_awaited()

    async def test_already_correct_makes_no_api_call(self):
        member = self.member(Role(1001))
        await self.reconcile(member, 10)
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    async def test_a_member_with_no_milestone_role_below_them_all_is_untouched(self):
        member = self.member()
        await self.reconcile(member, 2)
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()


class AdminLevelChangeTests(unittest.IsolatedAsyncioTestCase):
    """What the commands do to Discord afterwards."""

    async def apply(self, old_level, new_level):
        import cogs.utils as utils

        member = SimpleNamespace(
            id=7, guild=SimpleNamespace(id=99),
            roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock())
        result = {"stats": (0, 0, new_level, 0, 0), "old_level": old_level,
                  "old_xp": 0, "xp_changed": True}
        announce = AsyncMock()
        with patch.object(utils, "announce_level_up", announce), \
                patch.object(utils, "reconcile_level_roles", AsyncMock()) as roles, \
                patch.object(utils, "guild_setting", AsyncMock(return_value={})), \
                patch.object(utils, "mark_top_ranker_dirty", lambda guild: None):
            applied = await utils.apply_admin_level_change(member, result)
        return announce, roles, applied

    async def test_a_promotion_is_announced(self):
        announce, roles, _ = await self.apply(5, 12)
        announce.assert_awaited_once()
        roles.assert_awaited_once()

    async def test_a_demotion_is_not_announced(self):
        """"@member reached level 3" after a punishment would be worse than
        saying nothing, and the moderation log has the record either way."""
        announce, roles, _ = await self.apply(12, 3)
        announce.assert_not_awaited()
        roles.assert_awaited_once()

    async def test_no_level_change_moves_no_roles(self):
        announce, roles, _ = await self.apply(7, 7)
        announce.assert_not_awaited()
        roles.assert_not_awaited()

    async def test_a_refused_role_is_reported_rather_than_raised(self):
        """The level did change; the operator has to be told the role did not."""
        import cogs.utils as utils
        import discord

        member = SimpleNamespace(
            id=7, guild=SimpleNamespace(id=99),
            roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock())
        result = {"stats": (0, 0, 3, 0, 0), "old_level": 12, "old_xp": 0,
                  "xp_changed": True}
        failing = AsyncMock(side_effect=discord.HTTPException(
            SimpleNamespace(status=403, reason="Forbidden"), "nope"))
        with patch.object(utils, "reconcile_level_roles", failing), \
                patch.object(utils, "mark_top_ranker_dirty", lambda guild: None):
            self.assertFalse(await utils.apply_admin_level_change(member, result))


class LevelChangeReportingTests(unittest.IsolatedAsyncioTestCase):
    """One embed per channel.

    The reply and the moderation-log record carry the same facts, so running the
    command *inside* the log channel posted the identical embed twice — once
    under the "used /setlevel" header and once as a plain bot message. It reads
    exactly like the command firing twice, and it is what this installation saw
    first, because its log channel is the channel staff work in.
    """

    LOG_CHANNEL = 1420070400000000009

    def build(self, invoked_in):
        posts = []
        log_channel = SimpleNamespace(
            id=self.LOG_CHANNEL,
            send=AsyncMock(side_effect=lambda **kw: posts.append(("log", kw))))
        guild = SimpleNamespace(
            id=99,
            get_channel=lambda cid: log_channel if cid == self.LOG_CHANNEL else None)
        ctx = SimpleNamespace(
            guild=guild,
            channel=log_channel if invoked_in == "log"
            else SimpleNamespace(id=1, send=AsyncMock()),
            author=SimpleNamespace(display_name="Woody"),
            send=AsyncMock(side_effect=lambda **kw: posts.append(("reply", kw))),
        )
        return ctx, posts

    async def report(self, invoked_in):
        import cogs.admin as admin
        import cogs.utils as utils

        ctx, posts = self.build(invoked_in)
        member = SimpleNamespace(id=7, mention="<@7>")
        result = {"stats": (0, 1210, 12, 0, 0), "old_level": 6, "old_xp": 337,
                  "xp_changed": True}
        cog = admin.Admin.__new__(admin.Admin)
        with patch.object(utils, "guild_setting",
                          AsyncMock(return_value=self.LOG_CHANNEL)):
            await cog._report_level_change(ctx, member, result, "missed xp", True)
        return posts

    async def test_run_in_the_log_channel_it_posts_once(self):
        posts = await self.report("log")
        self.assertEqual(["reply"], [where for where, _ in posts])

    async def test_run_anywhere_else_the_log_still_gets_its_copy(self):
        """The skip must be narrow: the record has to exist when the reply went
        somewhere nobody reviewing moderation will look."""
        posts = await self.report("elsewhere")
        self.assertEqual(["reply", "log"], [where for where, _ in posts])

    async def test_both_copies_are_the_same_embed(self):
        posts = await self.report("elsewhere")
        self.assertEqual(posts[0][1]["embed"], posts[1][1]["embed"])


class CommandRegistrationTests(unittest.TestCase):
    """The guards that would otherwise be found by an operator."""

    def test_both_commands_have_a_policy(self):
        from core.feature_access import COMMAND_POLICIES

        for name in ("setlevel", "givexp"):
            with self.subTest(name=name):
                self.assertIn(name, COMMAND_POLICIES)
                # `levels`, not `economy`: with the levels system off there is
                # nothing to correct.
                self.assertEqual("levels", COMMAND_POLICIES[name].feature_key)

    def test_both_refuse_a_disabled_levels_feature(self):
        """`update_user_data` silently zeroes an XP change while levels are off,
        so without an explicit check the command reports a success it never
        had."""
        with open(os.path.join(ROOT, "cogs", "admin.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for name in ("setlevel", "givexp"):
            with self.subTest(name=name):
                body = next(node for node in ast.walk(tree)
                            if isinstance(node, ast.AsyncFunctionDef)
                            and node.name == name)
                dumped = ast.dump(body)
                self.assertIn("is_enabled", dumped)
                self.assertIn("level_feature_off", dumped)

    def test_setlevel_writes_the_floor_rather_than_the_level_column(self):
        """The invariant the whole feature rests on."""
        with open(os.path.join(ROOT, "cogs", "admin.py"), encoding="utf-8") as handle:
            source = strip_comments(handle.read())
        start = source.index("async def setlevel")
        body = source[start:source.index("async def givexp")]
        self.assertIn("xp_for_level", body)
        self.assertIn("set_user_experience", body)


if __name__ == "__main__":
    unittest.main()
