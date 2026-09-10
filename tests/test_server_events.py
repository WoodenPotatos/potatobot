import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.serverevents import ServerEvents
import settings_cache
from cogs.utils import config, is_premium, voice_reward_block


class MemberAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These read channel settings, which resolve through the process-global
        # cache before falling back to the patched `config`.
        settings_cache.invalidate()

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


class VoiceRewardRuleTests(unittest.TestCase):
    """Who a minute in voice pays, and why it does not.

    A member sat deafened in a voice channel for four hours, earned nothing, and
    found nothing anywhere that said why — so it arrived as a bug report about
    missing XP. The rule is right and stays; what it needed was one definition
    that `/profile` can also read, which is `cogs.utils.voice_reward_block`.
    """

    def member(self, *, deaf=False, self_deaf=False, afk=False, channel=True):
        afk_channel = SimpleNamespace(id=2)
        voice_channel = afk_channel if afk else SimpleNamespace(id=1)
        return SimpleNamespace(
            id=7, bot=False, guild=SimpleNamespace(id=99, afk_channel=afk_channel),
            voice=SimpleNamespace(channel=voice_channel if channel else None,
                                  deaf=deaf, self_deaf=self_deaf),
        )

    def test_a_listening_member_is_paid(self):
        self.assertIsNone(voice_reward_block(self.member()))

    def test_muting_alone_still_pays(self):
        """Deafening is the line, not muting: somebody listening and not talking
        is still present."""
        member = self.member()
        member.voice.mute = True
        member.voice.self_mute = True
        self.assertIsNone(voice_reward_block(member))

    def test_a_deafened_member_is_not_paid(self):
        self.assertEqual("deafened", voice_reward_block(self.member(self_deaf=True)))
        self.assertEqual("deafened", voice_reward_block(self.member(deaf=True)))

    def test_the_afk_channel_is_not_paid(self):
        self.assertEqual("afk_channel", voice_reward_block(self.member(afk=True)))

    def test_a_member_out_of_voice_has_no_problem_to_report(self):
        """`not_in_voice` is the absence of a problem, which is why `/profile`
        renders nothing for it — a notice there would appear on every profile."""
        self.assertEqual("not_in_voice",
                         voice_reward_block(self.member(channel=False)))
        self.assertEqual("not_in_voice",
                         voice_reward_block(SimpleNamespace(voice=None)))


class VoicePremiumRateTests(unittest.TestCase):
    """The premium voice rate follows the premium role, not only a Nitro boost.

    `voice_xp_paycheck` read `member.premium_since`, which is the boost timestamp
    alone, while every other paid path in the bot uses `is_premium` — a premium
    **role** or a boost. So a member given the premium role was paid the normal
    rate in voice and the premium rate everywhere else, silently.
    """

    def test_the_loop_uses_the_shared_definition(self):
        """Asserted against the source with comments stripped, because a comment
        explaining why `premium_since` is *not* used reads as it being used."""
        import ast

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "cogs", "serverevents.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        loop = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "voice_xp_paycheck"
        )
        names = {ast.dump(node) for node in ast.walk(loop)}
        self.assertTrue(
            any("is_premium" in name for name in names),
            "the voice rate must use the shared premium definition")
        self.assertFalse(
            any("premium_since" in name for name in names),
            "the voice rate must not read the boost timestamp directly")

    def test_a_premium_role_holder_is_premium_without_boosting(self):
        """The behaviour the source check stands for: `is_premium` says yes for a
        role holder who has never boosted."""
        member = SimpleNamespace(
            guild=SimpleNamespace(id=99), premium_since=None,
            roles=[SimpleNamespace(id=555)])
        with patch("cogs.utils.guild_setting_sync", return_value=[555]):
            self.assertTrue(is_premium(member))
        with patch("cogs.utils.guild_setting_sync", return_value=[]):
            self.assertFalse(is_premium(member))
