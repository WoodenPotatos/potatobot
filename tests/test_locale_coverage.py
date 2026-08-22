"""Enforce the localization rules that only a registry sweep can check.

`tests/test_localization_policy.py` covers what a grep can see: catalogs with
identical shapes, no Hungarian prose outside a locale file, and every *literal*
`t("…")` key resolving. This file covers the keys the code composes at runtime
from a registry, an enum or a reason code, which no grep finds and which render
as a raw `[dashboard.foo]` to a user when one is missing.

`scripts/locale_audit.py` owns the definition of those families, so the report
an operator reads and the gate the build runs cannot describe different things.
"""

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Minigame datasets deliberately left incomplete, by language. `data/loldle/` is
# maintained by a named administrator who asked that it not be edited
# automatically, so LoLdle is unavailable in English until its owner fills the
# catalog in. Anything added here needs the same kind of stated reason.
KNOWN_INCOMPLETE_MINIGAMES = {"en": ("loldle",)}


def load_audit_module():
    """Import the audit script, which is not importable as a package member."""
    spec = importlib.util.spec_from_file_location(
        "locale_audit", ROOT / "scripts" / "locale_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComposedKeyCoverageTests(unittest.TestCase):
    audit = load_audit_module()
    CATALOG = json.loads(
        (ROOT / "locales" / "hu.json").read_text(encoding="utf-8")
    )

    def test_every_runtime_composed_key_exists_in_hungarian(self):
        families = self.audit.composed_key_families()
        self.assertTrue(families, "the audit found no key families at all")
        for family, keys in families.items():
            missing = sorted(
                key for key in set(keys)
                if not self.audit.resolves(self.CATALOG, key)
            )
            with self.subTest(family=family):
                self.assertEqual([], missing)

    def test_no_composed_key_is_blank_in_the_primary_catalog(self):
        """An empty Hungarian value is worse than a missing key: it renders as
        nothing at all rather than as a visibly wrong placeholder."""
        flat = self.audit.flatten(self.CATALOG)
        blank = sorted({
            key
            for keys in self.audit.composed_key_families().values()
            for key in keys
            if isinstance(flat.get(key), str) and not flat[key].strip()
        })
        self.assertEqual([], blank)

    def test_every_literal_key_in_the_source_resolves(self):
        report = self.audit.literal_key_report(self.CATALOG)
        self.assertEqual({}, report["missing"])
        # A sanity floor: if the scan silently stopped finding keys, the two
        # assertions above would pass while checking nothing.
        self.assertGreater(report["referenced"], 500)


class PrimaryCatalogCompletenessTests(unittest.TestCase):
    audit = load_audit_module()

    def test_the_hungarian_catalog_has_no_empty_value(self):
        """Hungarian is the source language, so a blank there is an unfinished
        feature rather than an untranslated one."""
        report = self.audit.catalog_report()
        self.assertEqual(0, report["by_language"]["hu"]["empty"])

    def test_the_hungarian_game_catalogs_would_load_every_minigame(self):
        """A blank entity name makes `load_or_disable` disable the game, so an
        incomplete primary catalog silently removes a whole minigame."""
        for game, languages in self.audit.game_catalog_report().items():
            with self.subTest(game=game):
                self.assertTrue(languages["hu"]["loads"])
                self.assertEqual(0, languages["hu"]["empty"])


class EnglishCatalogTests(unittest.TestCase):
    """English is generated alongside Hungarian, not left for a translator.

    The maintainer lifted the no-generated-translations rule for English on
    2026-08-22. Hungarian stays primary; every language after English keeps the
    old rule, which is why only `en` is required to be complete here.
    """

    audit = load_audit_module()

    def test_the_english_catalog_has_no_empty_value(self):
        report = self.audit.catalog_report()
        self.assertEqual(0, report["by_language"]["en"]["empty"])
        self.assertEqual(100.0, report["by_language"]["en"]["percent"])

    def test_no_english_value_carries_hungarian_text(self):
        """A Hungarian accent in the English catalog means a copied value.

        This is the signal that matters: before the embargo was lifted, 599 of
        these values were the Hungarian text verbatim, which also defeated the
        fallback because `t()` treats a present value as a hit.
        """
        english = self.audit.flatten(
            self.audit.load_catalog(ROOT / "locales" / "en.json")
        )
        hungarian_letters = re.compile(r"[ÁÉÍÓÖŐÚÜŰáéíóöőúüű]")
        # A Discord channel name is a reference, not prose: translating
        # `#szerepkörök` would point the member at a channel that does not exist.
        channel_reference = re.compile(r"#\S+")
        # Proper nouns that keep their Hungarian spelling in any language.
        proper_nouns = ("Rómeó",)

        offenders = []
        for key, value in english.items():
            if not isinstance(value, str):
                continue
            # `dashboard.languages.*` holds each language's own endonym.
            if key.startswith("dashboard.languages."):
                continue
            stripped = channel_reference.sub("", value)
            for noun in proper_nouns:
                stripped = stripped.replace(noun, "")
            if hungarian_letters.search(stripped):
                offenders.append(key)
        self.assertEqual([], sorted(offenders))

    def test_an_identical_value_is_a_name_or_a_template_never_a_sentence(self):
        """Some values are the same in both languages — a brand, a slash-command
        usage line, a format-only string. A *sentence* that is identical is a
        copy, so the rule is on shape rather than on a hand-kept allowlist."""
        hungarian = self.audit.flatten(
            self.audit.load_catalog(ROOT / "locales" / "hu.json")
        )
        english = self.audit.flatten(
            self.audit.load_catalog(ROOT / "locales" / "en.json")
        )
        # Strip the parts that carry no language: placeholders, custom emoji,
        # markdown and punctuation. What is left is the prose, if any.
        noise = re.compile(r"\{[^{}]*\}|<a?:[A-Za-z0-9_]+:\d+>|[`*_#>\[\]()/.,!?:;|—•·\-]")
        sentences = []
        for key, value in hungarian.items():
            if not isinstance(value, str) or english.get(key) != value:
                continue
            if value.startswith("/"):
                continue  # A slash-command usage line is not prose.
            words = [word for word in noise.sub(" ", value).split()
                     if any(character.isalpha() for character in word)]
            if len(words) > 4:
                sentences.append((key, value))
        self.assertEqual([], sentences)

    def test_every_selectable_language_is_actually_complete(self):
        """`language` is a constrained choice, so anything it offers has to work.

        A blank general-catalog value degrades to Hungarian, but a blank
        minigame entity name disables that minigame outright, so a language is
        only selectable once both are complete.
        """
        from settings_registry import SUPPORTED_LANGUAGES, SETTING_DEFINITIONS

        self.assertEqual(
            tuple(SUPPORTED_LANGUAGES),
            SETTING_DEFINITIONS["language"].choices,
        )
        catalogs = self.audit.catalog_report()["by_language"]
        games = self.audit.game_catalog_report()
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                self.assertIn(language, catalogs)
                self.assertEqual(0, catalogs[language]["empty"])
                for game, languages in games.items():
                    if game in KNOWN_INCOMPLETE_MINIGAMES.get(language, ()):
                        continue
                    self.assertTrue(
                        languages[language]["loads"],
                        f"{game} would be disabled in {language}",
                    )

    def test_the_loldle_exception_is_deliberate_and_recorded(self):
        """`data/loldle/` is maintained by a named administrator who asked that
        it not be edited automatically, so its English catalog stays empty and
        LoLdle is unavailable in English. This asserts that is still the only
        exception, and that it is written down where an operator would look."""
        self.assertEqual({"en": ("loldle",)}, KNOWN_INCOMPLETE_MINIGAMES)
        games = self.audit.game_catalog_report()
        self.assertFalse(games["loldle"]["en"]["loads"])
        for name in ("CLAUDE.md", "docs/localization_status.md"):
            with self.subTest(document=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("data/loldle/", text)

    def test_an_unsupported_language_is_rejected_on_save(self):
        from settings_registry import SETTING_DEFINITIONS, validate_setting_value

        definition = SETTING_DEFINITIONS["language"]
        for language in definition.choices:
            validate_setting_value(definition, language)
        for rejected in ("de", "", "HU", "hu-HU"):
            with self.subTest(value=rejected):
                with self.assertRaises(ValueError):
                    validate_setting_value(definition, rejected)


class LocaleFallbackTests(unittest.TestCase):
    """`t()` resolves requested language, then English, then Hungarian, and an
    empty value counts as a miss. Before that, an untranslated key returned an
    empty string and a switched language answered with blank embeds."""

    def test_an_empty_value_falls_through_instead_of_returning_blank(self):
        """The chain is requested, then English, then Hungarian. A third
        language with a blank value therefore lands on English, not on an empty
        string, which is what used to happen."""
        from cogs import utils

        original = utils.locales.get("xx")
        utils.locales["xx"] = {"system": {"command_not_found": "   "}}
        try:
            resolved = utils.t("system.command_not_found", lang="xx")
            self.assertTrue(resolved.strip())
            self.assertEqual(
                utils.t("system.command_not_found", lang="en"), resolved
            )
        finally:
            if original is None:
                utils.locales.pop("xx", None)
            else:
                utils.locales["xx"] = original

    def test_a_translated_value_wins_over_the_fallback(self):
        from cogs import utils

        original = utils.locales.get("xx")
        utils.locales["xx"] = {"system": {"command_not_found": "translated"}}
        try:
            self.assertEqual(
                "translated", utils.t("system.command_not_found", lang="xx")
            )
        finally:
            if original is None:
                utils.locales.pop("xx", None)
            else:
                utils.locales["xx"] = original

    def test_a_missing_key_is_reported_rather_than_guessed(self):
        from cogs import utils

        self.assertEqual("[no.such.key]", utils.t("no.such.key"))

    def test_a_missing_format_argument_returns_the_template(self):
        """It used to be caught by the missing-key handler, which returned the
        other catalog's value or an empty string."""
        from cogs import utils

        self.assertEqual("Pong! {latency} ms", utils.t("system.ping"))
        self.assertEqual("Pong! 5 ms", utils.t("system.ping", latency=5))


class UnlocalizedTextTests(unittest.TestCase):
    audit = load_audit_module()

    # User-visible literals that have not been moved into a catalog yet. Each is
    # a Discord audit-log reason, which is read by server staff in the audit log
    # and is therefore user-visible text under the project's own rule. They are
    # listed rather than tolerated silently, so the count cannot grow unnoticed.
    KNOWN_UNLOCALIZED = {
        "cogs/roleselect.py",
        "cogs/shop.py",
        "cogs/tickets.py",
    }

    def test_no_new_surface_gains_an_unlocalized_user_visible_literal(self):
        offenders = {
            finding["where"].split(":")[0]
            for finding in self.audit.unlocalized_text_report()
        }
        self.assertEqual(
            [], sorted(offenders - self.KNOWN_UNLOCALIZED),
            "a user-visible string literal was added outside a locale catalog",
        )


if __name__ == "__main__":
    unittest.main()
