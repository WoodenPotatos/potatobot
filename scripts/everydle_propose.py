"""Turn Everydle drift into a reviewable patch, then apply it.

Generating and applying are deliberately two commands. The generator writes a
patch file with every field the source could supply and `null` wherever a person
has to decide; applying refuses while any of those are still null. Nothing here
edits `data/` except `apply`, and `apply` will not run when it would disturb a
puzzle that is currently in play.

Usage:
    python scripts/everydle_propose.py draft  -o patch.json [--fixtures DIR]
    python scripts/everydle_propose.py apply patch.json [--dry-run]

After applying, the bot has to re-read the files: `?reload everydle`, or a
restart. `cogs/everydle.py` loads its datasets at import time.

See `docs/everydle_data_updates.md`. LoLdle is excluded throughout.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import everydle_drift as drift_module  # noqa: E402
import everydle_sources  # noqa: E402
from everydle_sources import (  # noqa: E402
    LOCKED_GAMES,
    MANAGED_DATASETS,
    MECHANICS_FILE,
    SourceError,
    load_local,
    mint_value_id,
)

PATCH_VERSION = 1
STATE_FILE = ROOT / "data" / "everydle_state.json"
# Which persisted daily belongs to which dataset, so `apply` can tell whether a
# change would land on a puzzle someone is part-way through.
DAILY_KEYS = {("valdle", "agents"): ("valdle",),
              ("dbdle", "killers"): ("dbdle_killer", "dbdle")}


def _catalog_languages(local) -> tuple[str, ...]:
    """Every language this dataset has a catalog for.

    A patch has to carry text for all of them: a blank entity name does not
    degrade, it disables the whole minigame through `load_or_disable`.
    """
    return tuple(sorted(local.catalogs))


def draft(opener=None) -> dict:
    """Build a patch from the current drift.

    Two sections. `additions` are new entities with the lore fields left null for
    a person. `updates` are balance changes on fields the game publishes, so they
    arrive already filled in — a killer that was buffed or nerfed follows the
    game, and there is nothing to decide.
    """
    additions = []
    updates = []
    for game, dataset in MANAGED_DATASETS:
        report = drift_module.compare(game, dataset, opener)
        for entry in report["balance_changes"]:
            updates.append({
                "game": game,
                "dataset": dataset,
                "entity_id": entry["entity_id"],
                "field": entry["field"],
                "from": entry["local"],
                "to": entry["merged"],
                "upstream": entry["upstream"],
            })
        if not report["new_upstream"]:
            continue
        local = load_local(game, dataset)
        languages = _catalog_languages(local)
        for entry in report["new_upstream"]:
            fields = {}
            todo = []
            for field_name, value in entry["supplied"].items():
                fields[field_name] = {"value": value, "from": "source"}
            for field_name in entry["needs_a_person"]:
                fields[field_name] = {"value": None, "from": "you"}
                todo.append(field_name)
            additions.append({
                "game": game,
                "dataset": dataset,
                "entity_id": entry["entity_id"],
                "names": {language: entry["display_name"] for language in languages},
                "aliases": {language: [entry["display_name"]] for language in languages},
                "fields": fields,
                "needs_a_person": todo,
            })
    return {
        "patch_version": PATCH_VERSION,
        "additions": additions,
        "updates": updates,
        # Text for any attribute label that is not in the catalog yet, as
        # {field: {english_label: {language: text}}}. `apply` says exactly what
        # it needs here, so this stays empty until it asks.
        "labels": {},
        "note": (
            "Entries under `updates` are already complete: the game publishes "
            "those fields, so they follow it. "
            "Replace every null under `fields` with the value for that entity, "
            "then run: python scripts/everydle_propose.py apply <this file>. "
            "Numeric fields take a number; everything else takes the English "
            "label, which is matched against the catalog and minted if new. "
            "Add the Hungarian text for any minted label when prompted."
        ),
    }


def _resolve_or_mint(local, field_name: str, value, minted: dict):
    """Map an English label to its value id, minting one if it is new.

    An existing value's id is always reused: the mechanics file references it,
    and a fresh id would orphan every entity pointing at the old one.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value, None
    existing = local.value_ids(field_name)
    if value in existing:
        return existing[value], None
    if (field_name, value) in minted:
        return minted[(field_name, value)], None
    taken = set(existing.values())
    for language in local.catalogs:
        section = local.catalogs[language]["datasets"][local.dataset]
        taken.update((section.get("attributes") or {}).get(field_name, {}))
    value_id = mint_value_id(local.dataset, field_name, value, taken)
    minted[(field_name, value)] = value_id
    return value_id, value


