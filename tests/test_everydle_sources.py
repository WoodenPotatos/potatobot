"""The Everydle upstream adapters and the drift report.

The fragile part of this tooling is the shape of a third-party payload, so every
adapter test runs against a recorded fixture rather than the network. Re-record
with `python scripts/everydle_sources.py record tests/fixtures/everydle`.

Two properties matter more than the parsing. The tool must refuse LoLdle by name,
because generalising it and pointing it at a hand-maintained dataset is the one
way it could do real harm. And it must never turn an upstream outage into a
proposal to delete a roster.
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "everydle"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name):
    """Import a script from `scripts/`, registered under its own name.

    Registering it in `sys.modules` matters: the drift and propose scripts do
    `from everydle_sources import …` themselves, and without this each one would
    get a separate copy of the module — so redirecting `DATA_DIR` on one would
    not be seen by the others, and `SourceError` would be a different class in
    each.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sources = load_script("everydle_sources")
drift = load_script("everydle_drift")
propose = load_script("everydle_propose")


def fixture_opener():
    return drift.load_fixtures(FIXTURES)


class FixtureTests(unittest.TestCase):
    # Row count per fixture, so a truncated recording cannot make every adapter
    # test silently vacuous. The Valorant payload wraps its rows in `data`.
    EXPECTED_ROWS = {
        "valorant_agents.json": ("data", 20),
        "dbd_characters.json": (None, 80),
        "dbd_dlc.json": (None, 40),
    }

    def test_the_fixtures_are_present_and_plausible(self):
        for name, (wrapper, minimum) in self.EXPECTED_ROWS.items():
            with self.subTest(fixture=name):
                path = FIXTURES / name
                self.assertTrue(path.exists(), f"re-record {name}")
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows = payload[wrapper] if wrapper else payload
                self.assertGreaterEqual(len(rows), minimum, f"re-record {name}")


class SlugTests(unittest.TestCase):
    def test_a_macron_collapses_onto_the_existing_id(self):
        """Upstream spells the killer `The Onryō`. If that slugified to a new id
        the report would call an existing killer a new one, and applying it would
        rename an entity — which re-draws the day's answer mid-game."""
        self.assertEqual("the_onryo", sources.slugify("The Onryō"))
        self.assertEqual("the_onryo", sources.slugify("The Onryo"))

    def test_punctuation_and_case_are_folded(self):
        self.assertEqual("kay_o", sources.slugify("KAY/O"))
        self.assertEqual("the_ghost_face", sources.slugify("The Ghost Face"))
        self.assertEqual("leon_s_kennedy", sources.slugify("Leon S. Kennedy"))


class MintTests(unittest.TestCase):
    def test_a_minted_id_keeps_the_established_shape(self):
        minted = sources.mint_value_id("agents", "origin", "Croatia", set())
        self.assertTrue(minted.startswith("origin_"))
        self.assertEqual(10, len(minted.split("_")[-1]))
        self.assertEqual(minted,
                         sources.mint_value_id("agents", "origin", "Croatia", set()))

    def test_minting_avoids_an_id_already_in_use(self):
        first = sources.mint_value_id("agents", "origin", "Croatia", set())
        second = sources.mint_value_id("agents", "origin", "Croatia", {first})
        self.assertNotEqual(first, second)

    def test_the_same_value_in_two_datasets_gets_two_ids(self):
        """Matching the data's own convention: value ids are dataset-scoped, so
        `Male` in `killers` is a different id from `Male` in `survivors`."""
        self.assertNotEqual(
            sources.mint_value_id("killers", "gender", "Male", set()),
            sources.mint_value_id("survivors", "gender", "Male", set()),
        )


