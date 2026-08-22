# Multilingual Minigame Data

Each game separates language-independent mechanics from display text:

- `data/<game>/*.json` contains schema metadata, stable entity IDs, stable attribute IDs, and numeric mechanics such as release years.
- `data/<game>/locales/hu.json` contains the complete primary Hungarian entity names, accepted aliases, and attribute labels.
- `data/<game>/locales/en.json` mirrors every Hungarian entry with empty values until a human supplies the English translation.

`minigame_data.load_localized_dataset()` merges these files at startup. Missing names, attributes, duplicate aliases, invalid schemas, or missing locale files disable only the affected minigame and produce an English operational log. Empty datasets are allowed for unfinished modes such as LoLdle hard mode.

## Adding or Updating Content

1. Add a stable, lowercase English entity ID to the shared dataset. Do not use a translated display name as an ID.
2. Store numeric mechanics directly. Represent any displayable categorical value with a stable field-prefixed ID.
3. Add the entity, display name, aliases, and attribute values to the Hungarian catalog.
4. Add the identical structure to every other language catalog, using empty strings for untranslated text. Do not copy Hungarian text or generate a translation.
5. Run `python -m unittest tests.test_minigame_localization -v` before deployment.

Daily target state uses stable entity IDs. The loader recognizes the previous Hungarian display names and migrates existing daily answers and shuffled decks atomically, preserving the active target during deployment.
