"""Schema 11, the settings cache, and the role-menu editor's contract.

Two failures here are silent and expensive, which is why each has its own test.
A cache that falls back to *nothing* instead of to `config.json` does not refuse
a command, it changes where every command is allowed. And maintenance failing
closed instead of open turns an unreadable setting into an outage — it is one
line away from `is_enabled`, which must fail the other way.
"""

import ast
import asyncio
import json
import subprocess
import shutil
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from pathlib import Path

import database
import settings_cache
import settings_registry
from settings_registry import (
    JSON_SHAPE_ROLE_MENU,
    ROLE_MENU_ENTRY_LIMIT,
    SETTING_DEFINITIONS,
    SettingScope,
    SettingValueType,
    validate_setting_value,
)

ROOT = Path(__file__).resolve().parents[1]


def role_menu_definition():
    """A stand-in definition carrying the role-menu shape.

    The shape had three settings — `game_roles`, `news_roles`, `theme_roles` —
    until schema 12 moved role menus into `managed_messages`, where a guild may
    have any number of them. The shape and its validator survive that move
    because the builder route checks a menu's entries with the same rule, so
    these tests drive the validator directly rather than through a setting that
    no longer exists.
    """
    return settings_registry._setting(
        "role_menu_shape_probe", "community", "role_menus",
        SettingValueType.JSON, {}, json_shape=JSON_SHAPE_ROLE_MENU)


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

    def test_the_file_is_a_read_only_fallback_for_an_unsaved_setting(self):
        """Which is what makes the import a migration, not a prerequisite.

        Pull the change and `config.json` still answers for anything never saved
        in the dashboard; run `scripts/import_config.py` and the rows answer
        instead. Nothing writes the file any more.
        """
        # Never saved: the file answers.
        self.assertEqual([777], settings_cache.setting(111, "economy_channels"))
        database.set_guild_settings(
            111, 9, [{"key": "economy_channels", "value": [999], "revision": 0}])
        asyncio.run(settings_cache.refresh([111], force=True))
        # Saved: the row answers, and the file is left exactly as it was.
        self.assertEqual([999], settings_cache.setting(111, "economy_channels"))
        self.assertEqual([777], self.config["channels"]["economy"])

    def test_the_cache_never_writes_the_configuration_it_reads(self):
        """It used to project rows back into `config` so unconverted readers went
        live. Every reader goes through the cache now, so that bridge is gone —
        and a cache that mutates its own fallback cannot be reasoned about."""
        import inspect
        source = inspect.getsource(settings_cache)
        self.assertNotIn("save_config", source)
        self.assertNotIn("CONFIG_LOCK", source)
        self.assertNotIn("project_into_config", source)


