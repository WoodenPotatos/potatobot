"""Schema 11, the settings cache, and the role-menu editor's contract.

Two failures here are silent and expensive, which is why each has its own test.
A cache that falls back to *nothing* instead of to `config.json` does not refuse
a command, it changes where every command is allowed. And maintenance failing
closed instead of open turns an unreadable setting into an outage — it is one
line away from `is_enabled`, which must fail the other way.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

import database
import settings_cache
import settings_registry
from settings_registry import (
    JSON_SHAPE_ROLE_MENU,
    ROLE_MENU_ENTRY_LIMIT,
    SETTING_DEFINITIONS,
    SettingScope,
    validate_setting_value,
)


class InstanceScopeTests(unittest.TestCase):
    """An installation-wide setting must not be storable per guild."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(111, "One")
        database.register_guild(222, "Two")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()
        settings_cache.invalidate()

    def test_the_five_installation_wide_settings_are_declared(self):
        declared = {key for key, definition in SETTING_DEFINITIONS.items()
                    if definition.scope is SettingScope.INSTANCE}
        self.assertEqual(
            {"language", "currency_emoji", "maintenance", "command_prefix",
             "data_retention_days"},
            declared,
        )

    def test_an_instance_write_lands_in_the_table_with_no_guild_column(self):
        database.set_guild_settings(
            111, 9, [{"key": "maintenance", "value": True, "revision": 0}])
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(
                0,
                conn.execute("SELECT COUNT(*) FROM guild_settings "
                             "WHERE setting_key = 'maintenance'").fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute("SELECT COUNT(*) FROM instance_settings "
                             "WHERE setting_key = 'maintenance'").fetchone()[0],
            )

    def test_an_instance_value_is_the_same_for_every_guild(self):
        database.set_guild_settings(
            111, 9, [{"key": "maintenance", "value": True, "revision": 0}])
        for guild_id in (111, 222):
            with self.subTest(guild_id=guild_id):
                self.assertIs(
                    True, database.get_guild_settings(guild_id)["maintenance"]["value"])

    def test_a_guild_value_stays_that_guild_s(self):
        database.set_guild_settings(
            111, 9, [{"key": "ticket_logs", "value": 5, "revision": 0}])
        self.assertIn("ticket_logs", database.get_guild_settings(111))
        self.assertNotIn("ticket_logs", database.get_guild_settings(222))

    def test_an_instance_row_stored_per_guild_is_promoted_once(self):
        """The pre-schema-11 shape, and the collision it can carry."""
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute("DELETE FROM instance_settings")
            conn.executemany(
                "INSERT INTO guild_settings (guild_id, setting_key, value_json, "
                "revision, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(111, "language", '"hu"', 3, 1, "2026-01-01"),
                 (222, "language", '"en"', 1, 1, "2026-05-01")],
            )
            conn.execute("PRAGMA user_version = 10")
            conn.commit()

        database.initialize_database()

        # The most recently updated row wins; there is only one right answer for
        # an installation-wide value and picking silently would be worse.
        self.assertEqual("en", database.get_instance_settings()["language"]["value"])
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(
                0,
                conn.execute("SELECT COUNT(*) FROM guild_settings "
                             "WHERE setting_key = 'language'").fetchone()[0],
            )
        # And re-running it changes nothing.
        database.initialize_database()
        self.assertEqual("en", database.get_instance_settings()["language"]["value"])


