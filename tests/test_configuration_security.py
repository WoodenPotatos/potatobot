import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class SettingRegistryCoverageTests(unittest.TestCase):
    """Every registered setting must be reachable from the dashboard.

    A value nothing labels or sections is not configurable at all for an
    operator who only has the control plane, and nothing else in the suite
    would notice one being added with neither.
    """

    def test_every_setting_has_an_operator_facing_label(self):
        """The dashboard renders the label straight from the registry, so a
        missing one shows the operator a raw `[dashboard.settings.x]`."""
        from core.settings_registry import SETTING_DEFINITIONS

        labels = json.loads(
            (ROOT / "locales" / "hu.json").read_text(encoding="utf-8")
        )["dashboard"]["settings"]
        missing = sorted(
            key for key in SETTING_DEFINITIONS if not labels.get(key)
        )
        self.assertEqual([], missing)

    def test_every_setting_page_has_a_section_heading(self):
        from core.settings_registry import SETTING_DEFINITIONS

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
        from core import settings_cache

        original = settings_cache.database.get_instance_settings
        settings_cache.database.get_instance_settings = (
            lambda: (_ for _ in ()).throw(RuntimeError("no database")))
        try:
            self.assertFalse(settings_cache.load_instance_sync())
        finally:
            settings_cache.database.get_instance_settings = original


