"""Invite logging: attribution by use counts, the channel it posts to, and the gate.

The cog module is resolved at call time rather than imported at module scope:
`tests/test_cog_loading.py` unloads every extension, which removes the module
from `sys.modules`, and a module-level import would then hold a copy whose
globals nothing patches.
"""

import importlib
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

# Discord-epoch ids above 2**53, never live ones.
GUILD = 1420070400000000001
LOG = 1420070400000000002
SEPARATE = 1420070400000000003
INVITER = 1420070400000000004
MEMBER = 1420070400000000005


def invitelog():
    return importlib.import_module("cogs.invitelog")


def state(uses, max_uses=0, inviter=None):
    return invitelog().InviteState(uses, max_uses, inviter)


class AttributeJoinTests(unittest.TestCase):

    def attribution(self, before, after):
        return invitelog().attribute_join(before, after)

    def test_the_one_invite_whose_count_rose_is_the_answer(self):
        result = self.attribution({"abc": state(3), "xyz": state(1)},
                                  {"abc": state(4), "xyz": state(1)})
        self.assertEqual(("invite", "abc"), (result.kind, result.code))

    def test_an_invite_created_after_the_snapshot_counts_from_zero(self):
        result = self.attribution({"abc": state(3)}, {"abc": state(3), "new": state(1)})
        self.assertEqual(("invite", "new"), (result.kind, result.code))

    def test_the_vanity_link(self):
        vanity = invitelog().VANITY
        result = self.attribution({vanity: state(10), "abc": state(1)},
                                  {vanity: state(11), "abc": state(1)})
        self.assertEqual("vanity", result.kind)

    def test_two_counts_rising_is_unknown_rather_than_a_guess(self):
        result = self.attribution({"abc": state(1), "xyz": state(1)},
                                  {"abc": state(2), "xyz": state(2)})
        self.assertEqual("unknown", result.kind)

    def test_an_invite_spent_to_its_limit_is_the_answer(self):
        """Discord deletes an invite the moment its last use is spent."""
        result = self.attribution({"once": state(0, max_uses=1), "abc": state(5)},
                                  {"abc": state(5)})
        self.assertEqual(("invite", "once"), (result.kind, result.code))

    def test_an_invite_gone_short_of_its_limit_was_deleted_not_used(self):
        result = self.attribution({"tens": state(2, max_uses=10)}, {})
        self.assertEqual("unknown", result.kind)

    def test_no_snapshot_on_either_side_is_unknown(self):
        self.assertEqual("unknown", self.attribution(None, {"abc": state(1)}).kind)
        self.assertEqual("unknown", self.attribution({"abc": state(1)}, None).kind)

    def test_nothing_changed_is_unknown(self):
        self.assertEqual("unknown", self.attribution({"abc": state(1)},
                                                     {"abc": state(1)}).kind)


class ChannelResolutionTests(unittest.TestCase):

    def resolve(self, settings):
        module = invitelog()
        with patch.object(module, "guild_setting_sync",
                          side_effect=lambda guild_id, key: settings.get(key)):
            return module.invite_log_channel_id(GUILD)

    def test_unticked_posts_to_the_bot_log(self):
        self.assertEqual(LOG, self.resolve({
            "bot_log_channel": LOG, "invite_log_use_separate_channel": False,
            "invite_log_channel": SEPARATE}))

    def test_ticked_with_a_channel_posts_there(self):
        self.assertEqual(SEPARATE, self.resolve({
            "bot_log_channel": LOG, "invite_log_use_separate_channel": True,
            "invite_log_channel": SEPARATE}))

    def test_ticked_without_a_channel_falls_back_to_the_bot_log(self):
        self.assertEqual(LOG, self.resolve({
            "bot_log_channel": LOG, "invite_log_use_separate_channel": True,
            "invite_log_channel": None}))

    def test_nothing_set_is_no_channel(self):
        self.assertIsNone(self.resolve({}))


class JoinListenerTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.module = invitelog()
        self.inviter = SimpleNamespace(id=INVITER, name="hostuser",
                                       mention=f"<@{INVITER}>")
        self.channel = SimpleNamespace(id=LOG, send=AsyncMock())
        self.guild = SimpleNamespace(
            id=GUILD, features=[],
            get_channel=lambda cid: self.channel if cid == LOG else None,
            invites=AsyncMock(return_value=[SimpleNamespace(
                code="abc", uses=4, max_uses=0, inviter=self.inviter)]),
            vanity_invite=AsyncMock(return_value=None),
        )
        self.member = SimpleNamespace(
            id=MEMBER, name="newmember", mention=f"<@{MEMBER}>", bot=False,
            guild=self.guild, display_avatar=SimpleNamespace(url="https://cdn/x.png"),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.cog = self.module.InviteLog(MagicMock())
        self.cog.snapshots[GUILD] = {"abc": state(3, inviter=self.inviter)}

    def gates(self, enabled=True, maintenance=False):
        settings = {"bot_log_channel": LOG}
        return (
            patch.object(self.module, "is_enabled", return_value=enabled),
            patch.object(self.module, "maintenance_blocks", return_value=maintenance),
            patch.object(self.module, "guild_setting_sync",
                         side_effect=lambda guild_id, key: settings.get(key)),
        )

    async def join(self, **gates):
        enabled, maintenance, settings = self.gates(**gates)
        with enabled, maintenance, settings:
            await self.cog.on_member_join(self.member)

    async def test_a_join_names_its_inviter_without_pinging_anyone(self):
        await self.join()
        self.channel.send.assert_awaited_once()
        kwargs = self.channel.send.await_args.kwargs
        fields = {field.value for field in kwargs["embed"].fields}
        self.assertTrue(any("hostuser" in value and str(INVITER) in value
                            for value in fields), fields)
        self.assertTrue(any(str(MEMBER) in value for value in fields), fields)
        mentions = kwargs["allowed_mentions"]
        self.assertFalse(mentions.users or mentions.roles or mentions.everyone)
        # The snapshot moved forward, so the next join diffs against this one.
        self.assertEqual(4, self.cog.snapshots[GUILD]["abc"].uses)

    async def test_a_disabled_guild_makes_no_request_and_posts_nothing(self):
        await self.join(enabled=False)
        self.guild.invites.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_maintenance_stops_it(self):
        await self.join(maintenance=True)
        self.guild.invites.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_missing_manage_server_still_logs_the_join_as_unknown(self):
        self.guild.invites.side_effect = discord.Forbidden(
            MagicMock(status=403), "missing permissions")
        await self.join()
        values = {field.value for field in
                  self.channel.send.await_args.kwargs["embed"].fields}
        self.assertIn(self.module.t("invitelog.unknown"), values)
        # The snapshot is kept rather than replaced by nothing.
        self.assertIn("abc", self.cog.snapshots[GUILD])

    async def test_an_invite_created_while_disabled_is_not_recorded(self):
        invite = SimpleNamespace(guild=self.guild, code="new", uses=0, max_uses=0,
                                 inviter=self.inviter)
        with patch.object(self.module, "is_enabled", return_value=False):
            await self.cog.on_invite_create(invite)
        self.assertNotIn("new", self.cog.snapshots[GUILD])
        with patch.object(self.module, "is_enabled", return_value=True):
            await self.cog.on_invite_create(invite)
        self.assertIn("new", self.cog.snapshots[GUILD])


if __name__ == "__main__":
    unittest.main()
