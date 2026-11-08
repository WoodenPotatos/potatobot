"""The server event log (decided 2026-11-06): what it posts, and above all
what it must not post — a disabled feature, an unset family, bots' messages,
excluded channels and roles."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG_CHANNEL = 1420070400000000001
IGNORED_CHANNEL = 1420070400000000002
IGNORED_ROLE = 1420070400000000003
CHANNEL = 1420070400000000004


class EventLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import cogs.eventlog  # inside: tests/test_cog_loading.py reloads cogs
        self.module = cogs.eventlog
        self.log = SimpleNamespace(send=AsyncMock())
        self.settings = {
            "event_log_messages_channel": LOG_CHANNEL,
            "event_log_members_channel": LOG_CHANNEL,
            "event_log_voice_channel": None,
            "event_log_server_channel": LOG_CHANNEL,
            "event_log_ignored_channels": [IGNORED_CHANNEL],
            "event_log_ignored_roles": [IGNORED_ROLE],
        }
        self.guild = SimpleNamespace(
            id=10, get_channel=lambda channel_id: self.log if channel_id == LOG_CHANNEL else None)
        self.cog = cogs.eventlog.EventLog(SimpleNamespace(get_guild=lambda _id: self.guild))
        self.patches = [
            patch.object(cogs.eventlog, "guild_setting_sync",
                         side_effect=lambda _guild, key: self.settings[key]),
            patch.object(cogs.eventlog, "is_enabled", return_value=True),
            # SimpleNamespace is not a discord Messageable; the target check is
            # about the channel existing, which the fake does.
            patch.object(cogs.eventlog.discord.abc, "Messageable", SimpleNamespace),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def message(self, content, *, bot=False, channel_id=CHANNEL, roles=()):
        author = SimpleNamespace(bot=bot, mention="<@7>",
                                 roles=[SimpleNamespace(id=role) for role in roles])
        channel = SimpleNamespace(id=channel_id, mention=f"<#{channel_id}>",
                                  parent_id=None, category_id=None)
        return SimpleNamespace(guild=self.guild, author=author, channel=channel,
                               content=content, jump_url="https://discord.com/x")

    def posted(self):
        return [call.kwargs["embed"] for call in self.log.send.await_args_list]

    async def test_an_edit_is_posted_with_both_texts(self):
        await self.cog.on_message_edit(self.message("old"), self.message("new"))
        fields = {field.name: field.value for field in self.posted()[0].fields}
        self.assertEqual("old", fields[self.module.t("eventlog.before")])
        self.assertEqual("new", fields[self.module.t("eventlog.after")])
        # A log never pings anybody, whatever the text says.
        self.assertEqual(discord.AllowedMentions.none().to_dict(),
                         self.log.send.await_args.kwargs["allowed_mentions"].to_dict())

    async def test_what_the_message_log_must_not_post(self):
        cases = {
            "a bot's message": dict(bot=True),
            "an excluded channel": dict(channel_id=IGNORED_CHANNEL),
            "an excluded role": dict(roles=(IGNORED_ROLE,)),
        }
        for why, case in cases.items():
            with self.subTest(why=why):
                await self.cog.on_message_edit(self.message("old", **case),
                                               self.message("new", **case))
        self.assertEqual([], self.posted())

    async def test_nothing_is_posted_while_the_feature_is_off(self):
        with patch.object(self.module, "is_enabled", return_value=False):
            await self.cog.on_message_edit(self.message("old"), self.message("new"))
        self.assertEqual([], self.posted())

    async def test_an_unset_family_logs_nothing(self):
        member = SimpleNamespace(bot=False, guild=self.guild, mention="<@7>", roles=[])
        channel = SimpleNamespace(id=CHANNEL, mention="<#x>", parent_id=None,
                                  category_id=None)
        await self.cog.on_voice_state_update(member, SimpleNamespace(channel=None),
                                             SimpleNamespace(channel=channel))
        self.assertEqual([], self.posted())

    async def test_an_uncached_deletion_is_logged_without_text(self):
        payload = SimpleNamespace(guild_id=10, channel_id=CHANNEL, cached_message=None)
        self.guild.get_channel_or_thread = lambda _id: None
        await self.cog.on_raw_message_delete(payload)
        embed = self.posted()[0]
        self.assertEqual([], embed.fields)
        self.assertIn(f"<#{CHANNEL}>", embed.description)

    async def test_a_new_invite_names_its_creator_and_its_limits(self):
        invite = SimpleNamespace(
            guild=SimpleNamespace(id=10), code="abc123",
            channel=SimpleNamespace(id=CHANNEL), inviter=SimpleNamespace(mention="<@7>"),
            max_uses=0, expires_at=None)
        await self.cog.on_invite_create(invite)
        text = self.posted()[0].description
        for part in ("abc123", "<@7>", self.module.t("eventlog.invite_unlimited"),
                     self.module.t("eventlog.invite_never")):
            self.assertIn(part, text)

    async def test_long_text_is_cut_to_an_embed_field(self):
        clipped = self.module.clip("x" * 5000)
        self.assertEqual(self.module.FIELD_LIMIT, len(clipped))
        self.assertEqual(self.module.t("eventlog.no_text"), self.module.clip("   "))


if __name__ == "__main__":
    unittest.main()
