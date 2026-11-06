import ast
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_harness  # noqa: E402


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

    def test_it_refuses_any_database_but_a_loopback_development_one(self):
        """Served with OAuth bypassed and migrated in place, so it may never be
        a database something else serves: loopback, `*_dev`, and not the one
        libpq's PG* environment names. All three, because a self-hoster's real
        database can be on localhost too. (On SQLite, `--db economy.db` once
        migrated and served the real file in place with OAuth bypassed.)"""
        module = self.load_script()
        module.refuse_real_database("postgresql://potatobot@127.0.0.1:55432/potatobot_dev")
        refused = {
            "postgresql://potatobot@db.example:5432/potatobot_dev": "loopback",
            "host=/var/run/postgresql dbname=potatobot_dev": "loopback",
            "postgresql://potatobot@127.0.0.1:5432/potatobot": "development database",
            "postgresql://potatobot@localhost/potatobot_dev": "PG\\* environment",
        }
        for dsn, reason in refused.items():
            with self.subTest(dsn=dsn), patch.dict(os.environ, {"PGDATABASE": "potatobot_dev"}
                                                   if "localhost" in dsn else {}):
                with self.assertRaisesRegex(SystemExit, reason):
                    module.refuse_real_database(dsn)

    def test_its_files_stay_out_of_any_real_data_directory(self):
        self.assertIn('os.environ["POTATOBOT_DATA_DIR"] = str(LOCAL_DEV_DIR)', self.SOURCE)

    def load_script(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "local_dashboard_under_test", ROOT / "scripts" / "local_dashboard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_stand_ins_satisfy_the_real_readers(self):
        """The stand-in bot is read by production code, so it drifts silently.

        The resource list grew a role `color`, the permission audit a
        `guild.default_role`, the audit feed a `guild.get_member` and the
        entitlements page `guild.stickers`; the
        stand-ins had none of them, so every local page
        load's resource fetch raised, `loadGuild` aborted, and the dashboard ran
        with no feature state — no sub-toggles, no generated sub-pages — which
        read as a broken feature rather than a broken harness.
        """
        import os
        import tempfile
        import dashboard_api
        from core import database, permission_audit

        script = self.load_script()
        guild_id = 1420070400000000001
        original_path, original_bot = database.DB_PATH, dashboard_api._dashboard_bot
        with tempfile.TemporaryDirectory() as directory:
            database.DB_PATH = os.path.join(directory, "standin.db")
            pg_harness.fresh_database()
            try:
                database.initialize_database()
                dashboard_api._dashboard_bot = script.build_dev_bot([guild_id])
                resources = dashboard_api._resources_from_bot_cache(guild_id)
                self.assertTrue(resources["roles"], "the premise: roles are listed")
                guild = dashboard_api._dashboard_bot.get_guild(guild_id)
                permission_audit.build_report(guild, {}, {})
                # The audit feed names each change's author through the bot.
                self.assertEqual("local-dev", dashboard_api._actor_name_from_bot(
                    guild_id, int(script.LOCAL_HOST_ID)))
                self.assertIsNone(dashboard_api._actor_name_from_bot(guild_id, 7))
                # The entitlements page names each granted item through the bot;
                # a live copy holding a sticker grant made that page a 500.
                for kind in ("emoji", "sticker", "sound", "role"):
                    with self.subTest(kind=kind):
                        self.assertIsNone(
                            dashboard_api._item_name_from_bot(guild_id, kind, 7))
            finally:
                database.DB_PATH = original_path
                dashboard_api._dashboard_bot = original_bot

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
    #: measure, and the only one**: tokens track characters, and a line break
    #: costs about what a space does. A line limit was dropped on 2026-10-30
    #: because it punished the bullet layout that made rules easier to read,
    #: while the character count it should have tracked fell by 4,500. A budget
    #: left far above the file cannot catch creep, so bring it down after a
    #: trim. Moving a *procedure* into a skill under `.claude/skills/` frees
    #: very little here and is not done for the size: what is left in this
    #: file is constraint, which has to stay loaded because a skill applies
    #: only when something thought to load it.
    CLAUDE_MD_MAX_CHARS = 67_000

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
        self.assertLessEqual(
            len(text), self.CLAUDE_MD_MAX_CHARS,
            f"CLAUDE.md is {len(text)} characters. Move narrative to "
            f"docs/lessons.md rather than dropping a rule.")

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

    def test_one_ignore_file_serves_both_tools(self):
        """Docker reads only `.dockerignore`; podman prefers `.containerignore`
        and falls back to `.dockerignore` when it is absent. So `.dockerignore`
        is the one file, and a `.containerignore` must not exist: podman would
        read it instead, and a copy missing `.env` bakes the bot token, the
        Discord client secret and the Twitch credentials into an image layer,
        unremovable by a later `RUN rm`.

        This is the build failure that *succeeds*, which is why it is asserted.
        Until 2026-10-22 the two files were kept byte-identical instead.
        """
        self.assertTrue((ROOT / ".dockerignore").exists(), ".dockerignore is missing")
        self.assertFalse((ROOT / ".containerignore").exists(),
                         "podman would read .containerignore instead of .dockerignore")

    def test_the_ignore_files_exclude_every_secret_and_data_path(self):
        """Named explicitly, because the cost of one omission is a credential in
        a published image layer."""
        text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (".env", ".env.postgres", "*.db", "*.db-wal", "*.dump",
                        "backups/", "logs/", ".dashboard_session_secret", "venv/", ".git"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, text)

    def test_the_ignore_files_exclude_the_local_working_files_too(self):
        """`.gitignore` kept these out of the repository but the image's
        `COPY . .` from a working tree did not -- session notes, screenshots of
        the live dashboard and rehearsal copies of a database included."""
        text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
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
        for setting in ("POTATOBOT_DATA_DIR=/data", "POTATOBOT_LOG_DIR=/data/logs",
                        "POTATOBOT_EVERYDLE_STATE_PATH=/data/everydle_state.json"):
            with self.subTest(setting=setting):
                self.assertIn(setting, containerfile)

    def test_the_generated_session_secret_follows_the_database(self):
        import stat
        import tempfile
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

    def test_the_secret_and_the_cards_follow_the_data_directory(self):
        """`POTATOBOT_DATA_DIR` names where the installation's own files live,
        apart from the database, which on PostgreSQL has no directory."""
        import tempfile
        import dashboard_api
        from core import database, welcome_card
        with tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as db_dir, \
                tempfile.TemporaryDirectory() as code_dir:
            with patch.dict(os.environ, {"POTATOBOT_DASHBOARD_SESSION_SECRET": ""}), \
                    patch.object(database, "DB_PATH", str(Path(db_dir) / "economy.db")), \
                    patch.object(database, "DATA_DIR", data), \
                    patch.object(dashboard_api, "LEGACY_SESSION_SECRET_PATH",
                                 str(Path(code_dir) / ".dashboard_session_secret")):
                generated = dashboard_api._load_session_secret()
                self.assertEqual(generated, (Path(data) / ".dashboard_session_secret")
                                 .read_text(encoding="utf-8"))
                self.assertFalse((Path(db_dir) / ".dashboard_session_secret").exists())
                self.assertEqual(Path(data) / "cards" / "7-join.png",
                                 welcome_card.background_path(7, "join"))

    def test_an_unset_or_blank_data_directory_means_beside_the_database(self):
        from core import database
        for unset in (None, ""):
            with patch.object(database, "DATA_DIR", unset), \
                    patch.object(database, "DB_PATH", "/srv/potato/economy.db"):
                self.assertEqual("/srv/potato", database.data_dir())

    def test_the_environment_value_is_read_once_and_blank_is_unset(self):
        import subprocess
        import sys
        probe = "from core import database; print(repr(database.DATA_DIR))"
        root = Path(__file__).resolve().parents[1]
        for value, expected in (("   ", "None"), ("/data", "'/data'"), (" /data ", "'/data'")):
            env = dict(os.environ, POTATOBOT_DATA_DIR=value)
            out = subprocess.run([sys.executable, "-c", probe], cwd=root, env=env,
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(expected, out.splitlines()[-1], value)

    def test_the_suite_runs_with_no_data_directory_configured(self):
        """A test redirects `DB_PATH` to a temporary file and expects the cards
        and the session secret to follow it. With `POTATOBOT_DATA_DIR` set in
        the shell they would land in that real directory instead."""
        from core import database
        self.assertIsNone(database.DATA_DIR,
                          "unset POTATOBOT_DATA_DIR before running the suite")

    def test_ci_runs_the_suite_against_a_pinned_postgresql(self):
        """The PostgreSQL tests fail without a server, so CI must provide one:
        pinned by digest, with the locale the deployment uses."""
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        verify = workflow[workflow.index("  verify:"):workflow.index("  container:")]
        self.assertRegex(verify, r"image: postgres:18@sha256:[0-9a-f]{64}")
        self.assertIn("--locale-provider=builtin --builtin-locale=C.UTF-8", verify)
        self.assertIn("POTATOBOT_TEST_DSN: postgresql://potatobot@127.0.0.1:55432/potatobot",
                      verify)

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
        "__init__.py", "announcement_templates.py", "bounded.py", "clock.py",
        "control_actions.py",
        "database.py", "deployment.py",
        "feature_access.py", "item_catalog.py", "logging_setup.py",
        "managed_messages.py", "minigame_data.py", "music_extract.py",
        "permission_audit.py", "schema_types.py",
        "settings_cache.py", "settings_registry.py", "support_tickets.py",
        "supported_games.py", "version.py", "welcome_card.py",
        "wordchain_dictionary.py",
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
        # `bot/` on the LUKS mount; PostgreSQL's `pgdata/` sits beside it.
        self.assertEqual(["/var/lib/potatobot/bot:/data:Z"], self.values("Volume"))
        environment = self.values("Environment")
        # No POTATOBOT_DB_PATH since F4: it existed only so the copy script
        # found 2.20's SQLite file (the image still sets it for operators who
        # have yet to copy). POTATOBOT_DATA_DIR is what places the bot's files.
        self.assertNotIn("POTATOBOT_DB_PATH=/data/economy.db", environment)
        for path in ("POTATOBOT_DATA_DIR=/data",
                     "POTATOBOT_LOG_DIR=/data/logs",
                     "POTATOBOT_EVERYDLE_STATE_PATH=/data/everydle_state.json"):
            with self.subTest(path=path):
                self.assertIn(path, environment)

    def test_new_files_are_not_world_readable(self):
        self.assertIn("--umask=0027", self.values("PodmanArgs"))

    def test_log_lines_stay_in_the_encrypted_volume(self):
        """The journal is on the host's plain disk and log lines name members
        (PRIVACY §9, §14): they go to /data/logs alone, and
        `deploy/potatobot-log` is how they are read."""
        self.assertIn("POTATOBOT_LOG_CONSOLE=false", self.values("Environment"))

    def test_the_image_sets_the_umask_for_every_way_it_is_started(self):
        """Compose has no umask key, and a run without the Quadlet's
        `--umask` wrote the database 0644 (measured 2026-10-30, the same image
        with its entrypoint bypassed); the image's own entrypoint closes that."""
        containerfile = (ROOT / "container" / "Containerfile").read_text(encoding="utf-8")
        entrypoint = re.search(r"^ENTRYPOINT (.+)$", containerfile, re.MULTILINE)
        self.assertIsNotNone(entrypoint)
        self.assertIn("umask 0027", entrypoint.group(1))
        self.assertIn('exec \\"$@\\"', entrypoint.group(1))
        compose = (ROOT / "container" / "compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("entrypoint:", compose, "an override would drop the umask")

    def test_the_root_filesystem_is_read_only(self):
        """Every write goes to /data; the code is not the process's to change.
        Podman adds the /tmp tmpfs itself; docker does not, so compose says so."""
        self.assertEqual(["true"], self.values("ReadOnly"))
        compose = (ROOT / "container" / "compose.yaml").read_text(encoding="utf-8")
        hardening = compose[compose.index("x-hardening:"):compose.index("services:")]
        self.assertIn("read_only: true", hardening)
        self.assertRegex(hardening, r"tmpfs:\s*\n\s*- /tmp")

    def test_secrets_come_from_a_file_outside_the_image(self):
        self.assertEqual(["/etc/potatobot/potatobot.env"], self.values("EnvironmentFile"))

    def test_the_container_keeps_the_hosts_timezone(self):
        """`core/clock.py` keeps calendar days local and reads an offset-less
        stored timestamp as local time. A container defaults to UTC, which
        moved the day boundary to 02:00 CEST and read those values two hours
        off -- seen in the log the moment the cutover started it."""
        self.assertEqual(["local"], self.values("Timezone"))

    def test_a_stop_reaches_the_bot_rather_than_timing_out(self):
        """Python as PID 1 ignores SIGTERM, so without an init every stop
        waited ten seconds and ended in SIGKILL."""
        self.assertEqual(["true"], self.values("RunInit"))

    def test_a_stop_is_not_reported_as_a_failure(self):
        """The bot ends on SIGTERM, so a stop exits 143; without this every
        clean stop left the unit `failed` (each one since 2026-10-22)."""
        service = [value for name, value in self.unit["Service"] if name == "SuccessExitStatus"]
        self.assertEqual(["143"], service)

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

    def test_the_podman_host_backup_reads_the_container_data_as_its_owner(self):
        """The offsite unit for the podman host. Each line is one that would
        fail silently: under `woody` it cannot read the `750` data directory;
        pointed at `/opt/potatobot` it uploads a file frozen at the cutover
        and still reports success; given `.env` it holds every credential for
        a job that needs none."""
        service = self.directives("potatobot-offsite-backup-podman.service")["Service"]
        values = lambda key: [value for name, value in service if name == key]
        self.assertEqual(["potatobot"], values("User"))
        # The dump is taken inside the database's container, which publishes
        # no port; the 90 is the retention docs/legal/PRIVACY.md §7 promises
        # for the pre-migration copies, and changing it means changing the policy.
        self.assertEqual(
            ['/usr/bin/python3 /usr/local/libexec/potatobot/offsite_backup.py gdrivecrypt: '
             '--dump-command "podman exec potatobot-db pg_dump --format=custom -U potatobot potatobot" '
             '--restore-command "podman exec -i potatobot-db pg_restore" '
             '--backups-dir /var/lib/potatobot/bot/backups --prune-local-backups-days 90 '
             '--workdir /var/lib/potatobot/bot/offsite-tmp'],
            values("ExecStart"))
        self.assertEqual(["/var/lib/potatobot/bot", "/home/potatobot/.config/rclone",
                          "/home/potatobot/.local/share/containers", "/run/user/984"],
                         values("ReadWritePaths"))
        self.assertEqual(["read-only"], values("ProtectHome"))
        self.assertEqual(["strict"], values("ProtectSystem"))
        self.assertEqual([], values("EnvironmentFile"))
        self.assertNotIn("/opt/potatobot",
                         " ".join(value for _, value in service))



class DatabaseUnitTests(unittest.TestCase):
    """`deploy/potatobot-db.container`, `potatobot.network` and the bot's link
    to them (2.21). Each property was established by a trial under the flags
    Quadlet generates (2026-11-01), and each would fail silently if lost."""

    directives = staticmethod(QuadletUnitTests.directives)

    def setUp(self):
        self.db = self.directives("potatobot-db.container")
        self.container = self.db["Container"]

    def values(self, key, section=None):
        entries = self.db[section] if section else self.container
        return [value for name, value in entries if name == key]

    def test_no_error_line_in_the_journal_carries_a_value(self):
        """The server logs to the journal on the plain disk: no DETAIL (a
        constraint's `Key (user_id)=(…)`) and no STATEMENT text. Measured on
        the pinned image 2026-11-02."""
        self.assertEqual(
            ["postgres -c log_error_verbosity=terse -c log_min_error_statement=panic"],
            self.values("Exec"))

    def test_the_server_is_the_one_ci_tests_against(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        ci_image = re.search(r"image: (postgres:18@sha256:[0-9a-f]{64})", workflow).group(1)
        self.assertEqual([f"docker.io/library/{ci_image}"], self.values("Image"))

    def test_the_locale_flags_reach_initdb_as_one_value(self):
        """Unquoted, systemd split the value at its spaces and the locale flags
        were dropped: a glibc-collated database sorting text differently from
        SQLite and the suite, with no error anywhere."""
        initdb = [value for value in self.values("Environment")
                  if "POSTGRES_INITDB_ARGS" in value]
        self.assertEqual(['"POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale-provider=builtin '
                          '--builtin-locale=C.UTF-8"'], initdb)

    def test_nothing_outside_the_network_reaches_the_database(self):
        self.assertEqual([], self.values("PublishPort"))
        self.assertEqual(["potatobot.network"], self.values("Network"))
        network = self.directives("potatobot.network")["Network"]
        self.assertIn(("NetworkName", "potatobot"), network)

    def test_ready_means_healthy_over_tcp(self):
        """A socket probe called the image's temporary init-time server healthy."""
        self.assertEqual(["healthy"], self.values("Notify"))
        self.assertTrue(self.values("HealthCmd")[0].startswith("pg_isready -h 127.0.0.1"))

    def test_the_superuser_password_lives_apart_from_the_bot(self):
        self.assertEqual(["/etc/potatobot/potatobot-db.env"], self.values("EnvironmentFile"))
        bot = QuadletUnitTests.directives("potatobot.container")["Container"]
        self.assertEqual(["/etc/potatobot/potatobot.env"],
                         [value for name, value in bot if name == "EnvironmentFile"])

    def test_the_data_is_on_the_encrypted_mount_and_the_bot_follows_the_database(self):
        """The guard against writing to the plain disk is that each volume's
        source is a subdirectory of the encrypted mount, absent while it is
        closed. `RequiresMountsFor=` in a user unit was the first draft's guard
        and guarded nothing: a rootless user manager sees only existing mounts,
        so `/` satisfied it (measured 2026-11-01). It must not come back
        looking like protection."""
        self.assertIn("/var/lib/potatobot/pgdata:/var/lib/postgresql:Z", self.values("Volume"))
        bot = QuadletUnitTests.directives("potatobot.container")
        self.assertIn(("Volume", "/var/lib/potatobot/bot:/data:Z"), bot["Container"])
        for name in ("potatobot.container", "potatobot-db.container",
                     "potatobot-offsite-backup-podman.service"):
            with self.subTest(unit=name):
                directives = QuadletUnitTests.directives(name)
                self.assertNotIn("RequiresMountsFor",
                                 [key for key, _ in directives.get("Unit", [])])
        unit = [value for name, value in bot["Unit"]]
        self.assertIn("potatobot-db.service", unit)
        self.assertIn(("Network", "potatobot.network"), bot["Container"])

    def test_the_container_is_read_only_and_keeps_no_umask(self):
        """The image's entrypoint could not enter its own data directories under
        `--umask=0027`; PostgreSQL makes its files 0600 itself."""
        self.assertEqual(["true"], self.values("ReadOnly"))
        self.assertEqual(["true"], self.values("NoNewPrivileges"))
        self.assertNotIn("--umask", " ".join(self.values("PodmanArgs")))

    def test_the_bots_role_is_no_superuser(self):
        script = (ROOT / "deploy" / "postgres-init" / "10-potatobot-role.sh").read_text(
            encoding="utf-8")
        self.assertIn("CREATE ROLE potatobot LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE", script)
        self.assertIn("CREATE DATABASE potatobot OWNER potatobot", script)


class VaultUnlockTests(unittest.TestCase):
    """`deploy/potatobot-unlock` opens the encrypted data volume after a boot.

    Run here against stand-in commands that record their arguments. The
    refusal is the one that matters: a file under the bare mountpoint means
    something wrote member data to the plain disk, and mounting over it would
    hide that rather than stop it.
    """

    SCRIPT = ROOT / "deploy" / "potatobot-unlock"
    STUBS = ("id", "mountpoint", "cryptsetup", "mount", "systemctl")

    def setUp(self):
        import tempfile
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.work)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.log = self.work / "calls.log"
        for name in self.STUBS:
            stub = self.bin / name
            stub.write_text(
                "#!/bin/sh\n"
                f'echo "{name} $*" >> "{self.log}"\n'
                f'[ "{name}" = id ] && {{ echo "${{FAKE_UID:-0}}"; exit 0; }}\n'
                f'[ "{name}" = mountpoint ] && exit "${{FAKE_MOUNTED:-1}}"\n'
                "exit 0\n")
            stub.chmod(0o755)
        self.mountpoint = self.work / "potatobot"
        self.mountpoint.mkdir()

    def run_script(self, *args, **env):
        environment = {"PATH": f"{self.bin}:/usr/bin:/bin",
                       "POTATOBOT_VAULT_MOUNTPOINT": str(self.mountpoint),
                       "POTATOBOT_VAULT_NAME": "probe-vault-that-is-not-open", **env}
        result = subprocess.run(["/bin/sh", str(self.SCRIPT), *args], env=environment,
                                capture_output=True, text=True)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, [c for c in calls if not c.startswith(("id", "mountpoint"))]

    def test_it_opens_mounts_hardened_and_starts_the_bot(self):
        result, calls = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(calls[0].startswith("cryptsetup open --type luks2 "), calls)
        self.assertNotIn("--key-file", calls[0], "the passphrase is typed, never stored")
        self.assertEqual(f"mount -o nodev,nosuid,noexec /dev/mapper/probe-vault-that-is-not-open "
                         f"{self.mountpoint}", calls[1])
        # A boot's failed starts are cleared before the start, one unit a call.
        self.assertEqual(["systemctl --user -M potatobot@ reset-failed potatobot-db",
                          "systemctl --user -M potatobot@ reset-failed potatobot",
                          "systemctl --user -M potatobot@ start potatobot",
                          "systemctl --user -M potatobot@ is-active potatobot"], calls[2:])

    def test_something_under_the_bare_mountpoint_is_refused(self):
        (self.mountpoint / "economy.db").write_text("")
        result, calls = self.run_script()
        self.assertEqual(1, result.returncode)
        self.assertIn("plain disk", result.stderr)
        self.assertEqual([], calls, "nothing is opened, mounted or started")

    def test_mount_only_starts_nothing(self):
        result, calls = self.run_script("--mount-only")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse([c for c in calls if c.startswith("systemctl")], calls)

    def test_an_open_volume_is_not_mounted_twice(self):
        result, calls = self.run_script("--mount-only", FAKE_MOUNTED="0")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], calls)

    def test_it_needs_root_and_a_known_argument(self):
        self.assertEqual(2, self.run_script(FAKE_UID="1000")[0].returncode)
        self.assertEqual(2, self.run_script("--force")[0].returncode)
        self.assertFalse(self.log.exists() and any(
            c.startswith(("cryptsetup", "mount ")) for c in self.log.read_text().splitlines()))

    def test_it_is_executable_and_the_bot_unit_relies_on_its_guard(self):
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))
        unit = (ROOT / "deploy" / "potatobot.container").read_text(encoding="utf-8")
        self.assertIn("Volume=/var/lib/potatobot/bot:/data:Z", unit)


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



