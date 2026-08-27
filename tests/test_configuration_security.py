import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationCoverageTests(unittest.TestCase):
    """Every value in `config.json` must be reachable from the dashboard.

    A value that only a file edit can change is not configurable at all for an
    operator who only has the control plane, and nothing else in the suite would
    notice one being added.
    """

    CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    # Paths that are deliberately not dashboard settings. Nothing is exempt
    # today; an entry added here needs a stated reason, because the alternative
    # is a value only a file edit can change.
    EXEMPT_PATHS = frozenset()
    # Top-level keys registered whole, as one JSON setting, rather than per leaf.
    # Settings whose whole value is one JSON document. The walk stops at the
    # top key rather than treating each entry — a role menu, a level milestone —
    # as a setting of its own.
    WHOLE_KEY_SETTINGS = {
        "factions", "lfg_channels", "level_roles",
    }

    def config_paths(self):
        """Every leaf path in config.json, stopping at whole-key settings."""
        def walk(node, prefix=()):
            if prefix and prefix[0] in self.WHOLE_KEY_SETTINGS:
                yield prefix[:1]
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    yield from walk(value, prefix + (key,))
            else:
                yield prefix
        return {path for path in walk(self.CONFIG)}

    def test_every_config_value_has_a_typed_setting(self):
        from settings_registry import SETTING_DEFINITIONS

        registered = {
            definition.legacy_path
            for definition in SETTING_DEFINITIONS.values()
            if definition.legacy_path
        }
        unreachable = sorted(
            ".".join(path) for path in self.config_paths()
            if path not in registered and path not in self.EXEMPT_PATHS
        )
        self.assertEqual([], unreachable)

    def test_every_setting_has_an_operator_facing_label(self):
        """The dashboard renders the label straight from the registry, so a
        missing one shows the operator a raw `[dashboard.settings.x]`."""
        from settings_registry import SETTING_DEFINITIONS

        labels = json.loads(
            (ROOT / "locales" / "hu.json").read_text(encoding="utf-8")
        )["dashboard"]["settings"]
        missing = sorted(
            key for key in SETTING_DEFINITIONS if not labels.get(key)
        )
        self.assertEqual([], missing)

    def test_every_setting_page_has_a_section_heading(self):
        from settings_registry import SETTING_DEFINITIONS

        pages = json.loads(
            (ROOT / "locales" / "hu.json").read_text(encoding="utf-8")
        )["dashboard"]["pages"]
        missing = sorted({
            definition.page for definition in SETTING_DEFINITIONS.values()
            if not pages.get(definition.page)
        })
        self.assertEqual([], missing)

    def test_the_command_prefix_is_read_from_configuration(self):
        """It used to be a literal in main.py, so the setting would do nothing.

        It now comes from the typed setting through the cache, loaded
        synchronously because discord.py binds the prefix before there is an
        event loop. The fallback matters as much as the read: an unreadable
        database must still leave the bot on `?`, or a database problem locks
        the operator out of `?reload` and `?reloadconfig` — the two commands
        that would fix it.
        """
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('command_prefix="?"', source)
        self.assertIn('settings_cache.load_instance_sync()', source)
        self.assertIn('settings_cache.setting(None, "command_prefix") or "?"',
                      source)

    def test_loading_the_installation_settings_cannot_stop_startup(self):
        """The fallback above is only real if the load itself cannot raise."""
        import settings_cache

        original = settings_cache.database.get_instance_settings
        settings_cache.database.get_instance_settings = (
            lambda: (_ for _ in ()).throw(RuntimeError("no database")))
        try:
            self.assertFalse(settings_cache.load_instance_sync())
        finally:
            settings_cache.database.get_instance_settings = original


