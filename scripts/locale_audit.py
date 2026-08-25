"""Report the localization state of every surface: bot, dashboard, and games.

The test suite already enforces the rules that can be stated absolutely — the
catalogs are structurally identical, no Hungarian prose sits outside a locale
file, and every *literal* `t("…")` key exists. What no test answers is the
question an operator actually asks: how much is translated, where are the
blind spots, and which text can still reach a user without passing through a
catalog at all. This script answers that.

It checks five things:

1. **Catalog completeness.** Key and empty-value counts per language, broken
   down by namespace, for `locales/` and for every `data/<game>/locales/`.
2. **Composed keys.** Keys built at runtime from a registry or an enum rather
   than written as literals — `dashboard.settings.<key>`,
   `admin.permissions_finding_<code>`, `gacha.rewards.<key>` and the rest. No
   grep finds these, and a missing one renders as a raw key to a user.
3. **Literal keys.** Every `t("…")` in Python and every `'dashboard.…'` in the
   front end resolves.
4. **Unreferenced keys.** Catalog entries nothing appears to use, which are
   either dead weight or a renamed key whose old spelling was left behind.
5. **Unlocalized text.** String literals reaching a user-visible parameter
   without going through `t()`. The Hungarian-prose test cannot see these
   because English text passes it.

Usage:
    python scripts/locale_audit.py            # full report
    python scripts/locale_audit.py --brief    # counts and problems only
    python scripts/locale_audit.py --json     # machine-readable

Exits non-zero when a key that something references is missing, which is the
only class of finding here that is unambiguously a defect.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRIMARY_LANGUAGE = "hu"
LOCALES_DIR = ROOT / "locales"
DATA_DIR = ROOT / "data"

PYTHON_SOURCES = [
    *ROOT.glob("*.py"),
    *(ROOT / "cogs").glob("*.py"),
    *(ROOT / "scripts").glob("*.py"),
]
FRONT_END_SOURCES = [
    *(ROOT / "dashboard").glob("*.js"),
    *(ROOT / "dashboard").glob("*.html"),
]

# Keyword arguments and call targets whose string argument is shown to a person.
USER_VISIBLE_KEYWORDS = {
    "title", "description", "label", "placeholder", "content", "text",
    "reason", "name", "value", "footer",
}
USER_VISIBLE_CALLS = {"send", "send_message", "reply", "followup", "edit_message"}
# `name=` is prose on an embed field and a label on almost everything else, so
# these callers are excluded rather than reported every run.
NON_PROSE_NAME_CALLS = {
    "create_task", "Thread", "get", "getattr", "loop", "start", "getLogger",
    "add_field_name",
}
# Literals that are structural rather than prose, and never worth translating.
IGNORED_LITERAL = re.compile(
    r"""^(
        \s*                      # blank or whitespace-only
        |​                  # zero-width space used as an empty embed field
        |https?://\S+            # a URL
        |[a-z0-9_.]+             # an identifier, key, or path
        |[A-Z_]+                 # a constant
        |\W{1,4}                 # punctuation or a lone emoji
        |\{[a-z_]+\}             # a bare format placeholder
    )$""",
    re.VERBOSE,
)


# ------------------------------------------------------------------ catalogs

def load_catalog(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def flatten(node, prefix="") -> dict:
    """Every leaf of a catalog as a dotted key mapped to its value."""
    flat = {}
    if isinstance(node, dict):
        for key, value in node.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else key))
    else:
        flat[prefix] = node
    return flat


def resolves(catalog: dict, dotted: str) -> bool:
    current = catalog
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def catalog_report() -> dict:
    """Per-language, per-namespace filled and empty counts."""
    languages = sorted(path.stem for path in LOCALES_DIR.glob("*.json"))
    primary = flatten(load_catalog(LOCALES_DIR / f"{PRIMARY_LANGUAGE}.json"))
    namespaces = sorted({key.split(".", 1)[0] for key in primary})

    report = {"languages": languages, "total_keys": len(primary), "by_language": {}}
    for language in languages:
        flat = flatten(load_catalog(LOCALES_DIR / f"{language}.json"))
        per_namespace = {}
        for namespace in namespaces:
            keys = [key for key in flat if key.split(".", 1)[0] == namespace]
            empty = [
                key for key in keys
                if isinstance(flat[key], str) and not flat[key].strip()
            ]
            per_namespace[namespace] = {
                "keys": len(keys), "empty": len(empty),
                "filled": len(keys) - len(empty),
            }
        empty_total = sum(item["empty"] for item in per_namespace.values())
        report["by_language"][language] = {
            "keys": len(flat),
            "empty": empty_total,
            "filled": len(flat) - empty_total,
            "percent": round(100 * (len(flat) - empty_total) / max(1, len(flat)), 1),
            "namespaces": per_namespace,
        }
    return report


def game_catalog_report() -> dict:
    """Per-game locale completeness, and whether the game would still load.

    `minigame_data.load_localized_dataset` raises on a missing or blank entity
    name, and `load_or_disable` turns that into a disabled game. An empty
    catalog is therefore not a cosmetic gap: selecting that language takes the
    minigame offline entirely.
    """
    report = {}
    for locale_dir in sorted(DATA_DIR.glob("*/locales")):
        game = locale_dir.parent.name
        report[game] = {}
        for path in sorted(locale_dir.glob("*.json")):
            flat = flatten(load_catalog(path))
            strings = {key: value for key, value in flat.items()
                       if isinstance(value, str)}
            empty = [key for key, value in strings.items() if not value.strip()]
            names = [key for key in strings if key.endswith(".name")]
            blank_names = [key for key in names if not strings[key].strip()]
            report[game][path.stem] = {
                "strings": len(strings),
                "empty": len(empty),
                "percent": round(
                    100 * (len(strings) - len(empty)) / max(1, len(strings)), 1
                ),
                "entity_names": len(names),
                "blank_entity_names": len(blank_names),
                "loads": not blank_names,
            }
    return report


# ------------------------------------------------------------- composed keys

def composed_key_families() -> dict:
    """Every key family built at runtime, with the keys it must contain.

    These are the keys no grep can find. Each entry maps a human-readable family
    name to the exact dotted keys the code will ask for, derived from the same
    registry, enum or catalog the code derives them from.
    """
    import dashboard_api
    import database
    import item_catalog
    import permission_audit
    from feature_access import COMMAND_POLICIES
    import settings_registry
    from settings_registry import (
        FEATURE_DEFINITIONS,
        FEATURE_GROUP_ORDER,
        SETTING_DEFINITIONS,
    )

    families: dict[str, list[str]] = {}

    families["feature labels"] = [
        definition.locale_key for definition in FEATURE_DEFINITIONS.values()
    ]
    families["feature groups"] = [
        f"dashboard.feature_groups.{group}" for group in FEATURE_GROUP_ORDER
    ]
    families["setting labels"] = [
        definition.locale_key for definition in SETTING_DEFINITIONS.values()
    ]
    families["setting sections"] = sorted({
        f"dashboard.pages.{definition.page}"
        for definition in SETTING_DEFINITIONS.values()
    })
    families["setting categories"] = sorted({
        f"dashboard.category_{definition.category}"
        for definition in SETTING_DEFINITIONS.values()
    })
    families["apply behaviours"] = [
        f"dashboard.apply_{behaviour}"
        for behaviour in ("live", "subsystem_reload", "restart")
    ]

    # Command help and usage. The cog-loading test enforces help coverage; this
    # reports it here too so one document describes the whole surface.
    families["command descriptions"] = [
        f"general.cmd_{name.split()[0]}" for name in sorted(COMMAND_POLICIES)
    ]
    families["command usage"] = [
        f"general.usage_{name.split()[0]}" for name in sorted(COMMAND_POLICIES)
    ]

    # Model-layer rejection reasons reach an operator through
    # `invalid_request_response`, which maps a reason code to a key of the same
    # name and logs an error if it is missing.
    reasons = sorted(set(re.findall(
        r'ValidationError\(\s*"([a-z0-9_]+)"',
        (ROOT / "database.py").read_text(encoding="utf-8"),
    )))
    families["validation reasons"] = [
        f"dashboard.errors.{reason}" for reason in reasons
    ]

    # `gacha_reward_label` is asked for a key only when it can actually be held:
    # a banner reward, an inventory row, or a vault. A catalog item that is
    # neither drawable nor stackable — a role, a bodyguard, a rent ticket — never
    # reaches it, so requiring a label for those would be a false alarm. Custom
    # shop vouchers compose `<asset>_<days>d` and are covered by
    # `gacha.custom_asset_voucher` plus `gacha.asset_types.*` instead.
    families["gacha reward labels"] = sorted(
        {
            f"gacha.rewards.{entry['key']}"
            for tier in database.DEFAULT_GACHA_CONFIG["rewards"].values()
            for entry in tier
        }
        | {f"gacha.rewards.{key}" for key in item_catalog.INVENTORY_ITEM_KEYS}
        | {f"gacha.rewards.{key}" for key in item_catalog.VAULT_ITEMS}
    )
    families["asset voucher types"] = [
        f"gacha.asset_types.{asset}" for asset in ("emoji", "sticker", "sound")
    ]
    families["shop item labels"] = sorted(
        f"shop.items.{key}.{field}"
        for key in item_catalog.SHOP_ITEMS
        for field in ("name", "desc")
    )
    # Pull and redemption outcomes, composed from the model's reason codes.
    families["gacha outcomes"] = [
        f"gacha.{reason}" for reason in
        ("banner_disabled", "banner_unknown", "not_enough_money")
    ] + [
        f"gacha.redeem_{reason}" for reason in
        ("not_found", "already_redeemed", "premium_success", "fulfillment_success")
    ]
    # Outbox failure codes, rendered by the actions column on the builders page.
    # An executor *returns* its code rather than assigning it, so both forms are
    # matched — otherwise a whole executor's failures would be invisible here.
    families["action error codes"] = sorted(
        f"dashboard.action_errors.{code}"
        for code in set(re.findall(
            r'(?:error_code = |return )"([a-z_]+)"',
            (ROOT / "dashboard_api.py").read_text(encoding="utf-8"),
        ))
    )
    # The content-builder pages, whose titles and subtitles are composed from the
    # page id and whose kind labels come from the managed-message kinds.
    families["content builder pages"] = sorted(
        key
        for page in ("embeds", "rules_panel", "role_menus", "ticket_launcher",
                     "entry_gate")
        for key in (f"dashboard.title_{page}", f"dashboard.subtitle_{page}")
    )
    # `/setup_games` and friends compose their embed text from the menu key, so
    # these three trios are asked for by name and found by no grep. They are the
    # fallback for a menu with no operator-set title, which is every menu the
    # schema 12 migration seeded.
    families["seeded role menu text"] = sorted(
        f"roleselect.{menu_key}_{part}"
        for _setting_key, menu_key in database.SEEDED_ROLE_MENUS
        for part in ("title", "desc", "updated")
    )
    # The label a preview falls back to when an operator has set none, one per
    # kind whose button the bot owns rather than the operator.
    families["default button labels"] = [
        f"dashboard.managed_default_button_{kind}"
        for kind in ("rules", "ticket", "airlock")
    ]
    families["appearance modes"] = [
        f"dashboard.themes.{mode}" for mode in ("system", "light", "dark")
    ]
    families["language names"] = [
        f"dashboard.languages.{path.stem}"
        for path in sorted(LOCALES_DIR.glob("*.json"))
    ]
    families["gacha reward kinds"] = [
        f"dashboard.reward_kind_{kind}"
        for kind in ("coins", "item", "vault", "voucher")
    ]
    families["shop templates"] = sorted(
        f"dashboard.template_{template}"
        for template in dashboard_api.SAFE_SHOP_TEMPLATES
    )

    permissions = sorted(
        {name for definition in FEATURE_DEFINITIONS.values()
         for name in definition.required_discord_permissions}
        | {name for required in permission_audit.CHANNEL_KIND_REQUIREMENTS.values()
           for name in required}
    )
    families["permission names (bot)"] = [
        f"admin.permission_names.{name}" for name in permissions
    ]
    families["permission names (dashboard)"] = [
        f"dashboard.permission_names.{name}" for name in permissions
    ]
    finding_codes = sorted(set(re.findall(
        r'code="([a-z_]+)"',
        (ROOT / "permission_audit.py").read_text(encoding="utf-8"),
    )))
    families["permission findings (bot)"] = [
        f"admin.permissions_finding_{code}" for code in finding_codes
    ]
    families["permission findings (dashboard)"] = [
        f"dashboard.permission_finding_{code}" for code in finding_codes
    ]
    families["permission severities"] = [
        f"{namespace}permissions_severity_{severity}"
        for namespace in ("admin.", "dashboard.")
        for severity in (permission_audit.SEVERITY_BLOCKING,
                         permission_audit.SEVERITY_DEGRADED)
    ]

    # `/work` responses used to be locale keys. They live in the database now —
    # one guild, one language — so there is nothing to check here beyond the tier
    # labels the dashboard shows.
    families["work tiers (dashboard)"] = [
        f"dashboard.work_tier_{tier}" for tier in database.WORK_TIERS
    ]

    # Labels for a constrained setting's values, derived from the prefix each
    # definition declares. Generic on purpose: the interface used to match on
    # the setting's key for `language`, and a second such special case would
    # have been a second list to keep in step.
    families["setting choice labels"] = sorted({
        f"{definition.choice_locale_prefix}.{choice}"
        for definition in SETTING_DEFINITIONS.values()
        if definition.choice_locale_prefix
        for choice in definition.choices
    })

    # Warn tags and their consequences are composed by f-string in
    # `cogs/moderation.py`, so nothing greps them: the tag label appears in the
    # public warn embed, in `/modlogs` and in the escalation alert, and a
    # missing one would render as a bracketed key on a moderation record.
    families["warn tags (bot)"] = [
        f"moderation.warn_tags.{tag}" for tag in settings_registry.WARN_TAGS
    ]
    families["warn escalation outcomes"] = [
        f"moderation.escalation_applied_{action}"
        for action in settings_registry.WARN_ACTIONS if action != "none"
    ] + [
        f"moderation.escalation_blocked_{reason}"
        for reason in ("protected", "hierarchy", "forbidden", "failed")
    ]

    # Dashboard page titles, from the navigation the shell actually renders.
    pages = sorted(set(re.findall(
        r'data-page="([a-z-]+)"',
        (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8"),
    )))
    families["page titles"] = [
        f"dashboard.title_{page.replace('-', '_')}" for page in pages
    ]

    return families


def composed_key_report(catalogs: dict) -> dict:
    report = {}
    for family, keys in composed_key_families().items():
        missing = sorted({key for key in keys
                          if not resolves(catalogs[PRIMARY_LANGUAGE], key)})
        untranslated = {}
        for language, catalog in catalogs.items():
            if language == PRIMARY_LANGUAGE:
                continue
            flat = flatten(catalog)
            untranslated[language] = sorted(
                key for key in keys
                if isinstance(flat.get(key), str) and not flat[key].strip()
            )
        report[family] = {
            "keys": len(set(keys)),
            "missing": missing,
            "untranslated": untranslated,
        }
    return report


# -------------------------------------------------------------- literal keys

def literal_key_report(catalog: dict) -> dict:
    """Keys written out in full in the source, and whether each resolves."""
    referenced = {}

    for path in PYTHON_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                referenced.setdefault(node.args[0].value, []).append(
                    f"{path.relative_to(ROOT)}:{node.lineno}"
                )

    for path in FRONT_END_SOURCES:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"""["']dashboard\.[A-Za-z0-9_.]+["']""", text):
            key = match.group(0).strip("\"'")
            line = text.count("\n", 0, match.start()) + 1
            referenced.setdefault(key, []).append(
                f"{path.relative_to(ROOT)}:{line}"
            )
        for match in re.finditer(r'data-i18n[a-z-]*="([A-Za-z0-9_.]+)"', text):
            line = text.count("\n", 0, match.start()) + 1
            referenced.setdefault(match.group(1), []).append(
                f"{path.relative_to(ROOT)}:{line}"
            )

    missing = {
        key: sorted(set(where)) for key, where in sorted(referenced.items())
        if not resolves(catalog, key)
    }
    return {"referenced": len(referenced), "missing": missing,
            "_referenced_keys": set(referenced)}


# ------------------------------------------------------- unreferenced keys

# Prefixes a runtime-composed key can live under. A catalog entry below one of
# these is assumed reachable even when no literal names it.
COMPOSED_PREFIXES = (
    "dashboard.features.", "dashboard.settings.", "dashboard.pages.",
    "dashboard.errors.", "dashboard.feature_groups.",
    "dashboard.permission_names.", "dashboard.category_",
    "dashboard.title_", "dashboard.apply_", "dashboard.template_",
    "dashboard.reward_kind_", "dashboard.permission_finding_",
    "dashboard.permissions_severity_", "dashboard.work_tier_",
    "dashboard.asset_", "dashboard.status_", "dashboard.relative_",
    "dashboard.theme_", "dashboard.gacha_",
    "admin.permission_names.", "admin.permissions_finding_",
    "admin.permissions_severity_",
    "general.cmd_", "general.usage_",
    "gacha.rewards.", "gacha.voucher_status.", "gacha.asset_types.",
    "casino.job_",
    "shop.items.", "dashboard.action_errors.", "dashboard.themes.",
    "dashboard.languages.",
    "dashboard.hints.",
    "dashboard.warn_actions.", "dashboard.warn_tags.",
    "moderation.warn_tags.", "moderation.escalation_applied_",
    "moderation.escalation_blocked_",
    # Minigame attribute labels are addressed by dataset field and value id.
    "loldle.", "valdle.", "dbdle.", "everydle.",
)


def unreferenced_report(catalog: dict, referenced: set, composed: dict) -> list:
    """Catalog keys nothing appears to reach.

    This is a review list, not a defect list: a key can be composed in a way
    this script does not model. A renamed key whose old spelling stayed behind
    looks exactly like one of these, which is why it is worth reading.
    """
    reachable = set(referenced)
    for keys in composed.values():
        reachable.update(keys)
    return sorted(
        key for key in flatten(catalog)
        if key not in reachable and not key.startswith(COMPOSED_PREFIXES)
    )


# ------------------------------------------------------------ unlocalized text

def unlocalized_text_report() -> list:
    """String literals that can reach a user without passing through `t()`.

    The suite's Hungarian-prose test cannot see these: an English literal in an
    embed title passes it and then ships untranslatable text.
    """
    findings = []
    for path in PYTHON_SOURCES:
        if path.parent.name == "scripts":
            continue  # Operator tooling is deliberately English-only.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            candidates = []
            for keyword in node.keywords:
                if keyword.arg not in USER_VISIBLE_KEYWORDS:
                    continue
                if keyword.arg == "name" and call_name in NON_PROSE_NAME_CALLS:
                    continue
                candidates.append((keyword.arg, keyword.value))
            if call_name in USER_VISIBLE_CALLS and node.args:
                candidates.append(("positional", node.args[0]))
            for label, value in candidates:
                if not (isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    continue
                if IGNORED_LITERAL.match(value.value):
                    continue
                findings.append({
                    "where": f"{path.relative_to(ROOT)}:{value.lineno}",
                    "parameter": label,
                    "call": call_name,
                    "text": value.value[:70],
                })
    return findings


# ------------------------------------------------------------------- rendering

def build_report() -> dict:
    catalogs = {
        path.stem: load_catalog(path)
        for path in sorted(LOCALES_DIR.glob("*.json"))
    }
    literals = literal_key_report(catalogs[PRIMARY_LANGUAGE])
    families = composed_key_families()
    return {
        "catalogs": catalog_report(),
        "games": game_catalog_report(),
        "composed": composed_key_report(catalogs),
        "literals": {k: v for k, v in literals.items()
                     if not k.startswith("_")},
        "unreferenced": unreferenced_report(
            catalogs[PRIMARY_LANGUAGE], literals["_referenced_keys"], families
        ),
        "unlocalized": unlocalized_text_report(),
    }


def print_report(report: dict, brief: bool) -> None:
    catalogs = report["catalogs"]
    print("=" * 74)
    print("LOCALIZATION AUDIT")
    print("=" * 74)

    print(f"\n1. GENERAL CATALOGS  ({catalogs['total_keys']} keys, "
          f"{len(catalogs['languages'])} languages)")
    for language, data in catalogs["by_language"].items():
        marker = " (primary)" if language == PRIMARY_LANGUAGE else ""
        print(f"   {language}{marker}: {data['filled']}/{data['keys']} filled "
              f"({data['percent']}%), {data['empty']} empty")
    if not brief:
        secondary = [item for item in catalogs["languages"]
                     if item != PRIMARY_LANGUAGE]
        for language in secondary:
            namespaces = catalogs["by_language"][language]["namespaces"]
            gaps = sorted(
                ((name, data) for name, data in namespaces.items() if data["empty"]),
                key=lambda entry: -entry[1]["empty"],
            )
            if not gaps:
                continue
            print(f"\n   {language} gaps by namespace:")
            for name, data in gaps:
                print(f"     {name:24} {data['empty']:5} empty of {data['keys']:5}")

    print("\n2. GAME CATALOGS")
    for game, languages in report["games"].items():
        for language, data in languages.items():
            state = "loads" if data["loads"] else "WOULD DISABLE THE GAME"
            print(f"   {game}/{language}: {data['strings'] - data['empty']}"
                  f"/{data['strings']} filled ({data['percent']}%), "
                  f"{data['blank_entity_names']} blank entity names -> {state}")

    print("\n3. COMPOSED KEY FAMILIES  (built at runtime; no grep finds these)")
    for family, data in report["composed"].items():
        missing = len(data["missing"])
        gaps = ", ".join(
            f"{language} {len(keys)} untranslated"
            for language, keys in data["untranslated"].items() if keys
        )
        status = "OK" if not missing else f"{missing} MISSING"
        print(f"   {family:34} {data['keys']:4} keys  {status:12} {gaps}")
        if missing and not brief:
            for key in data["missing"]:
                print(f"       missing: {key}")

    print("\n4. LITERAL KEYS")
    print(f"   {report['literals']['referenced']} distinct keys referenced in "
          f"source; {len(report['literals']['missing'])} missing")
    for key, where in report["literals"]["missing"].items():
        print(f"     MISSING {key}  <- {', '.join(where)}")

    print(f"\n5. UNREFERENCED CATALOG KEYS  ({len(report['unreferenced'])})")
    if brief:
        print("   (use the full report to list them)")
    else:
        for key in report["unreferenced"]:
            print(f"     {key}")

    print(f"\n6. UNLOCALIZED USER-VISIBLE LITERALS  ({len(report['unlocalized'])})")
    if brief:
        print("   (use the full report to list them)")
    else:
        for finding in report["unlocalized"]:
            print(f"     {finding['where']:44} {finding['parameter']:11} "
                  f"{finding['text']!r}")

    blocking = (
        len(report["literals"]["missing"])
        + sum(len(data["missing"]) for data in report["composed"].values())
    )
    print("\n" + "=" * 74)
    print(f"{'FAILED' if blocking else 'OK'}: {blocking} missing key(s)")
    print("=" * 74)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit localization coverage.")
    parser.add_argument("--brief", action="store_true",
                        help="Counts and problems only.")
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as JSON.")
    arguments = parser.parse_args()

    report = build_report()
    if arguments.json:
        # Sets are not JSON-serializable and are internal to the scan anyway.
        json.dump(report, sys.stdout, indent=2, sort_keys=True, default=sorted)
        sys.stdout.write("\n")
    else:
        print_report(report, arguments.brief)

    return 1 if (
        report["literals"]["missing"]
        or any(data["missing"] for data in report["composed"].values())
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
