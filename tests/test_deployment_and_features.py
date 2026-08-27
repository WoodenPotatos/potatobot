import ast
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import database
from deployment import DeploymentProfile, load_deployment_settings
from feature_access import (
    COMMAND_POLICIES,
    PotatoContext,
    PotatoCommandTree,
    ResponsePolicy,
    is_enabled,
    refresh_feature_cache,
    refresh_feature_cache_async,
    seed_cached_feature,
)
from settings_registry import (
    FEATURE_DEFINITIONS,
    FEATURE_GROUP_ORDER,
    SETTING_DEFINITIONS,
    SettingValueType,
)
from settings_registry import DataCategory, DataScopeType


class DeploymentSettingsTests(unittest.TestCase):
    def test_private_profile_preserves_current_dashboard_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = load_deployment_settings()
        self.assertEqual(settings.profile, DeploymentProfile.PRIVATE)
        self.assertTrue(settings.dashboard_enabled)
        self.assertEqual(settings.dashboard_host, "127.0.0.1")
        self.assertEqual(settings.dashboard_port, 5000)

    def test_tailscale_oauth_origin_must_match_callback(self):
        environment = {
            "POTATOBOT_DASHBOARD_EXTERNAL_URL": "https://bot.example.ts.net",
            "DISCORD_REDIRECT_URI": "https://other.example.ts.net/api/callback",
            "POTATOBOT_DASHBOARD_HOST": "127.0.0.1",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "origins must match"):
                load_deployment_settings()

    def test_tailscale_oauth_configuration_accepts_loopback_proxy(self):
        environment = {
            "POTATOBOT_DASHBOARD_EXTERNAL_URL": "https://bot.example.ts.net",
            "DISCORD_REDIRECT_URI": "https://bot.example.ts.net/api/callback",
            "POTATOBOT_DASHBOARD_HOST": "127.0.0.1",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = load_deployment_settings()
        self.assertEqual(settings.discord_redirect_uri, environment["DISCORD_REDIRECT_URI"])

    def test_self_hosted_dashboard_is_disabled_until_configured(self):
        with patch.dict(
            os.environ, {"POTATOBOT_DEPLOYMENT_PROFILE": "self_hosted"}, clear=True
        ):
            settings = load_deployment_settings()
        self.assertFalse(settings.dashboard_enabled)


class FeaturePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "features.db")
        database.initialize_database()
        database.register_guild(123, "Test Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_missing_rows_use_registry_defaults(self):
        states = database.get_feature_states(123)
        self.assertEqual(set(states), set(FEATURE_DEFINITIONS))
        self.assertEqual(
            {key: item["enabled"] for key, item in states.items()},
            {key: definition.default for key, definition in FEATURE_DEFINITIONS.items()},
        )
        self.assertTrue(all(item["revision"] == 0 for item in states.values()))

    def test_feature_update_is_revision_checked_and_audited(self):
        result = database.set_feature_state(123, "social_twitch", False, 42, 0)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(set(result["changes"]), {"social_twitch"})
        self.assertFalse(database.is_feature_enabled(123, "social_twitch"))

        with self.assertRaises(database.DatabaseOperationError):
            database.set_feature_state(123, "social_twitch", True, 42, 0)

        with database.get_connection() as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM settings_audit WHERE guild_id = 123"
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)

    def test_runtime_feature_checks_use_memory_cache(self):
        database.set_feature_state(123, "social_twitch", False, 42, 0)
        refresh_feature_cache(123)
        self.assertFalse(is_enabled(123, "social_twitch"))
        seed_cached_feature(123, "social_twitch", True)
        self.assertTrue(is_enabled(123, "social_twitch"))

    def test_uninitialized_feature_cache_fails_closed(self):
        self.assertFalse(is_enabled(999999, "economy"))
        database.register_guild(999999, "Cold Cache")
        refresh_feature_cache(999999)
        self.assertTrue(is_enabled(999999, "economy"))

    def test_disabling_dependency_atomically_cascades_to_dependents(self):
        result = database.set_feature_state(123, "economy", False, 42, 0)
        states = database.get_feature_states(123)
        expected = {
            key for key, definition in FEATURE_DEFINITIONS.items()
            if (key == "economy" or "economy" in definition.dependencies)
            and definition.default
        }
        self.assertTrue(expected <= set(result["changes"]))
        self.assertTrue(all(not states[key]["enabled"] for key in expected))

    def test_enabling_feature_still_requires_dependencies(self):
        database.set_feature_state(123, "economy", False, 42, 0)
        with self.assertRaises(ValueError):
            database.set_feature_state(123, "shop", True, 42, 1)

    def test_realm_scope_requires_host_approved_membership(self):
        realm_id = database.create_realm("Trusted Guilds", 42)
        with self.assertRaises(ValueError):
            database.set_guild_data_scope(
                123, "profile", "realm", realm_id, 42, 0
            )

        database.request_realm_membership(realm_id, 123)
        database.approve_realm_membership(realm_id, 123, 42)
        result = database.set_guild_data_scope(
            123, "profile", "realm", realm_id, 42, 0
        )
        self.assertEqual(result["scope_type"], "realm")
        self.assertEqual(result["realm_id"], realm_id)
        self.assertEqual(result["revision"], 1)

    def test_scope_switch_preserves_previous_scoped_account(self):
        now = "2026-07-23T00:00:00"
        with database.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO scoped_accounts
                    (scope_type, scope_id, user_id, balance, xp, level,
                     created_at, updated_at)
                VALUES ('guild', 123, 9, 750, 10, 2, ?, ?)
                """,
                (now, now),
            )
        database.set_guild_data_scope(123, "economy", "instance", None, 42, 0)
        with database.get_connection() as conn:
            balance = conn.execute(
                "SELECT balance FROM scoped_accounts "
                "WHERE scope_type = 'guild' AND scope_id = 123 AND user_id = 9"
            ).fetchone()[0]
        self.assertEqual(balance, 750)

    def test_data_context_resolves_scope_and_member_opt_out(self):
        realm_id = database.create_realm("Context Realm", 42)
        database.request_realm_membership(realm_id, 123)
        database.approve_realm_membership(realm_id, 123, 42)
        database.set_guild_data_scope(123, "economy", "realm", realm_id, 42, 0)
        shared = database.resolve_data_context(123, "economy", 9)
        self.assertEqual(shared.category, DataCategory.ECONOMY)
        self.assertEqual(shared.scope_type, DataScopeType.REALM)
        self.assertEqual(shared.scope_id, realm_id)

        database.set_user_sharing_preference(9, 123, "economy", True)
        isolated = database.resolve_data_context(123, "economy", 9)
        self.assertEqual(isolated.scope_type, DataScopeType.GUILD)
        self.assertEqual(isolated.scope_id, 123)

    def test_members_can_opt_out_only_from_shareable_categories(self):
        database.set_user_sharing_preference(9, 123, "economy", True)
        with self.assertRaises(ValueError):
            database.set_user_sharing_preference(9, 123, "moderation", True)

    def test_legacy_adoption_seeds_guild_and_instance_once(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO users "
                "(user_id, balance, xp, level, last_daily, last_active) "
                "VALUES (9, 777, 250, 6, 'daily-marker', 'activity-marker')"
            )
            conn.execute(
                "INSERT INTO warnings (user_id, mod_id, reason, date) "
                "VALUES (9, 42, 'test', 'warning-date')"
            )

        first = database.adopt_legacy_database(123)
        second = database.adopt_legacy_database(123)
        self.assertTrue(first["adopted"])
        self.assertFalse(second["adopted"])
        self.assertEqual(first["user_count"], 1)

        with database.get_connection() as conn:
            states = conn.execute(
                "SELECT scope_type, scope_id, balance, xp, level, last_daily, "
                "last_active FROM scoped_accounts WHERE user_id = 9 "
                "ORDER BY scope_type"
            ).fetchall()
            legacy = conn.execute(
                "SELECT balance, xp, level, last_daily, last_active "
                "FROM users WHERE user_id = 9"
            ).fetchone()
            warning_guild = conn.execute(
                "SELECT guild_id FROM warnings WHERE user_id = 9"
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM activity_events "
                "WHERE user_id = 9 AND event_type = 'legacy_snapshot'"
            ).fetchone()[0]

        self.assertEqual(
            states,
            [
                ("guild", 123, 777, 250, 6, "daily-marker", "activity-marker"),
                ("instance", 0, 777, 250, 6, "daily-marker", "activity-marker"),
            ],
        )
        self.assertEqual(
            legacy, (777, 250, 6, "daily-marker", "activity-marker")
        )
        self.assertEqual(warning_guild, 123)
        self.assertEqual(event_count, 1)

    def test_warning_removal_is_guild_scoped_and_audited(self):
        database.add_warning(9, 42, "Reason", "2026-07-23T00:00:00", 123)
        with database.get_connection() as conn:
            warning_id = conn.execute(
                "SELECT id FROM warnings WHERE user_id = 9"
            ).fetchone()[0]

        self.assertIsNone(database.remove_warning(warning_id, 9, 999, 42))
        removed = database.remove_warning(warning_id, 9, 123, 42)
        self.assertEqual(removed["warning_id"], warning_id)
        self.assertEqual(database.get_warning_count(9, 123), 0)

        with database.get_connection() as conn:
            audit = conn.execute(
                "SELECT action, target_key FROM settings_audit "
                "WHERE guild_id = 123 AND action = 'warning.delete'"
            ).fetchone()
        self.assertEqual(audit, ("warning.delete", str(warning_id)))


# Real interactions always carry a guild and a member; the fakes mirror that so
# the shared maintenance predicate sees the same shape it does in production.
GUILD = SimpleNamespace(id=987654321)
MEMBER = SimpleNamespace(
    id=42, guild_permissions=SimpleNamespace(administrator=False)
)


class InteractionAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        seed_cached_feature(987654321, "general", True)

    async def test_regular_application_command_is_deferred_immediately(self):
        deferred = []

        async def defer(**kwargs):
            deferred.append(kwargs)

        interaction = SimpleNamespace(
            id=111,
            command=SimpleNamespace(qualified_name="version"),
            guild_id=987654321,
            guild=GUILD,
            user=MEMBER,
            type=discord.InteractionType.application_command,
            response=SimpleNamespace(is_done=lambda: False, defer=defer),
        )
        allowed = await PotatoCommandTree.interaction_check(None, interaction)
        self.assertTrue(allowed)
        self.assertEqual(deferred, [{"ephemeral": False}])

    async def test_private_application_command_is_deferred_ephemerally(self):
        deferred = []

        async def defer(**kwargs):
            deferred.append(kwargs)

        interaction = SimpleNamespace(
            id=112,
            command=SimpleNamespace(qualified_name="help"),
            guild_id=987654321,
            guild=GUILD,
            user=MEMBER,
            type=discord.InteractionType.application_command,
            response=SimpleNamespace(is_done=lambda: False, defer=defer),
        )
        allowed = await PotatoCommandTree.interaction_check(None, interaction)
        self.assertTrue(allowed)
        self.assertEqual(deferred, [{"ephemeral": True}])

    async def test_modal_first_command_is_not_deferred(self):
        deferred = []

        async def defer(**kwargs):
            deferred.append(kwargs)

        interaction = SimpleNamespace(
            id=113,
            command=SimpleNamespace(qualified_name="embedsend"),
            guild_id=987654321,
            guild=GUILD,
            user=MEMBER,
            type=discord.InteractionType.application_command,
            response=SimpleNamespace(is_done=lambda: False, defer=defer),
        )
        allowed = await PotatoCommandTree.interaction_check(None, interaction)
        self.assertTrue(allowed)
        self.assertEqual(deferred, [])

    def test_command_registry_has_no_implicit_visibility(self):
        self.assertTrue(COMMAND_POLICIES)
        self.assertTrue(
            all(isinstance(policy.response, ResponsePolicy) for policy in COMMAND_POLICIES.values())
        )

    def test_command_registry_covers_every_application_command(self):
        command_names = set()
        for path in (Path(__file__).resolve().parents[1] / "cogs").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    function = decorator.func
                    if not isinstance(function, ast.Attribute):
                        continue
                    decorator_name = ast.unparse(function)
                    if decorator_name not in {
                        "commands.hybrid_command",
                        "app_commands.command",
                        "discord.app_commands.command",
                    }:
                        continue
                    for keyword in decorator.keywords:
                        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                            command_names.add(keyword.value.value)
        self.assertEqual(set(COMMAND_POLICIES), command_names)

    async def test_public_defer_is_removed_before_private_error(self):
        deleted = []
        edited = []

        async def edit_original_response(**kwargs):
            edited.append(kwargs)

        async def delete_original_response():
            deleted.append(True)

        interaction = SimpleNamespace(
            id=114,
            command=SimpleNamespace(qualified_name="version"),
            is_expired=lambda: False,
            response=SimpleNamespace(is_done=lambda: True),
            edit_original_response=edit_original_response,
            delete_original_response=delete_original_response,
        )
        context = object.__new__(PotatoContext)
        context.interaction = interaction
        with patch(
            "discord.ext.commands.Context.send",
            new=AsyncMock(return_value="sent"),
        ) as base_send:
            result = await context.send("private", ephemeral=True)
        self.assertEqual(result, "sent")
        self.assertEqual(edited, [{"content": "\u200b"}])
        self.assertEqual(deleted, [True])
        base_send.assert_awaited_once_with("private", ephemeral=True)


class FeatureRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_revision_skips_full_feature_reload(self):
        guild_id = 918273645
        states = {
            key: {"enabled": definition.default, "revision": 0}
            for key, definition in FEATURE_DEFINITIONS.items()
        }
        mocked_run = AsyncMock(side_effect=[5, states, 5])
        with patch("feature_access.database.run_read", new=mocked_run):
            self.assertTrue(await refresh_feature_cache_async(guild_id, force=True))
            self.assertFalse(await refresh_feature_cache_async(guild_id))
        self.assertEqual(mocked_run.await_count, 3)


class LevelRoleTests(unittest.TestCase):
    """Level milestones were the one setting reachable from nowhere.

    `cogs/utils.py` read a `level_roles` key that was in neither `config.json`
    nor the registry, with the milestones hard-coded as a fallback. Registering
    it has to change no behaviour, and the parser has to survive operator-edited
    JSON, because it runs inside the level-up path.
    """

    def test_the_default_ships_no_role_ids(self):
        """The shipped default must stay empty.

        It briefly held the private deployment's own nine role ids, so every
        copy of the bot carried one guild's snowflakes — the same leak the
        `/work` responses had. A role id cannot be guessed for somebody else's
        guild, so there is no honest default; `docs/level_setup.md` documents
        the recommended ladder instead, and this installation's own mapping
        lives in `config.json` like every other id it uses.
        """
        default = SETTING_DEFINITIONS["level_roles"].default
        self.assertEqual({}, default)
        self.assertEqual("levels", SETTING_DEFINITIONS["level_roles"].owner_feature)

    def test_no_shipped_default_carries_a_discord_snowflake(self):
        """Generalised, because level_roles will not be the last one tempted.

        A snowflake in a registry default is a private guild's identifier
        travelling with every installation, and it can only ever be wrong for
        the guild that receives it.
        """
        def snowflakes(value):
            if isinstance(value, bool):
                return
            if isinstance(value, int) and value > 1 << 52:
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from snowflakes(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    yield from snowflakes(item)

        offenders = {
            key: sorted(set(snowflakes(definition.default)))
            for key, definition in SETTING_DEFINITIONS.items()
            if list(snowflakes(definition.default))
        }
        self.assertEqual({}, offenders)

    def test_no_shipped_text_names_one_guild_s_currency(self):
        """The bot is universal; the currency's *name* is not.

        "PC" and "Potatocoin" are Potato Empire's coin. They were in 37 strings
        per catalog — a dashboard label reading "Daily normal PC reward" and a
        Discord embed reading "1 pull - 5,000 PC" are the same defect as a role
        id in a default: one guild's fact shipped to every guild. The symbol is
        the `currency_emoji` setting; the word is "coins" / "érme".
        """
        import json
        import re
        from pathlib import Path

        import database

        root = Path(__file__).resolve().parents[1]
        # "PC" as a standalone token, so "PCem" or a stray acronym is not a hit.
        forbidden = re.compile(r"(?<![A-Za-z])PC(?![A-Za-z])|potatocoin", re.I)
        offenders = {}

        def walk(node, path, into):
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}" if path else key, into)
            elif isinstance(node, str) and forbidden.search(node):
                into.append(path)

        for catalog in sorted((root / "locales").glob("*.json")):
            found = []
            walk(json.loads(catalog.read_text(encoding="utf-8")), "", found)
            if found:
                offenders[catalog.name] = found

        shipped_work = [
            message for _tier, message in database.WORK_DEFAULT_RESPONSES
            if forbidden.search(message)
        ]
        if shipped_work:
            offenders["WORK_DEFAULT_RESPONSES"] = shipped_work

        self.assertEqual(
            {}, offenders,
            "name the currency 'coins'/'érme' and let currency_emoji carry the symbol",
        )

    def test_no_shipped_text_carries_a_discord_snowflake(self):
        """The same rule, widened to everything else that ships.

        The narrow version passed while one guild's currency emoji sat in 105
        places — 80 of them in the locale catalogs and 11 in the shipped `/work`
        responses, neither of which it looked at. A custom emoji belongs to one
        guild and renders as raw `<:name:id>` text everywhere else, so it is the
        same defect as a role id in a default, in a place the first test could
        not see.
        """
        import json
        import re
        from pathlib import Path

        import database

        root = Path(__file__).resolve().parents[1]
        custom_emoji = re.compile(r"<a?:[A-Za-z0-9_]+:\d{17,20}>")

        offenders = {}
        for name in ("hu", "en"):
            text = (root / "locales" / f"{name}.json").read_text(encoding="utf-8")
            found = sorted(set(custom_emoji.findall(text)))
            if found:
                offenders[f"locales/{name}.json"] = found

        shipped_work = " ".join(message for _, message in database.WORK_DEFAULT_RESPONSES)
        found = sorted(set(custom_emoji.findall(shipped_work)))
        if found:
            offenders["WORK_DEFAULT_RESPONSES"] = found

        self.assertEqual(
            {}, offenders,
            "shipped text must use the {coin} token, not a guild's own emoji")

    def test_a_role_id_and_a_role_name_are_both_accepted(self):
        """The reader has always taken either, and an installation that never
        configured this relies on the names."""
        from cogs.utils import level_milestones

        self.assertEqual(
            {5: "Level 5", 10: 1420070400000000001},
            level_milestones({"5": "Level 5", "10": 1420070400000000001}),
        )

    def test_operator_edited_json_cannot_break_a_level_up(self):
        """Each of these used to be a raise inside the level-up path, which would
        have swallowed the member's level-up entirely."""
        from cogs.utils import level_milestones

        self.assertEqual(
            {20: "Level 20"},
            level_milestones({
                "not-a-level": "Level 5",   # key is not a number
                "5": "",                     # blank name
                "10": None,                  # no role at all
                "15": True,                  # a bool is not a snowflake
                "20": "Level 20",
            }),
        )
        self.assertEqual({}, level_milestones(None))
        self.assertEqual({}, level_milestones({}))

    def test_level_two_is_announced_even_without_a_role(self):
        """Level 2 has always been announced without granting anything, so it is
        added to the milestone list rather than to the role map."""
        source = (Path(__file__).resolve().parents[1] / "cogs" / "utils.py")
        text = source.read_text(encoding="utf-8")
        self.assertIn("if 2 not in milestones:", text)


