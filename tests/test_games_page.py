"""The Game channels page, driven in jsdom from the real routes' payloads.

Schema 26 folded LFG's channel map and the per-game patch settings into one
`games` row per guild; this checks the editor that writes it. The registry,
feature and settings payloads come from the real API on a temporary database,
so the stub the harness serves cannot drift from what the server sends.
"""

import sys
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from core import settings_cache

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402
import node_harness  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Discord-epoch ids above 2**53, never live ones.
LOL_CHANNEL = "1420070400000000011"
LOL_ROLE = "1420070400000000012"
VALORANT_CHANNEL = "1420070400000000013"
CUSTOM_ROLE = "1420070400000000014"


class GamesPageHarnessTests(unittest.TestCase):
    def test_the_games_editor_round_trips_and_draws_each_row_correctly(self):
        node = node_harness.require_node(self, jsdom=True)
        from core import database
        import dashboard_api
        from core.supported_games import SUPPORTED_GAME_KEYS

        with tempfile.TemporaryDirectory() as temp_dir:
            original_path, original_admin = database.DB_PATH, dashboard_api.ADMIN_ID
            database.DB_PATH = os.path.join(temp_dir, "fixture.db")
            pg_harness.fresh_database()
            dashboard_api.ADMIN_ID = "42"
            settings_cache.invalidate()
            try:
                database.initialize_database()
                database.register_guild(1, "Test Guild")
                dashboard_api.app.config.update(TESTING=True)
                dashboard_api._rate_limit_events.clear()
                client = dashboard_api.app.test_client()
                with client.session_transaction() as session:
                    session.update({
                        "logged_in": True, "user_id": "42",
                        "display": {"username": "tester", "avatar": None},
                        "csrf_token": "csrf-token",
                        "server_session_id": "server-session",
                        "authorized_guild_ids": ["1"],
                        "authenticated_at": time.time(),
                    })
                saved = client.patch("/api/guilds/1/settings", json={"changes": [{
                    "key": "games", "revision": 0, "value": {
                        "lol": {"role": LOL_ROLE, "channel": LOL_CHANNEL, "lfg": True},
                        "Among Us": {"role": CUSTOM_ROLE},
                    }}]}, headers={"X-CSRF-Token": "csrf-token"})
                self.assertEqual(200, saved.status_code, saved.get_json())
                fixture = {
                    "registry": client.get("/api/settings/registry").get_json()["data"],
                    "features": client.get("/api/guilds/1/features").get_json()["data"],
                    "settings": client.get("/api/guilds/1/settings").get_json()["data"],
                    "supportedGames": list(SUPPORTED_GAME_KEYS),
                    "valorantChannel": VALORANT_CHANNEL,
                    # The shape `_resources_from_bot_cache` sends.
                    "resources": {
                        "channels": [
                            {"id": LOL_CHANNEL, "name": "league", "type": "text",
                             "parent_id": None, "position": 1},
                            {"id": VALORANT_CHANNEL, "name": "valorant", "type": "text",
                             "parent_id": None, "position": 2},
                        ],
                        "roles": [
                            {"id": LOL_ROLE, "name": "LoL", "position": 2,
                             "color": 0, "manageable": True},
                            {"id": CUSTOM_ROLE, "name": "Among Us", "position": 1,
                             "color": 0, "manageable": True},
                        ],
                    },
                }
            finally:
                database.DB_PATH = original_path
                dashboard_api.ADMIN_ID = original_admin
                settings_cache.invalidate()
            fixture_path = os.path.join(temp_dir, "fixture.json")
            with open(fixture_path, "w", encoding="utf-8") as handle:
                json.dump(fixture, handle)
            result = subprocess.run(
                [node, str(ROOT / "tests" / "js" / "games_editor.js"), str(ROOT),
                 fixture_path], capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, f"{result.stdout}\n{result.stderr}")
        # The premise: a run that checked nothing would also exit zero.
        self.assertGreaterEqual(result.stdout.count("ok "), 9, result.stdout)


if __name__ == "__main__":
    unittest.main()
