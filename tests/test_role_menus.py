"""Role menus after schema 12, when they stopped being three fixed settings.

Two things here are easy to get wrong and neither shows up as an error. A
persistent view is shared by every message it serves, so a role id baked into a
button is one guild's role answering another guild's click. And a menu key is
operator-supplied text, so a menu a guild does not have must be refused rather
than created on demand.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
from cogs import roleselect


class RoleMenuStorageTests(unittest.TestCase):
    """The cog reads `managed_messages`, not the retired settings."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        # Two guilds, because every bug this file is about only exists with two.
        for guild_id, name in ((111, "first"), (222, "second")):
            database.register_guild(guild_id, name)
        database.save_managed_message(
            111, 1, "role_menu", "games", "Games", 0,
            entries=[{"label": "Valorant", "role_id": 1420070400000000001,
                      "emoji": "🔫"},
                     {"label": "Minecraft", "role_id": 1420070400000000002,
                      "emoji": ""}])
        database.save_managed_message(
            222, 1, "role_menu", "games", "Games", 0,
            entries=[{"label": "Valorant", "role_id": 1420070400000000003,
                      "emoji": "🔫"}])

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_entries_come_from_the_managed_message(self):
        entries = roleselect.menu_entries(111, "games")
        self.assertEqual({"Valorant", "Minecraft"}, set(entries))
        self.assertEqual("🔫", entries["Valorant"]["emoji"])

    def test_a_guild_without_the_menu_gets_nothing(self):
        """Not the other guild's menu, and not a menu created on demand."""
        self.assertEqual({}, roleselect.menu_entries(333, "games"))
        self.assertEqual({}, roleselect.menu_entries(111, "no_such_menu"))
        self.assertEqual([], roleselect.guild_menu_keys(333))

    def test_a_shared_label_resolves_to_the_clicking_guild_s_role(self):
        """The reason the button carries no role id.

        Both guilds call a menu entry "Valorant" and mean different roles. A
        persistent view is one instance for every message, so if the id were on
        the button one of these two guilds would be granting the other's role.
        """
        class Interaction:
            def __init__(self, guild_id):
                self.guild_id = guild_id

        self.assertEqual(1420070400000000001,
                         roleselect.resolve_menu_role(Interaction(111), "Valorant"))
        self.assertEqual(1420070400000000003,
                         roleselect.resolve_menu_role(Interaction(222), "Valorant"))
        self.assertIsNone(
            roleselect.resolve_menu_role(Interaction(222), "Minecraft"),
            "the second guild has no such entry")

    def test_the_routing_instance_takes_the_union_and_carries_no_role(self):
        labels = roleselect.registered_menu_labels()
        self.assertEqual({"Valorant", "Minecraft"}, set(labels))
        for label, data in labels.items():
            with self.subTest(label=label):
                self.assertNotIn("id", data,
                                 "a registered view must hold no guild's role")

    def test_a_menu_added_later_is_found_without_a_restart(self):
        """`guild_menu_keys` is read per click, so a new menu needs no restart."""
        self.assertEqual(["games"], roleselect.guild_menu_keys(111))
        database.save_managed_message(111, 1, "role_menu", "news", "News", 0,
                                      entries=[{"label": "Patch notes",
                                                "role_id": 1420070400000000004,
                                                "emoji": ""}])
        self.assertEqual(["games", "news"], roleselect.guild_menu_keys(111))

    def test_a_posted_message_is_remembered_rather_than_asked_for(self):
        """Which is what let `/update_games` drop its message-id argument."""
        self.assertIsNone(
            database.get_managed_message(111, "role_menu", "games")["message_id"])
        database.record_managed_post(111, "role_menu", "games",
                                     1420070400000000005, 1420070400000000006)
        stored = database.get_managed_message(111, "role_menu", "games")
        self.assertEqual("1420070400000000006", stored["message_id"])
        self.assertEqual("1420070400000000005", stored["channel_id"])
        self.assertGreater(int(stored["message_id"]), 2 ** 53,
                           "a regression must not pass by using a small id")


class RetiredSettingsTests(unittest.TestCase):
    """The three settings are gone, and nothing may quietly read them again."""

    def test_the_settings_are_not_in_the_registry(self):
        from settings_registry import SETTING_DEFINITIONS
        for key in ("game_roles", "news_roles", "theme_roles"):
            with self.subTest(key=key):
                self.assertNotIn(key, SETTING_DEFINITIONS)

    def test_the_cog_reads_no_setting(self):
        """A stale reader is worse than none: it would answer with old roles."""
        source = (ROOT / "cogs" / "roleselect.py").read_text(encoding="utf-8")
        for needle in ("guild_setting_sync", "game_roles", "news_roles",
                       "theme_roles"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
