import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.serverevents import ServerEvents
from cogs.utils import config


class MemberAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def make_member(self, channel):
        guild = SimpleNamespace(
            id=123,
            get_channel=lambda channel_id: channel if channel_id in {10, 11} else None,
            get_role=lambda role_id: None,
        )
        return SimpleNamespace(
            id=9,
            guild=guild,
            mention="<@9>",
            avatar=None,
            default_avatar=SimpleNamespace(url="https://example.invalid/avatar.png"),
        )

    async def test_join_announcement_is_sent_before_database_provisioning(self):
        events = []

        class Channel:
            id = 10

            async def send(self, **kwargs):
                events.append("announcement")

        async def database_run(function, *args, **kwargs):
            events.append("database")
            return False

        member = self.make_member(Channel())
        cog = ServerEvents(SimpleNamespace())
        globals_ = cog.on_member_join.__func__.__globals__
        with (
            patch.dict(config, {"channels": {"join": 10}, "roles": {}}, clear=True),
            patch.dict(globals_, {"is_enabled": lambda *args: True}),
            patch.object(globals_["database"], "run", side_effect=database_run),
        ):
            await cog.on_member_join(member)
        self.assertEqual(events[0], "announcement")

    async def test_leave_announcement_uses_member_guild_channel(self):
        sent = []

        class Channel:
            id = 11

            async def send(self, **kwargs):
                sent.append(kwargs)

        member = self.make_member(Channel())
        cog = ServerEvents(SimpleNamespace(get_channel=lambda channel_id: None))
        globals_ = cog.on_member_remove.__func__.__globals__
        with (
            patch.dict(config, {"channels": {"leave": 11}}, clear=True),
            patch.dict(globals_, {"is_enabled": lambda *args: True}),
        ):
            await cog.on_member_remove(member)
        self.assertEqual(len(sent), 1)

    async def test_disabled_announcements_do_not_send(self):
        class Channel:
            async def send(self, **kwargs):
                self.fail("announcement should not be sent")

        member = self.make_member(Channel())
        cog = ServerEvents(SimpleNamespace())
        globals_ = cog.on_member_remove.__func__.__globals__
        with patch.dict(globals_, {"is_enabled": lambda *args: False}):
            await cog.on_member_remove(member)


if __name__ == "__main__":
    unittest.main()