class LockedGameTests(unittest.TestCase):
    def test_loldle_is_refused_by_name_from_every_entry_point(self):
        self.assertIn("loldle", sources.LOCKED_GAMES)
        for game, dataset in (("loldle", "champions"), ("loldle", "hard_mode")):
            with self.subTest(dataset=dataset):
                with self.assertRaises(sources.SourceError):
                    sources.fetch(game, dataset)
                with self.assertRaises(sources.SourceError):
                    sources.load_local(game, dataset)

    def test_no_adapter_is_registered_for_a_locked_game(self):
        for game, _ in sources.ADAPTERS:
            self.assertNotIn(game, sources.LOCKED_GAMES)
        for game, _ in sources.MANAGED_DATASETS:
            self.assertNotIn(game, sources.LOCKED_GAMES)

    def test_the_managed_list_covers_only_what_the_cog_loads(self):
        """`dbdle`'s survivors, perks and roster datasets are unfinished
        scaffolding no command loads, so the tool must not report on them."""
        # Derived rather than listed, so adding a game updates this by itself
        # and the two can never silently disagree.
        import re as _re
        cog = (ROOT / "cogs" / "everydle.py").read_text(encoding="utf-8")
        loaded = set(_re.findall(
            r'load_game_dataset\(\s*"([a-z]+)"\s*,\s*"[^"]+"\s*,\s*"([a-z_]+)"',
            cog))
        managed = set(sources.MANAGED_DATASETS)
        self.assertTrue(managed <= loaded,
                        f"managed but not loaded by the cog: {managed - loaded}")


class AdapterTests(unittest.TestCase):
    def test_valorant_agents_parse_into_roles_and_release_years(self):
        agents = sources.fetch_valdle(fixture_opener())
        self.assertGreater(len(agents), 20)
        self.assertIn("jett", agents)
        self.assertEqual("Duelist", agents["jett"].fields["role"])
        # Lore is never invented.
        for field_name in ("gender", "species", "origin"):
            self.assertIn(field_name, agents["jett"].unknown_fields)

    def test_an_epoch_release_date_is_unknown_rather_than_1970(self):
        """Older agents carry the Unix epoch as a placeholder, so a naive read
        would propose changing every release year to 1970."""
        agents = sources.fetch_valdle(fixture_opener())
        self.assertNotIn("year", agents["jett"].fields)
        self.assertIn("year", agents["jett"].unknown_fields)
        self.assertEqual(2026, agents["miks"].fields["year"])

    def test_dbd_killers_parse_gender_height_and_tunables(self):
        killers = sources.fetch_dbdle(fixture_opener())
        self.assertGreater(len(killers), 40)
        trapper = killers["the_trapper"]
        self.assertEqual("Male", trapper.fields["gender"])
        self.assertEqual("Tall", trapper.fields["height"])
        self.assertEqual("4.6 m/s", trapper.fields["movement_speed"])
        self.assertEqual("32m", trapper.fields["terror_radius"])
        # Nationality is lore; no source carries it.
        self.assertIn("country", trapper.unknown_fields)

    def test_a_base_game_killer_has_no_release_year_upstream(self):
        """A base-game killer has no chapter, so there is no store timestamp to
        read a year from. That is a gap, not a zero."""
        killers = sources.fetch_dbdle(fixture_opener())
        self.assertIn("release", killers["the_trapper"].unknown_fields)
        self.assertEqual(2026, killers["the_slasher"].fields["release"])

    def test_only_killers_are_returned(self):
        killers = sources.fetch_dbdle(fixture_opener())
        self.assertNotIn("dwight_fairfield", killers)


class OutageTests(unittest.TestCase):
    """An upstream outage must be an error, never a roster of zero entities."""

    def test_an_empty_payload_is_refused(self):
        opener = sources.fixture_opener(
            {sources.VALORANT_AGENTS_URL: {"data": []}}
        )
        with self.assertRaises(sources.SourceError):
            sources.fetch_valdle(opener)

    def test_a_truncated_payload_is_refused(self):
        agents = json.loads(
            (FIXTURES / "valorant_agents.json").read_text(encoding="utf-8")
        )
        opener = sources.fixture_opener(
            {sources.VALORANT_AGENTS_URL: {"data": agents["data"][:3]}}
        )
        with self.assertRaises(sources.SourceError):
            sources.fetch_valdle(opener)

    def test_a_row_without_a_role_is_refused_rather_than_guessed(self):
        opener = sources.fixture_opener({
            sources.VALORANT_AGENTS_URL: {
                "data": [{"displayName": f"Agent {index}", "role": {"displayName": "Duelist"}}
                         for index in range(25)]
                        + [{"displayName": "Broken", "role": None}]
            }
        })
        with self.assertRaises(sources.SourceError):
            sources.fetch_valdle(opener)

    def test_an_unreachable_source_raises_rather_than_returning_nothing(self):
        with self.assertRaises(sources.SourceError):
            sources.fetch_json("https://example.invalid/nope",
                               sources.fixture_opener({}))


