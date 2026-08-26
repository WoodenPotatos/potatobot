"""Regression coverage for the emergency stop, mention safety and bounded state.

These invariants are cheap to break silently: maintenance mode used to apply to
only one of several entry points, a nickname could smuggle a role mention into
the one message that permits them, and transient maps grew for the process
lifetime. Each test here pins one of those behaviours.
"""

import ast
import asyncio
import builtins
import json
import os
import symtable
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
    seed_cached_feature,
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
        seed_cached_feature(GUILD.id, "economy", True)
        interaction = fake_interaction()
        allowed = asyncio.run(require_interaction_feature(interaction, "economy"))
        self.assertFalse(allowed)
        self.assertEqual(len(interaction.response.messages), 1)

    def test_already_acknowledged_interactions_are_refused_via_followup(self):
        seed_cached_feature(GUILD.id, "economy", True)
        interaction = fake_interaction(done=True)
        allowed = asyncio.run(require_interaction_feature(interaction, "economy"))
        self.assertFalse(allowed)
        self.assertEqual(len(interaction.followup.messages), 1)

    def test_maintenance_outranks_an_enabled_feature(self):
        seed_cached_feature(GUILD.id, "economy", True)
        self.assertTrue(is_enabled(GUILD.id, "economy"))
        interaction = fake_interaction()
        self.assertFalse(asyncio.run(require_interaction_feature(interaction, "economy")))

    def test_components_without_their_own_flag_still_clear_maintenance(self):
        interaction = fake_interaction()
        self.assertFalse(asyncio.run(require_interaction_feature(interaction, None)))

    def test_normal_operation_allows_the_same_component(self):
        config["bot_settings"]["maintenance"] = False
        seed_cached_feature(GUILD.id, "economy", True)
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
        """Each field by name, rather than a count of escape calls.

        The count said "at least three" and was satisfied by escaping one field
        three times; it also had to be revisited every time the command grew a
        branch. What matters is *which* values reach message content: the
        member's nickname, which they choose, and the free-text game name.
        """
        source = (ROOT / "cogs" / "general.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        search = next(node for node in ast.walk(tree)
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.name == "search")
        escaped = {
            ast.unparse(inner.args[0])
            for inner in ast.walk(search)
            if isinstance(inner, ast.Call)
            and ast.unparse(inner.func) == "discord.utils.escape_mentions"
            and inner.args
        }
        for field in ("ctx.author.display_name", "game"):
            with self.subTest(field=field):
                self.assertIn(field, escaped)

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

    def test_the_journal_is_not_written_twice(self):
        """Both halves of a duplication that doubled every log line.

        `waitress.serve` calls `logging.basicConfig()`, which adds a *root*
        handler when nothing else has one, so anything that propagates is
        emitted again in Python's default format. And `bot.run()` calls
        `setup_logging()` on the `discord` logger this project already
        configured. The result halved a 500 MB journal cap for nothing and made
        a grep count read double.
        """
        setup = (ROOT / "logging_setup.py").read_text(encoding="utf-8")
        self.assertIn("configured.propagate = False", setup,
                      "a configured logger must not also reach the root logger")
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("bot.run(TOKEN, log_handler=None)", source,
                      "discord.py must not configure the `discord` logger too")
        # And every logger the bot configures goes through the one helper.
        for name in ("'discord'", "'PotatoBot'", "'waitress'"):
            self.assertIn(f"configure_logger({name})", source)

    def test_a_standalone_dashboard_configures_its_own_logging(self):
        """The two-process split got no configuration at all.

        The handlers lived in `main.py`, so `python dashboard_api.py` — the
        split this codebase prefers beyond the private deployment — started with
        none of them, and `waitress.serve`'s `basicConfig()` then decided the
        format of every line it emitted, with no rotating file behind it. A cog
        cannot import `main` and neither can the dashboard, so the setup lives in
        a module both reach.
        """
        api = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        self.assertIn("logging_setup.configure_dashboard_logging()", api)
        setup = (ROOT / "logging_setup.py").read_text(encoding="utf-8")
        for name in ('"PotatoBot"', '"waitress"'):
            self.assertIn(f"configure_logger({name})", setup)

    def test_configuring_a_logger_twice_does_not_double_it(self):
        """Both entry points may configure the same logger in one process."""
        import logging

        import logging_setup

        name = "PotatoBot.TestDoubleConfigure"
        try:
            first = logging_setup.configure_logger(name)
            count = len(first.handlers)
            self.assertGreater(count, 0)
            again = logging_setup.configure_logger(name)
            self.assertEqual(count, len(again.handlers))
            self.assertFalse(again.propagate)
        finally:
            logging.getLogger(name).handlers.clear()

    def test_nothing_in_the_dashboard_writes_config_json(self):
        """The mirror is gone, and this is what stops it coming back.

        `config.json` was written by the dashboard on every save and rebuilt
        from the rows at startup, which is why the read-modify-write had to hold
        CONFIG_LOCK for its whole sequence — a snapshot taken under the lock and
        saved after it still dropped a concurrent writer's keys. There is now one
        fewer writer than that: the file is a read-only fallback for a setting an
        installation has never saved, and every reader goes through
        `settings_cache`.
        """
        source = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Parsed rather than grepped, so a comment explaining the absence does
        # not read as the thing being present.
        called = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        defined = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        imported = {
            alias.asname or alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) for alias in node.names
        }
        for gone in ("save_config", "snapshot_config", "CONFIG_LOCK"):
            self.assertNotIn(gone, called | imported,
                             f"{gone} writes or guards config.json")
        for gone in ("_apply_legacy_config_values",
                     "reconcile_legacy_config_mirror", "_legacy_guild_id"):
            self.assertNotIn(gone, called | defined,
                             f"{gone} is mirror machinery")

    def test_the_per_guild_price_and_reward_rows_are_still_written(self):
        """Deleting the mirror must not take these with it.

        They are not the mirror: they are per-guild rows in `shop_prices` and
        `rewards` that the shop and the reward paths read directly, and they live
        beside the mirror only because one function used to write both.
        """
        source = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        self.assertIn("def _mirror_price_and_reward_tables", source)
        self.assertIn("_mirror_price_and_reward_tables(", source)


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