class LogReaderScriptTests(unittest.TestCase):
    """`deploy/potatobot-log`, run through sudo as `potatobot`: the one way the
    maintainer's account reads the bot's log once it is out of the journal.
    It must take nothing but a line count, refuse to run as anyone else, and
    print the rotations in time order."""

    SCRIPT = ROOT / "deploy" / "potatobot-log"

    def run_script(self, *args, user=None, log_dir=None):
        import getpass
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if log_dir is not None:
            env["POTATOBOT_LOG_READER_DIR"] = str(log_dir)
        # `id -un` is the identity check; a stand-in `id` plays `potatobot`.
        if user is not None:
            import tempfile
            stub = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, stub)
            with open(os.path.join(stub, "id"), "w") as handle:
                handle.write(f"#!/bin/sh\necho {user}\n")
            os.chmod(os.path.join(stub, "id"), 0o755)
            env["PATH"] = stub + os.pathsep + env["PATH"]
        self.assertNotEqual("potatobot", getpass.getuser())
        return subprocess.run(["sh", str(self.SCRIPT), *args], env=env,
                              capture_output=True, text=True)

    def test_it_refuses_anyone_but_potatobot(self):
        self.assertEqual(2, self.run_script("10").returncode)

    def test_it_takes_only_a_line_count(self):
        for args in (["abc"], ["-1"], ["0"], ["100001"], ["5", "6"], ["5;id"]):
            with self.subTest(args=args):
                self.assertEqual(2, self.run_script(*args, user="potatobot").returncode)

    def test_it_reads_the_rotations_oldest_first(self):
        import tempfile
        logs = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, logs)
        (logs / "bot.log.2").write_text("a\nb\n")
        (logs / "bot.log.1").write_text("c\n")
        (logs / "bot.log").write_text("d\ne\n")
        result = self.run_script("4", user="potatobot", log_dir=logs)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("b\nc\nd\ne\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