class RegistryPresentationTests(unittest.TestCase):
    """The dashboard renders straight from the registry, so a definition that
    names a group or a channel kind the interface cannot label is a blank or an
    empty selector rather than an error anyone would see."""

    CATALOG = json.loads(
        (Path(__file__).resolve().parents[1] / "locales" / "hu.json")
        .read_text(encoding="utf-8")
    )

    def test_every_feature_group_is_ordered_and_localized(self):
        groups = {definition.group for definition in FEATURE_DEFINITIONS.values()}
        labels = self.CATALOG["dashboard"]["feature_groups"]
        self.assertEqual([], sorted(groups - set(FEATURE_GROUP_ORDER)))
        self.assertEqual([], sorted(groups - set(labels)))
        self.assertEqual([], [key for key in groups if not labels[key]])

    def test_the_games_group_stays_separable_into_families(self):
        """The `games` group holds three families and nothing else.

        The original defect was grouping *by dependency*: every casino game and
        every Everydle game depends on `economy`, so they collapsed into one
        undifferentiated block. The operator has since asked for casino,
        minigames and Everydle to sit in one `games` group deliberately — which
        is safe only because each family is now a master toggle with its games
        as children, so the block is read as three things rather than fifteen.
        What must stay true is that separability: every member of the group is
        one of the three masters or a child of one of them.
        """
        masters = ("casino", "minigames", "everydle")
        group = {key for key, definition in FEATURE_DEFINITIONS.items()
                 if definition.group == "games"}
        self.assertEqual(set(masters), group & set(masters))
        for key in group - set(masters):
            parent = FEATURE_DEFINITIONS[key].parent
            self.assertIn(parent, masters,
                          f"{key} is in the games group under no master")
        # A family is still identifiable by name, which is what keeps the
        # dashboard's rendering and the registry from disagreeing.
        for master in masters:
            children = {key for key in group
                        if FEATURE_DEFINITIONS[key].parent == master}
            self.assertTrue(children, f"{master} has no games under it")
            prefix = "everydle_" if master == "everydle" else (
                "minigame_" if master == "minigames" else "casino_")
            self.assertTrue(all(key.startswith(prefix) for key in children))

    def test_each_game_family_has_one_master_and_the_rest_are_children(self):
        """Eight near-identical games in the flat Features list pushed everything
        else off it, so each family collapsed to one master toggle."""
        for parent in ("casino", "everydle"):
            with self.subTest(parent=parent):
                self.assertIn(parent, FEATURE_DEFINITIONS)
                self.assertIsNone(FEATURE_DEFINITIONS[parent].parent,
                                  "a master is not itself a child")
                children = [key for key, definition in FEATURE_DEFINITIONS.items()
                            if definition.parent == parent]
                self.assertGreater(len(children), 1)
                for child in children:
                    # Depending on the parent is what makes the existing
                    # transitive cascade switch the children off with it; `parent`
                    # alone is only a rendering hint.
                    self.assertIn(parent, FEATURE_DEFINITIONS[child].dependencies)

    def test_a_child_renders_on_a_category_named_after_its_parent(self):
        """The convention the dashboard relies on, and the reason it is one name
        rather than two that can disagree."""
        parents = {definition.parent for definition in FEATURE_DEFINITIONS.values()
                   if definition.parent}
        html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        for parent in sorted(parents):
            with self.subTest(parent=parent):
                self.assertIn(f'data-category="{parent}"', html,
                              "a parent needs a settings page to render its "
                              "children on")

    def test_channel_settings_declare_a_usable_channel_kind(self):
        channel_types = {
            SettingValueType.CHANNEL, SettingValueType.CHANNEL_LIST,
        }
        known = set(__import__("dashboard_api").DISCORD_CHANNEL_TYPES.values())
        for key, definition in SETTING_DEFINITIONS.items():
            if definition.value_type not in channel_types:
                continue
            with self.subTest(setting=key):
                self.assertTrue(definition.channel_types,
                                "a channel setting must say what it may name")
                self.assertEqual(
                    [], sorted(set(definition.channel_types) - known)
                )

    def test_required_permissions_name_real_discord_flags(self):
        valid = set(dict(discord.Permissions.all()))
        for key, definition in FEATURE_DEFINITIONS.items():
            with self.subTest(feature=key):
                self.assertEqual(
                    [], sorted(set(definition.required_discord_permissions) - valid)
                )


