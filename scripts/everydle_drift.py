"""Report where the Everydle datasets have drifted from the games they describe.

Read-only. It never edits `data/`, and it is the component that carries almost
all of the value: the expensive step in keeping these puzzles current is
*noticing* that a game shipped a character, not editing three JSON files.

Four kinds of finding:

- **new upstream** — an entity the game has and the dataset does not, with every
  field the source could supply and an explicit list of the lore fields a person
  still has to fill in.
- **missing upstream** — an entity the dataset has and the source does not. This
  is usually a spelling change rather than a removal, so it is never a
  suggestion to delete anything.
- **disagreement** — both sides have the entity and a field differs. The tool
  does not know which side is right; that is the point of showing both.
- **local gap** — a field absent from an entity, which renders as an empty
  column in the puzzle.

Usage:
    python scripts/everydle_drift.py                 # live sources
    python scripts/everydle_drift.py --fixtures DIR  # recorded payloads
    python scripts/everydle_drift.py --json

Exits non-zero when there is drift, so it can gate a scheduled check. An
unreachable source is reported and exits 2, distinct from "found drift", because
a source being down is not a data problem.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from everydle_sources import (  # noqa: E402
    MANAGED_DATASETS,
    PIVOT_LANGUAGE,
    SOURCE_AUTHORITATIVE,
    SourceError,
    fetch,
    fixture_opener,
    is_accepted,
    load_local,
    merge_base_value,
)

FIXTURE_FILES = {
    "https://valorant-api.com/v1/agents?isPlayableCharacter=true":
        "valorant_agents.json",
    "https://dbd.tricky.lol/api/characters": "dbd_characters.json",
    "https://dbd.tricky.lol/api/dlc": "dbd_dlc.json",
}


def load_fixtures(directory: Path):
    responses = {}
    for url, name in FIXTURE_FILES.items():
        path = directory / name
        if path.exists():
            responses[url] = json.loads(path.read_text(encoding="utf-8"))
    return fixture_opener(responses)


def compare(game: str, dataset: str, opener=None) -> dict:
    """Diff one local dataset against its upstream source."""
    local = load_local(game, dataset)
    upstream = fetch(game, dataset, opener)

    local_ids = set(local.entities)
    upstream_ids = set(upstream)
    catalog = local.catalogs[PIVOT_LANGUAGE]["datasets"][dataset]["entities"]

    new_upstream = []
    for entity_id in sorted(upstream_ids - local_ids):
        entity = upstream[entity_id]
        new_upstream.append({
            "entity_id": entity_id,
            "display_name": entity.display_name,
            "supplied": dict(sorted(entity.fields.items())),
            "needs_a_person": list(entity.unknown_fields),
        })

    missing_upstream = [
        {"entity_id": entity_id,
         "display_name": (catalog.get(entity_id) or {}).get("name", entity_id)}
        for entity_id in sorted(local_ids - upstream_ids)
    ]

    authoritative = SOURCE_AUTHORITATIVE.get((game, dataset), ())
    disagreements = []
    balance_changes = []
    accepted = []
    for entity_id in sorted(local_ids & upstream_ids):
        resolved = local.resolved(entity_id)
        for field_name, upstream_value in sorted(upstream[entity_id].fields.items()):
            if field_name not in resolved:
                continue  # A local gap, reported separately.
            local_value = resolved[field_name]
            if local_value == upstream_value:
                continue
            # The local data lists power-state values after the base one
            # (`4.6 m/s, 10.12 m/s`); upstream has only the base tunable, so a
            # prefix match is agreement on the part upstream actually knows.
            if (isinstance(local_value, str) and isinstance(upstream_value, str)
                    and local_value.startswith(upstream_value)):
                continue
            finding = {
                "entity_id": entity_id,
                "field": field_name,
                "local": local_value,
                "upstream": upstream_value,
            }
            if is_accepted(game, dataset, entity_id, field_name):
                accepted.append(finding)
            elif field_name in authoritative:
                # The game publishes this field and changes it with a patch, so
                # the local value follows rather than being argued with.
                finding["merged"] = merge_base_value(local_value, upstream_value)
                balance_changes.append(finding)
            else:
                disagreements.append(finding)

    fields = local.fields()
    local_gaps = [
        {"entity_id": entity_id, "field": field_name}
        for entity_id in sorted(local_ids)
        for field_name in fields
        if field_name not in local.entities[entity_id]
    ]

    # An unnamed alias is how a spelling change is absorbed without renaming an
    # entity id, so a name upstream uses that the catalog does not list is worth
    # reporting even when the id already matches.
    alias_gaps = []
    for entity_id in sorted(local_ids & upstream_ids):
        known = {(catalog.get(entity_id) or {}).get("name", "")}
        known.update((catalog.get(entity_id) or {}).get("aliases") or [])
        for name in upstream[entity_id].aliases:
            if name and name not in known:
                alias_gaps.append({"entity_id": entity_id, "alias": name})

    # Two value ids whose labels differ only in whitespace are worse than a
    # typo: the puzzle compares by id, so two killers whose speed *reads* the
    # same are scored as different. Reported rather than merged, because
    # repointing an entity at another id changes an in-flight puzzle.
    confusable = []
    for field_name in fields:
        by_shape = {}
        for value_id, text in _attribute_values(local, field_name).items():
            by_shape.setdefault("".join(text.split()).casefold(), []).append(
                (value_id, text)
            )
        for group in by_shape.values():
            if len(group) > 1:
                confusable.append({
                    "field": field_name,
                    "values": [{"value_id": value_id, "text": text}
                               for value_id, text in sorted(group)],
                })

    return {
        "game": game,
        "dataset": dataset,
        "local_count": len(local_ids),
        "upstream_count": len(upstream_ids),
        "new_upstream": new_upstream,
        "missing_upstream": missing_upstream,
        "disagreements": disagreements,
        "balance_changes": balance_changes,
        "accepted_divergences": accepted,
        "local_gaps": local_gaps,
        "alias_gaps": alias_gaps,
        "confusable_values": confusable,
    }


def _attribute_values(local, field_name: str) -> dict:
    """Value id -> label for one field, in the pivot language."""
    section = local.catalogs[PIVOT_LANGUAGE]["datasets"][local.dataset]
    return (section.get("attributes") or {}).get(field_name, {})


# `accepted_divergences` is deliberately absent: a divergence somebody has
# already decided about is context, not a finding, and counting it would make the
# report noisy enough to be ignored.
FINDING_KEYS = ("new_upstream", "missing_upstream", "disagreements",
                "balance_changes", "local_gaps", "alias_gaps",
                "confusable_values")


def has_drift(report: dict) -> bool:
    return any(report[key] for key in FINDING_KEYS)


def print_report(reports: list[dict]) -> None:
    print("=" * 74)
    print("EVERYDLE DATA DRIFT")
    print("=" * 74)
    for report in reports:
        print(f"\n{report['game']}.{report['dataset']}: "
              f"{report['local_count']} local / {report['upstream_count']} upstream")

        if report["new_upstream"]:
            print("\n  NEW UPSTREAM — the game has these and the dataset does not")
            for entry in report["new_upstream"]:
                print(f"    {entry['entity_id']}  ({entry['display_name']})")
                for name, value in entry["supplied"].items():
                    print(f"        {name:16} {value}  (from the source)")
                if entry["needs_a_person"]:
                    print(f"        needs a person:  "
                          f"{', '.join(entry['needs_a_person'])}")

        if report["missing_upstream"]:
            print("\n  MISSING UPSTREAM — usually a rename, never auto-delete")
            for entry in report["missing_upstream"]:
                print(f"    {entry['entity_id']}  ({entry['display_name']})")

        if report["alias_gaps"]:
            print("\n  ALIAS GAPS — upstream uses a name the catalog does not list")
            for entry in report["alias_gaps"]:
                print(f"    {entry['entity_id']:20} add alias {entry['alias']!r}")

        if report["balance_changes"]:
            print("\n  BALANCE CHANGES — the game publishes this field, so it follows")
            for entry in report["balance_changes"]:
                print(f"    {entry['entity_id']:20} {entry['field']:16} "
                      f"{entry['local']!r} -> {entry['merged']!r}")

        if report["disagreements"]:
            print("\n  DISAGREEMENTS — one side is wrong; the tool does not know which")
            for entry in report["disagreements"]:
                print(f"    {entry['entity_id']:20} {entry['field']:16} "
                      f"local {entry['local']!r} vs upstream {entry['upstream']!r}")

        if report["local_gaps"]:
            print("\n  LOCAL GAPS — an absent field is an empty puzzle column")
            for entry in report["local_gaps"]:
                print(f"    {entry['entity_id']:20} has no {entry['field']!r}")

        if report["confusable_values"]:
            print("\n  CONFUSABLE LABELS — read the same, compare as different")
            for entry in report["confusable_values"]:
                rendered = ", ".join(
                    f"{value['value_id']}={value['text']!r}"
                    for value in entry["values"]
                )
                print(f"    {entry['field']:16} {rendered}")

        if report["accepted_divergences"]:
            print("\n  accepted divergences (decided already, not findings)")
            for entry in report["accepted_divergences"]:
                print(f"    {entry['entity_id']:20} {entry['field']:16} "
                      f"keeping {entry['local']!r} over {entry['upstream']!r}")

        if not has_drift(report):
            print("  in step with upstream")

    total = sum(len(report[key]) for report in reports for key in FINDING_KEYS)
    print("\n" + "=" * 74)
    print(f"{'DRIFT' if total else 'OK'}: {total} finding(s)")
    print("=" * 74)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures", type=Path, default=None,
        help="Read recorded payloads from this directory instead of the network.",
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as JSON.")
    arguments = parser.parse_args()

    opener = load_fixtures(arguments.fixtures) if arguments.fixtures else None
    reports = []
    try:
        for game, dataset in MANAGED_DATASETS:
            reports.append(compare(game, dataset, opener))
    except SourceError as error:
        # A source being unreachable is not a data finding, so it gets its own
        # exit code: a scheduled check can log it and move on rather than
        # reporting that the roster changed.
        print(f"source unavailable: {error}", file=sys.stderr)
        return 2

    if arguments.json:
        json.dump(reports, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print_report(reports)
    return 1 if any(has_drift(report) for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
