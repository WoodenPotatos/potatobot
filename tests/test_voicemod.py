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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402

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
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
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
        database.DATA_DIR = self.original_path
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
        self.original_path = database.DATA_DIR
        database.DATA_DIR = self.temp_dir.name
        pg_harness.fresh_database()
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
        database.DATA_DIR = self.original_path
        self.temp_dir.cleanup()

    async def test_an_allowed_target_is_visible_in_the_recreated_room(self):
        owner_id, allowed_id, blocked_id = 7, 42, 43
        database.set_voice_permission(1, owner_id, allowed_id, 1)
        database.set_voice_permission(1, owner_id, blocked_id, 0)

        # The shape `create_voice_channel` really returns: the new panel is
        # rendered from the created channel's own name, limits and overwrites.
        new_channel = _PanelChannel(None)
        new_channel.id = 1000
        new_channel.send = AsyncMock()
        new_channel.delete = AsyncMock()
        guild = _FakeGuild(1, new_channel)
        new_channel.guild = guild
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
        # The panel arrives with its state, so a new room never shows a bare line.
        self.assertIsInstance(new_channel.send.await_args.kwargs.get("embed"), discord.Embed)



class _PanelGuild:
    def __init__(self, guild_id=1):
        self.id = guild_id
        self.default_role = _Role(guild_id)
        self.me = _Entity(id=999)


class _Role(discord.Role):
    """A real `discord.Role` subclass, so the renderer's isinstance check is the
    production one, without a gateway payload behind it."""

    def __init__(self, role_id):  # noqa: D401 - deliberately skips Role.__init__
        self.id = role_id

    def __hash__(self):
        return hash(("role", self.id))

    def __eq__(self, other):
        return isinstance(other, _Role) and other.id == self.id


class _PanelChannel:
    def __init__(self, guild, *, name="Room", user_limit=0, bitrate=64000):
        self.id = 555
        self.guild = guild
        self.name = name
        self.user_limit = user_limit
        self.bitrate = bitrate
        self.overwrites = {}

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())


def _fields(embed):
    return {field.name: field.value for field in embed.fields}