if __name__ == "__main__":
    unittest.main()


class FactionVoiceLockTests(unittest.TestCase):
    """The faction lock spans two features and one persistent view.

    Both halves have a failure mode that looks like nothing at all: a cascade
    that stops short leaves the button live under a disabled faction system, and
    a registered view missing the button drops the click instead of refusing it.
    """

    def _transitive_dependants(self, key):
        """What set_feature_state disables along with `key`."""
        found, frontier = set(), {key}
        while frontier:
            following = set()
            for definition in FEATURE_DEFINITIONS.values():
                if set(definition.dependencies) & frontier and definition.key not in found:
                    found.add(definition.key)
                    following.add(definition.key)
            frontier = following
        return found

    def test_flag_ships_disabled_and_declares_both_halves(self):
        definition = FEATURE_DEFINITIONS["temporary_voice_faction_lock"]
        # A guild with no factions configured would otherwise show a button that
        # can only ever refuse.
        self.assertFalse(definition.default)
        self.assertEqual(set(definition.dependencies),
                         {"temporary_voice", "factions"})

    def test_disabling_either_half_cascades_to_the_lock(self):
        for owner in ("temporary_voice", "factions", "moderation"):
            with self.subTest(owner=owner):
                self.assertIn("temporary_voice_faction_lock",
                              self._transitive_dependants(owner))

    def test_factions_is_its_own_group_and_category(self):
        # The Moderation page held only factions and the inactivity ignore list,
        # so its name described neither.
        self.assertEqual(FEATURE_DEFINITIONS["factions"].group, "factions")
        self.assertEqual(SETTING_DEFINITIONS["factions"].category, "factions")
        self.assertIn("factions", FEATURE_GROUP_ORDER)
        self.assertNotIn(
            "factions",
            {key for key, definition in SETTING_DEFINITIONS.items()
             if definition.category == "moderation"},
        )

    def test_registered_view_keeps_every_button_the_flag_can_hide(self):
        """A persistent view routes a click by custom_id.

        The instance passed to bot.add_view() must therefore carry the faction
        button even where no guild has the flag on, or a panel posted while it
        was enabled answers a click with nothing.
        """
        from cogs.voicemod import VoiceControlView

        registered = {item.custom_id for item in VoiceControlView().children}
        rendered_off = {item.custom_id
                        for item in VoiceControlView(faction_lock=False).children}
        self.assertIn("vc_faction_lock", registered)
        self.assertNotIn("vc_faction_lock", rendered_off)
        # Nothing else may depend on the flag: the rest of the panel is fixed.
        self.assertEqual(registered - rendered_off, {"vc_faction_lock"})

    def test_member_faction_roles_never_widen_to_everyone(self):
        from unittest.mock import patch

        import cogs.utils as utils

        role = lambda role_id: SimpleNamespace(id=role_id)
        member = lambda *ids: SimpleNamespace(
            roles=[role(i) for i in ids], guild=SimpleNamespace(id=55))
        factions = {
            "alpha": {"leader_role_id": 1, "manageable_ids": [2, 3]},
            "beta": {"leader_role_id": 4, "manageable_ids": [5]},
            # An operator-authored blob can hold anything; a malformed faction
            # must be skipped rather than raise inside a button callback.
            "malformed": "not a mapping",
        }
        with patch.object(utils, "guild_setting_sync",
                          lambda guild_id, key: factions):
            self.assertEqual(utils.member_faction_role_ids(member(3)), {1, 2, 3})
            self.assertEqual(utils.member_faction_role_ids(member(1)), {1, 2, 3})
            self.assertEqual(utils.member_faction_role_ids(member(3, 5)),
                             {1, 2, 3, 4, 5})
            # The important one: no faction means no faction, not every faction.
            self.assertEqual(utils.member_faction_role_ids(member(99)), set())
            self.assertEqual(utils.all_faction_role_ids(55), {1, 2, 3, 4, 5})

    def test_a_member_with_no_guild_has_no_faction(self):
        """A guild is where a faction map lives, so no guild is no faction."""
        import cogs.utils as utils
        self.assertEqual(
            set(), utils.member_faction_role_ids(SimpleNamespace(roles=[])))
