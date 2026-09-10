"""`/profile` says when voice is paying nothing, and why.

A member sat deafened in a voice channel for hours, earned no coins and no XP,
and there was nowhere in the bot that explained it — so it was reported as
missing XP rather than recognised as the anti-idle rule it is. The rule itself is
unchanged and lives in `cogs.utils.voice_reward_block`, which the paying loop and
this embed both read, so the notice can never describe a rule the bot does not
apply.

The embed is built with stand-ins rather than a Discord connection: what is being
checked is which field appears for which voice state.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def profiles_module():
    """The cog module as it exists *now*, not as it was at import time.

    `tests/test_cog_loading.py` loads and unloads every extension, and
    discord.py's unload removes the module from `sys.modules`. A module-level
    `from cogs.profiles import ProfileView` therefore holds a class whose globals
    belong to a module nothing can reach any more: `patch("cogs.profiles.…")`
    re-imports a fresh copy and patches *that*, so the stand-ins silently do not
    apply and the real functions run. It passed alone and failed in the full run,
    which is the signature of exactly this.

    Resolving the class and the patch targets from one lookup keeps them the same
    object whichever test ran first.
    """
    import cogs.profiles

    return cogs.profiles


class ProfileVoiceNoticeTests(unittest.IsolatedAsyncioTestCase):

    #: What `get_user_profile` returns: level, xp, balance, wins, losses,
    #: streak, last streak update.
    PROFILE_ROW = (5, 260, 1000, 3, 1, 0, None)

    def member(self, *, deaf=False, afk=False, in_voice=True):
        afk_channel = SimpleNamespace(id=2)
        voice = None
        if in_voice:
            voice = SimpleNamespace(
                channel=afk_channel if afk else SimpleNamespace(id=1),
                deaf=False, self_deaf=deaf)
        return SimpleNamespace(
            id=7, display_name="Tester", avatar=None,
            default_avatar=SimpleNamespace(url="https://example.invalid/a.png"),
            guild=SimpleNamespace(id=99, afk_channel=afk_channel),
            voice=voice,
        )

    async def embed_for(self, member, *, voice_rewards=True):
        async def fake_run(function, *args, **kwargs):
            if function.__name__ == "get_user_profile":
                return self.PROFILE_ROW
            if function.__name__ == "get_user_rank":
                return 1
            raise AssertionError(f"unexpected read: {function.__name__}")

        def enabled(guild_id, feature):
            return voice_rewards if feature == "voice_rewards" else False

        profiles = profiles_module()
        with patch.object(profiles.database, "run", side_effect=fake_run), \
                patch.object(profiles, "guild_member_ids", return_value=[7]), \
                patch.object(profiles, "is_enabled", side_effect=enabled), \
                patch.object(profiles, "t", side_effect=lambda key, **kw: key):
            return await profiles.ProfileView(member).generate_embed()

    def field_names(self, embed):
        return [field.name for field in embed.fields]

    def value_for(self, embed, name):
        return next(field.value for field in embed.fields if field.name == name)

    async def test_a_deafened_member_is_told_why(self):
        embed = await self.embed_for(self.member(deaf=True))
        self.assertIn("profiles.voice_blocked_label", self.field_names(embed))
        self.assertEqual("profiles.voice_blocked_deafened",
                         self.value_for(embed, "profiles.voice_blocked_label"))

    async def test_the_afk_channel_is_named_separately(self):
        """Two states with different answers: undeafen, or move. One notice
        covering both would tell half the readers to do the wrong thing."""
        embed = await self.embed_for(self.member(afk=True))
        self.assertEqual("profiles.voice_blocked_afk_channel",
                         self.value_for(embed, "profiles.voice_blocked_label"))

    async def test_a_member_who_is_earning_gets_no_notice(self):
        embed = await self.embed_for(self.member())
        self.assertNotIn("profiles.voice_blocked_label", self.field_names(embed))

    async def test_a_member_out_of_voice_gets_no_notice(self):
        """Otherwise the notice is on every profile in the guild."""
        embed = await self.embed_for(self.member(in_voice=False))
        self.assertNotIn("profiles.voice_blocked_label", self.field_names(embed))

    async def test_nothing_is_said_when_voice_rewards_are_off(self):
        """There is no reward being withheld to explain."""
        embed = await self.embed_for(self.member(deaf=True), voice_rewards=False)
        self.assertNotIn("profiles.voice_blocked_label", self.field_names(embed))


if __name__ == "__main__":
    unittest.main()
