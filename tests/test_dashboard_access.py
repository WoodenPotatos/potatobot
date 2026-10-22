"""Role changes reach the dashboard as events, not only as the 30 s refresh.

Two halves, tested apart because they may run in different processes and meet
only in SQLite: `cogs/dashboard_access.py` writes a row when Discord reports a
change that can move dashboard authority, and `dashboard_api` forgets the
cached permission snapshots a row touches, so the next request asks Discord.
"""

import importlib
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import dashboard_api
from core import database

GUILD = 1420070400000000001
OTHER_GUILD = 1420070400000000002
MEMBER = 1420070400000000003


def cog_module():
    """Resolved per call: `test_cog_loading` unloads extensions, which drops
    the module from `sys.modules`, and a module-scope import would then patch a
    copy nothing runs."""
    return importlib.import_module("cogs.dashboard_access")


def member(permissions, user_id=MEMBER):
    return SimpleNamespace(id=user_id, guild=SimpleNamespace(id=GUILD),
                           guild_permissions=permissions)


class BotSideEventTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        module = cog_module()
        self.cog = module.DashboardAccess(SimpleNamespace())
        self.write = AsyncMock()
        self.patch = patch.object(module.database, "run_write", self.write)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def rows(self):
        return [call.args[1] for call in self.write.await_args_list]

    async def test_losing_manage_server_names_the_member(self):
        await self.cog.on_member_update(
            member(discord.Permissions(manage_guild=True)),
            member(discord.Permissions.none()))
        self.assertEqual([[(GUILD, MEMBER)]], self.rows())

    async def test_gaining_administrator_names_the_member_too(self):
        await self.cog.on_member_update(
            member(discord.Permissions.none()),
            member(discord.Permissions(administrator=True)))
        self.assertEqual([[(GUILD, MEMBER)]], self.rows())

    async def test_a_change_that_moves_neither_bit_writes_nothing(self):
        await self.cog.on_member_update(
            member(discord.Permissions(send_messages=True)),
            member(discord.Permissions(send_messages=True, kick_members=True)))
        self.assertEqual([], self.rows())

    async def test_a_departing_manager_is_named_and_anyone_else_is_not(self):
        await self.cog.on_member_remove(member(discord.Permissions(manage_guild=True)))
        await self.cog.on_member_remove(member(discord.Permissions.none(), user_id=5))
        self.assertEqual([[(GUILD, MEMBER)]], self.rows())

    async def test_a_role_losing_its_authority_names_the_whole_guild(self):
        guild = SimpleNamespace(id=GUILD)
        before = SimpleNamespace(guild=guild, permissions=discord.Permissions(administrator=True))
        after = SimpleNamespace(guild=guild, permissions=discord.Permissions.none())
        renamed = SimpleNamespace(guild=guild, permissions=discord.Permissions(administrator=True))
        await self.cog.on_guild_role_update(before, after)
        await self.cog.on_guild_role_update(before, renamed)
        self.assertEqual([[(GUILD, None)]], self.rows())

    async def test_deleting_an_authority_role_names_the_whole_guild(self):
        role = SimpleNamespace(guild=SimpleNamespace(id=GUILD),
                               permissions=discord.Permissions(manage_guild=True))
        await self.cog.on_guild_role_delete(role)
        self.assertEqual([[(GUILD, None)]], self.rows())

    async def test_an_ownership_transfer_names_both_owners(self):
        await self.cog.on_guild_update(SimpleNamespace(id=GUILD, owner_id=1),
                                       SimpleNamespace(id=GUILD, owner_id=2))
        self.assertEqual([[(GUILD, 1), (GUILD, 2)]], self.rows())

    async def test_a_failed_write_is_logged_not_raised(self):
        self.write.side_effect = database.DatabaseOperationError("down")
        await self.cog.on_member_remove(member(discord.Permissions(manage_guild=True)))


class DashboardSideInvalidationTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "dashboard.db")
        database.initialize_database()
        database.register_guild(GUILD, "Guild")
        database.register_guild(OTHER_GUILD, "Other")
        dashboard_api.app.config.update(TESTING=True)
        self.client = dashboard_api.app.test_client()
        self.original_admin_id = dashboard_api.ADMIN_ID
        dashboard_api.ADMIN_ID = "42"
        dashboard_api._rate_limit_events.clear()
        dashboard_api._permission_cache._entries.clear()
        dashboard_api._session_users.clear()
        # Process-global, and every test here has its own database.
        dashboard_api._last_access_invalidation_id = None
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["user_id"] = str(MEMBER)
            session["display"] = {"username": "manager", "avatar": None}
            session["csrf_token"] = "csrf-token"
            session["server_session_id"] = "server-session"
            session["authorized_guild_ids"] = [str(GUILD)]
            session["authenticated_at"] = time.time()
        self.refresh = patch.object(
            dashboard_api, "_refresh_authorized_guilds",
            return_value=([str(GUILD)], []))
        self.refreshes = self.refresh.start()

    def tearDown(self):
        self.refresh.stop()
        dashboard_api.ADMIN_ID = self.original_admin_id
        dashboard_api._permission_cache._entries.clear()
        dashboard_api._session_users.clear()
        dashboard_api._last_access_invalidation_id = None
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def read(self):
        response = self.client.get(f"/api/guilds/{GUILD}/audit")
        self.assertEqual(200, response.status_code)

    def test_the_cache_holds_without_an_event(self):
        """The baseline: within the window, Discord is asked once."""
        self.read()
        self.read()
        self.assertEqual(1, self.refreshes.call_count)

    def test_an_event_naming_the_member_re_asks_discord_at_once(self):
        self.read()
        database.record_access_invalidations([(GUILD, MEMBER)])
        self.read()
        self.assertEqual(2, self.refreshes.call_count)

    def test_an_event_naming_someone_else_leaves_the_session_alone(self):
        self.read()
        database.record_access_invalidations([(GUILD, 5)])
        self.read()
        self.assertEqual(1, self.refreshes.call_count)

    def test_a_guild_wide_event_reaches_sessions_for_that_guild_only(self):
        self.read()
        database.record_access_invalidations([(OTHER_GUILD, None)])
        self.read()
        self.assertEqual(1, self.refreshes.call_count)
        database.record_access_invalidations([(GUILD, None)])
        self.read()
        self.assertEqual(2, self.refreshes.call_count)

    def test_an_event_is_applied_once(self):
        self.read()
        database.record_access_invalidations([(GUILD, MEMBER)])
        self.read()
        self.read()
        self.assertEqual(2, self.refreshes.call_count)

    def test_an_unreadable_table_fails_open_to_the_cache(self):
        """Never closed: an unreadable signal must not lock anybody out."""
        self.read()
        database.record_access_invalidations([(GUILD, MEMBER)])
        def get_access_invalidations_since(after_id):
            raise database.DatabaseOperationError("down")

        with patch.object(dashboard_api.database, "get_access_invalidations_since",
                          get_access_invalidations_since), \
                self.assertLogs("PotatoBot.Dashboard", "WARNING"):
            response = self.client.get(f"/api/guilds/{GUILD}/audit")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, self.refreshes.call_count)


if __name__ == "__main__":
    unittest.main()
