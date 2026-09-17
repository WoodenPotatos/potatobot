"""The command-layer items from the 2026-09-11 audit, each pinned.

Small fixes across nine files; the tests are correspondingly small, and most
read the source they guard -- what matters is that the guard cannot be quietly
removed, which a source assertion catches as well as anything.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core import database
from core.bounded import BoundedCooldownMap, BoundedValueMap

ROOT = Path(__file__).resolve().parents[1]


def _body(source: str, name: str) -> str:
    start = source.index(f"async def {name}(")
    end = source.find("\n    @", start + 1)
    return source[start:end if end > 0 else None]


class ModerationCommandTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "cogs" / "moderation.py").read_text(encoding="utf-8")

    def test_kick_ban_and_timeout_check_the_bots_own_hierarchy(self):
        for name in ("kick", "ban", "timeout"):
            body = _body(self.source, name)
            self.assertIn("ctx.guild.me.top_role", body, name)
            # Not `bot_hierarchy_error`: that key belongs to `/manage` and says
            # "move the bot higher", which is the wrong sentence here. The two
            # collided once and JSON kept whichever came last.
            self.assertIn("moderation.bot_cannot_moderate", body, name)

    def test_kick_ban_and_timeout_escape_and_bound_the_reason(self):
        for name in ("kick", "ban", "timeout"):
            self.assertIn("escape_mentions(reason)[:1024]", _body(self.source, name), name)

    def test_no_bare_except_survives_in_the_moderation_or_admin_cogs(self):
        for path in ("cogs/moderation.py", "cogs/admin.py"):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertEqual([], re.findall(r"^\s*except\s*:\s*$", text, re.M), path)


class AdminCommandTests(unittest.TestCase):
    def test_rules_verify_takes_only_an_https_banner(self):
        source = (ROOT / "cogs" / "admin.py").read_text(encoding="utf-8")
        body = _body(source, "rules_verify")
        self.assertIn('startswith("https://")', body)
        self.assertIn("admin.invalid_banner_url", body)

    def test_awardall_writes_in_chunks(self):
        source = (ROOT / "cogs" / "admin.py").read_text(encoding="utf-8")
        self.assertIn("AWARDALL_CHUNK", _body(source, "awardall"))


class UnknownCommandReplyTests(unittest.TestCase):
    def test_the_reply_is_rate_limited_per_member(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        handler = source[source.index("async def on_command_error("):]
        handler = handler[:handler.index("\n@bot.command")]
        self.assertIn("_unknown_command_times", handler)
        self.assertIn("UNKNOWN_COMMAND_REPLY_COOLDOWN", handler)

    def test_a_missing_token_exits_non_zero(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("sys.exit(1)", source)
        self.assertNotRegex(source, r"^\s*exit\(\)\s*$")


class CooldownReadTests(unittest.TestCase):
    def test_a_database_error_is_raised_not_read_as_no_cooldown(self):
        original = database.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            database.DB_PATH = directory  # a directory is not a database
            try:
                with self.assertRaises(database.DatabaseOperationError):
                    database.get_cooldown(1, "last_valdle")
            finally:
                database.DB_PATH = original

    def test_the_log_lines_are_english(self):
        source = (ROOT / "core" / "database.py").read_text(encoding="utf-8")
        self.assertNotIn("Hiba a", source)


class VoiceModalTests(unittest.TestCase):
    def test_both_modals_share_the_panels_cooldown(self):
        source = (ROOT / "cogs" / "voicemod.py").read_text(encoding="utf-8")
        for cls, nxt in (("class LimitModal", "class RenameModal"),
                         ("class RenameModal", "class BitrateSelect")):
            region = source[source.index(cls):source.index(nxt)]
            self.assertIn("voice_interaction_times", region, cls)


class LfgGameTextTests(unittest.TestCase):
    def test_a_long_game_name_is_refused_before_the_embed(self):
        source = (ROOT / "cogs" / "general.py").read_text(encoding="utf-8")
        self.assertIn("LFG_GAME_TEXT_LIMIT", _body(source, "search"))


class BoundedMapTests(unittest.TestCase):
    def test_server_events_keeps_bounded_maps(self):
        import cogs.serverevents
        cog = cogs.serverevents.ServerEvents(SimpleNamespace())
        self.assertIsInstance(cog.message_cooldowns, BoundedCooldownMap)
        self.assertIsInstance(cog.daily_activity_cache, BoundedValueMap)


class TwitchPollTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self):
        import cogs.socials
        cog = cogs.socials.Socials(SimpleNamespace())
        cog.twitch_client_id, cog.twitch_client_secret = "id", "secret"
        return cog

    async def test_the_app_token_is_minted_once_and_reused(self):
        cog = self._cog()
        cog.request = AsyncMock(return_value=(200, {"access_token": "tok", "expires_in": 3600}))
        self.assertEqual("tok", await cog.get_twitch_token())
        self.assertEqual("tok", await cog.get_twitch_token())
        cog.request.assert_awaited_once()

    async def test_streamers_are_asked_for_in_helix_sized_requests(self):
        import cogs.socials
        cog = self._cog()
        cog.request = AsyncMock(return_value=(200, {"data": [{"user_name": "x"}]}))
        logins = [f"streamer{i}" for i in range(250)]
        streams, complete = await cog._live_streams({}, "url", logins)
        self.assertTrue(complete)
        self.assertEqual(3, cog.request.await_count)
        sizes = [len(call.kwargs["params"]) for call in cog.request.await_args_list]
        self.assertEqual([100, 100, 50], sizes)
        self.assertLessEqual(max(sizes), cogs.socials.TWITCH_LOGINS_PER_REQUEST)

    async def test_a_401_drops_the_cached_token(self):
        cog = self._cog()
        cog._twitch_token, cog._twitch_token_expires_at = "stale", 10**12
        cog.request = AsyncMock(return_value=(401, {}))
        streams, complete = await cog._live_streams({}, "url", ["a"])
        self.assertFalse(complete)
        self.assertIsNone(cog._twitch_token)


class MusicExecutorTests(unittest.TestCase):
    def test_the_executor_outnumbers_the_semaphore(self):
        import cogs.music
        self.assertEqual(4, cogs.music.music_extract_executor._max_workers)