class RoleMenuShapeTests(unittest.TestCase):
    """The editor can express it, so the API has to validate it."""

    def setUp(self):
        self.definition = role_menu_definition()

    def test_the_shape_survived_the_settings_that_declared_it(self):
        """It is the builder's validator now, not a setting's.

        The three role-menu settings are gone; the rule about what a menu entry
        may contain is not, because a menu is still 25 buttons with an 80
        character label and an emoji. If the validator is ever deleted, the
        builder route silently accepts whatever it is handed.
        """
        self.assertIn(JSON_SHAPE_ROLE_MENU,
                      settings_registry._JSON_SHAPE_VALIDATORS)
        self.assertEqual([], [key for key, definition
                              in SETTING_DEFINITIONS.items()
                              if definition.json_shape == JSON_SHAPE_ROLE_MENU],
                         "role menus are managed messages, not settings")

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

    def test_no_json_setting_is_left_as_a_text_box(self):
        """Which is what makes "configured from the dashboard" true.

        Three settings were still hand-written JSON — the level ladder, the LFG
        map and the faction map — so an operator was editing a config file that
        happened to live in a form.
        """
        unshaped = sorted(
            key for key, definition in SETTING_DEFINITIONS.items()
            if definition.value_type is SettingValueType.JSON
            and not definition.json_shape
        )
        self.assertEqual([], unshaped)

    def test_every_shape_has_a_validator_and_a_snowflake_declaration(self):
        """A shape the editor can render but the API cannot check is a hole."""
        import settings_registry
        for shape in (settings_registry.JSON_SHAPE_ITEM_VALUES,
                      settings_registry.JSON_SHAPE_ROLE_MENU,
                      settings_registry.JSON_SHAPE_LEVEL_ROLES,
                      settings_registry.JSON_SHAPE_LFG_CHANNELS,
                      settings_registry.JSON_SHAPE_FACTIONS):
            with self.subTest(shape=shape):
                self.assertIn(shape, settings_registry._JSON_SHAPE_VALIDATORS)
                self.assertIn(shape,
                              settings_registry.JSON_SHAPE_SNOWFLAKE_FIELDS)

    #: A shape whose every setting is `edited_elsewhere` never reaches the
    #: settings form, so it cannot fall back to a JSON text box there — its
    #: editing surface is a page of its own. Listed rather than inferred, so
    #: adding a shape still forces a decision about where it is edited, and the
    #: test below checks the claim rather than trusting it.
    EDITED_ON_THEIR_OWN_PAGE = {"item_values"}

    def test_the_client_renders_every_shape_the_registry_declares(self):
        """Otherwise a setting silently falls back to the JSON text box.

        A shape edited on its own page is exempt from the row editor but not
        from being editable: `item_values` is typed beside each item on the item
        page, the way `shop_price_*` is, which is why it is `edited_elsewhere`.
        The exemption is verified below rather than taken on trust.
        """
        import re
        import settings_registry
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        rendered = set(re.findall(r"^    (\w+): \{$", source, re.MULTILINE))
        for shape in settings_registry.JSON_SHAPE_SNOWFLAKE_FIELDS:
            if shape in self.EDITED_ON_THEIR_OWN_PAGE:
                continue
            with self.subTest(shape=shape):
                self.assertIn(shape, rendered)

    def test_a_shape_exempt_from_the_row_editor_is_edited_somewhere(self):
        """The exemption above is a claim about the interface, so check it.

        Every setting declaring such a shape must be `edited_elsewhere` — that
        is what keeps it off the form — and the client must actually name the
        setting, or it is configurable in code and nowhere else.
        """
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        for shape in self.EDITED_ON_THEIR_OWN_PAGE:
            owners = [key for key, definition in SETTING_DEFINITIONS.items()
                      if definition.json_shape == shape]
            self.assertTrue(owners, f"{shape} is declared by no setting")
            for key in owners:
                with self.subTest(setting=key):
                    self.assertTrue(
                        SETTING_DEFINITIONS[key].edited_elsewhere,
                        "a shape exempt from the row editor would render as a "
                        "raw JSON box on the settings form")
                    self.assertIn(f"'{key}'", source,
                                  "nothing in the client writes this setting")


    def test_the_client_columns_match_what_the_validator_accepts(self):
        """A column the API would reject is an unexplained rejection.

        The editor and the validator are two halves of one contract, and they
        live in different languages, so this pins the field names against each
        other. Nested-entry shapes only: where the entry *is* the id there are no
        field names to agree about.
        """
        import re
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        expected = {
            "role_menu": {"id", "emoji"},
            "factions": {"leader_role_id", "manageable_ids"},
        }
        for shape, fields in expected.items():
            with self.subTest(shape=shape):
                block = source[source.index(f"    {shape}: {{"):]
                block = block[:block.index("\n    },")]
                declared = set(re.findall(r"\{name: '(\w+)'", block))
                self.assertEqual(fields, declared)

    def test_a_shape_whose_entry_is_the_id_declares_one_column(self):
        """`level_roles` and `lfg_channels` map straight to a single role."""
        import re
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        for shape in ("level_roles", "lfg_channels"):
            with self.subTest(shape=shape):
                block = source[source.index(f"    {shape}: {{"):]
                block = block[:block.index("\n    },")]
                self.assertEqual(1, len(re.findall(r"\{name: '(\w+)'", block)))