class LocalDevelopmentDashboardTests(unittest.TestCase):
    """`scripts/local_dashboard.py` runs the control plane with no Discord.

    The one load-bearing property that is not visible at a glance: it must not
    be able to serve a real installation.
    """

    SOURCE = (ROOT / "scripts" / "local_dashboard.py").read_text(encoding="utf-8")

    def test_the_legacy_guild_id_is_still_pinned_to_zero(self):
        """A dead guard here is cheap insurance against a live one elsewhere.

        `POTATOBOT_LEGACY_GUILD_ID` has a reader in `main.py`, which uses it to
        adopt a legacy database into scoped accounts. This script never starts a
        bot, so nothing on its path reads the variable today — but the script
        working on a private-profile, single-active-guild copy is exactly the
        shape that reader looks for, so pinning it to 0 costs nothing and
        survives the script growing a path that does.
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

    def load_script(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "local_dashboard_under_test", ROOT / "scripts" / "local_dashboard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_refuses_to_serve_a_real_database_in_place(self):
        """`copy_database` reuses an existing target, so `--db economy.db`
        migrated and served the real file with OAuth bypassed."""
        import os
        import tempfile
        from unittest.mock import patch
        script = self.load_script()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            configured = Path(directory) / "configured.db"
            with patch.dict(os.environ, {"POTATOBOT_DB_PATH": str(configured)}):
                for target in (source, configured, ROOT / "economy.db",
                               Path(os.path.relpath(ROOT / "economy.db"))):
                    with self.subTest(target=str(target)):
                        with self.assertRaises(SystemExit):
                            script.refuse_real_database(source, target)
                script.refuse_real_database(source, Path(directory) / "copy.db")

    def test_the_injected_session_is_loopback_only_with_a_per_run_token(self):
        import flask
        script = self.load_script()
        tokens = []
        for _ in range(2):
            app = flask.Flask("probe")
            app.secret_key = "probe"
            stand_in = type("Module", (), {"app": app})

            @app.route("/probe")
            def probe():
                return flask.session.get("csrf_token", "")

            script.install_session_injection(stand_in, [1])
            client = app.test_client()
            local = client.get("/probe", headers={"Host": "127.0.0.1:5001"})
            self.assertEqual(200, local.status_code)
            tokens.append(local.get_data(as_text=True))
            rebound = app.test_client().get("/probe",
                                            headers={"Host": "attacker.example"})
            self.assertEqual(403, rebound.status_code)
        self.assertNotEqual(tokens[0], tokens[1])
        self.assertNotIn("local-development-csrf-token", tokens)

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
    def test_environment_template_documents_twitch_credentials(self):
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("TWITCH_CLIENT_ID=", template)
        self.assertIn("TWITCH_CLIENT_SECRET=", template)

    #: What `CLAUDE.md` may cost. It is loaded into context on every turn of every
    #: session, so its size is paid continuously rather than when somebody opens
    #: it. Set a little above what the file currently is, which leaves room to
    #: record a rule and not room to retell an incident. **Characters are the
    #: honest measure** — the 2026-09-17 pass cut 33% of them while the line
    #: count moved less, because it rewrapped paragraphs that had been single
    #: 400-character lines. A budget left far above the file cannot catch creep,
    #: so bring both down after a trim. Moving a *procedure* into a skill under
    #: `.claude/skills/` frees very little here and is not done for the size:
    #: what is left in this file is constraint, which has to stay loaded because
    #: a skill applies only when something thought to load it.
    CLAUDE_MD_MAX_LINES = 1345
    CLAUDE_MD_MAX_CHARS = 105_600

    def test_the_instruction_file_stays_within_its_budget(self):
        """Nothing noticed `CLAUDE.md` reaching six times its recommended size.

        That is the same silent-absence failure the file itself keeps warning
        about, applied to the file itself: no error, no log line, just a session
        that got slower every month. The fix when this fails is to move the
        *narrative* to `docs/lessons.md`, or one subsystem's detail to
        `docs/subsystems/` — neither is loaded — and keep the
        rule with one clause of why. Deleting a rule to get under the budget is
        the one wrong answer.
        """
        if not (ROOT / "CLAUDE.md").exists():
            # Excluded from a published tree by name, same as the skills below.
            return
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        lines = len(text.splitlines())
        self.assertLessEqual(
            lines, self.CLAUDE_MD_MAX_LINES,
            f"CLAUDE.md is {lines} lines. Move narrative to docs/lessons.md "
            f"rather than dropping a rule.")
        self.assertLessEqual(
            len(text), self.CLAUDE_MD_MAX_CHARS,
            f"CLAUDE.md is {len(text)} characters. Same remedy.")

    def test_every_skill_declares_what_it_is_for(self):
        """A skill with no `description` is one Claude will never load on its own.

        That is this project's recurring failure shape applied to its own
        tooling: the skill exists, `/name` still works, and the automatic path
        silently does nothing. The body is only worth writing if the frontmatter
        can be matched against, so the description is required rather than
        recommended here. Skills are also excluded from the public snapshot by
        the `.claude/` prefix, which is asserted alongside so a future publisher
        change cannot start shipping them.
        """
        skills = ROOT / ".claude" / "skills"
        if not skills.is_dir():
            return
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "publish_public", ROOT / "scripts" / "publish_public.py")
        publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publisher)

        found = sorted(child for child in skills.iterdir() if child.is_dir())
        self.assertTrue(found, "docs/ says skills live here; none were found")
        for directory in found:
            definition = directory / "SKILL.md"
            self.assertTrue(
                definition.is_file(),
                f"{directory.name} is a skill directory with no SKILL.md")
            text = definition.read_text(encoding="utf-8")
            self.assertTrue(
                text.startswith("---\n"),
                f"{directory.name}/SKILL.md has no frontmatter block")
            frontmatter = text.split("---", 2)[1]
            description = re.search(
                r"^description:\s*(\S.*)$", frontmatter, re.M)
            self.assertIsNotNone(
                description,
                f"{directory.name} declares no description, so nothing will "
                f"load it unless the operator types /{directory.name}")
            self.assertGreater(
                len(description.group(1).strip()), 40,
                f"{directory.name}'s description is too short to match against")
            relative = f".claude/skills/{directory.name}/SKILL.md"
            self.assertTrue(
                publisher.is_excluded(relative),
                f"{relative} would ship in the public snapshot")

    def test_the_private_documents_are_never_published(self):
        """Each names this deployment, its channels and its dates, which is
        exactly why the narrative was allowed to move to them. A document that
        exists to absorb detail from CLAUDE.md inherits CLAUDE.md's exclusion,
        or the trim publishes what the file it came from never did."""
        if not (ROOT / "scripts" / "publish_public.py").exists():
            # The publisher that would answer this is excluded from a
            # published tree by name, for the reason this test asserts.
            return
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "publish_public", ROOT / "scripts" / "publish_public.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("docs/lessons.md", module.EXCLUDED_PATHS)
        self.assertIn("CLAUDE.md", module.EXCLUDED_PATHS)
        self.assertIn("docs/deployment_host.md", module.EXCLUDED_PATHS)

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
        from core import database

        # Take the allowlist's own regexes out of the file and run them the way
        # gitleaks does — against the matched text — rather than reconstructing
        # the alternation, which is what drifted last time.
        config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        sources = re.findall(r"'''(.*?)'''", config, re.DOTALL)
        patterns = [re.compile(source) for source in sources if "key" in source]
        self.assertTrue(patterns, "no reward-key allowlist regex in .gitleaks.toml")
        # `new_banner_config()` as well as the shipped table. Since no shipped
        # reward is a coin any more, its `coins_250` placeholder is the only
        # `key": "coins_..."` literal left in `database.py` — so without this the
        # allowlist alternative covering it would be checked by nothing, and
        # removing that alternative would turn CI red for a reason nobody could
        # trace back to here.
        keys = {
            entry["key"]
            for table in (database.DEFAULT_GACHA_CONFIG["rewards"],
                          database.new_banner_config()["rewards"])
            for tier in table.values()
            for entry in tier
        }
        unmatched = [
            key for key in sorted(keys)
            if not any(pattern.search(f'key": "{key}"') for pattern in patterns)
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
        from core import database
        from core import item_catalog

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

        # Every fingerprint block carries a verdict. This used to assert the
        # literal "ROTATION REQUIRED", which pinned the ledger to the day one
        # credential was still open -- the rotation then failed the build. The
        # invariant is that each finding *says* where it stands, whichever of
        # the three that is.
        verdicts = ("ROTATED 20", "NOTHING TO ROTATE", "ROTATION REQUIRED")
        blocks = [block for block in ignore.split("\n# ---")[1:]]
        self.assertEqual(len(blocks), 5, "one block per historical finding")
        for block in blocks:
            self.assertIn("# Status:", block, block[:80])
            self.assertTrue(any(v in block for v in verdicts), block[:80])
        self.assertIn("Known credential exposure in Git history", security)
        # The procedure stays even after rotation, for the next time.
        self.assertIn("dev.twitch.tv/console/apps", security)

    def test_workflow_actions_are_off_node20(self):
        """Node 20 leaves GitHub-hosted runners on 2026-09-16."""
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        deprecated = {
            "actions/checkout@v4", "actions/checkout@v3",
            "actions/setup-python@v5", "actions/setup-python@v4",
            "gitleaks/gitleaks-action@v2",
        }
        # Restated when the actions were pinned by commit: the version now
        # lives in the `# vN` comment beside the SHA, so that is what is read.
        uses = re.findall(r"uses:\s*([\w./-]+)@[0-9a-f]{40}\s+#\s*(v\d+)", workflow)
        self.assertTrue(uses)
        for action, version in uses:
            self.assertNotIn(f"{action}@{version}", deprecated,
                             f"{action}@{version} runs on Node 20")
        # The secret scan needs full history and its own job.
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("secrets:", workflow)

    def test_every_action_is_pinned_to_a_commit(self):
        """A tag is a moving name; whoever controls the action's repository
        can repoint it, and the next run executes that code with our token."""
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", workflow, re.MULTILINE)
        self.assertTrue(uses)
        for reference, rest in uses:
            with self.subTest(action=reference):
                self.assertRegex(reference, r"@[0-9a-f]{40}$")
                self.assertRegex(rest, r"#\s*v\d+", "keep the tag as a comment")

    def test_a_pull_request_is_scanned_with_the_base_branchs_allowlist(self):
        """Otherwise one change can add a secret and the entry that hides it."""
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('git checkout "origin/${GITHUB_BASE_REF}" -- .gitleaks.toml .gitleaksignore',
                      workflow)
        self.assertLess(workflow.index("GITHUB_BASE_REF"),
                        workflow.index("gitleaks/gitleaks-action@"))

    def test_split_deployment_keeps_one_schema_owner(self):
        """Only the bot may create the schema, so the packaged dashboard must
        never also start an in-process one.

        `User=woody`, not a dedicated `potatobot` system account: this test
        used to assert `User=potatobot`, matching what both unit files
        claimed -- but no such account exists on the deployed host, and the
        live units had been hand-patched to `woody` without either the repo
        files or this assertion ever being updated to match. Caught
        2026-09-25 when a new unit copied the same stale pattern and failed
        outright ("Failed to determine user credentials: No such process").
        A container's own `USER potatobot` (`test_container_runs_unprivileged...`
        below) is unrelated -- that account is created fresh at image build
        time, not assumed to already exist on some host."""
        compose = (ROOT / "container" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('POTATOBOT_DASHBOARD_ENABLED: "false"', compose)
        # Exec form, so the process is PID 1 and receives signals directly.
        self.assertIn('command: ["python", "dashboard_api.py"]', compose)
        # The database belongs on a shared volume, never inside the image.
        self.assertIn("data:/data", compose)

        for unit in ("potatobot.service", "potatobot-dashboard.service"):
            text = (ROOT / "deploy" / unit).read_text(encoding="utf-8")
            with self.subTest(unit=unit):
                for directive in ("User=woody", "Restart=on-failure",
                                  "NoNewPrivileges=true", "ProtectSystem=strict",
                                  "ReadWritePaths=/opt/potatobot",
                                  # The audit's additions. `UMask=0027` is the one
                                  # that would have kept a migration's backup of
                                  # every member's balance from landing 0644.
                                  "UMask=0027", "CapabilityBoundingSet=",
                                  "SystemCallFilter=@system-service",
                                  "SystemCallErrorNumber=EPERM",
                                  "PrivateDevices=true", "ProtectProc=invisible",
                                  "ProtectClock=true", "ProtectHostname=true",
                                  "RemoveIPC=true"):
                    self.assertIn(directive, text)

        # Tried on the host and kept (2026-10-15), so it ships. The dashboard
        # unit was never measured with it and still inherits systemd's `false`.
        bot_unit = (ROOT / "deploy" / "potatobot.service").read_text(encoding="utf-8")
        self.assertIn("\nMemoryDenyWriteExecute=true\n", bot_unit)

        dashboard_unit = (ROOT / "deploy" / "potatobot-dashboard.service").read_text(
            encoding="utf-8"
        )
        # Ordering matters: the bot migrates the database first.
        self.assertIn("After=potatobot.service", dashboard_unit)

    def test_container_runs_unprivileged_and_keeps_data_outside_the_image(self):
        containerfile = (ROOT / "container" / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("USER potatobot", containerfile)
        # Pinned by digest: a tag is a moving name.
        self.assertRegex(containerfile, r"FROM python:3\.13-slim@sha256:[0-9a-f]{64}")
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

    def test_the_ignore_files_exclude_the_local_working_files_too(self):
        """`.gitignore` kept these out of the repository but the image's
        `COPY . .` from a working tree did not -- session notes, screenshots of
        the live dashboard and rehearsal copies of a database included."""
        text = (ROOT / ".containerignore").read_text(encoding="utf-8")
        for pattern in ("handoff/", "graphify-out/", "Screenshot*.png",
                        "migration-rehearsal-*/", "data/everydle_state.json",
                        "node_modules/"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, text.splitlines())

    def test_the_image_code_is_not_writable_by_the_runtime_user(self):
        """Code the process can rewrite is code a compromise can persist in,
        so `/app` stays root-owned and every runtime write is on the volume."""
        containerfile = (ROOT / "container" / "Containerfile").read_text(encoding="utf-8")
        instructions = [line for line in containerfile.splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
        self.assertNotIn("chown potatobot:potatobot /app", "\n".join(instructions))
        self.assertIn("COPY . .", instructions)
        for setting in ("POTATOBOT_LOG_DIR=/data/logs",
                        "POTATOBOT_EVERYDLE_STATE_PATH=/data/everydle_state.json"):
            with self.subTest(setting=setting):
                self.assertIn(setting, containerfile)

    def test_the_generated_session_secret_follows_the_database(self):
        import stat
        import tempfile
        from unittest.mock import patch
        import dashboard_api
        from core import database
        with tempfile.TemporaryDirectory() as directory, \
                tempfile.TemporaryDirectory() as code_dir:
            legacy = Path(code_dir) / ".dashboard_session_secret"
            with patch.dict(os.environ, {"POTATOBOT_DASHBOARD_SESSION_SECRET": ""}), \
                    patch.object(database, "DB_PATH", str(Path(directory) / "economy.db")), \
                    patch.object(dashboard_api, "LEGACY_SESSION_SECRET_PATH", str(legacy)):
                generated = dashboard_api._load_session_secret()
                beside = Path(directory) / ".dashboard_session_secret"
                self.assertEqual(generated, beside.read_text(encoding="utf-8"))
                self.assertEqual(0o600, stat.S_IMODE(beside.stat().st_mode))
                self.assertFalse(legacy.exists())

                # A secret at the old location still wins, so moving the default
                # does not end every existing session.
                legacy.write_text("x" * 40, encoding="utf-8")
                self.assertEqual("x" * 40, dashboard_api._load_session_secret())

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


class RepositoryLayoutTests(unittest.TestCase):
    """The root holds what is *run*; `core/` holds what they import.

    Sixteen modules used to sit in the root with nothing distinguishing the
    three that get executed from the thirteen that only ever got imported. The
    split is only worth anything if it holds, and nothing about adding another
    shared module to the root would look wrong at review time -- it would
    simply work, the way all thirteen did. `SHARED_MODULES` below is the
    current set.

    The three entry points stay at the root because that is where the things
    that run them look: `deploy/potatobot.service` names `main.py` by absolute
    path, `container/compose.yaml` and the two units name `dashboard_api.py`
    and `update_db.py`. Moving one is a production change, which is the other
    half of why this is pinned.
    """

    ENTRY_POINTS = {"main.py", "dashboard_api.py", "update_db.py"}

    def tracked_root_modules(self):
        """Tracked files only -- a local scratch file is not committed clutter."""
        listed = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=ROOT,
            capture_output=True, text=True, check=True).stdout.split()
        return {name for name in listed if "/" not in name}

    def test_the_root_holds_only_the_entry_points(self):
        self.assertEqual(self.ENTRY_POINTS, self.tracked_root_modules())

    # Named rather than counted. A threshold reads as a guard and is not one:
    # `len(modules) > 10` passes with three of them deleted, which is how the
    # hardening walk in `test_hardening_invariants.py` came to cover nothing.
    SHARED_MODULES = {
        "__init__.py", "bounded.py", "clock.py", "control_actions.py",
        "database.py", "deployment.py",
        "feature_access.py", "item_catalog.py", "logging_setup.py",
        "managed_messages.py", "minigame_data.py", "permission_audit.py",
        "settings_cache.py", "settings_registry.py", "support_tickets.py",
        "supported_games.py", "version.py", "wordchain_dictionary.py",
    }

    def test_the_shared_modules_live_in_core(self):
        modules = {p.name for p in (ROOT / "core").glob("*.py")}
        self.assertEqual(self.SHARED_MODULES, modules)
        self.assertEqual(set(), modules & self.ENTRY_POINTS)

    def test_every_entry_point_is_named_by_what_runs_it(self):
        """A root module nothing invokes has no reason to be at the root."""
        invocations = "\n".join(
            (ROOT / name).read_text(encoding="utf-8") for name in (
                "container/Containerfile", "container/compose.yaml", "README.md",
                "deploy/potatobot.service", "deploy/potatobot-dashboard.service"))
        for entry in sorted(self.ENTRY_POINTS):
            with self.subTest(entry=entry):
                self.assertIn(entry, invocations)

    def test_core_re_exports_nothing(self):
        """Two spellings for one module is worse than a long import line."""
        source = (ROOT / "core" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [type(node).__name__ for node in tree.body
                     if not isinstance(node, ast.Expr)]
        self.assertEqual([], offenders)


class OutboxIsDrainedInEveryTopologyTests(unittest.TestCase):
    """Only the bot executes `control_actions`, so it must always start the worker.

    It was started only when `POTATOBOT_DASHBOARD_ENABLED` was true, because the
    worker lived in `dashboard_api` and the bot imported that module only then.
    Under the two-process split every publish, deletion, erasure and fulfilment
    notice was queued with nothing to run it, and nothing about that is an
    error: the dashboard reports "queued" and the action waits forever.
    """

    def main_tree(self):
        return ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))

    @staticmethod
    def guarded_by_dashboard_flag(tree, target):
        """Whether any `if` enclosing `target` tests `dashboard_enabled`."""
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "dashboard_enabled" in ast.unparse(node.test):
                if any(child is target for child in ast.walk(node)):
                    return True
        return False

    def test_main_starts_the_worker_whatever_the_dashboard_flag(self):
        tree = self.main_tree()
        starts = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "control_actions.control_action_worker"
        ]
        self.assertEqual(1, len(starts))
        self.assertFalse(self.guarded_by_dashboard_flag(tree, starts[0]))

    def test_main_imports_the_worker_unconditionally(self):
        imports = [node for node in self.main_tree().body
                   if isinstance(node, ast.ImportFrom) and node.module == "core"
                   and any(alias.name == "control_actions" for alias in node.names)]
        self.assertEqual(1, len(imports))

    def test_the_worker_module_brings_no_dashboard_with_it(self):
        """Importing `dashboard_api` loads, and may create, the session secret,
        which is why the worker could not simply be imported from there."""
        probe = ("import sys, core.control_actions; "
                 "print(sorted(m for m in ('dashboard_api', 'flask', 'waitress') "
                 "if m in sys.modules))")
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT,
            capture_output=True, text=True, check=True)
        self.assertEqual("[]", result.stdout.strip())

    def test_dashboard_api_no_longer_defines_the_worker(self):
        """One definition, so the two processes cannot grow two workers."""
        tree = ast.parse((ROOT / "dashboard_api.py").read_text(encoding="utf-8"))
        defined = {node.name for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertEqual(set(), {name for name in defined
                                 if name == "control_action_worker"
                                 or name.startswith("execute_")})


class QuadletUnitTests(unittest.TestCase):
    """`deploy/potatobot.container` is the podman migration's unit, and each
    property pinned here is one the bare-metal unit gets from systemd instead.

    None of them shows up as an error when it is lost: a dashboard published on
    every interface still answers, a named volume still holds the database, a
    container running as root still runs, and a 0022 umask still writes the
    backups -- readable by every account on the host.
    """

    @staticmethod
    def directives(name):
        """Section -> list of (key, value), keeping repeated keys such as
        `Environment=`, which a ConfigParser would collapse to the last one."""
        sections: dict[str, list[tuple[str, str]]] = {}
        current = None
        for raw in (ROOT / "deploy" / name).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = sections.setdefault(line[1:-1], [])
                continue
            key, _, value = line.partition("=")
            current.append((key, value))
        return sections

    def setUp(self):
        self.unit = self.directives("potatobot.container")
        self.container = self.unit["Container"]

    def values(self, key):
        return [value for name, value in self.container if name == key]

    def test_the_dashboard_is_published_on_loopback_only(self):
        self.assertEqual(["127.0.0.1:5000:5000"], self.values("PublishPort"))

    def test_the_container_runs_as_the_images_unprivileged_account(self):
        """keep-id onto the uid the Containerfile creates, so the host account
        and the container's `potatobot` own the bind mount alike."""
        containerfile = (ROOT / "container" / "Containerfile").read_text(encoding="utf-8")
        uid = re.search(r"useradd [^\n]*--uid (\d+) potatobot", containerfile).group(1)
        self.assertEqual([f"keep-id:uid={uid},gid={uid}"], self.values("UserNS"))
        self.assertNotIn("User", dict(self.container))

    def test_data_is_a_host_bind_mount_with_an_selinux_label(self):
        self.assertEqual(["/var/lib/potatobot:/data:Z"], self.values("Volume"))
        environment = self.values("Environment")
        for path in ("POTATOBOT_DB_PATH=/data/economy.db",
                     "POTATOBOT_LOG_DIR=/data/logs",
                     "POTATOBOT_EVERYDLE_STATE_PATH=/data/everydle_state.json"):
            with self.subTest(path=path):
                self.assertIn(path, environment)

    def test_new_files_are_not_world_readable(self):
        self.assertIn("--umask=0027", self.values("PodmanArgs"))

    def test_secrets_come_from_a_file_outside_the_image(self):
        self.assertEqual(["/etc/potatobot/potatobot.env"], self.values("EnvironmentFile"))

    def test_the_hardening_subset_matches_compose(self):
        self.assertEqual(["true"], self.values("NoNewPrivileges"))
        self.assertEqual(["all"], self.values("DropCapability"))

    def test_the_restart_backoff_matches_the_bare_metal_unit(self):
        """GATEWAY_GRACE_SECONDS is sized against these (RestartLimitTests);
        the two ways of running the bot must give systemd the same limits."""
        bare = dict(self.directives("potatobot.service")["Unit"])
        unit = dict(self.unit["Unit"])
        for key in ("StartLimitIntervalSec", "StartLimitBurst"):
            with self.subTest(key=key):
                self.assertEqual(bare[key], unit[key])
        self.assertEqual("on-failure", dict(self.unit["Service"])["Restart"])


