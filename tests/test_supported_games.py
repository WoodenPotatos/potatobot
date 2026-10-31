"""`core/supported_games.py` is the one ordered list of games this
installation has any special awareness of, and it has to stay in step with
two things nothing else forces it against: `cogs/patchbot.py`'s own game
roster, and the display names both the bot's Discord embeds and the
dashboard client read from `dashboard.game_names.<key>`.
"""

import importlib
import json
import os
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SupportedGameKeysMatchPatchbotTests(unittest.TestCase):
    def test_the_catalog_matches_patchbots_own_roster(self):
        from core.supported_games import SUPPORTED_GAME_KEYS

        patchbot = importlib.import_module("cogs.patchbot")
        self.assertEqual(
            set(SUPPORTED_GAME_KEYS), set(patchbot.SUPPORTED_GAMES),
            "core.supported_games.SUPPORTED_GAME_KEYS has drifted from "
            "cogs.patchbot.SUPPORTED_GAMES; a game added to one must be "
            "added to the other",
        )

    def test_every_key_has_a_matching_feature(self):
        from core.settings_registry import FEATURE_DEFINITIONS
        from core.supported_games import SUPPORTED_GAME_KEYS

        for key in SUPPORTED_GAME_KEYS:
            with self.subTest(key=key):
                self.assertIn(f"patchbot_{key}", FEATURE_DEFINITIONS)


class SupportedGameDisplayNameCoverageTests(unittest.TestCase):
    def test_every_key_has_a_non_empty_dashboard_name_in_every_catalog(self):
        from core.supported_games import SUPPORTED_GAME_KEYS

        for lang in ("hu", "en"):
            catalog = json.loads(
                (ROOT / "locales" / f"{lang}.json").read_text(encoding="utf-8")
            )
            names = catalog.get("dashboard", {}).get("game_names", {})
            for key in SUPPORTED_GAME_KEYS:
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(
                        names.get(key, "").strip(),
                        f"dashboard.game_names.{key} is missing or empty "
                        f"in locales/{lang}.json",
                    )



class GamesReaderTests(unittest.TestCase):
    """The two answers the cogs take from `games` (schema 26)."""

    ROWS = {
        "lol": {"role": 11, "channel": 21, "lfg": True, "patch_channel": 31},
        "valorant": {"role": 12, "channel": 22, "lfg": False, "patch_channel": None},
        "Among Us": {"role": 13, "channel": 23, "lfg": True, "patch_channel": None},
    }

    def test_search_pings_the_role_of_an_lfg_game_in_this_channel(self):
        from core.supported_games import lfg_role_for_channel
        self.assertEqual(11, lfg_role_for_channel(self.ROWS, 21))
        self.assertEqual(13, lfg_role_for_channel(self.ROWS, 23))
        # A game with LFG off answers nothing, even in its own channel.
        self.assertIsNone(lfg_role_for_channel(self.ROWS, 22))
        self.assertIsNone(lfg_role_for_channel(None, 21))

    def test_patch_notes_go_to_the_override_else_the_game_channel(self):
        from core.supported_games import patch_target
        self.assertEqual((31, 11), patch_target(self.ROWS, "lol"))
        self.assertEqual((22, 12), patch_target(self.ROWS, "valorant"))
        self.assertEqual((None, None), patch_target(self.ROWS, "cs2"))
        self.assertEqual((None, None), patch_target({"lol": "broken"}, "lol"))


if __name__ == "__main__":
    unittest.main()