class LevelLadderShapeTests(unittest.TestCase):
    def setUp(self):
        self.definition = SETTING_DEFINITIONS["level_roles"]

    def test_an_id_is_normalised_and_a_name_survives(self):
        """`check_level_roles` has always accepted both, and an operator who
        configured a name by hand must not have their ladder rejected."""
        value = validate_setting_value(
            self.definition, {"5": "1420070400000000002", "10": "Level 10"})
        self.assertEqual(1420070400000000002, value["5"])
        self.assertGreater(value["5"], 2 ** 53)
        self.assertEqual("Level 10", value["10"])

    def test_a_malformed_ladder_is_refused(self):
        for value, why in (
            ({"nope": 1}, "a milestone that is not a number"),
            ({"1": 1}, "a milestone below level 2"),
            ({"5": None}, "no role at all"),
            ({"5": True}, "a boolean masquerading as an id"),
            ([], "a list rather than a map"),
        ):
            with self.subTest(why=why):
                with self.assertRaises(ValueError):
                    validate_setting_value(self.definition, value)


class LfgAndFactionShapeTests(unittest.TestCase):
    def test_the_lfg_map_is_snowflakes_on_both_sides(self):
        definition = SETTING_DEFINITIONS["lfg_channels"]
        value = validate_setting_value(
            definition, {"1420070400000000004": "1420070400000000005"})
        self.assertEqual({"1420070400000000004": 1420070400000000005}, value)
        for bad in ({"abc": 1}, {"1": "not an id"}, []):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, TypeError)):
                    validate_setting_value(definition, bad)

    def test_a_faction_is_a_leader_plus_the_roles_it_manages(self):
        definition = SETTING_DEFINITIONS["factions"]
        value = validate_setting_value(definition, {
            " alpha ": {"leader_role_id": "1420070400000000006",
                        "manageable_ids": ["1420070400000000003"]}})
        self.assertEqual(
            {"alpha": {"leader_role_id": 1420070400000000006,
                       "manageable_ids": [1420070400000000003]}}, value)

    def test_a_malformed_faction_is_refused(self):
        definition = SETTING_DEFINITIONS["factions"]
        for value, why in (
            ({"": {"leader_role_id": 1}}, "a blank name"),
            ({"a": {"leader_role_id": 1, "extra": 2}}, "an unknown field"),
            ({"a": "not an object"}, "an entry that is not an object"),
            ({"a": {"leader_role_id": 1, "manageable_ids": "nope"}},
             "managed roles that are not a list"),
            ({"a" * 61: {"leader_role_id": 1}}, "a name past the limit"),
        ):
            with self.subTest(why=why):
                with self.assertRaises((ValueError, TypeError)):
                    validate_setting_value(definition, value)

    def test_every_shape_sends_its_ids_as_strings(self):
        """A nested snowflake rounds exactly as readily as a top-level one."""
        import dashboard_api
        cases = {
            "level_roles": ({"5": 1420070400000000002}, "1420070400000000002"),
            "lfg_channels": ({"1": 1420070400000000005}, "1420070400000000005"),
        }
        for key, (value, expected) in cases.items():
            with self.subTest(key=key):
                wired = dashboard_api._wire_value(
                    SETTING_DEFINITIONS[key], value)
                self.assertEqual(expected, list(wired.values())[0])
                self.assertGreater(int(expected), 2 ** 53)
        wired = dashboard_api._wire_value(
            SETTING_DEFINITIONS["factions"],
            {"a": {"leader_role_id": 1420070400000000006,
                   "manageable_ids": [1420070400000000003]}})
        self.assertEqual("1420070400000000006", wired["a"]["leader_role_id"])
        self.assertEqual(["1420070400000000003"], wired["a"]["manageable_ids"])


if __name__ == "__main__":
    unittest.main()