class DriftReportTests(unittest.TestCase):
    def setUp(self):
        self.opener = fixture_opener()

    def test_valdle_is_in_step_with_upstream(self):
        """Locks in the Phase 1 correction: Miks was added by hand."""
        report = drift.compare("valdle", "agents", self.opener)
        self.assertEqual([], report["new_upstream"])
        self.assertEqual([], report["missing_upstream"])
        self.assertEqual([], report["local_gaps"])
        self.assertEqual([], report["disagreements"])
        self.assertFalse(drift.has_drift(report))

    def test_dbdle_has_no_new_or_missing_entity(self):
        report = drift.compare("dbdle", "killers", self.opener)
        self.assertEqual([], report["new_upstream"])
        self.assertEqual([], report["missing_upstream"])
        self.assertEqual([], report["local_gaps"])
        self.assertEqual([], report["alias_gaps"])

    def test_dbdle_has_no_undecided_disagreement_left(self):
        """The speeds were corrected and the genders were accepted, so anything
        appearing here now is fresh drift that nobody has looked at."""
        report = drift.compare("dbdle", "killers", self.opener)
        self.assertEqual([], report["disagreements"])
        self.assertEqual([], report["balance_changes"])
        self.assertFalse(drift.has_drift(report))

    def test_the_accepted_gender_divergences_are_reported_but_not_findings(self):
        """Deliberate divergences have to stay visible without counting, or the
        report becomes noise an operator learns to skim past."""
        report = drift.compare("dbdle", "killers", self.opener)
        accepted = {entry["entity_id"] for entry in report["accepted_divergences"]}
        self.assertEqual({"the_singularity", "the_twins", "the_unknown"}, accepted)
        self.assertNotIn("accepted_divergences", drift.FINDING_KEYS)
        for entry in report["accepted_divergences"]:
            self.assertEqual("gender", entry["field"])

    def test_every_accepted_divergence_carries_a_reason(self):
        for key, reason in sources.ACCEPTED_DIVERGENCES.items():
            with self.subTest(divergence=key):
                self.assertEqual(4, len(key))
                self.assertGreater(len(reason.strip()), 20)

    def test_the_corrected_speeds_match_the_game(self):
        """The Blight is a 110% killer, so his base is 4.4 m/s; The Shape's and
        The Pig's labels list the base first, as every other killer's does."""
        local = sources.load_local("dbdle", "killers")
        self.assertEqual("4.4 m/s, 9.2 m/s",
                         local.resolved("the_blight")["movement_speed"])
        self.assertEqual("4.6 m/s, 4.2 m/s",
                         local.resolved("the_shape")["movement_speed"])
        self.assertEqual("4.6 m/s, 4 m/s",
                         local.resolved("the_pig")["movement_speed"])

    def test_no_attribute_label_is_left_referenced_by_nobody(self):
        """A label no entity points at is dead weight that misleads an editor."""
        for game, dataset in sources.MANAGED_DATASETS:
            local = sources.load_local(game, dataset)
            for field_name in local.fields():
                referenced = {mechanics[field_name]
                              for mechanics in local.entities.values()
                              if isinstance(mechanics.get(field_name), str)}
                for language, catalog in local.catalogs.items():
                    labels = set((catalog["datasets"][dataset].get("attributes")
                                  or {}).get(field_name, {}))
                    with self.subTest(dataset=dataset, field=field_name,
                                      language=language):
                        self.assertEqual(set(), labels - referenced)

    def test_a_power_state_speed_is_not_a_disagreement(self):
        """The local data lists power-state speeds after the base one; upstream
        has only the base tunable, so a prefix match is agreement."""
        report = drift.compare("dbdle", "killers", self.opener)
        speeds = {entry["entity_id"] for entry in report["disagreements"]
                  if entry["field"] == "movement_speed"}
        self.assertNotIn("the_hillbilly", speeds)
        self.assertNotIn("the_nurse", speeds)

    def test_no_confusable_label_remains(self):
        for game, dataset in sources.MANAGED_DATASETS:
            with self.subTest(dataset=dataset):
                self.assertEqual(
                    [], drift.compare(game, dataset, self.opener)["confusable_values"]
                )

    def test_the_report_never_proposes_a_deletion(self):
        source = (ROOT / "scripts" / "everydle_drift.py").read_text(encoding="utf-8")
        self.assertNotIn("del ", source)
        self.assertNotIn("write_text", source)
        self.assertNotIn("unlink", source)

    def test_a_source_failure_exits_two_not_one(self):
        """Exit 1 means "the data drifted"; a source being down is neither that
        nor a success, so a scheduled check can tell them apart."""
        source = (ROOT / "scripts" / "everydle_drift.py").read_text(encoding="utf-8")
        self.assertIn("return 2", source)


