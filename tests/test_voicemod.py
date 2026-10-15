"""Temporary voice rooms: the add/remove-user buttons and their replay on rejoin.

`channel.set_permissions(target, **kwargs)` replaces the whole overwrite for
that target rather than merging with whatever was there -- both in discord.py
and at the Discord API level, which treats a permission-overwrite PUT as a
full replace. `permit()` passed only `connect=True`, so a permitted member
could join but Discord would not list the room in their client (and would not
let them go live into it) unless the lobby's own default already made it
visible. `block()` needs no symmetric fix: because it still only passes
`connect=False`, the replace already resets `view_channel` to unset/inherited
as a side effect.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

import discord

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import database

# Not imported at module scope: tests.test_cog_loading unloads every cog,
# which discord.py removes from sys.modules -- a module-level import here
# would go stale and `patch()` would then patch a *different*, freshly
# re-imported module object than the one this file's classes actually call
# into. Each test resolves `cogs.voicemod` fresh instead (see setUp below).


class _Entity:
    """A hashable stand-in for a discord.Member/Role used as an overwrite key.

    types.SimpleNamespace defines __eq__ without __hash__, which makes
    instances unhashable -- exactly what a permission-overwrite dict key
    needs to be.
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeChannel:
    """Enough of a voice channel for set_permissions() to record real intent."""

    def __init__(self, channel_id):
        self.id = channel_id
        self.members = []
        self.overwrites = {}

    async def set_permissions(self, target, *, overwrite=None, **perms):
        if overwrite is not None:
            self.overwrites[target] = overwrite
        else:
            self.overwrites[target] = discord.PermissionOverwrite(**perms)


class UserActionViewVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """permit()/block() directly, isolated from the feature/ownership gate --
    same pattern as ShopHideCommandTests in tests/test_shop_items.py."""

    def setUp(self):
        import cogs.voicemod
        self.voicemod = cogs.voicemod

        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(1, "Guild")

        self.owner_id = 7
        self.target = _Entity(id=42, display_name="Target")
        self.channel = _FakeChannel(555)
        self.view = self.voicemod.UserActionView(self.channel, self.target, self.owner_id)

        self.require_feature_patch = patch.object(
            self.voicemod, "require_interaction_feature", AsyncMock(return_value=True)
        )
        self.require_feature_patch.start()
        self.owns_channel_patch = patch.object(
            self.voicemod, "owns_current_channel", AsyncMock(return_value=True)
        )
        self.owns_channel_patch.start()

    def tearDown(self):
        self.require_feature_patch.stop()
        self.owns_channel_patch.stop()
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _interaction(self):
        return types.SimpleNamespace(
            user=types.SimpleNamespace(id=self.owner_id),
            guild_id=1,
            response=types.SimpleNamespace(edit_message=AsyncMock()),
        )

    async def test_permit_grants_connect_and_view_channel(self):
        await self.view.permit(self._interaction())
        overwrite = self.channel.overwrites[self.target]
        self.assertTrue(overwrite.connect)
        self.assertTrue(
            overwrite.view_channel,
            "connect alone leaves the room unlisted in the target's client",
        )

    async def test_block_only_touches_connect(self):
        """Regression guard: adding view_channel here would stop the replace
        from reverting it to the lobby's inherited default on its own."""
        await self.view.block(self._interaction())
        overwrite = self.channel.overwrites[self.target]
        self.assertFalse(overwrite.connect)
        self.assertIsNone(
            overwrite.view_channel,
            "block() must not pass view_channel; the replace already resets it",
        )

    async def test_block_reverts_a_previously_permitted_target(self):
        """The actual 'revert to inherited default' behavior end to end."""
        await self.view.permit(self._interaction())
        self.assertTrue(self.channel.overwrites[self.target].view_channel)

        await self.view.block(self._interaction())
        self.assertIsNone(self.channel.overwrites[self.target].view_channel)


class _FakeGuild:
    def __init__(self, guild_id, new_channel):
        self.id = guild_id
        self.bitrate_limit = 96000
        self.default_role = _Entity(name="@everyone")
        self.me = _Entity(name="bot")
        self._members = {}
        self.create_voice_channel = AsyncMock(return_value=new_channel)

    def get_member(self, member_id):
        return self._members.get(member_id)

    def get_role(self, role_id):
        return None


class RejoinPermissionReplayTests(unittest.IsolatedAsyncioTestCase):
    """The per-user overwrites saved from a prior room must survive a
    recreate, with the same view_channel decision as the live buttons."""

    def setUp(self):
        import cogs.voicemod
        self.voicemod = cogs.voicemod

        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(1, "Guild")

        self.is_enabled_patch = patch.object(self.voicemod, "is_enabled", return_value=True)
        self.is_enabled_patch.start()
        self.guild_setting_patch = patch.object(
            self.voicemod, "guild_setting_sync", return_value=[999]
        )
        self.guild_setting_patch.start()

    def tearDown(self):
        self.is_enabled_patch.stop()
        self.guild_setting_patch.stop()
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    async def test_an_allowed_target_is_visible_in_the_recreated_room(self):
        owner_id, allowed_id, blocked_id = 7, 42, 43
        database.set_voice_permission(1, owner_id, allowed_id, 1)
        database.set_voice_permission(1, owner_id, blocked_id, 0)

        new_channel = types.SimpleNamespace(
            id=1000, send=AsyncMock(), delete=AsyncMock()
        )
        guild = _FakeGuild(1, new_channel)
        allowed_member = _Entity(id=allowed_id, display_name="Allowed")
        blocked_member = _Entity(id=blocked_id, display_name="Blocked")
        guild._members = {allowed_id: allowed_member, blocked_id: blocked_member}

        owner = _Entity(
            id=owner_id, display_name="Owner", mention="@Owner", guild=guild,
            move_to=AsyncMock(),
        )
        lobby = types.SimpleNamespace(id=999, category=None, overwrites={})
        before = types.SimpleNamespace(channel=None)
        after = types.SimpleNamespace(channel=lobby)

        await self.voicemod.VoiceMods.on_voice_state_update(None, owner, before, after)

        overwrites = guild.create_voice_channel.call_args.kwargs["overwrites"]
        self.assertTrue(overwrites[allowed_member].view_channel)
        self.assertIsNone(overwrites[blocked_member].view_channel)
        self.assertTrue(overwrites[allowed_member].connect)
        self.assertFalse(overwrites[blocked_member].connect)


if __name__ == "__main__":
    unittest.main()