class NoCogReadsTheLegacyFileTests(unittest.TestCase):
    """The conversion is only finished if it cannot quietly come undone.

    Every cog read `config` directly, which meant a dashboard save in a separate
    process needed `?reloadconfig` before the bot saw it, and it meant a setting
    could only ever be single-tenant. They all resolve through `settings_cache`
    now. This walks the cogs' syntax trees rather than grepping, so a comment
    about `config` does not read as a use of it.
    """

    #: `cogs/utils.py` owns the dictionary and the resolver, so it is the one
    #: module allowed to touch it. `main.py` reloads it for `?reloadconfig`.
    ALLOWED = {"utils.py"}

    def _config_subscripts(self, tree):
        """`config[...]` and `config.get(...)`, as the cogs used to write them."""
        uses = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "config"):
                uses.append(node.lineno)
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "config"):
                uses.append(node.lineno)
        return sorted(uses)

    def test_no_cog_reads_the_legacy_configuration_dictionary(self):
        offenders = {}
        for path in sorted((ROOT / "cogs").glob("*.py")):
            if path.name in self.ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            lines = self._config_subscripts(tree)
            if lines:
                offenders[path.name] = lines
        self.assertEqual(
            {}, offenders,
            "read the typed setting through settings_cache "
            "(cogs.utils.guild_setting_sync) instead of config.json",
        )

    def test_no_cog_imports_the_dictionary_it_no_longer_reads(self):
        """An unused import is how the next reader finds it again."""
        offenders = []
        for path in sorted((ROOT / "cogs").glob("*.py")):
            if path.name in self.ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "cogs.utils":
                    if any(alias.name == "config" for alias in node.names):
                        offenders.append(path.name)
        self.assertEqual([], offenders)


class LegacyImportTests(unittest.TestCase):
    """`scripts/import_config.py` runs once against a real installation.

    Both cases here came out of running it against this repository's own
    `config.json` rather than a synthetic one, which is the only reason either
    was found.
    """

    def setUp(self):
        import scripts.import_config as importer
        self.importer = importer
        self.definitions = SETTING_DEFINITIONS

    def test_a_legacy_id_list_is_converted_to_the_declared_type(self):
        """`roles.ignored_users` holds integers and the setting is a string list.

        A snowflake cannot cross to a browser as a number, so the string list is
        correct; `config.json` predates that and holds integers. Refusing would
        make the import unusable on a real installation, and coercing silently
        would hide a genuine mismatch — so it converts only losslessly, and says
        that it did.
        """
        definition = self.definitions["ignored_users"]
        converted = self.importer.coerce_to_declared_type(
            definition, [1420070400000000007, 1420070400000000008])
        self.assertEqual(["1420070400000000007", "1420070400000000008"], converted)
        validate_setting_value(definition, converted)

    def test_a_wrongly_shaped_value_is_left_to_fail_validation(self):
        """A value that is the wrong *shape* is a mistake in the file, and the
        import must refuse rather than invent a conversion for it."""
        definition = self.definitions["ignored_users"]
        self.assertEqual(
            {"not": "a list"},
            self.importer.coerce_to_declared_type(definition, {"not": "a list"}))
        with self.assertRaises(ValueError):
            validate_setting_value(definition, {"not": "a list"})

    def test_a_value_equal_to_the_default_is_not_imported(self):
        """A missing row and a row holding the default are different states.

        The dashboard shows the second as configured, so importing a value that
        equals the default would mark a setting nobody ever set.
        """
        import json
        import tempfile
        from pathlib import Path

        original = self.importer.CONFIG_PATH
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            # `hu` is the shipped default for `language`.
            path.write_text(json.dumps({"bot_settings": {"language": "hu"}}),
                            encoding="utf-8")
            self.importer.CONFIG_PATH = path
            try:
                loaded = self.importer.load_config()
            finally:
                self.importer.CONFIG_PATH = original
        self.assertEqual("hu", self.definitions["language"].default)
        self.assertEqual("hu", loaded["bot_settings"]["language"])


class IgnoredUsersComparisonTests(unittest.TestCase):
    def test_the_ignore_list_is_compared_as_ids_not_as_stored(self):
        """It is a string list and `member.id` is an integer.

        Comparing them directly was False for anything ever saved from the
        dashboard, so the list silently stopped ignoring anyone — a defect that
        was invisible while the value only ever came from `config.json`.
        """
        source = (ROOT / "cogs" / "serverevents.py").read_text(encoding="utf-8")
        self.assertNotIn("if member.id in ignored_users: continue\n"
                         "                result", source)
        self.assertIn("int(entry) for entry in", source)


