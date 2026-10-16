"""The help menu answers only a value it actually offered this member.

The options are filtered when the menu is built -- `staff`, `admin` and
`leaders` only for whoever may see them -- but the callback indexed the help
data with whatever value arrived, so a forged component payload showed a
hidden list or raised `KeyError`.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import discord


class HelpSelectTests(unittest.IsolatedAsyncioTestCase):

    async def choose(self, value):
        import cogs.general as general
        select = general.HelpSelect(user_roles=[], is_admin=False, guild_id=None)
        interaction = SimpleNamespace(response=SimpleNamespace(
            edit_message=AsyncMock(), defer=AsyncMock()))
        with patch.object(general, "require_interaction_feature",
                          AsyncMock(return_value=True)), \
                patch.object(discord.ui.Select, "values",
                             new_callable=PropertyMock, return_value=[value]):
            await select.callback(interaction)
        return select, interaction

    async def test_a_hidden_list_not_offered_is_not_shown(self):
        select, interaction = await self.choose("staff")
        self.assertNotIn("staff", {option.value for option in select.options})
        interaction.response.edit_message.assert_not_awaited()

    async def test_an_unknown_value_does_not_raise(self):
        _, interaction = await self.choose("no-such-section")
        interaction.response.edit_message.assert_not_awaited()

    async def test_an_offered_section_still_renders(self):
        import cogs.general as general
        select = general.HelpSelect(user_roles=[], is_admin=False, guild_id=None)
        offered = select.options[0].value
        _, interaction = await self.choose(offered)
        interaction.response.edit_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