class BalanceChangeTests(unittest.TestCase):
    """A killer that is buffed or nerfed has to follow the game.

    The dataset is copied to a temporary directory, so nothing here can touch
    the real data.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data = Path(self.temp_dir.name) / "data"
        shutil.copytree(ROOT / "data", self.data)
        self.original_dir = sources.DATA_DIR
        self.original_state = propose.STATE_FILE
        sources.DATA_DIR = self.data
        propose.STATE_FILE = self.data / "everydle_state.json"

    def tearDown(self):
        sources.DATA_DIR = self.original_dir
        propose.STATE_FILE = self.original_state
        self.temp_dir.cleanup()

    def set_label(self, entity_id, field_name, text):
        """Rewrite one entity's label in the scratch copy, to simulate drift."""
        local = sources.load_local("dbdle", "killers")
        value_id = local.entities[entity_id][field_name]
        for language in local.catalogs:
            path = self.data / "dbdle" / "locales" / f"{language}.json"
            catalog = json.loads(path.read_text(encoding="utf-8"))
            catalog["datasets"]["killers"]["attributes"][field_name][value_id] = text
            path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    def test_merge_base_value_replaces_only_the_base(self):
        self.assertEqual("4.4 m/s, 9.2 m/s",
                         sources.merge_base_value("4.6 m/s, 9.2 m/s", "4.4 m/s"))
        # A single-valued label is simply replaced.
        self.assertEqual("Tall", sources.merge_base_value("Short", "Tall"))
        self.assertEqual("32m, 40m",
                         sources.merge_base_value("24m, 40m", "32m"))

    def test_a_nerf_is_a_balance_change_not_a_disagreement(self):
        """Pretend the game nerfed The Hillbilly's base speed."""
        self.set_label("the_hillbilly", "movement_speed", "4.2 m/s, 10.12 m/s")
        report = drift.compare("dbdle", "killers", fixture_opener())
        self.assertEqual([], report["disagreements"])
        change = next(entry for entry in report["balance_changes"]
                      if entry["entity_id"] == "the_hillbilly")
        self.assertEqual("movement_speed", change["field"])
        # Upstream knows only the base, so the power-state value survives.
        self.assertEqual("4.6 m/s, 10.12 m/s", change["merged"])

    def test_a_lore_field_is_never_treated_as_a_balance_change(self):
        authoritative = sources.SOURCE_AUTHORITATIVE[("dbdle", "killers")]
        self.assertNotIn("country", authoritative)
        self.assertNotIn("gender", authoritative)
        self.assertNotIn("release", authoritative)

    def test_a_drafted_balance_change_applies_and_keeps_the_power_state(self):
        import minigame_data

        self.set_label("the_hillbilly", "movement_speed", "4.2 m/s, 10.12 m/s")
        # Scoped: a patch covering every game is refused whole when any
        # entity anywhere still needs a person, which a third dataset
        # legitimately has.
        patch = propose.draft(fixture_opener(), "dbdle")
        self.assertTrue(patch["updates"])
        changes = propose.apply_patch(patch)
        self.assertTrue(any("the_hillbilly" in change for change in changes))
        for language in ("hu", "en"):
            entities, _ = minigame_data.load_localized_dataset(
                self.data / "dbdle" / "killers.json",
                self.data / "dbdle" / "locales" / f"{language}.json",
                "killers",
            )
            self.assertEqual("4.6 m/s, 10.12 m/s",
                             entities["the_hillbilly"]["movement_speed"])

    def test_a_stale_balance_patch_is_refused(self):
        """The value moved again between drafting and applying."""
        self.set_label("the_hillbilly", "movement_speed", "4.2 m/s, 10.12 m/s")
        patch = propose.draft(fixture_opener(), "dbdle")
        self.set_label("the_hillbilly", "movement_speed", "4.3 m/s, 10.12 m/s")
        with self.assertRaises(sources.SourceError) as error:
            propose.apply_patch(patch)
        self.assertIn("re-draft", str(error.exception))

    def test_an_accepted_divergence_is_never_drafted(self):
        patch = propose.draft(fixture_opener(), "dbdle")
        touched = {(entry["entity_id"], entry["field"])
                   for entry in patch["updates"]}
        self.assertNotIn(("the_twins", "gender"), touched)
        self.assertNotIn(("the_singularity", "gender"), touched)

    def test_a_balance_change_on_a_daily_answer_waits_for_the_boundary(self):
        self.set_label("the_hillbilly", "movement_speed", "4.2 m/s, 10.12 m/s")
        patch = propose.draft(fixture_opener(), "dbdle")
        (self.data / "everydle_state.json").write_text(
            json.dumps({"dailies": {"dbdle_killer": {"date": "2026-08-22",
                                                     "answer": "the_hillbilly"}},
                        "decks": {}}),
            encoding="utf-8",
        )
        with self.assertRaises(sources.SourceError) as error:
            propose.apply_patch(patch)
        self.assertIn("current daily answer", str(error.exception))


