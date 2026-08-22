"""Regression coverage for the emergency stop, mention safety and bounded state.

These invariants are cheap to break silently: maintenance mode used to apply to
only one of several entry points, a nickname could smuggle a role mention into
the one message that permits them, and transient maps grew for the process
lifetime. Each test here pins one of those behaviours.
"""

import ast
import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from bounded import BoundedCooldownMap, BoundedTimestampMap, BoundedValueMap
from cogs.utils import CONFIG_LOCK, config, save_config, snapshot_config
from feature_access import (
    MAINTENANCE_EXEMPT_COMMANDS,
    is_enabled,
    maintenance_blocks,
    require_interaction_feature,
    update_cached_feature,
)
from settings_registry import FEATURE_DEFINITIONS

ROOT = Path(__file__).resolve().parents[1]

ADMIN = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=True))
MEMBER = SimpleNamespace(id=2, guild_permissions=SimpleNamespace(administrator=False))
GUILD = SimpleNamespace(id=987654321)


class FakeResponse:
    def __init__(self, done=False):
        self._done = done
        self.messages = []

    def is_done(self):
        return self._done

    async def send_message(self, content, **kwargs):
        self.messages.append(content)
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append(content)


def fake_interaction(done=False, user=MEMBER):
    return SimpleNamespace(
        id=1,
        guild=GUILD,
        guild_id=GUILD.id,
        user=user,
        response=FakeResponse(done),
        followup=FakeFollowup(),
    )


class MaintenanceGateTests(unittest.TestCase):
    def setUp(self):
        self._previous = config.get("bot_settings", {}).get("maintenance", False)
        config.setdefault("bot_settings", {})["maintenance"] = True

    def tearDown(self):
        config.setdefault("bot_settings", {})["maintenance"] = self._previous

    def test_maintenance_refuses_an_ordinary_member(self):
        self.assertTrue(maintenance_blocks(GUILD, MEMBER, "bal"))

    def test_administrators_keep_access_for_recovery(self):
        self.assertFalse(maintenance_blocks(GUILD, ADMIN, "bal"))

    def test_status_command_stays_reachable(self):
        for command_name in MAINTENANCE_EXEMPT_COMMANDS:
            self.assertFalse(maintenance_blocks(GUILD, MEMBER, command_name))

    def test_component_callbacks_are_refused_during_maintenance(self):
        update_cached_feature(GUILD.id, "economy", True)
        interaction = fake_interaction()
        allowed = asyncio.run(require_interaction_feature(interaction, "economy"))
        self.assertFalse(allowed)
        self.assertEqual(len(interaction.response.messages), 1)

    def test_already_acknowledged_interactions_are_refused_via_followup(self):
        update_cached_feature(GUILD.id, "economy", True)
        interaction = fake_interaction(done=True)
        allowed = asyncio.run(require_interaction_feature(interaction, "economy"))
        self.assertFalse(allowed)
        self.assertEqual(len(interaction.followup.messages), 1)

    def test_maintenance_outranks_an_enabled_feature(self):
        update_cached_feature(GUILD.id, "economy", True)
        self.assertTrue(is_enabled(GUILD.id, "economy"))
        interaction = fake_interaction()
        self.assertFalse(asyncio.run(require_interaction_feature(interaction, "economy")))

    def test_components_without_their_own_flag_still_clear_maintenance(self):
        interaction = fake_interaction()
        self.assertFalse(asyncio.run(require_interaction_feature(interaction, None)))

    def test_normal_operation_allows_the_same_component(self):
        config["bot_settings"]["maintenance"] = False
        update_cached_feature(GUILD.id, "economy", True)
        interaction = fake_interaction()
        self.assertTrue(asyncio.run(require_interaction_feature(interaction, "economy")))


