import json
import tempfile
import unittest
from pathlib import Path

from minigame_data import MinigameDataError, load_localized_dataset


ROOT = Path(__file__).resolve().parents[1]
# Datasets a specific administrator maintains by hand. They are never edited
# automatically, which is why their English catalogs stay blank and the games
# they back are unavailable in English.
OWNER_MAINTAINED_GAMES = {"loldle"}

DATASETS = {
    "loldle": [("champions.json", "champions"), ("loldlehardmode.json", "hard_mode")],
    "valdle": [("valdle.json", "agents")],
    "dbdle": [
        ("killers.json", "killers"),
        ("survivors.json", "survivors"),
        ("perks.json", "perks"),
        ("dbdle.json", "roster"),
    ],
}


def collect_leaf_paths(value, prefix=()):
    if isinstance(value, dict):
        result = set()
        for key, child in value.items():
            result.update(collect_leaf_paths(child, prefix + (key,)))
        return result
    if isinstance(value, list):
        return {prefix + (str(index),) for index in range(len(value))}
    return {prefix}


class MinigameLocalizationTests(unittest.TestCase):
    def test_hungarian_catalogs_load_every_dataset(self):
        expected_empty = {("loldle", "hard_mode")}
        for game, datasets in DATASETS.items():
            locale_path = ROOT / "data" / game / "locales" / "hu.json"
            for filename, dataset_name in datasets:
                entities, aliases = load_localized_dataset(
                    ROOT / "data" / game / filename,
                    locale_path,
                    dataset_name,
                )
                if (game, dataset_name) in expected_empty:
                    self.assertEqual({}, entities)
                else:
                    self.assertTrue(entities, f"{game}.{dataset_name} has no entities")
                    self.assertTrue(aliases, f"{game}.{dataset_name} has no aliases")

    def test_english_catalogs_match_the_hungarian_shape(self):
        for game in DATASETS:
            locale_dir = ROOT / "data" / game / "locales"
            hu = json.loads((locale_dir / "hu.json").read_text(encoding="utf-8"))
            en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
            with self.subTest(game=game):
                self.assertEqual(collect_leaf_paths(hu), collect_leaf_paths(en))

    def test_english_catalogs_load_every_dataset_except_the_owned_one(self):
        """English is generated alongside Hungarian since 2026-08-22, so these
        catalogs are complete rather than empty placeholders.

        `data/loldle/` is the exception: it is maintained by a named
        administrator who asked that it not be edited automatically, so its
        English catalog is still blank and LoLdle is unavailable in English.
        """
        expected_empty = {("loldle", "hard_mode")}
        for game, datasets in DATASETS.items():
            locale_path = ROOT / "data" / game / "locales" / "en.json"
            for filename, dataset_name in datasets:
                core = ROOT / "data" / game / filename
                with self.subTest(game=game, dataset=dataset_name):
                    if game in OWNER_MAINTAINED_GAMES:
                        # A blank entity name is fatal by design, which is what
                        # makes an unfinished catalog remove the game rather
                        # than render empty names to a player.
                        if (game, dataset_name) in expected_empty:
                            continue
                        with self.assertRaises(MinigameDataError):
                            load_localized_dataset(core, locale_path, dataset_name)
                        continue
                    entities, aliases = load_localized_dataset(
                        core, locale_path, dataset_name
                    )
                    if (game, dataset_name) in expected_empty:
                        self.assertEqual({}, entities)
                    else:
                        self.assertTrue(entities)
                        self.assertTrue(aliases)

    def test_an_incomplete_catalog_still_fails_closed(self):
        """The guarantee that makes an unfinished translation safe: a blank name
        is an error, not a blank label rendered to a player."""
        core = ROOT / "data" / "valdle" / "valdle.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = json.loads(
                (ROOT / "data" / "valdle" / "locales" / "en.json")
                .read_text(encoding="utf-8")
            )
            for identity in catalog["datasets"]["agents"]["entities"].values():
                identity["name"] = ""
            blank = Path(temp_dir) / "blank.json"
            blank.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(MinigameDataError):
                load_localized_dataset(core, blank, "agents")

    def test_english_entity_names_are_not_translated_away(self):
        """Entity names are proper nouns, so the two catalogs agree on them. An
        attribute label is prose and must not."""
        for game in set(DATASETS) - OWNER_MAINTAINED_GAMES:
            locale_dir = ROOT / "data" / game / "locales"
            hu = json.loads((locale_dir / "hu.json").read_text(encoding="utf-8"))
            en = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
            for dataset_name, dataset in en["datasets"].items():
                source = hu["datasets"][dataset_name]
                with self.subTest(game=game, dataset=dataset_name):
                    for entity_id, identity in dataset["entities"].items():
                        self.assertEqual(
                            source["entities"][entity_id]["name"], identity["name"]
                        )
                        self.assertTrue(identity["name"].strip())
                    for values in dataset["attributes"].values():
                        self.assertTrue(
                            all(value.strip() for value in values.values())
                        )

    def test_legacy_daily_names_are_upgraded_without_changing_answer(self):
        # Import here because the cog requires the Discord test dependency.
        import cogs.everydle as everydle

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "decks": {"loldle_easy": ["Ahri"]},
                        "dailies": {
                            "loldle_easy": {
                                "date": everydle.datetime.now().strftime("%Y-%m-%d"),
                                "answer": "Aatrox",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            original_state_file = everydle.STATE_FILE
            original_data_dir = everydle.DATA_DIR
            try:
                everydle.STATE_FILE = str(state_path)
                everydle.DATA_DIR = temp_dir
                answer = everydle.get_daily_target(
                    "loldle_easy",
                    list(everydle.CHAMPIONS),
                    everydle.CHAMPIONS_LOWER,
                )
            finally:
                everydle.STATE_FILE = original_state_file
                everydle.DATA_DIR = original_data_dir
            self.assertEqual("aatrox", answer)
            migrated = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("aatrox", migrated["dailies"]["loldle_easy"]["answer"])
            self.assertEqual(["ahri"], migrated["decks"]["loldle_easy"])


if __name__ == "__main__":
    unittest.main()