class CacheFallbackTests(unittest.TestCase):
    """What the cache answers when it does not know."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(111, "One")
        from cogs.utils import config
        self.config = config
        self.original_config = dict(config)
        config.clear()
        config.update({
            "bot_settings": {"maintenance": False, "language": "hu"},
            "channels": {"economy": [777], "join": 555},
            "roles": {"member": 222},
        })
        settings_cache.invalidate()

    def tearDown(self):
        self.config.clear()
        self.config.update(self.original_config)
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()
        settings_cache.invalidate()

    def test_a_cold_cache_resolves_through_the_file_not_to_nothing(self):
        """The one place copying `feature_access` would be a defect.

        An empty `economy_channels` does not refuse a command, it changes which
        channels every economy command is allowed in.
        """
        self.assertFalse(settings_cache.is_loaded())
        self.assertEqual([777], settings_cache.setting(111, "economy_channels"))
        self.assertEqual(222, settings_cache.setting(111, "member_role"))

    def test_maintenance_fails_open(self):
        """`is_enabled` fails closed. This must not, or a settings problem is
        an outage for the whole installation."""
        self.assertFalse(settings_cache.setting(111, "maintenance"))
        # Even with the file gone as well: the registry default is the floor.
        self.config.clear()
        self.assertFalse(settings_cache.setting(111, "maintenance"))

    def test_maintenance_blocks_fails_open_when_the_cache_raises(self):
        import feature_access
        from types import SimpleNamespace

        guild = SimpleNamespace(id=111)
        actor = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False))
        original = settings_cache.setting
        settings_cache.setting = lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertFalse(feature_access.maintenance_blocks(guild, actor))
        finally:
            settings_cache.setting = original

    def test_a_stored_row_wins_over_the_file(self):
        database.set_guild_settings(
            111, 9, [{"key": "economy_channels", "value": [999], "revision": 0}])
        asyncio.run(settings_cache.refresh([111], force=True))
        self.assertEqual([999], settings_cache.setting(111, "economy_channels"))

    def test_an_unregistered_key_raises_rather_than_guessing(self):
        with self.assertRaises(KeyError):
            settings_cache.setting(111, "no_such_setting")

    def test_the_revision_poll_reloads_only_when_something_moved(self):
        asyncio.run(settings_cache.refresh([111], force=True))
        self.assertFalse(asyncio.run(settings_cache.refresh([111])))
        database.set_guild_settings(
            111, 9, [{"key": "ticket_logs", "value": 5, "revision": 0}])
        self.assertTrue(asyncio.run(settings_cache.refresh([111])))
        self.assertEqual(5, settings_cache.setting(111, "ticket_logs"))

    def test_a_same_process_save_is_visible_immediately(self):
        asyncio.run(settings_cache.refresh([111], force=True))
        result = database.set_guild_settings(
            111, 9, [{"key": "ticket_logs", "value": 7, "revision": 0}])
        settings_cache.apply_changes(111, result)
        self.assertEqual(7, settings_cache.setting(111, "ticket_logs"))

    def test_projection_refreshes_config_without_clearing_untouched_keys(self):
        database.set_guild_settings(111, 9, [
            {"key": "economy_channels", "value": [999], "revision": 0},
            {"key": "maintenance", "value": True, "revision": 0},
        ])
        asyncio.run(settings_cache.refresh([111], force=True))
        self.assertEqual([999], self.config["channels"]["economy"])
        self.assertIs(True, self.config["bot_settings"]["maintenance"])
        # A key with no row keeps what the file held. Clearing it would be a
        # silent reset, and a missing row is not a row holding the default.
        self.assertEqual(555, self.config["channels"]["join"])

    def test_projection_never_writes_the_file(self):
        """While the mirror exists only the dashboard writes it; a second writer
        in another process drops the first one's keys."""
        import inspect
        source = inspect.getsource(settings_cache.project_into_config)
        self.assertNotIn("save_config", source)


class RoleMenuShapeTests(unittest.TestCase):
    """The editor can express it, so the API has to validate it."""

    def setUp(self):
        self.definition = SETTING_DEFINITIONS["game_roles"]

    def test_the_three_role_menus_declare_the_shape(self):
        for key in ("game_roles", "news_roles", "theme_roles"):
            with self.subTest(key=key):
                self.assertEqual(JSON_SHAPE_ROLE_MENU,
                                 SETTING_DEFINITIONS[key].json_shape)

    def test_a_role_id_is_normalised_to_an_integer(self):
        """It arrives as a string because a browser cannot hold it exactly."""
        value = validate_setting_value(
            self.definition, {"LoL": {"id": "1420070400000000001", "emoji": "x"}})
        self.assertEqual(1420070400000000001, value["LoL"]["id"])
        self.assertGreater(value["LoL"]["id"], 2 ** 53,
                           "a regression must not pass by using a small id")

    def test_it_crosses_the_wire_as_a_string(self):
        import dashboard_api
        wired = dashboard_api._wire_value(
            self.definition, {"LoL": {"id": 1420070400000000001, "emoji": "x"}})
        self.assertEqual("1420070400000000001", wired["LoL"]["id"])

    def test_a_malformed_menu_is_refused(self):
        for value, why in (
            ({"x": {"id": 1, "unexpected": 2}}, "unknown field"),
            ({"": {"id": 1}}, "empty label"),
            ({"x": "not an object"}, "entry is not an object"),
            ({"x" * 81: {"id": 1}}, "label past Discord's 80-character limit"),
            ({f"k{i}": {"id": i + 1}
              for i in range(ROLE_MENU_ENTRY_LIMIT + 1)}, "past the 25 components"),
            ({"x": {"id": 1, "emoji": "y" * 65}}, "emoji far too long"),
            ([], "a list rather than a map"),
        ):
            with self.subTest(why=why):
                with self.assertRaises(ValueError):
                    validate_setting_value(self.definition, value)

    def test_the_entry_limit_is_derived_from_discord(self):
        # 25 components per message, and a role menu is one message.
        self.assertEqual(25, ROLE_MENU_ENTRY_LIMIT)

    def test_a_plain_json_setting_is_left_alone(self):
        # Only a declared shape is constrained; `factions` is still free JSON
        # until it gets an editor of its own.
        self.assertIsNone(SETTING_DEFINITIONS["factions"].json_shape)
        self.assertEqual(
            {"anything": [1, 2]},
            validate_setting_value(SETTING_DEFINITIONS["factions"],
                                   {"anything": [1, 2]}))


if __name__ == "__main__":
    unittest.main()