class EverySettingHasAReaderTests(unittest.TestCase):
    """A registered setting nothing reads is a form that changes nothing.

    Six `gacha_*` settings were a name-for-name duplicate of the banner config —
    the runtime read the banner, the Economy page edited the copy, and the two
    could disagree indefinitely without either being wrong. Nothing detected it
    because a dead setting still validates, still saves, still audits and still
    renders a perfectly good input box.
    """

    #: Generated families whose members are read by a computed key rather than by
    #: their literal name, so a text search cannot find them.
    COMPUTED_PREFIXES = (
        "shop_price_",              # database.get_shop_price, by item key
        "reward_",                  # database.get_reward, by activity
        "warn_threshold_", "warn_action_", "warn_timeout_minutes_",
        "work_tier_", "work_xp_",
    )

    def test_every_setting_is_read_somewhere(self):
        searched = []
        for path in (ROOT.glob("*.py"), (ROOT / "cogs").glob("*.py"),
                     (ROOT / "dashboard").glob("*.js"),
                     (ROOT / "dashboard").glob("*.html"),
                     (ROOT / "scripts").glob("*.py")):
            for candidate in path:
                if candidate.name in ("settings_registry.py",):
                    continue
                searched.append(candidate.read_text(encoding="utf-8"))
        haystack = "\n".join(searched)

        orphans = sorted(
            key for key in SETTING_DEFINITIONS
            if not key.startswith(self.COMPUTED_PREFIXES)
            and key not in haystack
        )
        self.assertEqual(
            [], orphans,
            "these settings are editable and read by nothing; delete them or "
            "wire them up",
        )

    def test_the_computed_families_are_actually_generated(self):
        """The allowlist above must not become a place to hide a dead setting.

        Every prefix it exempts has to match more than one key — a prefix
        matching one key is that key being excused by name.
        """
        for prefix in self.COMPUTED_PREFIXES:
            with self.subTest(prefix=prefix):
                matches = [k for k in SETTING_DEFINITIONS if k.startswith(prefix)]
                self.assertGreater(len(matches), 1, prefix)


