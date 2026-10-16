"""`/unwarn` and `/untimeout` refuse what `/warn` and `/timeout` refuse.

`/unwarn` had no hierarchy check at all, so any staff member could clear the
record of somebody ranked above them -- or their own. `/untimeout` checked the
moderator's rank but not the bot's, so a member above the bot produced a
Discord 403 instead of the refusal `/timeout` gives.

`/unwarn` also compares the warning's *issuer*: a junior moderator may not clear
a warning a senior one issued. An issuer who has left, the issuer themselves,
an administrator and the host pass, so no warning is left with no way out.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class Role:
    def __init__(self, position):
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position


def member(member_id, position, administrator=False):
    return SimpleNamespace(id=member_id, top_role=Role(position),
                           mention=f"<@{member_id}>", display_name=str(member_id),
                           timeout=AsyncMock(),
                           guild_permissions=SimpleNamespace(administrator=administrator))


class ModerationHierarchyTests(unittest.IsolatedAsyncioTestCase):

    def ctx(self, author, bot_position=100, members=()):
        by_id = {m.id: m for m in members}
        guild = SimpleNamespace(id=123, owner_id=1,
                                me=SimpleNamespace(top_role=Role(bot_position)),
                                get_member=by_id.get)
        author.guild = guild
        return SimpleNamespace(author=author, guild=guild, send=AsyncMock())

    def cog(self):
        import cogs.moderation as moderation
        return moderation, moderation.Moderation.__new__(moderation.Moderation)

    async def test_unwarn_refuses_a_member_at_or_above_the_moderator(self):
        moderation, cog = self.cog()
        moderator = member(5, 50)
        for target in (member(9, 60), member(9, 50), moderator):
            with self.subTest(position=target.top_role.position, is_self=target is moderator):
                ctx = self.ctx(moderator)
                run = AsyncMock()
                with patch.object(moderation.database, "run", run):
                    await moderation.Moderation.unwarn.callback(cog, ctx, target, 3)
                run.assert_not_awaited()
                ctx.send.assert_awaited_once()

    async def test_unwarn_still_works_below_the_moderator(self):
        moderation, cog = self.cog()
        ctx = self.ctx(member(5, 50))
        run = AsyncMock(side_effect=[[(3, "r", "d", None, None)], {"id": 3}, 0])
        with patch.object(moderation.database, "run", run):
            await moderation.Moderation.unwarn.callback(cog, ctx, member(9, 10), 3)
        self.assertIn(moderation.database.remove_warning,
                      [call.args[0] for call in run.await_args_list])

    async def unwarn_issued_by(self, author, issuer_id, present=()):
        """Run `/unwarn` on warning 3, issued by `issuer_id`; True if removed."""
        moderation, cog = self.cog()
        ctx = self.ctx(author, members=present)
        run = AsyncMock(side_effect=[[(3, "r", "d", issuer_id, None)], {"id": 3}, 0])
        with patch.object(moderation.database, "run", run), \
                patch.dict("os.environ", {"ADMIN_DISCORD_ID": "777"}):
            await moderation.Moderation.unwarn.callback(cog, ctx, member(9, 10), 3)
        return moderation.database.remove_warning in [
            call.args[0] for call in run.await_args_list]

    async def test_unwarn_refuses_a_warning_issued_by_a_senior_or_peer(self):
        for issuer in (member(6, 80), member(6, 50)):
            with self.subTest(issuer_position=issuer.top_role.position):
                self.assertFalse(await self.unwarn_issued_by(
                    member(5, 50), issuer.id, present=[issuer]))

    async def test_unwarn_allows_a_warning_issued_by_a_junior(self):
        issuer = member(6, 20)
        self.assertTrue(await self.unwarn_issued_by(member(5, 50), issuer.id, [issuer]))

    async def test_unwarn_allows_the_issuer_an_absent_issuer_and_no_issuer(self):
        self.assertTrue(await self.unwarn_issued_by(member(5, 50), 5, [member(5, 50)]))
        self.assertTrue(await self.unwarn_issued_by(member(5, 50), 6, present=[]))
        self.assertTrue(await self.unwarn_issued_by(member(5, 50), None))

    async def test_unwarn_lets_an_administrator_and_the_host_past_the_issuer(self):
        senior = member(6, 90)
        self.assertTrue(await self.unwarn_issued_by(
            member(5, 50, administrator=True), senior.id, [senior]))
        self.assertTrue(await self.unwarn_issued_by(member(777, 50), senior.id, [senior]))

    async def test_untimeout_refuses_a_member_above_the_bot(self):
        moderation, cog = self.cog()
        target = member(9, 80)
        ctx = self.ctx(member(5, 90), bot_position=70)
        await moderation.Moderation.untimeout.callback(cog, ctx, target)
        target.timeout.assert_not_awaited()
        ctx.send.assert_awaited_once()

    async def test_untimeout_still_works_below_both(self):
        moderation, cog = self.cog()
        target = member(9, 10)
        ctx = self.ctx(member(5, 90))
        await moderation.Moderation.untimeout.callback(cog, ctx, target)
        target.timeout.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