def _current_daily(game: str, dataset: str) -> set[str]:
    """Entity ids that are somebody's answer right now."""
    if not STATE_FILE.exists():
        return set()
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    answers = set()
    for key in DAILY_KEYS.get((game, dataset), ()):
        for stored_key, daily in (state.get("dailies") or {}).items():
            if stored_key.startswith(key):
                answer = (daily or {}).get("answer")
                if answer:
                    answers.add(answer)
    return answers


_LOADED = {}


def _reuse_or_load(pending_writes, game: str, dataset: str):
    """One in-memory dataset per (game, dataset) across both patch sections.

    Additions and updates can touch the same files, and loading twice would make
    the second write discard the first.
    """
    key = (game, dataset)
    if key not in _LOADED:
        _LOADED[key] = load_local(game, dataset)
    return _LOADED[key]


def _queue_writes(pending_writes, game: str, dataset: str, local) -> None:
    game_dir = everydle_sources.DATA_DIR / game
    targets = [(game_dir / MECHANICS_FILE[(game, dataset)], local.mechanics)]
    for language, catalog in local.catalogs.items():
        targets.append((game_dir / "locales" / f"{language}.json", catalog))
    for target in targets:
        if target not in pending_writes:
            pending_writes.append(target)


def apply_patch(patch: dict, dry_run: bool = False) -> list[str]:
    """Apply a completed patch. Returns a list of what changed.

    Refuses on three grounds, each of which is a way this could go wrong
    silently: a field nobody filled in, a new attribute label with no text for
    every language, and an entity that already exists — because this tool only
    ever adds, and renaming an entity re-draws the day's answer.
    """
    if patch.get("patch_version") != PATCH_VERSION:
        raise SourceError(f"unsupported patch version: {patch.get('patch_version')}")

    additions = patch.get("additions") or []
    incomplete = [
        f"{entry['game']}.{entry['dataset']}.{entry['entity_id']}.{name}"
        for entry in additions
        for name, spec in entry["fields"].items()
        if spec.get("value") is None
    ]
    if incomplete:
        raise SourceError(
            "the patch still has fields nobody filled in: " + ", ".join(incomplete)
        )

    by_dataset = {}
    for entry in additions:
        if entry["game"] in LOCKED_GAMES:
            raise SourceError(f"{entry['game']} is excluded from automatic updates")
        by_dataset.setdefault((entry["game"], entry["dataset"]), []).append(entry)

    supplied_labels = patch.get("labels") or {}
    changes = []
    pending_writes = []
    _LOADED.clear()

    for (game, dataset), entries in sorted(by_dataset.items()):
        local = _reuse_or_load(pending_writes, game, dataset)
        in_play = _current_daily(game, dataset)
        minted = {}
        # First pass: resolve every value, minting ids for labels the catalog
        # does not have yet, and collect what still needs text.
        planned = []
        untranslated = []
        for entry in entries:
            entity_id = entry["entity_id"]
            if entity_id in local.entities:
                raise SourceError(
                    f"{game}.{dataset}.{entity_id} already exists; this tool only "
                    "adds entities, and renaming one would re-draw the day's answer"
                )
            if entity_id in in_play:
                raise SourceError(
                    f"{game}.{dataset}.{entity_id} is a current daily answer; "
                    "apply this after the next day boundary"
                )
            mechanics = {}
            for field_name, spec in sorted(entry["fields"].items()):
                value_id, new_label = _resolve_or_mint(
                    local, field_name, spec["value"], minted
                )
                mechanics[field_name] = value_id
                if new_label is None:
                    continue
                texts = (supplied_labels.get(field_name) or {}).get(new_label) or {}
                for language in local.catalogs:
                    if not str(texts.get(language, "")).strip():
                        untranslated.append(f"{field_name}/{new_label}/{language}")
            planned.append((entry, mechanics))

        if untranslated:
            raise SourceError(
                "these new attribute labels need text in every language — add a "
                "`labels` block to the patch, e.g. "
                '{"labels": {"origin": {"Croatia": {"hu": "…", "en": "Croatia"}}}}: '
                + ", ".join(sorted(set(untranslated)))
            )

        # Second pass: mutate the in-memory documents.
        for (field_name, label), value_id in minted.items():
            texts = supplied_labels[field_name][label]
            for language, catalog in local.catalogs.items():
                section = catalog["datasets"][dataset]
                section.setdefault("attributes", {}).setdefault(
                    field_name, {}
                )[value_id] = texts[language]
            changes.append(f"{game}.{dataset}: minted {value_id} for {label!r}")

        for entry, mechanics in planned:
            entity_id = entry["entity_id"]
            local.mechanics.setdefault("entities", {})[entity_id] = mechanics
            for language, catalog in local.catalogs.items():
                catalog["datasets"][dataset]["entities"][entity_id] = {
                    "name": entry["names"][language],
                    "aliases": list(entry["aliases"][language]),
                }
            changes.append(f"{game}.{dataset}: added {entity_id}")

        _queue_writes(pending_writes, game, dataset, local)

    # Balance changes: the game publishes these fields, so the local value
    # follows. This does change an in-flight puzzle's colour feedback, which is
    # why the day-boundary guard covers it too.
    updates = patch.get("updates") or []
    by_update_dataset = {}
    for entry in updates:
        if entry["game"] in LOCKED_GAMES:
            raise SourceError(f"{entry['game']} is excluded from automatic updates")
        by_update_dataset.setdefault((entry["game"], entry["dataset"]), []).append(entry)

    for (game, dataset), entries in sorted(by_update_dataset.items()):
        local = _reuse_or_load(pending_writes, game, dataset)
        in_play = _current_daily(game, dataset)
        minted = {}
        for entry in entries:
            entity_id, field_name = entry["entity_id"], entry["field"]
            if entity_id not in local.entities:
                raise SourceError(f"{game}.{dataset}.{entity_id} does not exist")
            if entity_id in in_play:
                raise SourceError(
                    f"{game}.{dataset}.{entity_id} is a current daily answer; "
                    "apply this after the next day boundary"
                )
            current = local.resolved(entity_id).get(field_name)
            if current != entry["from"]:
                raise SourceError(
                    f"{game}.{dataset}.{entity_id}.{field_name} now reads "
                    f"{current!r}, not the {entry['from']!r} this patch was "
                    "drafted against; re-draft it"
                )
            value_id, new_label = _resolve_or_mint(
                local, field_name, entry["to"], minted
            )
            if new_label is not None:
                # A numeric label reads the same in every language, which is the
                # only reason a balance change can mint text without asking.
                for catalog in local.catalogs.values():
                    catalog["datasets"][dataset].setdefault(
                        "attributes", {}
                    ).setdefault(field_name, {})[value_id] = new_label
                changes.append(f"{game}.{dataset}: minted {value_id} for {new_label!r}")
            local.entities[entity_id][field_name] = value_id
            changes.append(
                f"{game}.{dataset}.{entity_id}: {field_name} "
                f"{entry['from']!r} -> {entry['to']!r}"
            )
        _queue_writes(pending_writes, game, dataset, local)

    if not dry_run:
        for path, document in pending_writes:
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    draft_parser = commands.add_parser("draft", help="write a patch to fill in")
    draft_parser.add_argument("-o", "--output", type=Path, required=True)
    draft_parser.add_argument("--fixtures", type=Path, default=None)

    apply_parser = commands.add_parser("apply", help="apply a completed patch")
    apply_parser.add_argument("patch", type=Path)
    apply_parser.add_argument("--dry-run", action="store_true")

    arguments = parser.parse_args()
    try:
        if arguments.command == "draft":
            opener = (drift_module.load_fixtures(arguments.fixtures)
                      if arguments.fixtures else None)
            patch = draft(opener)
            arguments.output.write_text(
                json.dumps(patch, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            count = len(patch["additions"])
            print(f"wrote {arguments.output} with {count} addition(s)")
            for entry in patch["additions"]:
                todo = ", ".join(entry["needs_a_person"]) or "nothing"
                print(f"  {entry['game']}.{entry['dataset']}.{entry['entity_id']}: "
                      f"you still need to supply {todo}")
            if not count:
                print("  nothing to add; the datasets are in step with upstream")
            return 0

        patch = json.loads(arguments.patch.read_text(encoding="utf-8"))
        changes = apply_patch(patch, arguments.dry_run)
        for change in changes:
            print(("would " if arguments.dry_run else "") + change)
        if changes and not arguments.dry_run:
            print("\nRun `?reload everydle` or restart the bot: the datasets are "
                  "read at import time.")
        if not changes:
            print("nothing to apply")
        return 0
    except SourceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