class ProposeRoundTripTests(unittest.TestCase):
    """Draft a patch for a missing entity, fill it in, apply it, load it.

    The dataset is copied to a temporary directory and `everydle_sources.DATA_DIR`
    is redirected at it, so nothing here can touch the real data.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data = Path(self.temp_dir.name) / "data"
        shutil.copytree(ROOT / "data", self.data)
        self.original_dir = sources.DATA_DIR
        self.original_state = propose.STATE_FILE
        sources.DATA_DIR = self.data
        propose.STATE_FILE = self.data / "everydle_state.json"
        # Remove an agent so upstream looks ahead of the local dataset, and the
        # origin value only that agent uses, so the new-label path is exercised.
        self.remove_entity("valdle", "agents", "valdle.json", "miks")
        self.remove_attribute_value("valdle", "agents", "origin", "Croatia")

    def remove_attribute_value(self, game, dataset, field_name, english_label):
        value_id = None
        for locale in sorted((self.data / game / "locales").glob("*.json")):
            catalog = json.loads(locale.read_text(encoding="utf-8"))
            values = catalog["datasets"][dataset]["attributes"][field_name]
            if locale.stem == "en":
                value_id = next(key for key, text in values.items()
                                if text == english_label)
            if value_id in values:
                values.pop(value_id)
            locale.write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def tearDown(self):
        sources.DATA_DIR = self.original_dir
        propose.STATE_FILE = self.original_state
        self.temp_dir.cleanup()

    def remove_entity(self, game, dataset, mechanics_name, entity_id):
        path = self.data / game / mechanics_name
        document = json.loads(path.read_text(encoding="utf-8"))
        document["entities"].pop(entity_id)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        for locale in (self.data / game / "locales").glob("*.json"):
            catalog = json.loads(locale.read_text(encoding="utf-8"))
            catalog["datasets"][dataset]["entities"].pop(entity_id)
            locale.write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def draft(self):
        # Scoped to the game these tests manipulate: an unrelated
        # dataset with an entity nobody has filled in yet would refuse
        # the whole patch and mask what is being tested.
        return propose.draft(fixture_opener(), "valdle")

    def test_a_draft_names_the_fields_a_person_has_to_supply(self):
        patch = self.draft()
        entry = next(item for item in patch["additions"]
                     if item["entity_id"] == "miks")
        self.assertEqual("Controller", entry["fields"]["role"]["value"])
        self.assertEqual(2026, entry["fields"]["year"]["value"])
        self.assertEqual(["gender", "species", "origin"], entry["needs_a_person"])
        for field_name in entry["needs_a_person"]:
            self.assertIsNone(entry["fields"][field_name]["value"])
        # Both catalogs have to be filled, or the minigame will not load.
        self.assertEqual({"en", "hu"}, set(entry["names"]))

    def test_applying_an_unfinished_patch_is_refused(self):
        with self.assertRaises(sources.SourceError) as error:
            propose.apply_patch(self.draft())
        self.assertIn("nobody filled in", str(error.exception))

    def test_a_new_label_without_text_in_every_language_is_refused(self):
        patch = self.draft()
        entry = next(item for item in patch["additions"]
                     if item["entity_id"] == "miks")
        entry["fields"]["gender"]["value"] = "Male"
        entry["fields"]["species"]["value"] = "Radiant"
        entry["fields"]["origin"]["value"] = "Croatia"  # not in the catalog
        with self.assertRaises(sources.SourceError) as error:
            propose.apply_patch(patch)
        self.assertIn("origin/Croatia", str(error.exception))

    def completed_patch(self):
        patch = self.draft()
        entry = next(item for item in patch["additions"]
                     if item["entity_id"] == "miks")
        entry["fields"]["gender"]["value"] = "Male"
        entry["fields"]["species"]["value"] = "Radiant"
        entry["fields"]["origin"]["value"] = "Croatia"
        patch["labels"] = {"origin": {"Croatia": {"hu": "Horvatorszag",
                                                 "en": "Croatia"}}}
        return patch

    def test_a_completed_patch_applies_and_the_dataset_loads(self):
        import minigame_data

        changes = propose.apply_patch(self.completed_patch())
        self.assertTrue(any("added miks" in change for change in changes))
        self.assertTrue(any("minted origin_" in change for change in changes))
        for language in ("hu", "en"):
            entities, aliases = minigame_data.load_localized_dataset(
                self.data / "valdle" / "valdle.json",
                self.data / "valdle" / "locales" / f"{language}.json",
                "agents",
            )
            self.assertIn("miks", entities)
            self.assertEqual("Miks", entities["miks"]["_name"])
            self.assertEqual("Controller", entities["miks"]["role"])
            self.assertEqual(2026, entities["miks"]["year"])
            self.assertIn("miks", aliases.values())

    def test_a_dry_run_changes_nothing_on_disk(self):
        before = (self.data / "valdle" / "valdle.json").read_text(encoding="utf-8")
        changes = propose.apply_patch(self.completed_patch(), dry_run=True)
        self.assertTrue(changes)
        self.assertEqual(
            before, (self.data / "valdle" / "valdle.json").read_text(encoding="utf-8")
        )

    def test_applying_twice_is_refused_rather_than_renaming(self):
        """The same patch file re-applied. It has to refuse rather than rewrite
        the entity, because this tool only ever adds."""
        patch = self.completed_patch()
        propose.apply_patch(patch)
        with self.assertRaises(sources.SourceError) as error:
            propose.apply_patch(patch)
        self.assertIn("already exists", str(error.exception))

    def test_an_entity_that_is_a_current_daily_answer_is_refused(self):
        """Adding is safe any time, but the guard has to actually work, so it is
        exercised against a state file that names the entity as today's answer."""
        (self.data / "everydle_state.json").write_text(
            json.dumps({"dailies": {"valdle": {"date": "2026-08-22",
                                               "answer": "miks"}},
                        "decks": {}}),
            encoding="utf-8",
        )
        with self.assertRaises(sources.SourceError) as error:
            propose.apply_patch(self.completed_patch())
        self.assertIn("current daily answer", str(error.exception))

    def test_an_existing_label_reuses_its_id_rather_than_minting(self):
        """Minting a second id for a value already in the catalog would orphan
        every entity that points at the old one."""
        local = sources.load_local("valdle", "agents")
        existing = local.value_ids("role")["Controller"]
        propose.apply_patch(self.completed_patch())
        mechanics = json.loads(
            (self.data / "valdle" / "valdle.json").read_text(encoding="utf-8")
        )
        self.assertEqual(existing, mechanics["entities"]["miks"]["role"])

    def test_a_locked_game_in_a_patch_is_refused(self):
        patch = self.completed_patch()
        patch["additions"][0]["game"] = "loldle"
        with self.assertRaises(sources.SourceError):
            propose.apply_patch(patch)


