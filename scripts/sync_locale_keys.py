"""Align every locale catalog with the Hungarian one.

New Hungarian keys appear elsewhere as empty strings. Keys removed from
Hungarian are removed everywhere too, because the catalogs must stay
structurally identical and only the primary catalog decides the shape.

Hungarian and English are both first-class: an empty English value fails
`tests/test_locale_coverage.py`, so this script reports the English keys it left
blank rather than leaving them to be discovered by a red build. Every language
after English keeps the old rule and is left for a human translator.
"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIMARY_PATH = ROOT / "locales" / "hu.json"


def add_missing_shape(primary, target):
    """Recursively copy object shape while leaving new translated leaves empty."""
    for key, primary_value in primary.items():
        if isinstance(primary_value, dict):
            target_value = target.setdefault(key, {})
            if not isinstance(target_value, dict):
                raise TypeError(f"Locale structure mismatch at {key}")
            add_missing_shape(primary_value, target_value)
        elif key not in target:
            target[key] = ""


def drop_removed_keys(primary, target):
    """Delete keys the primary catalog no longer defines."""
    for key in [key for key in target if key not in primary]:
        del target[key]
    for key, primary_value in primary.items():
        if isinstance(primary_value, dict) and isinstance(target.get(key), dict):
            drop_removed_keys(primary_value, target[key])


# English is generated alongside Hungarian rather than left for a translator, so
# a blank value here is unfinished work rather than a pending translation.
REQUIRED_LANGUAGES = ("en",)


def blank_keys(catalog, prefix=""):
    """Every dotted key whose value is an empty string."""
    for key, value in catalog.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from blank_keys(value, dotted)
        elif isinstance(value, str) and not value.strip():
            yield dotted


def main():
    primary = json.loads(PRIMARY_PATH.read_text(encoding="utf-8"))
    outstanding = {}
    for target_path in sorted((ROOT / "locales").glob("*.json")):
        if target_path == PRIMARY_PATH:
            continue
        target = json.loads(target_path.read_text(encoding="utf-8"))
        add_missing_shape(primary, target)
        drop_removed_keys(primary, target)
        target_path.write_text(
            json.dumps(target, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if target_path.stem in REQUIRED_LANGUAGES:
            outstanding[target_path.stem] = sorted(blank_keys(target))

    for language, keys in outstanding.items():
        if not keys:
            continue
        print(f"{language}: {len(keys)} key(s) still need text:")
        for key in keys:
            print(f"  {key}")


if __name__ == "__main__":
    main()
