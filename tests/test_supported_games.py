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


if __name__ == "__main__":
    unittest.main()