class LocalDevelopmentDashboardTests(unittest.TestCase):
    """`scripts/local_dashboard.py` runs the control plane with no Discord.

    Two properties are load-bearing and neither is visible at a glance. It must
    not be able to serve a real installation, and it must not be able to write
    the tracked `config.json` — which it would by default, because the legacy
    mirror target is *inferred* from "private profile with one active guild",
    and a local copy of the server database has exactly one.
    """

    SOURCE = (ROOT / "scripts" / "local_dashboard.py").read_text(encoding="utf-8")

    def test_the_legacy_config_mirror_is_disabled(self):
        self.assertIn('os.environ["POTATOBOT_LEGACY_GUILD_ID"] = "0"', self.SOURCE)

    def test_config_writes_are_redirected_away_from_the_tracked_file(self):
        self.assertIn("def redirect_config_writes", self.SOURCE)
        self.assertIn("utils.CONFIG_PATH = str(local_config)", self.SOURCE)
        self.assertIn("redirect_config_writes(arguments.db.parent)", self.SOURCE)

    def test_the_legacy_guild_id_is_still_pinned_to_zero(self):
        """The mirror it disabled is gone; the variable is not.

        `POTATOBOT_LEGACY_GUILD_ID` has a second reader in `main.py`, which uses
        it to adopt a legacy database into scoped accounts. Pinning it to 0 kept
        the launcher from writing the tracked `config.json`; it now keeps the
        launcher from adopting a copy of the deployment's data, which is the same
        class of accident.
        """
        self.assertIn('os.environ["POTATOBOT_LEGACY_GUILD_ID"] = "0"', self.SOURCE)

    def test_it_refuses_to_run_in_a_proxied_or_managed_environment(self):
        self.assertIn("POTATOBOT_DASHBOARD_EXTERNAL_URL", self.SOURCE)
        self.assertIn('profile == "managed"', self.SOURCE)
        self.assertIn('os.environ["POTATOBOT_DASHBOARD_HOST"] = "127.0.0.1"',
                      self.SOURCE)

    def test_it_works_on_a_copy_rather_than_the_source_database(self):
        self.assertIn("economy.dev.db", self.SOURCE)
        # The WAL sidecars must travel with the file or recent commits are lost.
        self.assertIn('for suffix in ("", "-wal", "-shm")', self.SOURCE)

    def test_the_working_directory_is_untracked(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".local-dev/", ignore)

    def test_no_development_branch_leaks_into_the_dashboard_module(self):
        """The bypass lives in the script, so no deployment can take that path."""
        api = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        for marker in ("local_dev", "LOCAL_DEV", "local-dev", "DevGuild"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, api)