if __name__ == "__main__":
    unittest.main()


class GenshindleAdapterTests(unittest.TestCase):
    """Genshindle is the first dataset with no lore attribute at all.

    Every field it runs on is published by the game, so unlike Valdle and DbDle
    this adapter reports nothing as needing a person — the only local knowledge
    is the aliases, which the drift report surfaces separately.
    """

    def setUp(self):
        self.characters = sources.fetch_genshindle(fixture_opener())

    def test_the_roster_parses(self):
        self.assertGreater(len(self.characters), 100)
        self.assertIn("amber", self.characters)

    def test_a_complete_character_needs_nobody(self):
        amber = self.characters["amber"]
        self.assertEqual((), amber.unknown_fields)
        # `body_type` is deliberately absent. Upstream still publishes it and the
        # adapter deliberately drops it: five values across 120 characters, most
        # of them in two, so it filled a column and narrowed almost nothing.
        # Asserted as an exact dict rather than a subset, so putting it back
        # would have to be a decision rather than an accident.
        self.assertEqual(
            {"element": "Pyro", "weapon": "Bow", "region": "Mondstadt",
             "rarity": "4-star", "gender": "Female",
             "weekly_boss": "Dvalin's Sigh", "version": 100},
            amber.fields,
        )
        self.assertNotIn("body_type", amber.fields)

    def test_the_weekly_boss_comes_from_the_talent_cost(self):
        """Identified by material id range, not by name: 125 of 125 characters
        have exactly one cost in it at talent level 10."""
        for key in ("amber", "zhongli", "raiden_shogun"):
            self.assertTrue(self.characters[key].fields["weekly_boss"])
        self.assertEqual(
            "Dvalin's Sigh",
            sources._genshin_weekly_boss(
                {"lvl10": [{"id": 202, "name": "Mora", "count": 1},
                           {"id": 113005, "name": "Dvalin's Sigh", "count": 1}]}),
        )
        # Nothing in the range means nothing is claimed.
        self.assertIsNone(sources._genshin_weekly_boss(
            {"lvl10": [{"id": 202, "name": "Mora", "count": 1}]}))

    def test_a_mapped_material_collapses_into_its_boss(self):
        """The API does not say which boss drops a material, so the value is the
        material until somebody records the mapping."""
        original = dict(sources.GENSHIN_WEEKLY_BOSS)
        try:
            sources.GENSHIN_WEEKLY_BOSS["Dvalin's Sigh"] = "Dvalin"
            self.assertEqual("Dvalin", sources._genshin_weekly_boss(
                {"lvl10": [{"id": 113005, "name": "Dvalin's Sigh"}]}))
        finally:
            sources.GENSHIN_WEEKLY_BOSS.clear()
            sources.GENSHIN_WEEKLY_BOSS.update(original)

    def test_the_version_packs_so_it_compares_as_a_number(self):
        """`1.10` must not collide with `1.1`, which it would as a float, and
        must sort after it, which it would not as text."""
        versions = {key: entity.fields.get("version")
                    for key, entity in self.characters.items()}
        self.assertEqual(100, versions["amber"])
        self.assertTrue(all(isinstance(v, int) for v in versions.values() if v))
        # The packing itself, at the boundary that matters.
        self.assertLess(101, 110)

    def test_the_traveller_has_every_element_rather_than_none(self):
        """Upstream writes "None", which is the opposite of what is true: the
        talents endpoint carries a record per element, seven of them. So no
        single element or boss is right and "All" is, and it plays as a clue."""
        for key in ("aether", "lumine"):
            fields = self.characters[key].fields
            self.assertEqual("All", fields["element"])
            self.assertEqual("All", fields["weekly_boss"])
            self.assertEqual("Outsider", fields["region"])

    def test_a_blank_region_is_taken_from_the_association(self):
        """Upstream leaves `region` blank for eleven characters and still states
        an association for every one, so the nation is published under another
        name rather than absent."""
        self.assertEqual("Snezhnaya", self.characters["odette"].fields["region"])
        self.assertEqual("Nod-Krai", self.characters["zibai"].fields["region"])
        # The four associations that name no nation share one honest value.
        for key in ("aloy", "skirk", "nicole"):
            self.assertEqual("Outsider", self.characters[key].fields["region"])

    def test_an_unmapped_association_is_a_gap_rather_than_a_guess(self):
        """A nation added to the game must become a finding, not a wrong label."""
        self.assertIsNone(sources._genshin_region(
            {"region": "", "associationType": "ASSOC_SOMEWHERE_NEW"}))
        self.assertEqual("Liyue", sources._genshin_region(
            {"region": "Liyue", "associationType": "ASSOC_LIYUE"}))

    def test_every_character_upstream_describes_is_now_complete(self):
        """Nothing is left out for want of an attribute — only for a reason."""
        incomplete = {key: list(entity.unknown_fields)
                      for key, entity in self.characters.items()
                      if entity.unknown_fields}
        self.assertEqual({}, incomplete)

    def test_the_absences_are_recorded_with_reasons(self):
        """A recurring report needs somewhere to record a decision — but only
        for a permanent one. Being unplayable is verifiable and does not change;
        whether a character has released is a question for a person, and putting
        it here was a mistake that briefly kept Alyosha out of a version she had
        shipped in."""
        for key in ("manekin", "manekina"):
            self.assertTrue(sources.is_excluded("genshindle", "characters", key))
        for key in ("amber", "aether", "aloy", "odette", "alyosha"):
            self.assertIsNone(sources.is_excluded("genshindle", "characters", key))


