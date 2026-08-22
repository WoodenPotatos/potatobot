"""The publisher's job is to refuse.

This repository's history can never be mirrored — it carries a retired Discord
token and the Twitch credentials — so the public repository gets snapshots, and
a snapshot that "mostly" sanitised is worse than none: the leak ships and the
run reports success. Every check here is therefore a refusal path, and the
rendering is barely tested by comparison.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.publisher = _load("publish_public")

    def test_the_private_files_are_excluded(self):
        for path in ("CLAUDE.md", "todo.md", "config.json", "SECURITY.md",
                     ".gitleaksignore", "docs/performance_recovery_plan.md",
                     "docs/config_retirement_plan.md",
                     ".claude/settings.local.json",
                     "dashboard-reference/shot.png"):
            with self.subTest(path=path):
                self.assertTrue(self.publisher.is_excluded(path))

    def test_the_shipped_files_are_not_excluded(self):
        for path in ("main.py", "database.py", "dashboard/script.js",
                     "locales/hu.json", "docs/installation.md",
                     "docs/level_setup.md", "config.json.example",
                     ".env.example", "README.md", "CHANGELOG.md",
                     "deploy/potatobot.service", "pyproject.toml"):
            with self.subTest(path=path):
                self.assertFalse(self.publisher.is_excluded(path))

    def test_every_excluded_path_still_exists(self):
        """An exclusion for a file nobody has any more is a stale rule that
        stops describing the repository and starts lying about it."""
        for path in self.publisher.EXCLUDED_PATHS:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(),
                                f"{path} is excluded but no longer exists")


class ForbiddenFileTests(unittest.TestCase):
    def setUp(self):
        self.publisher = _load("publish_public")

    def test_secrets_and_data_never_ship(self):
        for path in (".env", "economy.db", "economy.db-wal", "economy.db-shm",
                     "economy.db.backup-v8-20260822-140216",
                     "backups/economy.db", "logs/bot.log",
                     ".dashboard_session_secret"):
            with self.subTest(path=path):
                self.assertTrue(
                    any(rule.search(path) for rule in self.publisher.FORBIDDEN_PATTERNS),
                    f"{path} is not caught by any forbidden pattern")

    def test_ordinary_files_are_not_caught(self):
        for path in ("database.py", "docs/installation.md", "data/dbdle/killers.json",
                     "dashboard/style.css"):
            with self.subTest(path=path):
                self.assertFalse(
                    any(rule.search(path) for rule in self.publisher.FORBIDDEN_PATTERNS))


class BuiltTreeVerificationTests(unittest.TestCase):
    """Verification runs against the tree on disk, not against the intent."""

    def setUp(self):
        self.publisher = _load("publish_public")

    def _tree(self, files: dict) -> Path:
        import tempfile
        directory = Path(tempfile.mkdtemp(prefix="publish-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        for name, content in files.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return directory

    def test_a_planted_excluded_file_is_caught(self):
        tree = self._tree({"main.py": "x = 1\n", "CLAUDE.md": "private\n"})
        problems = self.publisher.verify_tree(tree, ["main.py", "CLAUDE.md"])
        self.assertTrue(any("CLAUDE.md" in p for p in problems), problems)

    def test_a_planted_secret_file_is_caught_even_if_unlisted(self):
        """The disk walk exists for exactly this: a file the list did not
        mention still ships if it is in the directory."""
        tree = self._tree({"main.py": "x = 1\n", ".env": "DISCORD_TOKEN=abc\n"})
        problems = self.publisher.verify_tree(tree, ["main.py"])
        self.assertTrue(any(".env" in p for p in problems), problems)

    def test_a_snowflake_in_source_is_caught(self):
        tree = self._tree({"settings_registry.py": "DEFAULT = 1420070400000009999\n"})
        problems = self.publisher.verify_tree(tree, ["settings_registry.py"])
        self.assertTrue(any("Discord identifiers" in p for p in problems), problems)

    def test_snowflakes_are_tolerated_where_they_belong(self):
        tree = self._tree({"data/dbdle/x.json": '{"emoji": "<:a:1420070400000009999>"}\n'})
        problems = self.publisher.verify_tree(tree, ["data/dbdle/x.json"])
        self.assertFalse(any("Discord identifiers" in p for p in problems), problems)

    def test_a_missing_scanner_is_itself_a_refusal(self):
        """Publishing unscanned is not a degraded mode, it is a refusal."""
        import shutil as shutil_module
        original = self.publisher.shutil.which
        self.publisher.shutil.which = lambda name: None
        self.addCleanup(lambda: setattr(self.publisher.shutil, "which", original))
        tree = self._tree({"main.py": "x = 1\n"})
        problems = self.publisher.verify_tree(tree, ["main.py"])
        self.assertTrue(any("gitleaks" in p for p in problems), problems)
        self.assertIsNotNone(shutil_module)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.publisher = _load("publish_public")

    def test_an_alpha_version_is_not_publishable(self):
        problems = []
        self.publisher.check_version_is_publishable(problems, "2.1.0a4")
        self.assertTrue(any("alpha" in p for p in problems), problems)

    def test_a_beta_version_is_publishable(self):
        problems = []
        self.publisher.check_version_is_publishable(problems, "2.1.0b1")
        self.assertEqual([], problems)

    def test_the_shipped_defaults_carry_no_identifiers(self):
        problems = []
        self.publisher.check_no_snowflake_defaults(problems)
        self.assertEqual([], problems)

    def test_the_example_configs_carry_no_identifiers(self):
        problems = []
        self.publisher.check_example_configs(problems)
        self.assertEqual([], problems)


if __name__ == "__main__":
    unittest.main()


class SyntheticIdTests(unittest.TestCase):
    """The escape valve, and the limit on it."""

    def setUp(self):
        self.publisher = _load("publish_public")

    def test_a_documented_placeholder_is_not_a_finding(self):
        import tempfile, shutil
        directory = Path(tempfile.mkdtemp(prefix="publish-test-"))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        (directory / "notes.py").write_text(
            "# Number('1420070400000000001') rounds\n", encoding="utf-8")
        problems = self.publisher.verify_tree(directory, ["notes.py"])
        self.assertFalse(any("Discord identifiers" in p for p in problems), problems)

    def test_a_real_looking_id_beside_a_placeholder_is_still_caught(self):
        """The allowlist must narrow the report, never silence it."""
        import tempfile, shutil
        directory = Path(tempfile.mkdtemp(prefix="publish-test-"))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        (directory / "notes.py").write_text(
            "A = 1420070400000000001\nB = 1420070400000009999\n", encoding="utf-8")
        problems = self.publisher.verify_tree(directory, ["notes.py"])
        finding = next(p for p in problems if "Discord identifiers" in p)
        self.assertIn("1420070400000009999", finding)
        self.assertNotIn("1420070400000000001", finding)

    def test_every_placeholder_is_obviously_synthetic(self):
        """A real id parked here would be a leak the checker is told to ignore."""
        for value in self.publisher.SYNTHETIC_IDS:
            with self.subTest(value=value):
                # Either the Discord epoch itself, or a visibly sequential digit run.
                self.assertTrue(
                    value.startswith("1420070400000") or value == "123456789012345678",
                    f"{value} does not look synthetic enough to be allowlisted")


class ScannerTests(unittest.TestCase):
    """A scanner that never fires is worse than no scanner.

    CLAUDE.md requires a planted-secret negative test for any change touching
    the secret-scanning configuration, because an allowlist that hides a real
    secret is worse than a red build. This is that test, run every time rather
    than by hand once.
    """

    def setUp(self):
        import shutil
        if not shutil.which("gitleaks"):
            self.skipTest("gitleaks is not installed on this machine")
        self.publisher = _load("publish_public")

    def _tree(self, files: dict) -> Path:
        import shutil, tempfile
        directory = Path(tempfile.mkdtemp(prefix="scanner-test-"))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        # The repository's own allowlist travels with a published tree, so the
        # test scans under the same rules a real publish would.
        shutil.copy2(ROOT / ".gitleaks.toml", directory / ".gitleaks.toml")
        for name, content in files.items():
            (directory / name).write_text(content, encoding="utf-8")
        return directory

    def test_a_clean_tree_passes(self):
        tree = self._tree({"clean.py": "x = 1\n"})
        problems = self.publisher.verify_tree(tree, ["clean.py"])
        self.assertEqual([], [p for p in problems if "gitleaks" in p], problems)

    def test_a_planted_credential_is_caught(self):
        """Assembled at runtime, never written as a literal.

        Spelling a valid-looking credential out in this file would make the file
        itself a finding — the publisher caught exactly that when this test was
        first written, which is a fair demonstration that it works. Split, the
        pattern cannot match the source; joined, it is what the scanner sees.
        """
        token = "ghp_" + "aB3dEfGh1JkLmN0pQrSt" + "UvWxYz1234567890"
        tree = self._tree({"leak.py": f'TOKEN = "{token}"\n'})
        problems = self.publisher.verify_tree(tree, ["leak.py"])
        self.assertTrue(any("gitleaks" in p for p in problems),
                        f"the scanner did not fire: {problems}")

    def test_the_allowlist_ships_but_the_exposure_record_does_not(self):
        """`.gitleaks.toml` is rules and must travel, so a published tree scans
        under them. `.gitleaksignore` names this repository's own accepted
        exposures and is meaningless — and misleading — anywhere else."""
        self.assertFalse(self.publisher.is_excluded(".gitleaks.toml"))
        self.assertTrue(self.publisher.is_excluded(".gitleaksignore"))


class PrivateRemoteGuardTests(unittest.TestCase):
    """The one target that must never be accepted is this repository."""

    def setUp(self):
        self.publisher = _load("publish_public")

    def test_this_repository_is_refused_in_every_remote_form(self):
        for remote in ("https://github.com/WoodenPotatos/potatobotbeta.git",
                       "git@github.com:WoodenPotatos/potatobotbeta.git",
                       "/home/woody/Documents/GitHub/potatobotbeta"):
            with self.subTest(remote=remote):
                self.assertTrue(self.publisher.is_private_remote(remote))

    def test_the_public_repository_is_accepted(self):
        for remote in ("https://github.com/WoodenPotatos/potatobot.git",
                       "git@github.com:WoodenPotatos/potatobot.git"):
            with self.subTest(remote=remote):
                self.assertFalse(self.publisher.is_private_remote(remote))

    def test_a_path_that_merely_contains_the_name_is_not_refused(self):
        """A substring match rejected any path containing the name, which a
        scratch directory was enough to trip. The comparison is on the
        repository name, against the remotes actually configured here."""
        self.assertFalse(self.publisher.is_private_remote(
            "/tmp/build/-home-user-GitHub-potatobotbeta/out/public.git"))


class PromotionTests(unittest.TestCase):
    """Promotion is a property of the artefact, not a label on the push.

    The first end-to-end publish tagged v2.1.0b1 while the tree inside it still
    said 2.1.0a1, so a public installation's /version reported `alpha` and its
    README said "Private development build. Not published."
    """

    def setUp(self):
        self.publisher = _load("publish_public")

    def _tree(self) -> Path:
        """A miniature tree with the three files promotion rewrites."""
        import shutil, tempfile
        directory = Path(tempfile.mkdtemp(prefix="promote-test-"))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        (directory / "pyproject.toml").write_text(
            '[project]\nname = "potatobot"\nversion = "2.1.0a4"\n', encoding="utf-8")
        (directory / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 2.1.0-alpha.4 - Unreleased\n\n- A change.\n",
            encoding="utf-8")
        (directory / "README.md").write_text(
            "# PotatoBot\n\n<!-- BEGIN GENERATED: version -->\nstale\n"
            "<!-- END GENERATED: version -->\n", encoding="utf-8")
        scripts = directory / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts" / "update_readme.py", scripts)
        shutil.copy2(ROOT / "version.py", directory)
        return directory

    def test_the_version_is_written_into_the_tree(self):
        tree = self._tree()
        # The README regeneration needs its markers; only version is present, so
        # tolerate its refusal and assert on the parts promotion owns directly.
        try:
            self.publisher.promote_tree(tree, "2.1.0b1")
        except SystemExit:
            pass
        self.assertIn('version = "2.1.0b1"',
                      (tree / "pyproject.toml").read_text(encoding="utf-8"))

    def test_the_changelog_stops_saying_unreleased(self):
        tree = self._tree()
        try:
            self.publisher.promote_tree(tree, "2.1.0b1")
        except SystemExit:
            pass
        heading = [line for line in
                   (tree / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
                   if line.startswith("## ")][0]
        self.assertEqual("## 2.1.0-beta.1", heading)

    def test_a_tree_without_a_version_line_is_refused(self):
        tree = self._tree()
        (tree / "pyproject.toml").write_text("[project]\nname = \"x\"\n",
                                             encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.publisher.promote_tree(tree, "2.1.0b1")

    def test_the_private_repository_keeps_its_own_version(self):
        """Only the snapshot is rewritten."""
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as handle:
            here = tomllib.load(handle)["project"]["version"]
        import version as version_module
        self.assertEqual("alpha", version_module.channel_for(here),
                         "this repository is the alpha line and stays on it")


class RemoteReachabilityTests(unittest.TestCase):
    """An unreachable target must fail before anything is built.

    The first attempt at a real publish built the tree, verified it, printed
    "verification clean" and only then died on authentication, which reads as
    though the publish had worked.
    """

    def setUp(self):
        self.publisher = _load("publish_public")

    def test_no_remote_is_not_a_problem(self):
        problems = []
        self.publisher.check_remote_is_reachable(problems, None)
        self.assertEqual([], problems)

    def test_an_unreachable_local_path_is_refused(self):
        problems = []
        self.publisher.check_remote_is_reachable(problems, "/nonexistent/repo.git")
        self.assertTrue(any("cannot reach" in p for p in problems), problems)

    def test_a_reachable_local_repository_passes(self):
        import shutil, subprocess, tempfile
        directory = Path(tempfile.mkdtemp(prefix="remote-test-"))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        subprocess.run(["git", "init", "-q", "--bare", str(directory / "r.git")],
                       check=True)
        problems = []
        self.publisher.check_remote_is_reachable(problems, str(directory / "r.git"))
        self.assertEqual([], problems)

    def test_an_ssh_failure_suggests_the_https_url(self):
        """The specific mistake worth naming, since this repository's own
        credentials live in the gh helper and are HTTPS-only."""
        problems = []
        self.publisher.check_remote_is_reachable(
            problems, "git@github.com:WoodenPotatos/potatobot.git")
        joined = " ".join(problems)
        if "publickey" in joined:
            self.assertIn("https://github.com/WoodenPotatos/potatobot.git", joined)
        else:
            self.skipTest("this machine can reach GitHub over SSH")
