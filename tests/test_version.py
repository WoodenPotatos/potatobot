"""The version is derived, not declared, so the derivation needs pinning.

It used to live in `config.json` as an operator-editable setting and drifted from
the packaging metadata — the file said 1.9 while `pyproject.toml` said 2.0.0rc1.
These tests hold the replacement to its promises: one source, a channel that
follows from the version rather than from a label, and no way for a build to
claim it is stable while carrying a prerelease suffix.
"""

import tomllib
import unittest
from pathlib import Path

import version

ROOT = Path(__file__).resolve().parents[1]


def _declared_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]


class ChannelDerivationTests(unittest.TestCase):
    def test_each_prerelease_suffix_maps_to_its_channel(self):
        cases = {
            "2.1.0a1": version.CHANNEL_ALPHA,
            "2.1.0a47": version.CHANNEL_ALPHA,
            "2.1.0b1": version.CHANNEL_BETA,
            # A release candidate is public and not stable, so it is a beta as
            # far as an operator is concerned rather than a fourth channel.
            "2.0.0rc1": version.CHANNEL_BETA,
            "2.1.0": version.CHANNEL_STABLE,
            "10.0.3": version.CHANNEL_STABLE,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(version.channel_for(raw), expected)

    def test_an_unparseable_version_is_never_reported_as_stable(self):
        # Failing to alpha is the cautious direction: the wrong answer must not
        # be the one that claims stability.
        for raw in ("", "1.9", "garbage", "2.1", "v2.1.0", None):
            with self.subTest(raw=raw):
                self.assertEqual(version.channel_for(raw), version.CHANNEL_ALPHA)


class DisplayTests(unittest.TestCase):
    def test_pep440_is_rendered_for_people(self):
        cases = {
            "2.1.0a1": "2.1.0-alpha.1",
            "2.1.0b12": "2.1.0-beta.12",
            "2.0.0rc1": "2.0.0-rc.1",
            "2.1.0": "2.1.0",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(version.display_for(raw), expected)

    def test_an_unparseable_version_is_shown_as_given(self):
        self.assertEqual(version.display_for("garbage"), "garbage")
        self.assertEqual(version.display_for(""), version.UNKNOWN_VERSION)


class SingleSourceTests(unittest.TestCase):
    def test_the_reader_agrees_with_pyproject(self):
        declared = _declared_version()
        version.raw_version.cache_clear()
        try:
            self.assertEqual(version.raw_version(), declared)
        finally:
            version.raw_version.cache_clear()

    def test_the_declared_version_is_well_formed(self):
        declared = _declared_version()
        self.assertRegex(declared, r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")

    def test_version_is_no_longer_an_operator_setting(self):
        # The whole point of the move: a version a form can change is a version
        # that lies. Guards against either key being reintroduced.
        from settings_registry import SETTING_DEFINITIONS
        for key in ("release_version", "release_date"):
            self.assertNotIn(key, SETTING_DEFINITIONS)

    def test_config_carries_no_version_metadata(self):
        import json
        for name in ("config.json", "config.json.example"):
            with self.subTest(name=name):
                with open(ROOT / name, encoding="utf-8") as handle:
                    settings = json.load(handle)["bot_settings"]
                self.assertNotIn("version", settings)
                self.assertNotIn("release_date", settings)

    def test_the_repository_url_is_public_and_https(self):
        self.assertTrue(version.REPOSITORY_URL.startswith("https://github.com/"))
        self.assertNotIn("potatobotbeta", version.REPOSITORY_URL,
                         "the private repository must never be advertised")


class BumpScriptTests(unittest.TestCase):
    """The bump script is what an operator runs; its ordering rule is the guard."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bump_version", ROOT / "scripts" / "bump_version.py")
        self.bump = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bump)

    def test_prereleases_sort_before_their_own_release(self):
        order = ["2.1.0a1", "2.1.0a2", "2.1.0b1", "2.1.0rc1", "2.1.0", "2.1.1a1"]
        keys = [self.bump.sort_key(self.bump.parse(raw)) for raw in order]
        self.assertEqual(keys, sorted(keys), "PEP 440 ordering is what the "
                                             "backwards check relies on")

    def test_a_step_keeps_the_channel_unless_told_otherwise(self):
        current = self.bump.parse("2.1.0a4")
        self.assertEqual(self.bump.render(self.bump.bump(current, "patch", None)),
                         "2.1.1a1")
        self.assertEqual(self.bump.render(self.bump.bump(current, "minor", "beta")),
                         "2.2.0b1")
        self.assertEqual(self.bump.render(self.bump.bump(current, "prerelease", None)),
                         "2.1.0a5")

    def test_promoting_an_alpha_to_beta_moves_forward(self):
        alpha = self.bump.parse("2.1.0a4")
        beta = self.bump.parse("2.1.0b1")
        self.assertLess(self.bump.sort_key(alpha), self.bump.sort_key(beta))

    def test_a_prerelease_step_on_a_stable_version_is_refused(self):
        with self.assertRaises(SystemExit):
            self.bump.bump(self.bump.parse("2.1.0"), "prerelease", None)


if __name__ == "__main__":
    unittest.main()


class ReadmeGenerationTests(unittest.TestCase):
    """The README is hand-written; only the marked blocks are ours.

    A generator that appends to somebody's README when it cannot find its
    markers is a generator that gets removed from the pipeline, so the refusal
    matters more than the rendering.
    """

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "update_readme", ROOT / "scripts" / "update_readme.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_the_committed_readme_is_up_to_date(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertEqual(text, self.module.apply(text),
                         "run scripts/update_readme.py")

    def test_a_missing_marker_is_refused_rather_than_appended(self):
        with self.assertRaises(SystemExit) as caught:
            self.module.apply("# Just my words\n")
        self.assertIn("BEGIN GENERATED", str(caught.exception))

    def test_generation_is_idempotent(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertEqual(self.module.apply(text),
                         self.module.apply(self.module.apply(text)))

    def test_only_the_marked_regions_change(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        stale = text.replace("**Version", "**Stale version", 1)
        updated = self.module.apply(stale)
        # Everything outside the blocks survives verbatim.
        for line in ("# PotatoBot", "## Highlights", "## Development status"):
            self.assertIn(line, updated)
        self.assertNotIn("**Stale version", updated)

    def test_the_version_block_states_the_channel(self):
        rendered = self.module.render_version()
        self.assertIn(version.version_display(), rendered)
        self.assertIn(version.release_channel(), rendered)


class CurrencySymbolTests(unittest.TestCase):
    """The currency symbol was a custom emoji hard-coded in 105 places.

    A custom emoji belongs to one guild; everywhere else Discord renders the
    literal `<:name:id>` text, so every balance, price and payout was broken for
    every other installation. It is now one instance setting, substituted into a
    `{coin}` token.
    """

    def setUp(self):
        from cogs.utils import config
        self.config = config
        self.original = config.get("bot_settings", {}).get("currency_emoji")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.original is None:
            self.config["bot_settings"].pop("currency_emoji", None)
        else:
            self.config["bot_settings"]["currency_emoji"] = self.original

    def test_the_reader_falls_back_when_unset_or_blank(self):
        from cogs.utils import DEFAULT_CURRENCY_EMOJI, currency_emoji
        for value in (None, "", "   "):
            with self.subTest(value=value):
                if value is None:
                    self.config["bot_settings"].pop("currency_emoji", None)
                else:
                    self.config["bot_settings"]["currency_emoji"] = value
                self.assertEqual(DEFAULT_CURRENCY_EMOJI, currency_emoji())

    def test_the_fallback_matches_the_registry_default(self):
        from cogs.utils import DEFAULT_CURRENCY_EMOJI
        from settings_registry import SETTING_DEFINITIONS
        self.assertEqual(SETTING_DEFINITIONS["currency_emoji"].default,
                         DEFAULT_CURRENCY_EMOJI)

    def test_the_default_is_not_a_custom_emoji(self):
        """A custom emoji cannot exist in a guild the bot has never joined."""
        from settings_registry import SETTING_DEFINITIONS
        self.assertNotRegex(SETTING_DEFINITIONS["currency_emoji"].default,
                            r"<a?:[A-Za-z0-9_]+:\d+>")

    def test_t_substitutes_coin_without_the_caller_supplying_it(self):
        from cogs.utils import t
        self.config["bot_settings"]["currency_emoji"] = "🪙"
        rendered = t("admin.testboost_desc", lang="en", user="Woody", amount=5)
        self.assertEqual("Huge thanks for the boost, Woody! Your reward: **5 🪙**.",
                         rendered)

    def test_a_template_without_the_token_is_unaffected(self):
        """str.format ignores a keyword the template does not use, which is why
        no call site had to change."""
        from cogs.utils import t
        self.assertEqual("Pong! 42 ms", t("system.ping", lang="en", latency=42))

    def test_an_explicit_coin_argument_still_wins(self):
        from cogs.utils import t
        self.config["bot_settings"]["currency_emoji"] = "🪙"
        rendered = t("admin.testboost_desc", lang="en", user="Woody", amount=5,
                     coin="XYZ")
        self.assertIn("XYZ", rendered)
        self.assertNotIn("🪙", rendered)


class WorkResponseSubstitutionTests(unittest.TestCase):
    """Work responses use a literal replace, never str.format.

    The text is operator-authored and reaches message content, so a stray brace
    must not be able to raise inside the command.
    """

    def test_both_placeholders_are_substituted(self):
        import database
        from cogs.casino import work_response_text
        from cogs.utils import config, currency_emoji

        original = config.get("bot_settings", {}).get("currency_emoji")
        config["bot_settings"]["currency_emoji"] = "🪙"
        try:
            stored = [{"tier": "normal", "scope": "guild", "enabled": True,
                       "weight": 1,
                       "message": "You earned {earnings} {coin} today."}]
            rendered = work_response_text("normal", stored, 250)
            self.assertEqual("You earned 250 🪙 today.", rendered)
            self.assertEqual("🪙", currency_emoji())
        finally:
            if original is None:
                config["bot_settings"].pop("currency_emoji", None)
            else:
                config["bot_settings"]["currency_emoji"] = original

    def test_a_stray_brace_does_not_raise(self):
        from cogs.casino import work_response_text
        stored = [{"tier": "normal", "scope": "guild", "enabled": True,
                   "weight": 1, "message": "A {rogue} brace and {earnings}."}]
        self.assertEqual("A {rogue} brace and 7.",
                         work_response_text("normal", stored, 7))

    def test_the_shipped_defaults_use_the_token(self):
        import database
        for tier, message in database.WORK_DEFAULT_RESPONSES:
            with self.subTest(tier=tier, message=message[:40]):
                self.assertNotRegex(message, r"<a?:[A-Za-z0-9_]+:\d+>")