class PinnedDependencyTests(unittest.TestCase):
    """`pyproject.toml` and `requirements.lock` must pin the same versions.

    Nothing held them together, and a dependency bot found the gap first: it
    raised a pull request moving `yt-dlp` in `pyproject.toml` alone, which
    looked mergeable and would have left the packaging metadata claiming one
    version while the venv and `pip_audit -r requirements.lock` used another.

    The lockfile is the authority for what actually runs, so a disagreement is
    never resolved by preferring one — it is a bump that only got halfway, and
    the fix is to finish it.
    """

    PIN = re.compile(r"^\s*[\"']?([A-Za-z0-9._-]+)==([^\"'\s]+)")

    def _pins(self, path, lines):
        found = {}
        for line in lines:
            match = self.PIN.match(line)
            if match:
                # PEP 503: the distribution name is case-insensitive and
                # `-`/`_`/`.` are equivalent, so `yt-dlp` and `yt_dlp` are one
                # package and must not read as two.
                name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
                found[name] = match.group(2)
        self.assertTrue(found, f"no pinned requirement found in {path}")
        return found

    def test_the_two_files_agree_on_every_shared_pin(self):
        project = self._pins("pyproject.toml", (
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
            .split("dependencies = [")[1].split("]")[0].splitlines()))
        lock = self._pins("requirements.lock", (
            (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()))

        disagreements = {name: (version, lock[name])
                         for name, version in project.items()
                         if name in lock and lock[name] != version}
        self.assertEqual({}, disagreements)

    def test_every_declared_dependency_is_locked(self):
        """A dependency the lockfile never names is one nothing installs."""
        project = self._pins("pyproject.toml", (
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
            .split("dependencies = [")[1].split("]")[0].splitlines()))
        lock = self._pins("requirements.lock", (
            (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()))
        self.assertEqual(set(), set(project) - set(lock))

    # Both lockfiles are generated by `uv pip compile --generate-hashes` and
    # installed with `--require-hashes` (docs/development.md). Before that the
    # lock named only the top-level packages, so everything beneath them
    # floated on every fresh install.
    LOCKS = ("requirements.lock", "requirements-dev.lock")

    def _lock_pins(self, path):
        return self._pins(path, (ROOT / path).read_text(encoding="utf-8").splitlines())

    def test_every_locked_pin_carries_a_hash(self):
        """`--require-hashes` refuses an unhashed pin, so one would break every install."""
        for path in self.LOCKS:
            unhashed, current = [], None
            for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
                match = self.PIN.match(line)
                if match:
                    if current:
                        unhashed.append(current)
                    current = match.group(1)
                elif current and line.strip().startswith("--hash=sha256:"):
                    current = None
            if current:
                unhashed.append(current)
            with self.subTest(lock=path):
                self.assertEqual([], unhashed)

    def test_the_dev_lock_is_the_runtime_lock_plus_tooling(self):
        """CI tests with the dev lock, so a runtime pin moving there tests the wrong tree."""
        runtime = self._lock_pins("requirements.lock")
        dev = self._lock_pins("requirements-dev.lock")
        self.assertEqual(set(), set(runtime) - set(dev))
        self.assertEqual({}, {name: (version, dev[name])
                              for name, version in runtime.items()
                              if dev[name] != version})

    def test_the_dev_group_is_locked_at_its_declared_versions(self):
        group = self._pins("pyproject.toml [dependency-groups]", (
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
            .split("[dependency-groups]")[1].split("]")[0].splitlines()))
        dev = self._lock_pins("requirements-dev.lock")
        self.assertEqual({}, {name: (version, dev.get(name))
                              for name, version in group.items()
                              if dev.get(name) != version})

    def test_every_install_path_requires_hashes(self):
        for path in ("container/Containerfile", ".github/workflows/ci.yml",
                     "docs/installation.md", "scripts/update_readme.py"):
            installs = [line for line in
                        (ROOT / path).read_text(encoding="utf-8").splitlines()
                        if "-m pip install" in line and "requirements" in line]
            with self.subTest(path=path):
                self.assertTrue(installs, f"no lockfile install found in {path}")
                self.assertEqual([], [line for line in installs
                                      if "--require-hashes" not in line])


if __name__ == "__main__":
    unittest.main()
