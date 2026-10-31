"""Detect when the code has moved away from what the Privacy Policy says.

`docs/legal/PRIVACY.md` makes claims about the software: which tables hold a
member, what an erasure deletes, which hosts the bot talks to, which log lines
carry a user id, which loops act on their own, how long the dashboard session
lives. Each of those is a fact the repository can state mechanically, so this
script states them, and compares the statement to the one recorded when the
policy was last reviewed (`docs/legal/drift_stamp.json`).

    venv/bin/python scripts/legal_drift.py check   # exit 1 on drift (the CI gate)
    venv/bin/python scripts/legal_drift.py stamp   # record the facts after a review
    venv/bin/python scripts/legal_drift.py show    # print the facts

A drift report names the fact that moved and the policy section that covers
it. Re-stamping is a separate, deliberate command: the point is that a change
cannot pass CI without somebody having opened the policy. The procedure that
goes with a drift finding is the `/legal-update` skill; `/release` runs the
check before publishing. `tests/test_legal_drift.py` runs it in the suite.

What is deliberately *not* a fact here: prose. The script cannot tell whether
a sentence is still true; it can only tell that the thing the sentence is
about has changed shape. A model or a person then reads the diff and edits.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAMP_PATH = ROOT / "docs" / "legal" / "drift_stamp.json"

# The runtime files whose string literals, log calls and loops are facts. Tests,
# docs and maintainer tools are not: a hostname in a test fixture reaches nobody.
SCAN_FILES = (
    sorted(ROOT.glob("cogs/*.py"))
    + sorted(ROOT.glob("core/*.py"))
    + [ROOT / "main.py", ROOT / "dashboard_api.py", ROOT / "update_db.py",
       ROOT / "scripts" / "offsite_backup.py",
       ROOT / "scripts" / "everydle_sources.py",
       ROOT / "scripts" / "everydle_drift.py"]
)

# Where each fact is described, so a drift report tells the reader where to go.
# Every fact `compute_facts` returns must have an entry; a test holds the two
# together.
SECTION_FOR = {
    "subject_tables": "PRIVACY.md §4 (what is stored) and §8.1 (what the export returns)",
    "erasure_lists": "PRIVACY.md §8.3 (what erasure deletes, keeps and dereferences)",
    "schema": "PRIVACY.md §4 and docs/data_inventory.md §1 (every table and column)",
    "outbound_hosts": "PRIVACY.md §6.2 (recipients and processors) and data_inventory §5",
    "log_formats_naming_a_user": "PRIVACY.md §9 (what the logs contain)",
    "loops": "PRIVACY.md §10 (automated decisions) and §7 (automatic pruning)",
    "features": "PRIVACY.md §4 (one row per feature)",
    "privacy_settings": "PRIVACY.md §7 (retention) and §10 (automatic actions)",
    "intents": "PRIVACY.md §5 (message content)",
    "dashboard_session": "PRIVACY.md §11 (the dashboard's cookie, scopes and lifetimes)",
    "retention_constants": "PRIVACY.md §7 (how long we keep data) and §8.1 (export limits)",
}

# Named integer constants the policy quotes. Read from the source by AST, so the
# script needs no import of a cog (which would need discord.py) to read them.
RETENTION_CONSTANTS = (
    ("core/database.py", "EXPORT_ROW_LIMIT"),
    ("core/control_actions.py", "CONTROL_ACTION_RETENTION_DAYS"),
    ("core/database.py", "ACCESS_INVALIDATION_RETENTION_HOURS"),
    ("cogs/general.py", "LFG_PRUNE_DAYS"),
    ("cogs/privacy.py", "RETENTION_BATCH"),
    ("cogs/privacy.py", "REQUEST_COOLDOWN_SECONDS"),
    ("cogs/privacy.py", "CONFIRM_TIMEOUT_SECONDS"),
    ("cogs/tickets.py", "TRANSCRIPT_MESSAGE_LIMIT"),
    ("cogs/tickets.py", "TRANSCRIPT_PART_LIMIT"),
    ("dashboard_api.py", "MAX_TRACKED_SESSIONS"),
)

# Settings the policy describes individually: retention, the automatic
# moderation actions, and every channel that receives member-identifying output.
PRIVACY_SETTING_PATTERN = re.compile(
    r"retention|threshold|filter|ignored|log_channel|ticket_logs|invite|"
    r"announce|inactiv|timeout|alert|transcript|message_card", re.IGNORECASE)

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# -- facts --------------------------------------------------------------------

def _subject_tables():
    from core import database
    return [list(entry) for entry in database.SUBJECT_TABLES]


def _erasure_lists():
    from core import database
    return {
        "delete_order": [list(entry) for entry in database.ERASE_DELETE_ORDER],
        "rekey_subject": list(database.ERASE_REKEY_SUBJECT),
        "null_actor": [list(entry) for entry in database.ERASE_NULL_ACTOR],
        "rekey_actor": [list(entry) for entry in database.ERASE_REKEY_ACTOR],
        "scrubbed_json": [list(entry) for entry in database.ERASE_SCRUBBED_JSON],
    }


def _schema():
    """Every table and its columns, from a database built fresh in a temp dir.

    `database.DB_PATH` is a module global read at call time for exactly this, so
    the live file is never touched.
    """
    import sqlite3
    from core import database
    original = database.DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = os.path.join(tmp, "drift.db")
        try:
            database.initialize_database()
            with sqlite3.connect(database.DB_PATH) as conn:
                tables = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                return {
                    table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                    for table in tables
                }
        finally:
            database.DB_PATH = original


def _ast(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _string_literals(path: Path):
    """Every string constant in the code, minus comments and docstrings.

    A URL in a comment or a docstring reaches nobody (`cogs/utils.py` explains a
    markdown-injection bug with `https://evil`), so those are not facts; a
    literal in code is, whether or not the script can prove it is requested.
    """
    tree = _ast(path)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node.value


# Hostnames that appear as literals but are identifiers, not requests, each with
# its reason. A host missing from here and from the stamp is a new recipient.
NOT_OUTBOUND = {
    "www.w3.org": "the Atom XML namespace in the YouTube feed parser",
}


def _outbound_hosts():
    hosts = set()
    for path in SCAN_FILES:
        for literal in _string_literals(path):
            hosts.update(host.lower() for host in URL_HOST.findall(literal))
    return sorted(hosts - set(NOT_OUTBOUND))


def _log_formats_naming_a_user():
    """The format string of every log call whose text mentions a user or member.

    The policy says which log lines carry a user id; a new such line is a change
    to that paragraph, and renaming or removing one is too.
    """
    found = []
    for path in SCAN_FILES:
        for node in ast.walk(_ast(path)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in LOG_METHODS):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                text = first.value
            elif isinstance(first, ast.JoinedStr):
                text = ast.unparse(first)
            else:
                continue
            if re.search(r"user|member", text, re.IGNORECASE):
                found.append(f"{_rel(path)}: {text}")
    return sorted(found)


def _loops():
    found = []
    for path in SCAN_FILES:
        for node in ast.walk(_ast(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                target = call.func if call else decorator
                if isinstance(target, ast.Attribute) and target.attr == "loop":
                    found.append(f"{_rel(path)}:{node.name}")
    return sorted(found)


def _features():
    from core.settings_registry import FEATURE_DEFINITIONS
    return sorted(FEATURE_DEFINITIONS)


def _privacy_settings():
    from core.settings_registry import SETTING_DEFINITIONS
    facts = {}
    for key, definition in sorted(SETTING_DEFINITIONS.items()):
        if not PRIVACY_SETTING_PATTERN.search(key):
            continue
        facts[key] = {
            "scope": getattr(definition.scope, "value", str(definition.scope)),
            "type": getattr(definition.value_type, "value", str(definition.value_type)),
            "default": json.loads(json.dumps(definition.default, default=str)),
            "feature": definition.owner_feature,
        }
    return facts


def _intents():
    return dict(sorted(re.findall(r"intents\.(\w+) = (True|False)", _source(ROOT / "main.py"))))


def _dashboard_session():
    text = _source(ROOT / "dashboard_api.py")
    facts = {}
    for name, value in re.findall(
            r"^(SESSION_LIFETIME|SESSION_IDLE_TIMEOUT) = (.+)$", text, re.MULTILINE):
        facts[name] = value.strip()
    # A flag may be a parenthesised multi-line expression (SECURE derives from
    # the external URL), so the value runs to the end of that expression.
    for name, value in re.findall(
            r"SESSION_COOKIE_(\w+)=((?:\([^)]*\)[^,\n]*)|[^,\n]+)", text):
        facts[f"SESSION_COOKIE_{name}"] = " ".join(value.split())
    scopes = re.findall(r"\"scope\":\s*\"([^\"]+)\"", text)
    facts["oauth_scopes"] = sorted(set(scopes))
    return facts


def _assigned_literal(path: Path, name: str):
    for node in ast.walk(_ast(path)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.unparse(node.value)
    return None


def _retention_constants():
    facts = {}
    for relpath, name in RETENTION_CONSTANTS:
        facts[f"{relpath}:{name}"] = _assigned_literal(ROOT / relpath, name)
    logging_text = _source(ROOT / "core" / "logging_setup.py")
    facts["core/logging_setup.py:maxBytes"] = (
        re.search(r"maxBytes=([^,\n]+)", logging_text) or [None, None])[1]
    facts["core/logging_setup.py:backupCount"] = (
        re.search(r"backupCount=([^,\n]+)", logging_text) or [None, None])[1]
    backup_text = _source(ROOT / "scripts" / "offsite_backup.py")
    for flag in ("--retention-days", "--prune-local-backups-days"):
        match = re.search(rf"\"{flag}\".*?default=(\d+)", backup_text, re.DOTALL)
        facts[f"scripts/offsite_backup.py:{flag}"] = match.group(1) if match else None
    # The deployed units are where the retention the policy promises is actually
    # set, so their ExecStart lines are facts too.
    for unit in sorted(ROOT.glob("deploy/potatobot-offsite-backup*.service")):
        match = re.search(r"^ExecStart=(.+)$", _source(unit), re.MULTILINE)
        facts[f"{_rel(unit)}:ExecStart"] = match.group(1) if match else None
    return {key: (value.strip() if isinstance(value, str) else value)
            for key, value in facts.items()}


def compute_facts() -> dict:
    return {
        "subject_tables": _subject_tables(),
        "erasure_lists": _erasure_lists(),
        "schema": _schema(),
        "outbound_hosts": _outbound_hosts(),
        "log_formats_naming_a_user": _log_formats_naming_a_user(),
        "loops": _loops(),
        "features": _features(),
        "privacy_settings": _privacy_settings(),
        "intents": _intents(),
        "dashboard_session": _dashboard_session(),
        "retention_constants": _retention_constants(),
    }


# -- comparison ---------------------------------------------------------------

def _normalise(value):
    return json.loads(json.dumps(value, sort_keys=True))


def _describe(old, new) -> list[str]:
    """Human lines for one fact's change: what was added, removed or altered."""
    lines = []
    if isinstance(old, list) and isinstance(new, list):
        before = {json.dumps(item, sort_keys=True) for item in old}
        after = {json.dumps(item, sort_keys=True) for item in new}
        lines += [f"    + {item}" for item in sorted(after - before)]
        lines += [f"    - {item}" for item in sorted(before - after)]
    elif isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            if key not in old:
                lines.append(f"    + {key}: {json.dumps(new[key], sort_keys=True)}")
            elif key not in new:
                lines.append(f"    - {key}")
            elif _normalise(old[key]) != _normalise(new[key]):
                lines.append(f"    ~ {key}: {json.dumps(old[key], sort_keys=True)}"
                             f" -> {json.dumps(new[key], sort_keys=True)}")
    else:
        lines.append(f"    {json.dumps(old)} -> {json.dumps(new)}")
    return lines


