"""`/membercount`: the split is shown only when the member cache is complete.

The cog module is resolved at call time rather than imported at module scope:
`tests/test_cog_loading.py` unloads every extension, and a module-level import
would hold a copy nothing patches.
"""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def general():
    return importlib.import_module("cogs.general")


class MemberCountTests(unittest.IsolatedAsyncioTestCase):

    def guild(self, chunked):
        members = ([SimpleNamespace(bot=False)] * 7) + ([SimpleNamespace(bot=True)] * 2)
        return SimpleNamespace(
            name="Test Guild", member_count=9, chunked=chunked,
            members=members if chunked else members[:3],
            premium_subscription_count=4, premium_tier=1, icon=None,
        )

    async def run_command(self, guild):
        module = general()
        ctx = SimpleNamespace(guild=guild, send=AsyncMock())
        await module.General.membercount.callback(module.General(MagicMock()), ctx)
        embed = ctx.send.await_args.kwargs["embed"]
        return module, embed, {field.name: field.value for field in embed.fields}

    async def test_a_chunked_guild_splits_humans_and_bots(self):
        module, embed, fields = await self.run_command(self.guild(chunked=True))
        t = module.t
        self.assertEqual("9", fields[t("general.membercount_total")])
        self.assertEqual("7", fields[t("general.membercount_humans")])
        self.assertEqual("2", fields[t("general.membercount_bots")])
        self.assertIn("4", fields[t("general.membercount_boosts")])
        self.assertIsNone(embed.footer.text)

    async def test_an_unchunked_guild_shows_the_total_and_never_a_wrong_split(self):
        module, embed, fields = await self.run_command(self.guild(chunked=False))
        t = module.t
        self.assertEqual("9", fields[t("general.membercount_total")])
        self.assertNotIn(t("general.membercount_humans"), fields)
        self.assertNotIn(t("general.membercount_bots"), fields)
        self.assertEqual(t("general.membercount_partial"), embed.footer.text)


if __name__ == "__main__":
    unittest.main()