class ConfigurationSecurityTests(unittest.TestCase):
    def test_tracked_config_contains_no_twitch_credentials(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        socials = config.get("socials", {})
        self.assertNotIn("twitch_client_id", socials)
        self.assertNotIn("twitch_client_secret", socials)

    def test_environment_template_documents_twitch_credentials(self):
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("TWITCH_CLIENT_ID=", template)
        self.assertIn("TWITCH_CLIENT_SECRET=", template)

    def test_sanitized_config_example_matches_the_real_shape(self):
        """The example is what a new installation copies, so it must stay in
        step with the real file without carrying any of its identifiers."""
        root = Path(__file__).resolve().parents[1]
        real = json.loads((root / "config.json").read_text(encoding="utf-8"))
        example_text = (root / "config.json.example").read_text(encoding="utf-8")
        example = json.loads(example_text)

        self.assertEqual(sorted(real), sorted(example))
        for section in real:
            if isinstance(real[section], dict) and not any(
                re.fullmatch(r"\d{15,20}", key) for key in real[section]
            ):
                self.assertEqual(
                    sorted(real[section]), sorted(example[section]),
                    f"section {section} drifted from config.json",
                )

        leaked = re.findall(r"\d{15,20}", example_text)
        self.assertEqual([], leaked, "config.json.example leaks Discord identifiers")

    def test_secret_scanner_allowlist_stays_narrow(self):
        """The gitleaks allowlist exists only for the gacha reward identifiers.

        A broad allowlist would silently disable the scanner, so this pins the
        shape: extend the upstream rules, allowlist by match rather than path,
        and never exempt a whole file or directory.
        """
        config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        self.assertIn("useDefault = true", config)
        self.assertIn('regexTarget = "match"', config)
        # Path or file exemptions would hide every rule for that file.
        for forbidden in ("paths = [", "files = [", "stopwords = ["):
            self.assertNotIn(forbidden, config, f"{forbidden} is too broad")

    def test_every_reward_key_matches_the_scanner_allowlist(self):
        """A new reward name outside the allowlist pattern fails CI, so keep the
        pattern and the reward pool in step.

        The pattern is **read out of `.gitleaks.toml`** rather than restated
        here. It used to be hand-copied, and the copy had already drifted — the
        file had learnt `stacked_deck`, `lucky_charm` and `metal_detector` and
        this test had not, so it was asserting against a pattern that was not
        the one CI runs. A test that duplicates the artifact it guards can only
        ever agree with it by luck.
        """
        import database

        # Take the allowlist's own regexes out of the file and run them the way
        # gitleaks does — against the matched text — rather than reconstructing
        # the alternation, which is what drifted last time.
        config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        sources = re.findall(r"'''(.*?)'''", config, re.DOTALL)
        patterns = [re.compile(source) for source in sources if "key" in source]
        self.assertTrue(patterns, "no reward-key allowlist regex in .gitleaks.toml")
        unmatched = [
            entry["key"]
            for tier in database.DEFAULT_GACHA_CONFIG["rewards"].values()
            for entry in tier
            if not any(pattern.search(f'key": "{entry["key"]}"')
                       for pattern in patterns)
        ]
        self.assertEqual(
            [], unmatched,
            "extend the regex in .gitleaks.toml for these reward keys",
        )

    def test_every_drawable_catalog_item_is_in_the_shipped_reward_pool(self):
        """A `gacha_kind` that reaches no banner is an unobtainable item.

        Five casino consumables shipped marked drawable and were never added to
        `DEFAULT_GACHA_CONFIG`, so no banner could award them — and because
        `missing_shipped_rewards` compares a banner against the *shipped* table,
        nothing was reported as missing either. The operator's only route to
        them was "reset rewards", which discards their own table. Coverage has
        to be diffed in both directions: what the catalog offers against what
        the pool draws, and back.
        """
        import database
        import item_catalog

        shipped = {entry["key"]
                   for tier in database.DEFAULT_GACHA_CONFIG["rewards"].values()
                   for entry in tier}
        self.assertEqual(
            [], sorted(set(item_catalog.GACHA_ELIGIBLE_ITEMS) - shipped),
            "these catalog items are drawable but no banner can award them")

    def test_known_history_exposures_are_recorded(self):
        """Each accepted historical finding must say whether it needs rotating."""
        # Both files record *this* repository's accepted history exposures and
        # are deliberately not published, so there is nothing to check in a
        # snapshot — and nothing being wrong there either.
        if not (ROOT / ".gitleaksignore").exists():
            self.skipTest("not a development checkout")
        ignore = (ROOT / ".gitleaksignore").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

        fingerprints = [
            line for line in ignore.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertTrue(fingerprints, "no fingerprints recorded")
        for fingerprint in fingerprints:
            # commit:path:rule-id:line
            self.assertEqual(4, len(fingerprint.split(":")), fingerprint)

        self.assertIn("ROTATION REQUIRED", ignore)
        self.assertIn("Known credential exposure in Git history", security)
        self.assertIn("dev.twitch.tv/console/apps", security)

    def test_workflow_actions_are_off_node20(self):
        """Node 20 leaves GitHub-hosted runners on 2026-09-16."""
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        deprecated = {
            "actions/checkout@v4", "actions/checkout@v3",
            "actions/setup-python@v5", "actions/setup-python@v4",
            "gitleaks/gitleaks-action@v2",
        }
        for action in deprecated:
            self.assertNotIn(action, workflow, f"{action} runs on Node 20")
        # The secret scan needs full history and its own job.
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("secrets:", workflow)

    def test_split_deployment_keeps_one_schema_owner(self):
        """Only the bot may create the schema, so the packaged dashboard must
        never also start an in-process one."""
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('POTATOBOT_DASHBOARD_ENABLED: "false"', compose)
        # Exec form, so the process is PID 1 and receives signals directly.
        self.assertIn('command: ["python", "dashboard_api.py"]', compose)
        # The database belongs on a shared volume, never inside the image.
        self.assertIn("data:/data", compose)

        for unit in ("potatobot.service", "potatobot-dashboard.service"):
            text = (ROOT / "deploy" / unit).read_text(encoding="utf-8")
            with self.subTest(unit=unit):
                for directive in ("User=potatobot", "Restart=on-failure",
                                  "NoNewPrivileges=true", "ProtectSystem=strict",
                                  "ReadWritePaths=/opt/potatobot"):
                    self.assertIn(directive, text)

        dashboard_unit = (ROOT / "deploy" / "potatobot-dashboard.service").read_text(
            encoding="utf-8"
        )
        # Ordering matters: the bot migrates the database first.
        self.assertIn("After=potatobot.service", dashboard_unit)

    def test_container_runs_unprivileged_and_keeps_data_outside_the_image(self):
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("USER potatobot", containerfile)
        self.assertIn('VOLUME ["/data"]', containerfile)
        self.assertIn("POTATOBOT_DB_PATH=/data/economy.db", containerfile)
        # Music playback needs ffmpeg present in the image.
        self.assertIn("ffmpeg", containerfile)

    def test_both_ignore_files_exist_and_agree(self):
        """podman reads `.containerignore`; Docker BuildKit reads only
        `.dockerignore`. `compose.yaml` offers `docker compose up -d` as an
        equivalent, so a missing second file means a Docker build copies `.env`
        into an image layer — the bot token, the Discord client secret and the
        Twitch credentials, unremovable by a later `RUN rm`.

        This is the build failure that *succeeds*, which is why it is asserted
        rather than left to whoever next edits one of the two files.
        """
        container = ROOT / ".containerignore"
        docker = ROOT / ".dockerignore"
        for path in (container, docker):
            with self.subTest(path=path.name):
                self.assertTrue(path.exists(), f"{path.name} is missing")
        self.assertEqual(container.read_text(encoding="utf-8"),
                         docker.read_text(encoding="utf-8"),
                         "the two ignore files must stay byte-identical")

    def test_the_ignore_files_exclude_every_secret_and_data_path(self):
        """Named explicitly, because the cost of one omission is a credential in
        a published image layer."""
        text = (ROOT / ".containerignore").read_text(encoding="utf-8")
        for pattern in (".env", "*.db", "*.db-wal", "backups/", "logs/",
                        ".dashboard_session_secret", "venv/", ".git"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, text)

    def test_license_carries_the_canonical_agpl_text(self):
        text = (Path(__file__).resolve().parents[1] / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3, 19 November 2007", text)
        # The summary notice alone is not distributable; require the full text.
        self.assertIn("TERMS AND CONDITIONS", text)
        self.assertIn("END OF TERMS AND CONDITIONS", text)
        self.assertGreater(len(text.splitlines()), 600)

    def test_todo_md_is_the_only_active_todo_document(self):
        todo_files = sorted(
            path.name for path in ROOT.glob("*.md")
            if path.name.lower().startswith("todo")
        )
        # The backlog is not published, so a snapshot correctly has none.
        self.assertEqual(todo_files, ["todo.md"] if (ROOT / "todo.md").exists() else [])


if __name__ == "__main__":
    unittest.main()