class FeatureGateFailureModeTests(unittest.TestCase):
    def test_unknown_feature_key_fails_closed(self):
        self.assertFalse(is_enabled(GUILD.id, "no_such_feature"))

    def test_every_feature_key_used_in_cogs_is_registered(self):
        unknown = []
        for path in sorted((ROOT / "cogs").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name not in {"is_enabled", "require_interaction_feature"}:
                    continue
                key_node = node.args[-1] if node.args else None
                if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                    continue
                if key_node.value not in FEATURE_DEFINITIONS:
                    unknown.append(f"{path.relative_to(ROOT)}:{node.lineno} {key_node.value}")
        self.assertEqual([], unknown)


class MentionSafetyTests(unittest.TestCase):
    def test_escaping_neutralises_a_role_mention_in_a_nickname(self):
        hostile = "<@&123456789012345678>"
        self.assertNotIn(hostile, discord.utils.escape_mentions(hostile))

    def test_lfg_search_escapes_every_user_supplied_field(self):
        source = (ROOT / "cogs" / "general.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        escaped_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "search":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and ast.unparse(inner).startswith(
                    "discord.utils.escape_mentions("
                ):
                    escaped_calls += 1
        # One for the pinged branch nickname, two for the free-text branch.
        self.assertGreaterEqual(escaped_calls, 3)

    def test_role_pings_are_restricted_to_the_configured_lfg_role(self):
        source = (ROOT / "cogs" / "general.py").read_text(encoding="utf-8")
        self.assertIn("roles=[role]", source)
        self.assertNotIn("roles=True", source)


class BoundedContainerTests(unittest.TestCase):
    def test_cooldown_map_never_exceeds_its_capacity(self):
        cooldowns = BoundedCooldownMap(max_age=0.0, max_entries=8)
        for index in range(500):
            cooldowns[index] = float(index)
        self.assertLessEqual(len(cooldowns), 8)

    def test_cooldown_map_refresh_does_not_evict(self):
        cooldowns = BoundedCooldownMap(max_age=10_000, max_entries=4)
        for index in range(4):
            cooldowns[index] = float(index)
        cooldowns[0] = 99.0
        self.assertEqual(len(cooldowns), 4)
        self.assertEqual(cooldowns[0], 99.0)

    def test_value_map_evicts_oldest_first(self):
        names = BoundedValueMap(max_entries=3)
        for index in range(10):
            names[index] = f"name-{index}"
        self.assertEqual(len(names), 3)
        self.assertEqual(sorted(names), [7, 8, 9])

    def test_value_map_reinsert_keeps_one_entry(self):
        names = BoundedValueMap(max_entries=3)
        names[1] = "first"
        names[1] = "second"
        self.assertEqual(len(names), 1)
        self.assertEqual(names[1], "second")

    def test_timestamp_map_discards_unclaimed_entries(self):
        timings = BoundedTimestampMap(max_age=0.0, max_entries=16)
        for index in range(1000):
            timings.start(index, float(index))
        self.assertLessEqual(len(timings), 16)

    def test_timestamp_map_round_trip(self):
        timings = BoundedTimestampMap()
        timings.start(7, 1.5)
        self.assertEqual(timings.pop(7), 1.5)
        self.assertIsNone(timings.pop(7))

    def test_interaction_timing_map_is_bounded(self):
        import feature_access

        self.assertIsInstance(feature_access._INTERACTION_STARTED, BoundedTimestampMap)


class ConfigSnapshotTests(unittest.TestCase):
    def setUp(self):
        import cogs.utils as utils

        self.utils = utils
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = utils.CONFIG_PATH
        self.original_root = utils.ROOT_DIR
        self.original_config = json.loads(json.dumps(dict(config)))
        # save_config stages its temp file in ROOT_DIR before os.replace, so both
        # must move together or the rename crosses a filesystem boundary.
        utils.ROOT_DIR = self.temp_dir.name
        utils.CONFIG_PATH = os.path.join(self.temp_dir.name, "config.json")

    def tearDown(self):
        self.utils.CONFIG_PATH = self.original_path
        self.utils.ROOT_DIR = self.original_root
        config.clear()
        config.update(self.original_config)
        self.temp_dir.cleanup()

    def test_snapshot_is_deeply_isolated_from_the_live_dictionary(self):
        config["bot_settings"] = {"language": "hu"}
        snapshot = snapshot_config()
        snapshot["bot_settings"]["language"] = "en"
        self.assertEqual(config["bot_settings"]["language"], "hu")

    def test_concurrent_read_modify_write_keeps_both_keys(self):
        config.clear()
        config.update({"bot_settings": {}})
        errors = []

        def writer(key):
            try:
                for _ in range(40):
                    with CONFIG_LOCK:
                        updated = snapshot_config()
                        updated["bot_settings"][key] = key
                        save_config(updated)
            except Exception as error:  # pragma: no cover - surfaced by assertion
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(name,))
                   for name in ("alpha", "beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(config["bot_settings"].get("alpha"), "alpha")
        self.assertEqual(config["bot_settings"].get("beta"), "beta")

    def test_legacy_mirror_holds_the_lock_across_read_modify_write(self):
        """The dashboard's mirror must not snapshot outside CONFIG_LOCK.

        Taking the snapshot under the lock and releasing it before save_config
        still loses a concurrent writer's keys, so the whole sequence has to sit
        inside one `with CONFIG_LOCK` block.
        """
        source = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        target = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_apply_legacy_config_values"
        )
        guarded = [
            node for node in ast.walk(target)
            if isinstance(node, ast.With)
            and any("CONFIG_LOCK" in ast.unparse(item.context_expr) for item in node.items)
        ]
        self.assertTrue(guarded, "_apply_legacy_config_values must hold CONFIG_LOCK")
        body = "\n".join(ast.unparse(node) for node in guarded)
        self.assertIn("snapshot_config()", body)
        self.assertIn("save_config(updated)", body)


class DashboardReadPathTests(unittest.TestCase):
    def test_read_helper_refuses_unclassified_operations(self):
        import database

        with self.assertRaises(ValueError):
            database.run_read_sync(database.set_feature_state, 1, 2, "economy", True)

    def test_dashboard_reads_do_not_take_the_writer_lock(self):
        import database

        self.temp_dir = tempfile.TemporaryDirectory()
        original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "reads.db")
        try:
            database.initialize_database()
            database.register_guild(31, "Read Guild")
            held = threading.Event()
            release = threading.Event()

            def hold_writer():
                with database._DB_WRITE_LOCK:
                    held.set()
                    release.wait(5)

            holder = threading.Thread(target=hold_writer)
            holder.start()
            self.assertTrue(held.wait(5))
            try:
                # This would block until the writer released without the read path.
                guilds = database.run_read_sync(database.get_active_guild_ids)
                self.assertIn(31, guilds)
            finally:
                release.set()
                holder.join(5)
        finally:
            database.DB_PATH = original_path
            self.temp_dir.cleanup()

    def test_dashboard_module_has_no_unclassified_synchronous_reads(self):
        import database

        source = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Attribute):
                continue
            if getattr(function.value, "id", None) != "database":
                continue
            if function.attr in database.READ_ONLY_OPERATIONS:
                offenders.append(f"dashboard_api.py:{node.lineno} {function.attr}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