class GenshindleDatasetTests(unittest.TestCase):
    """The built dataset, as the cog will load it."""

    def test_it_loads_in_every_language_with_no_alias_collision(self):
        import minigame_data

        for language in ("hu", "en"):
            data, aliases = minigame_data.load_localized_dataset(
                ROOT / "data" / "genshindle" / "genshindle.json",
                ROOT / "data" / "genshindle" / "locales" / f"{language}.json",
                "characters",
            )
            self.assertGreater(len(data), 100, language)
            self.assertEqual(len(data), len(aliases), f"{language}: alias collision")

    def test_every_character_carries_every_attribute(self):
        """A missing attribute raises in `_resolve_value`, and `load_or_disable`
        turns that into the whole game disappearing — so a gap is not a gap, it
        is an outage."""
        import minigame_data

        from cogs.everydle import GENSHIN_FIELDS

        data, _ = minigame_data.load_localized_dataset(
            ROOT / "data" / "genshindle" / "genshindle.json",
            ROOT / "data" / "genshindle" / "locales" / "en.json",
            "characters",
        )
        for key, record in data.items():
            for field_name in (*GENSHIN_FIELDS, "version"):
                self.assertIn(field_name, record, f"{key} lacks {field_name}")
                self.assertTrue(record[field_name] not in (None, ""),
                                f"{key}.{field_name} is empty")

    def test_the_cog_compares_every_attribute_the_dataset_carries(self):
        """A field in the data but not in the row is a clue nobody ever sees."""
        import json

        from cogs.everydle import GENSHIN_FIELDS

        mechanics = json.loads(
            (ROOT / "data" / "genshindle" / "genshindle.json").read_text("utf-8"))
        carried = {name for record in mechanics["entities"].values()
                   for name in record}
        # `version` is compared higher/lower rather than matched, so it is
        # rendered separately and is not in the exact-match list.
        self.assertEqual(carried, set(GENSHIN_FIELDS) | {"version"})