class RowEditorReportsCleanTests(unittest.TestCase):
    """A shaped-JSON editor must not mark itself unsaved the moment it opens.

    Three asymmetries between what the editor writes and what the API sends did
    exactly that, and one of them was worse than cosmetic: an unset picker was
    serialised as the id `"0"`, which the API rejects as not a snowflake — and it
    rejects the *whole* patch, so one half-filled row made every change in the
    category fail to save while the section stayed marked unsaved.

    Driven through Node because the logic under test is JavaScript. No DOM is
    involved: the shape spec's `unpack`/`pack` pair and the required-column rule
    are plain data.
    """

    def _node(self):
        return shutil.which("node")

    def test_no_shape_reports_a_change_it_did_not_make(self):
        node = self._node()
        if node is None:
            self.skipTest("node is not installed")

        import dashboard_api

        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        start = source.index("const JSON_ROW_SHAPES = {")
        spec = source[start:source.index("\n};", start) + 3]
        # The comparison the dashboard actually uses, so this harness tests it
        # rather than a copy that could stay green while it broke.
        compare_start = source.index("function canonicalValue(value) {")
        compare = source[compare_start:source.index("\n}", compare_start) + 2]

        # One representative stored value per shape, wired the way the GET wires
        # it, so the fixture exercises the real transform rather than a guess.
        stored = {
            "role_menu": {"LoL": {"id": 1420070400000000001, "emoji": "x"}},
            "level_roles": {"5": 1420070400000000002},
            "lfg_channels": {"1420070400000000003": 1420070400000000004},
            "factions": {"alpha": {"leader_role_id": 1420070400000000005,
                                   "manageable_ids": [1420070400000000006,
                                                      1420070400000000002]}},
        }
        # `role_menu` has no setting since schema 12, so its definition is the
        # stand-in above; the others are real settings.
        definitions = {key: (role_menu_definition() if key == "role_menu"
                             else SETTING_DEFINITIONS[key]) for key in stored}
        wired = {key: dashboard_api._wire_value(definitions[key], value)
                 for key, value in stored.items()}
        shape_of = {key: definitions[key].json_shape for key in stored}

        driver = "\n".join([
            compare,
            spec,
            # Serialised by the dashboard app's own JSON provider, which is what
            # the browser receives. `app.json.sort_keys` defaults to True, so an
            # entry arrives with its fields alphabetical rather than in the order
            # the server built them — and the fixture used to use `json.dumps`,
            # whose default preserves insertion order. It was feeding this test a
            # payload the browser never sees, which is precisely why the test
            # could not see the bug it exists to catch.
            f"const wired = {dashboard_api.app.json.dumps(wired)};",
            f"const SHAPE_OF = {json.dumps(shape_of)};",
            (ROOT / "tests" / "js" / "row_editor_roundtrip.js").read_text(encoding="utf-8"),
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(driver)
            path = handle.name
        try:
            result = subprocess.run([node, path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(0, result.returncode,
                         result.stdout + result.stderr)

    def test_the_required_columns_are_declared(self):
        """The rule only works if the columns that must hold a value say so."""
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        spec = source[source.index("const JSON_ROW_SHAPES = {"):]
        spec = spec[:spec.index("\n};")]
        # One per shape: the role a menu grants, the role a level grants, the
        # role an LFG channel pings, the role that leads a faction.
        self.assertEqual(4, spec.count("required: true"))


class NavigationReachabilityTests(unittest.TestCase):
    """A page that exists must be reachable from the sidebar.

    `updateNavigation` hides a `.nav-item[data-category]` whose category owns no
    settings. The Builders item carried `data-category="builders"` while no
    setting declares that category, so it was hidden on every load and the embed,
    rules and panel builders were unreachable — they read as "missing from the
    dashboard" while being fully implemented.
    """

    def setUp(self):
        self.html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.categories = {definition.category
                           for definition in SETTING_DEFINITIONS.values()}

    def test_a_page_owning_a_section_is_not_gated_on_settings(self):
        import re

        offenders = []
        for page, category in re.findall(
            r'data-page="([a-z-]+)"(?:\s+data-category="([a-z]+)")?', self.html
        ):
            if not category:
                continue
            owns_section = f'id="{page}"' in self.html
            if owns_section and category not in self.categories:
                offenders.append(page)
        self.assertEqual(
            [], offenders,
            "these pages render from their own section but are hidden for "
            "owning no settings; drop their data-category",
        )

    def test_each_game_family_page_declares_the_feature_it_belongs_to(self):
        """A family's own page must react to its master switch.

        Casino, Minigames and Everydle each carry `data-category` and used to
        carry no `data-feature`, so turning the family off did nothing to its
        nav item: `updateNavigation` mutes only items that declare a feature,
        and it hides a `data-category` item only when the category owns no
        content — while `categoryHasVisibleSettings` counts the family's
        sub-toggles as content, which are still declared when the master is off.
        The item therefore looked switched on. It is muted now, not hidden: the
        settings behind it stay reachable so a family can be configured before
        it is enabled.
        """
        import re

        for family in ("casino", "minigames", "everydle"):
            pattern = (r'data-feature="([a-z_]+)"\s+data-page="'
                       + family + r'"')
            found = re.search(pattern, self.html)
            self.assertIsNotNone(
                found, f"the {family} nav item declares no data-feature")
            self.assertEqual(family, found.group(1))
            self.assertIn(family, settings_registry.FEATURE_DEFINITIONS)

    def test_every_settings_category_has_a_nav_entry(self):
        """The mirror image: a category nothing links to is unreachable too."""
        import re

        linked = set(re.findall(r'data-category="([a-z]+)"', self.html))
        self.assertEqual(set(), self.categories - linked,
                         "a settings category with no sidebar entry")

    #: Pages that clear an obligation a member has already paid for. These are
    #: the ones the original incident was really about, and the only ones that
    #: must be reachable whatever is switched off.
    OBLIGATION_PAGES = {"redeems", "audit"}

    def test_a_page_that_clears_an_obligation_is_never_feature_gated(self):
        """The rule that survived, restated once the behaviour changed.

        This began as "an editor page is not hidden by the feature it edits",
        because hiding them once removed staff's only route to **fulfillment
        requests members had already paid for**, gacha-sourced ones included. A
        switched-off feature was therefore muted rather than hidden.

        The operator has since asked for hiding back, on the grounds that making
        a page disappear is what a toggle is *for* — and that is now safe for the
        reason the muting was a workaround: Redeems is its own page and
        deliberately carries no `data-feature`, so the queue that discharges an
        obligation is reachable no matter what is off. What remains true, and is
        what this now asserts, is narrower and more important than the original:
        **a page holding money a member has already spent may not be gated at
        all.** Everything else coming back with its feature is a deliberate
        trade.
        """
        import re

        for page in self.OBLIGATION_PAGES:
            with self.subTest(page=page):
                pattern = re.compile(
                    r'<button[^>]*data-page="' + page + r'"', re.DOTALL)
                buttons = [self.html[m.start():self.html.index(">", m.start())]
                           for m in pattern.finditer(self.html)]
                self.assertTrue(buttons, f"no nav item for {page}")
                for button in buttons:
                    self.assertNotIn(
                        "data-feature", button,
                        f"{page} clears a paid-for obligation and must not be "
                        "hidden by any feature flag")

    def test_a_switched_off_feature_hides_its_page(self):
        """The behaviour the operator asked for, pinned so it does not drift
        back to muting by accident."""
        script = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        start = script.index("function updateNavigation()")
        body = script[start:script.index("\n}", start)]
        code = "\n".join(line for line in body.split("\n")
                         if not line.strip().startswith("//"))
        self.assertIn("dataset.feature", code, "the premise: it still reads them")
        self.assertIn("featureOff", code)
        self.assertNotIn("nav-item-off", code,
                         "muting is gone; a toggle hides its page")

    def test_the_off_notice_names_a_real_locale_family(self):
        """The notice is the only thing telling an operator the feature is off."""
        import json
        import re

        script = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        self.assertIn("dashboard.feature_off_notice", script)
        features = set(re.findall(r'data-feature="([a-z_]+)"', self.html))
        self.assertTrue(features, "the premise: there are feature pages")
        for path in ("hu", "en"):
            catalog = json.loads(
                (ROOT / "locales" / f"{path}.json").read_text(encoding="utf-8")
            )["dashboard"]
            self.assertTrue(catalog.get("feature_off_notice"))
            for feature in features:
                self.assertTrue(catalog["features"].get(feature),
                                f"{path}: no label for feature {feature}")


class StartupWiringTests(unittest.TestCase):
    """Every element the startup wiring binds to must exist in the markup.

    `document.getElementById('shop-item-form').addEventListener(...)` survived
    the markup it referred to being replaced. `getElementById` returns null, the
    property access throws, and because this runs during initialisation the
    throw took the *whole dashboard* down — "Cannot read properties of null",
    and no page at all.

    Nothing caught it. `node --check` parses and does not execute, no test drives
    the page, and the functions it named had been deleted too — so the only
    evidence was an operator opening the dashboard. This is the second time a
    throw during initialisation cost the entire interface; the first was a
    picker built from a definition missing its locale key.
    """

    def setUp(self):
        self.script = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        self.html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")

    def test_the_premise_holds(self):
        """A pattern that matches nothing would pass whatever the markup said."""
        import re
        self.assertGreater(
            len(re.findall(r"getElementById\('([a-z0-9-]+)'\)\.", self.script)), 5)

    def test_every_directly_dereferenced_element_exists(self):
        """Only the unguarded ones. `const x = getElementById(...)` followed by a
        null check is a deliberate optional element and stays allowed."""
        import re

        # An id the client creates itself counts: the gacha banner name field is
        # built in `renderGachaBannerBar` rather than written in the markup.
        created = set(re.findall(r"\.id = '([a-z0-9-]+)'", self.script))
        missing = []
        for name in sorted(set(re.findall(
                r"getElementById\('([a-z0-9-]+)'\)\.", self.script))):
            if f'id="{name}"' not in self.html and name not in created:
                missing.append(name)
        self.assertEqual(
            [], missing,
            "these are dereferenced without a null check but exist in neither "
            "the markup nor the client; getElementById returns null and the "
            "throw takes the whole page down if it happens during startup",
        )
