"""Load minigame mechanics and language-specific display data safely."""

import json
import logging
from pathlib import Path


data_logger = logging.getLogger("PotatoBot.MinigameData")
SUPPORTED_SCHEMA_VERSION = 1


class MinigameDataError(RuntimeError):
    """Raised when shared data and its selected locale cannot be combined."""


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as source:
        return json.load(source)


def _resolve_value(raw_value, field, translations, context):
    """Resolve enum IDs recursively while preserving numeric mechanics."""
    if isinstance(raw_value, list):
        return [
            _resolve_value(value, field, translations, context)
            for value in raw_value
        ]
    if not isinstance(raw_value, str):
        return raw_value
    translated = translations.get(field, {}).get(raw_value)
    if not translated:
        raise MinigameDataError(
            f"Missing localized minigame value for {context}.{field}.{raw_value}"
        )
    return translated


def load_localized_dataset(core_path, locale_path, dataset_name):
    """Merge one stable-ID dataset with one complete language catalog."""
    core = _read_json(core_path)
    locale = _read_json(locale_path)
    if core.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise MinigameDataError(f"Unsupported minigame schema: {core_path}")
    if locale.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise MinigameDataError(f"Unsupported minigame locale schema: {locale_path}")
    if core.get("dataset") != dataset_name:
        raise MinigameDataError(
            f"Dataset identity mismatch in {core_path}: expected {dataset_name}"
        )

    locale_section = locale.get("datasets", {}).get(dataset_name)
    if not isinstance(locale_section, dict):
        raise MinigameDataError(
            f"Locale {locale_path} has no {dataset_name} dataset section"
        )
    localized_entities = locale_section.get("entities", {})
    translations = locale_section.get("attributes", {})
    entities = {}
    aliases = {}

    for entity_id, mechanics in core.get("entities", {}).items():
        localized_identity = localized_entities.get(entity_id, {})
        display_name = localized_identity.get("name", "").strip()
        if not display_name:
            raise MinigameDataError(
                f"Missing localized minigame name for {dataset_name}.{entity_id}"
            )

        localized_record = {
            field: _resolve_value(
                raw_value,
                field,
                translations,
                f"{dataset_name}.{entity_id}",
            )
            for field, raw_value in mechanics.items()
        }
        localized_record["_id"] = entity_id
        localized_record["_name"] = display_name
        entities[entity_id] = localized_record

        candidate_aliases = [display_name, *localized_identity.get("aliases", [])]
        for alias in candidate_aliases:
            normalized_alias = alias.strip().casefold()
            if normalized_alias:
                existing = aliases.setdefault(normalized_alias, entity_id)
                if existing != entity_id:
                    raise MinigameDataError(
                        f"Duplicate localized alias in {dataset_name}: {alias}"
                    )

    return entities, aliases


def load_or_disable(core_path, locale_path, dataset_name):
    """Disable only the affected game when localized data is incomplete."""
    try:
        return load_localized_dataset(core_path, locale_path, dataset_name)
    except (OSError, json.JSONDecodeError, MinigameDataError) as error:
        data_logger.error(
            "Minigame dataset disabled (dataset=%s, reason=%s)", dataset_name, error
        )
        return {}, {}