def drift(stamp: dict, facts: dict) -> list[tuple[str, list[str]]]:
    """Every fact that differs from the stamp, with its description lines."""
    report = []
    for key in sorted(set(stamp) | set(facts)):
        old, new = stamp.get(key), facts.get(key)
        if _normalise(old) != _normalise(new):
            report.append((key, _describe(old, new)))
    return report


def load_stamp(path: Path = STAMP_PATH) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_stamp(facts: dict, path: Path = STAMP_PATH) -> None:
    path.write_text(json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def format_report(report) -> str:
    out = ["Legal drift: the code no longer matches docs/legal/drift_stamp.json.", ""]
    for key, lines in report:
        out.append(f"  {key}  ->  re-read {SECTION_FOR.get(key, '(no section mapped)')}")
        out.extend(lines[:40])
        if len(lines) > 40:
            out.append(f"    ... {len(lines) - 40} more")
        out.append("")
    out.append("Edit docs/legal/PRIVACY.md and PRIVACY.hu.md (the /legal-update skill "
               "has the steps), then `scripts/legal_drift.py stamp`.")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="check",
                        choices=("check", "stamp", "show"))
    arguments = parser.parse_args(argv)
    facts = compute_facts()
    if arguments.command == "show":
        print(json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    if arguments.command == "stamp":
        write_stamp(facts)
        print(f"  stamped {len(facts)} facts into {STAMP_PATH.relative_to(ROOT)}")
        print("  the policy sections those facts describe are now asserted current")
        return 0
    stamp = load_stamp()
    if stamp is None:
        print(f"No stamp at {STAMP_PATH.relative_to(ROOT)}; run `legal_drift.py stamp` "
              "after reviewing the policy.")
        return 1
    report = drift(stamp, facts)
    if report:
        print(format_report(report))
        return 1
    print("  no legal drift: the code still has the shape the policy describes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
