"""Conditional role rules (decided 2026-11-06): the planner, the listener and
the setting's shape.

The planner is where the behaviour lives, so it is tested as a pure function of
(rules, roles before, roles after); the listener is tested for what it must
refuse; the setting for its validation and its wire round trip.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import settings_registry  # noqa: E402

A, B, C, D = (1420070400000000001, 1420070400000000002,
              1420070400000000003, 1420070400000000004)


def plan(rules, before, after):
    import cogs.rolerules  # inside: tests/test_cog_loading.py reloads cogs
    return cogs.rolerules.plan_role_changes(rules, set(before), set(after))


class PlannerTests(unittest.TestCase):
    GRANT = {"g": {"type": "grants", "when_ids": [A], "then_ids": [B]}}
    REQUIRE = {"r": {"type": "requires", "when_ids": [A], "then_ids": [B]}}
    EXCLUSIVE = {"x": {"type": "exclusive", "when_ids": [], "then_ids": [B, C]}}

    def test_a_grant_gives_its_target(self):
        self.assertEqual(({B}, set()), plan(self.GRANT, [], [A]))

    def test_losing_the_condition_takes_the_grant_back(self):
        self.assertEqual((set(), {B}), plan(self.GRANT, [A, B], [B]))

    def test_a_role_given_by_hand_is_left_alone(self):
        """No condition changed in this update, so a hand-given B stays."""
        self.assertEqual((set(), set()), plan(self.GRANT, [], [B]))

    def test_another_grant_still_giving_it_keeps_the_target(self):
        rules = dict(self.GRANT, g2={"type": "grants", "when_ids": [C], "then_ids": [B]})
        self.assertEqual((set(), set()), plan(rules, [A, B, C], [B, C]))

    def test_a_requirement_removes_the_role_without_its_condition(self):
        self.assertEqual((set(), {B}), plan(self.REQUIRE, [A, B], [B]))
        self.assertEqual((set(), {B}), plan(self.REQUIRE, [], [B]))
        self.assertEqual((set(), set()), plan(self.REQUIRE, [A], [A, B]))

    def test_the_newest_role_of_an_exclusive_group_wins(self):
        self.assertEqual((set(), {B}), plan(self.EXCLUSIVE, [B], [B, C]))
        self.assertEqual((set(), {C}), plan(self.EXCLUSIVE, [C], [B, C]))

    def test_rules_chain_to_a_fixed_point(self):
        """A grants B, B grants C: one change settles both."""
        rules = {"one": {"type": "grants", "when_ids": [A], "then_ids": [B]},
                 "two": {"type": "grants", "when_ids": [B], "then_ids": [C]}}
        self.assertEqual(({B, C}, set()), plan(rules, [], [A]))

    def test_the_bots_own_edit_plans_nothing(self):
        """Discord reports the bot's change as one more update; it must end
        the chain rather than start another round."""
        rules = dict(self.GRANT, **self.EXCLUSIVE)
        to_add, to_remove = plan(rules, [], [A])
        after = ({A} | to_add) - to_remove
        self.assertEqual((set(), set()), plan(rules, [A], after))

    def test_a_contradictory_rule_set_stops_rather_than_loops(self):
        rules = {"give": {"type": "grants", "when_ids": [A], "then_ids": [B]},
                 "need": {"type": "requires", "when_ids": [D], "then_ids": [B]}}
        to_add, to_remove = plan(rules, [], [A])  # returns, whatever it decides
        self.assertIsInstance(to_add, set)
        self.assertIsInstance(to_remove, set)


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import cogs.rolerules
        self.module = cogs.rolerules
        roles = {role_id: SimpleNamespace(id=role_id) for role_id in (A, B, C)}
        self.roles = roles
        self.guild = SimpleNamespace(id=10, get_role=roles.get)
        self.member = SimpleNamespace(id=7, bot=False, guild=self.guild,
                                      roles=[roles[A]],
                                      add_roles=AsyncMock(), remove_roles=AsyncMock())
        self.cog = cogs.rolerules.RoleRules(SimpleNamespace())

    async def apply(self, *, enabled=True, allowed=True,
                    rules=PlannerTests.GRANT, before=()):
        with patch.object(self.module, "is_enabled", return_value=enabled), \
                patch.object(self.module, "maintenance_blocks", return_value=False), \
                patch.object(self.module, "guild_setting_sync", return_value=rules), \
                patch.object(self.module, "can_self_assign_role", return_value=allowed):
            await self.cog.apply_rules(self.member, set(before))

    async def test_a_grant_is_applied(self):
        await self.apply()
        self.assertEqual((self.roles[B],), self.member.add_roles.await_args.args)

    async def test_nothing_moves_while_the_feature_is_off(self):
        await self.apply(enabled=False)
        self.member.add_roles.assert_not_awaited()

    async def test_a_role_the_bot_must_not_grant_is_never_granted(self):
        await self.apply(allowed=False)
        self.member.add_roles.assert_not_awaited()

    async def test_bots_are_left_alone(self):
        self.member.bot = True
        await self.apply()
        self.member.add_roles.assert_not_awaited()


class SettingTests(unittest.TestCase):
    def definition(self):
        return settings_registry.SETTING_DEFINITIONS["role_rules"]

    def test_the_feature_ships_disabled_and_needs_manage_roles(self):
        feature = settings_registry.FEATURE_DEFINITIONS["role_rules"]
        self.assertFalse(feature.default)
        self.assertIn("manage_roles", feature.required_discord_permissions)
        self.assertEqual("role_rules", self.definition().owner_feature)
        self.assertEqual({}, self.definition().default)

    def test_malformed_rules_are_refused(self):
        bad = {
            "unknown type": {"type": "maybe", "when_ids": [A], "then_ids": [B]},
            "no condition": {"type": "grants", "when_ids": [], "then_ids": [B]},
            "own condition": {"type": "requires", "when_ids": [A], "then_ids": [A]},
            "group of one": {"type": "exclusive", "when_ids": [], "then_ids": [B]},
            "group with condition": {"type": "exclusive", "when_ids": [A],
                                     "then_ids": [B, C]},
            "extra field": {"type": "grants", "when_ids": [A], "then_ids": [B], "x": 1},
        }
        for why, rule in bad.items():
            with self.subTest(why=why):
                with self.assertRaises(ValueError):
                    settings_registry.validate_setting_value(self.definition(), {"r": rule})

    def test_the_wire_round_trip_is_exact(self):
        """Wire, then validate, returns what was stored; ids above 2**53 go
        out as strings and come back as the same integers."""
        stored = settings_registry.validate_setting_value(self.definition(), {
            "Verified": {"type": "requires", "when_ids": [str(A)],
                         "then_ids": [str(C), str(B)]}})
        wired = settings_registry.wire_json_shape(
            settings_registry.JSON_SHAPE_ROLE_RULES, stored)
        self.assertEqual([str(B), str(C)], wired["Verified"]["then_ids"])
        self.assertEqual(stored,
                         settings_registry.validate_setting_value(self.definition(), wired))


if __name__ == "__main__":
    unittest.main()