class UndefinedNameTests(unittest.TestCase):
    """A name a module reads must actually exist.

    Three features were broken at once by this, all from one refactor that
    removed a name and left a reference: `/checkperms` passed a `config` that was
    no longer imported, `/manage` read a `ctx` it never had, and the socials
    loops read a `social_cfg` that was deleted. Each raised `NameError` the moment
    the line ran, and `compileall` cannot see any of them because a free variable
    is resolved at call time.

    Nothing in the suite caught them. `tests/test_cog_loading.py` executes module
    scope and `__init__` only. The nearest check,
    `test_settings_cache.NoCogReadsTheLegacyFileTests`, is what *forced* the
    `config` import out of the cogs — and it matches `config[...]` and
    `config.get(...)`, so a bare `config` passed as an argument was invisible to
    the very test that created the hole.

    `symtable` does the scoping properly, and needs no new dependency.
    """

    # `logs`-style module dunders resolve as globals and are always present at
    # runtime; they are not what this test is looking for.
    ALLOWED = frozenset({"__file__", "__name__", "__doc__", "__package__",
                         "__spec__", "__loader__", "__builtins__",
                         "__conditional_annotations__"})

    def modules(self):
        """The runtime tree. Tests are excluded: they legitimately reference
        names a harness injects."""
        roots = [ROOT / name for name in (
            "main.py", "database.py", "dashboard_api.py", "managed_messages.py",
            "settings_cache.py", "settings_registry.py", "feature_access.py",
            "permission_audit.py", "item_catalog.py", "deployment.py",
            "bounded.py", "minigame_data.py", "version.py",
        )]
        return sorted(ROOT.glob("cogs/*.py")) + [p for p in roots if p.exists()]

    def undefined(self, path):
        source = path.read_text(encoding="utf-8")
        table = symtable.symtable(source, str(path), "exec")
        module_level = {symbol.get_name() for symbol in table.get_symbols()}
        found = []

        def walk(scope):
            for symbol in scope.get_symbols():
                name = symbol.get_name()
                # `is_local()` and `is_free()` are load-bearing: on Python 3.14 an
                # ordinary parameter reports `is_global()` as well, so without
                # them every function argument is a false positive.
                if (symbol.is_global()
                        and not symbol.is_local()
                        and not symbol.is_free()
                        and symbol.is_referenced()
                        and name not in module_level
                        and name not in self.ALLOWED
                        and not hasattr(builtins, name)):
                    found.append(f"{path.name}:{scope.get_name()}() reads "
                                 f"undefined {name!r}")
            for child in scope.get_children():
                walk(child)

        walk(table)
        return found

    def test_the_modules_were_actually_found(self):
        """Guards the premise: an empty file list would pass vacuously."""
        names = [path.name for path in self.modules()]
        self.assertIn("socials.py", names)
        self.assertIn("dashboard_api.py", names)
        self.assertGreater(len(names), 20)

    def test_no_module_reads_a_name_that_does_not_exist(self):
        problems = [problem for path in self.modules()
                    for problem in self.undefined(path)]
        self.assertEqual([], problems)

    def test_the_check_would_catch_a_planted_reference(self):
        """A check that never fires is worse than no check."""
        with tempfile.TemporaryDirectory() as scratch:
            planted = Path(scratch) / "planted.py"
            planted.write_text(
                "def handler(guild):\n"
                "    return social_cfg.get('twitch_role_id')\n",
                encoding="utf-8")
            self.assertEqual(
                ["planted.py:handler() reads undefined 'social_cfg'"],
                self.undefined(planted))

    def test_an_ordinary_parameter_is_not_a_finding(self):
        """The false-positive case the `is_local()` condition exists for."""
        with tempfile.TemporaryDirectory() as scratch:
            innocent = Path(scratch) / "innocent.py"
            innocent.write_text(
                "import json\n"
                "TOP = 1\n"
                "def handler(self, ctx, *args, **kwargs):\n"
                "    view = json.dumps(TOP)\n"
                "    return [view for _ in ctx]\n",
                encoding="utf-8")
            self.assertEqual([], self.undefined(innocent))


