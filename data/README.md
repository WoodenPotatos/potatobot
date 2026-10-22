# Multilingual Minigame Data

The Everydle games (`loldle`, `valdle`, `dbdle`, `genshindle`) each separate language-independent mechanics from display text:

- `data/<game>/*.json` contains schema metadata, stable entity IDs, stable attribute IDs, and numeric mechanics such as release years.
- `data/<game>/locales/hu.json` contains the complete primary Hungarian entity names, accepted aliases, and attribute labels.
- `data/<game>/locales/en.json` mirrors every Hungarian entry and is filled in the same change. An empty English value fails the build, except in a dataset that is deliberately incomplete in English, such as LoLdle hard mode, which `load_or_disable` then takes offline in English.

`core/minigame_data.py`'s `load_localized_dataset()` merges these files when `cogs/everydle.py` is imported, and `load_or_disable()` turns any failure into that one minigame vanishing for that language. Missing names, attributes, duplicate aliases, invalid schemas, or missing locale files disable only the affected minigame and produce an English operational log. Because the datasets load at import, a change needs `?reload everydle` or a restart.

`data/wordchain/` is different: plain word lists for the word-chain real-word check, with their source and licence in `data/wordchain/ATTRIBUTION.md`.

## Adding or Updating Content

1. Add a stable, lowercase English entity ID to the shared dataset. Do not use a translated display name as an ID, and never rename an existing one: add an alias instead.
2. Store numeric mechanics directly. Represent any displayable categorical value with a stable field-prefixed ID, and reuse an existing attribute value ID rather than deriving a new one.
3. Add the entity, display name, aliases, and attribute values to the Hungarian catalog, with every field filled: a missing attribute disables the whole game.
4. Add the identical structure to `en.json`, with the English text filled in. Every language after English gets the identical structure with empty strings, left for a human translator.
5. Run `venv/bin/python -m unittest tests.test_minigame_localization -v` before deployment.

For every Everydle dataset, LoLdle champions included since 2026-09-27, `scripts/everydle_drift.py` reports what changed upstream and `scripts/everydle_propose.py` drafts and applies a reviewed patch. Apply attribute changes at a day boundary, because feedback a player already received was computed against the old value. `docs/everydle_data_updates.md` has the details.

Daily target state uses stable entity IDs. The loader recognizes the previous Hungarian display names and migrates existing daily answers and shuffled decks atomically, preserving the active target during deployment.