class RoomPanelTests(unittest.IsolatedAsyncioTestCase):
    """The panel shows the room's state, read from the channel's overwrites."""

    def setUp(self):
        import cogs.voicemod
        self.voicemod = cogs.voicemod
        self.t = cogs.voicemod.t
        self.guild = _PanelGuild()
        self.channel = _PanelChannel(self.guild)

    def render(self, *, faction_lock=False, factions=()):
        with patch.object(self.voicemod, "all_faction_role_ids",
                          return_value=set(factions)):
            return self.voicemod.render_room_panel(
                self.channel, 7, faction_lock_enabled=faction_lock)

    def test_an_open_visible_room_with_no_limit(self):
        fields = _fields(self.render())
        self.assertEqual(self.t("voicemod.panel_access_open"),
                         fields[self.t("voicemod.panel_field_access")])
        self.assertEqual(self.t("voicemod.panel_visibility_visible"),
                         fields[self.t("voicemod.panel_field_visibility")])
        self.assertEqual(self.t("voicemod.panel_limit_none"),
                         fields[self.t("voicemod.panel_field_limit")])
        self.assertEqual(self.t("voicemod.panel_bitrate", bitrate=64),
                         fields[self.t("voicemod.panel_field_bitrate")])

    def test_a_locked_hidden_room_says_so(self):
        self.channel.overwrites[self.guild.default_role] = discord.PermissionOverwrite(
            connect=False, view_channel=False)
        self.channel.user_limit = 4
        fields = _fields(self.render())
        self.assertEqual(self.t("voicemod.panel_access_locked"),
                         fields[self.t("voicemod.panel_field_access")])
        self.assertEqual(self.t("voicemod.panel_visibility_hidden"),
                         fields[self.t("voicemod.panel_field_visibility")])
        self.assertEqual("4", fields[self.t("voicemod.panel_field_limit")])

    def test_let_in_members_are_listed_but_never_the_owner_a_bot_or_the_blocked(self):
        owner, friend, bot, blocked = (_Entity(id=7), _Entity(id=42),
                                       _Entity(id=50, bot=True), _Entity(id=60))
        self.channel.overwrites.update({
            owner: discord.PermissionOverwrite(connect=True, manage_channels=True),
            friend: discord.PermissionOverwrite(connect=True, view_channel=True),
            bot: discord.PermissionOverwrite(connect=True),
            blocked: discord.PermissionOverwrite(connect=False),
        })
        self.assertEqual("<@42>", _fields(self.render())[self.t("voicemod.panel_field_permitted")])

    def test_a_long_list_is_capped_with_a_count(self):
        for member_id in range(100, 100 + self.voicemod.PANEL_MEMBER_CAP + 3):
            self.channel.overwrites[_Entity(id=member_id)] = discord.PermissionOverwrite(connect=True)
        value = _fields(self.render())[self.t("voicemod.panel_field_permitted")]
        self.assertTrue(value.endswith(self.t("voicemod.panel_more", count=3)))

    def test_the_faction_lock_shows_its_factions_only_while_the_feature_is_on(self):
        faction = _Role(77)
        self.channel.overwrites[self.guild.default_role] = discord.PermissionOverwrite(connect=False)
        self.channel.overwrites[faction] = discord.PermissionOverwrite(connect=True)
        on = _fields(self.render(faction_lock=True, factions={77}))
        self.assertEqual(self.t("voicemod.panel_access_faction", factions="<@&77>"),
                         on[self.t("voicemod.panel_field_access")])
        off = _fields(self.render(faction_lock=False, factions={77}))
        self.assertEqual(self.t("voicemod.panel_access_locked"),
                         off[self.t("voicemod.panel_field_access")])

    async def _refresh(self, messages, owner_id=7):
        fresh = _PanelChannel(self.guild)
        fresh.overwrites[self.guild.default_role] = discord.PermissionOverwrite(connect=False)

        async def history(**_):
            for message in messages:
                yield message
        fresh.history = history
        self.guild.fetch_channel = AsyncMock(return_value=fresh)
        with patch.object(self.voicemod.database, "run", AsyncMock(return_value=owner_id)), \
                patch.object(self.voicemod, "is_enabled", return_value=False):
            await self.voicemod.refresh_room_panel(self.channel)

    def _message(self, author_id, custom_id):
        button = types.SimpleNamespace(custom_id=custom_id)
        return types.SimpleNamespace(
            author=types.SimpleNamespace(id=author_id),
            components=[types.SimpleNamespace(children=[button])],
            edit=AsyncMock(),
        )

    async def test_refresh_redraws_the_panel_from_the_fetched_channel_and_keeps_its_buttons(self):
        other = self._message(123, "vc_lock")          # not the bot's
        claim = self._message(999, "something_else")   # the bot's, not the panel
        panel = self._message(999, "vc_lock")
        await self._refresh([other, claim, panel])
        other.edit.assert_not_awaited()
        claim.edit.assert_not_awaited()
        panel.edit.assert_awaited_once()
        kwargs = panel.edit.await_args.kwargs
        # The fetched channel is locked; the cached one is not. The panel must
        # show what Discord now enforces, not the state before the click.
        self.assertEqual(self.t("voicemod.panel_access_locked"),
                         _fields(kwargs["embed"])[self.t("voicemod.panel_field_access")])
        self.assertNotIn("view", kwargs, "never re-send the persistent view")

    async def test_refresh_leaves_an_untracked_room_alone(self):
        panel = self._message(999, "vc_lock")
        await self._refresh([panel], owner_id=None)
        panel.edit.assert_not_awaited()

    async def test_a_failed_refresh_never_breaks_the_control_action(self):
        panel = self._message(999, "vc_lock")
        response = types.SimpleNamespace(status=500, reason="boom")
        panel.edit = AsyncMock(side_effect=discord.HTTPException(response, "boom"))
        with self.assertLogs("PotatoBot.VoiceMod", level="WARNING"):
            await self._refresh([panel])

    async def test_a_permit_redraws_the_panel(self):
        target = _Entity(id=42, display_name="Target")
        channel = types.SimpleNamespace(set_permissions=AsyncMock(), id=555)
        view = self.voicemod.UserActionView(channel, target, 7)
        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=7), guild_id=1,
            response=types.SimpleNamespace(edit_message=AsyncMock()))
        with patch.object(self.voicemod, "require_interaction_feature", AsyncMock(return_value=True)), \
                patch.object(self.voicemod, "owns_current_channel", AsyncMock(return_value=True)), \
                patch.object(self.voicemod.database, "run", AsyncMock()), \
                patch.object(self.voicemod, "refresh_room_panel", AsyncMock()) as refresh:
            await view.permit(interaction)
        refresh.assert_awaited_once_with(channel)


if __name__ == "__main__":
    unittest.main()