if __name__ == "__main__":
    unittest.main()


def _command_functions():
    """Every hybrid or application command body in `cogs/`, as (path, node)."""
    for path in sorted((ROOT / "cogs").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                dumped = ast.dump(decorator)
                if "hybrid_command" in dumped or (
                    "app_commands" in dumped and "command" in dumped
                ):
                    yield path, node
                    break


class InteractionAcknowledgementTests(unittest.TestCase):
    """A command body must not acknowledge its own interaction.

    `PotatoCommandTree.interaction_check` defers every non-modal application
    command using its declared `COMMAND_POLICIES` visibility, before the body
    runs. A second `ctx.defer()` therefore raises `InteractionResponded` and the
    command never executes — which is exactly what `/mydata` did, on every
    invocation, until it showed up in the deployment's journal. Nothing catches
    it: the body compiles, the tests that load cogs only execute module scope,
    and the failure needs someone to actually run the command.

    Component and modal callbacks are *separate* interactions and must keep
    acknowledging themselves, so this looks only at command bodies.
    """

    def test_the_premise_holds(self):
        """A scan that finds no commands would pass regardless."""
        self.assertGreater(len(list(_command_functions())), 30)

    def test_no_command_body_defers(self):
        offenders = []
        for path, node in _command_functions():
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if getattr(call.func, "attr", "") != "defer":
                    continue
                offenders.append(
                    f"{path.relative_to(ROOT)}:{call.lineno} in {node.name}"
                )
        self.assertEqual([], offenders)


class ResponseVisibilityTests(unittest.TestCase):
    """A PRIVATE command must not reply publicly through `ctx.send`.

    That direction is what strands a message reference. The tree defers
    ephemerally, the body sends publicly, and `PotatoContext.send` resolves the
    mismatch by deleting the original response — leaving Discord's "used
    /command" header above the reply pointing at a message that no longer
    exists, which the client draws as "Message could not be loaded". It happened
    on every successful `/gacha` pull.

    The opposite direction — a PUBLIC command with ephemeral refusals — is the
    intended, rare use of the swap and stays allowed.
    """

    def test_no_private_command_sends_publicly(self):
        from feature_access import COMMAND_POLICIES, ResponsePolicy

        offenders = []
        for path, node in _command_functions():
            policy = COMMAND_POLICIES.get(node.name)
            if policy is None or policy.response is not ResponsePolicy.PRIVATE:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not (getattr(func, "attr", "") == "send"
                        and isinstance(getattr(func, "value", None), ast.Name)
                        and func.value.id == "ctx"):
                    continue
                ephemeral = None
                for keyword in call.keywords:
                    if keyword.arg == "ephemeral":
                        ephemeral = keyword.value
                # Anything that is not a literal True. A conditional is flagged
                # too, because a PRIVATE command that *can* reply publicly hits
                # the swap on exactly the branch where it does.
                if not (isinstance(ephemeral, ast.Constant)
                        and ephemeral.value is True):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{call.lineno} in {node.name}"
                    )
        self.assertEqual([], offenders)
