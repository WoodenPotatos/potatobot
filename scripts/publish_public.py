"""Build a sanitised public snapshot, verify it, and only then publish it.

This repository is the private alpha line and its history can never be mirrored:
it contains a retired Discord token and the Twitch application credentials. The
public repository therefore receives *snapshots* — a clean tree, committed on a
`beta` branch with no ancestry from here.

Everything is a refusal, not a warning. A publish that "mostly" sanitised is
worse than no publish, because the leak ships and the report says fine. The whole
preflight runs before the first byte is written, so a rejected run leaves nothing
behind.

What ships is derived from `git ls-files` rather than from a copy of the working
tree, so an untracked database, a stray `.env` or a forgotten checkout cannot
travel even if somebody leaves one lying around.

Usage:
    python scripts/publish_public.py --dry-run
    python scripts/publish_public.py --promote --dry-run
    python scripts/publish_public.py --promote --remote git@github.com:you/potatobot.git
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import version as version_module  # noqa: E402

PUBLIC_BRANCH = "beta"

# Never published. Each is here for a reason, not for tidiness.
EXCLUDED_PATHS = {
    "CLAUDE.md",                            # instructions to an assistant
    "todo.md",                              # private backlog
    "config.json",                          # live guild, role and channel ids
    ".gitleaksignore",                      # names this repo's own exposures
    "SECURITY.md",                          # ditto, with rotation status
    "docs/performance_recovery_plan.md",    # private deployment detail
    "docs/config_retirement_plan.md",       # ditto
}
EXCLUDED_PREFIXES = (
    ".claude/",
    "dashboard-reference/",
)

# Nothing matching these may exist in the built tree, whatever its provenance.
FORBIDDEN_PATTERNS = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"\.db($|-wal$|-shm$)"),
    re.compile(r"\.db\.backup"),
    re.compile(r"(^|/)backups?/"),
    re.compile(r"\.log$"),
    re.compile(r"(^|/)\.dashboard_session_secret$"),
)

# A Discord id. Any of these in a shipped default or a shipped data file is one
# guild's identifier travelling with every installation.
SNOWFLAKE = re.compile(r"\b\d{17,20}\b")

# Snowflake-shaped values that are documented placeholders rather than anybody's
# guild. A recurring check needs somewhere to record a decision, or the accepted
# cases drown the real ones and the report stops being read — the same reason
# `everydle_drift.py` has ACCEPTED_DIVERGENCES. Adding one is a deliberate act:
# it must be obviously synthetic, and a real id can never be parked here.
SYNTHETIC_IDS = {
    # The Discord epoch with sequence 1. Used in comments and tests that need an
    # id above 2**53 to demonstrate JavaScript rounding.
    "1420070400000000001",
    # The long-standing placeholder in the hardening tests.
    "123456789012345678",
}

# Files allowed to contain a snowflake, with the reason. `data/` carries emoji
# references that are Discord ids by nature, and the changelog quotes history.
SNOWFLAKE_ALLOWED = (
    re.compile(r"^data/"),
    re.compile(r"^locales/"),
    re.compile(r"^botdata/"),
    re.compile(r"^CHANGELOG\.md$"),
    re.compile(r"^tests/fixtures/"),
    # This file exists to plant ids and require them to be caught; every value
    # in it is synthetic by construction.
    re.compile(r"^tests/test_public_release\.py$"),
)


class Refused(SystemExit):
    """A publish that must not happen."""


def run(command, cwd=None, check=True):
    result = subprocess.run(command, cwd=cwd or ROOT, capture_output=True,
                            text=True)
    if check and result.returncode != 0:
        raise Refused(f"  command failed: {' '.join(command)}\n{result.stderr.strip()}")
    return result


def tracked_files() -> list[str]:
    return [line for line in run(["git", "ls-files"]).stdout.splitlines() if line]


def is_excluded(path: str) -> bool:
    return path in EXCLUDED_PATHS or path.startswith(EXCLUDED_PREFIXES)


def _repository_name(remote: str) -> str:
    """The bare repository name from any remote form.

    Handles `git@host:owner/name.git`, `https://host/owner/name.git` and a plain
    filesystem path alike, because all three are things somebody will type.
    """
    tail = remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def is_private_remote(remote: str) -> bool:
    """Whether a target is this repository itself.

    Compared against the remotes actually configured here rather than against a
    substring, which matched any path that merely contained the name — a scratch
    directory was enough to trip it. The name check stays as a second line for a
    clone with no remote configured.
    """
    candidate = _repository_name(remote)
    configured = {
        _repository_name(line.split()[1])
        for line in run(["git", "remote", "-v"], check=False).stdout.splitlines()
        if len(line.split()) > 1
    }
    return candidate in configured or candidate == _repository_name(str(ROOT))


# --------------------------------------------------------------- preflight

def check_clean_tree(problems: list[str]) -> None:
    if run(["git", "status", "--porcelain"]).stdout.strip():
        problems.append("the working tree has uncommitted changes; "
                        "a snapshot must be of a committed state")


def check_tests(problems: list[str], skip: bool) -> None:
    if skip:
        return
    result = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                 check=False)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        problems.append(f"the test suite fails: {' '.join(tail)}")


def check_no_snowflake_defaults(problems: list[str]) -> None:
    """A registry default carrying a Discord id can only be wrong elsewhere."""
    from settings_registry import SETTING_DEFINITIONS

    def ids(value):
        if isinstance(value, bool):
            return
        if isinstance(value, int) and value > 1 << 52:
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from ids(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from ids(item)

    for key, definition in sorted(SETTING_DEFINITIONS.items()):
        found = sorted(set(ids(definition.default)))
        if found:
            problems.append(
                f"setting {key!r} ships a Discord id as its default: {found}")


def check_example_configs(problems: list[str]) -> None:
    for name in ("config.json.example", ".env.example"):
        path = ROOT / name
        if not path.exists():
            problems.append(f"{name} is missing; a public tree needs it")
            continue
        leaked = SNOWFLAKE.findall(path.read_text(encoding="utf-8"))
        if leaked:
            problems.append(f"{name} leaks Discord identifiers: {sorted(set(leaked))[:4]}")


def check_remote_is_reachable(problems: list[str], remote: str | None) -> None:
    """Fail on an unreachable target before anything is built.

    Without this the run builds the tree, verifies it, prints "verification
    clean" and only then dies on authentication — which reads as though the
    publish succeeded and something else went wrong afterwards.
    """
    if not remote:
        return
    result = subprocess.run(["git", "ls-remote", remote],
                            capture_output=True, text=True, timeout=60)
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout).strip().splitlines()
    first = detail[0] if detail else "unknown error"
    problems.append(f"cannot reach {remote}: {first}")
    if "publickey" in (result.stderr or ""):
        # The specific mistake worth naming: an SSH URL with no key registered.
        https = re.sub(r"^git@([^:]+):", r"https://\1/", remote)
        problems.append(
            f"    that is an SSH URL and no key is registered for it. "
            f"If you authenticate over HTTPS — the `gh` credential helper does — "
            f"use {https}")


def check_version_is_publishable(problems: list[str], raw: str) -> None:
    channel = version_module.channel_for(raw)
    if channel == version_module.CHANNEL_ALPHA:
        problems.append(
            f"version {raw} is an alpha; pass --promote to publish it as a beta")
    if raw == version_module.UNKNOWN_VERSION:
        problems.append("the version could not be read")


# ------------------------------------------------------------------- build

def build_tree(destination: Path, files: list[str]) -> list[str]:
    shipped = []
    for path in files:
        if is_excluded(path):
            continue
        source = ROOT / path
        if not source.exists():
            continue
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        shipped.append(path)
    return shipped


def promote_tree(destination: Path, published_raw: str) -> None:
    """Make the built tree say what it actually is.

    Promotion is a property of the artefact, not a label on the push: without
    this the published tree still carried the alpha version, so `/version` on a
    public installation reported `alpha`, the README said "Private development
    build. Not published.", and the changelog heading named a release that was
    never published. The private repository keeps its alpha; only the snapshot
    is rewritten.
    """
    display = version_module.display_for(published_raw)

    pyproject = destination / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    updated, count = VERSION_LINE.subn(
        lambda m: f'{m.group("prefix")}{published_raw}{m.group("suffix")}',
        text, count=1)
    if count != 1:
        raise Refused("  the built tree has no version line to promote")
    pyproject.write_text(updated, encoding="utf-8")

    # The top changelog section is the release being published, so it stops
    # being "Unreleased" and takes the published version's name.
    changelog = destination / "CHANGELOG.md"
    if changelog.exists():
        lines = changelog.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith("## "):
                lines[index] = f"## {display}\n"
                break
        changelog.write_text("".join(lines), encoding="utf-8")

    # Regenerated inside the tree, so its own script and its own pyproject are
    # what the README describes.
    run([sys.executable, str(destination / "scripts" / "update_readme.py")],
        cwd=destination)


VERSION_LINE = re.compile(r'^(?P<prefix>version\s*=\s*")(?P<value>[^"]+)(?P<suffix>")$',
                          re.MULTILINE)


def verify_tree(destination: Path, shipped: list[str]) -> list[str]:
    """Everything checked again against the built tree, not against intent."""
    problems = []

    for path in shipped:
        if is_excluded(path):
            problems.append(f"excluded path present in the built tree: {path}")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(path):
                problems.append(f"forbidden file in the built tree: {path}")

    # Walk what is actually on disk, in case the copy produced something the
    # file list did not describe.
    for entry in sorted(destination.rglob("*")):
        if entry.is_dir():
            continue
        relative = entry.relative_to(destination).as_posix()
        if is_excluded(relative):
            problems.append(f"excluded path on disk: {relative}")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(relative):
                problems.append(f"forbidden file on disk: {relative}")

    for relative in shipped:
        if any(rule.match(relative) for rule in SNOWFLAKE_ALLOWED):
            continue
        entry = destination / relative
        try:
            text = entry.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found = sorted(set(SNOWFLAKE.findall(text)) - SYNTHETIC_IDS)
        if found:
            problems.append(
                f"{relative} carries Discord identifiers: {found[:4]}"
                + (" …" if len(found) > 4 else ""))

    if shutil.which("gitleaks"):
        result = subprocess.run(
            ["gitleaks", "dir", str(destination), "--no-banner", "--redact"],
            capture_output=True, text=True)
        if result.returncode != 0:
            problems.append("gitleaks reported a finding in the built tree")
    else:
        problems.append("gitleaks is not installed; a publish must be scanned")

    return problems


# ----------------------------------------------------------------- publish

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--remote", help="public repository to push to")
    parser.add_argument("--promote", action="store_true",
                        help="turn the current alpha into the next beta first")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and verify, publish nothing")
    parser.add_argument("--skip-tests", action="store_true",
                        help="for iterating on this script only")
    parser.add_argument("--allow-missing-scanner", action="store_true",
                        help="proceed without gitleaks; never use for a real publish")
    arguments = parser.parse_args()

    raw = version_module.raw_version()
    published_raw = raw
    if arguments.promote:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bump_version", ROOT / "scripts" / "bump_version.py")
        bump = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bump)
        parts = bump.parse(raw)
        published_raw = bump.render({**parts, "kind": "b", "serial": 1}
                                    if parts["kind"] != "b"
                                    else {**parts, "serial": parts["serial"] + 1})

    print(f"  source version   : {raw} ({version_module.channel_for(raw)})")
    print(f"  publishing as    : {published_raw} "
          f"({version_module.channel_for(published_raw)})")

    problems: list[str] = []
    check_remote_is_reachable(problems, arguments.remote)
    check_clean_tree(problems)
    check_version_is_publishable(problems, published_raw)
    check_no_snowflake_defaults(problems)
    check_example_configs(problems)
    check_tests(problems, arguments.skip_tests)

    if problems:
        print("\n  REFUSED, nothing written:")
        for problem in problems:
            print(f"    - {problem}")
        return 1

    files = tracked_files()
    excluded = sorted(path for path in files if is_excluded(path))

    with tempfile.TemporaryDirectory(prefix="potatobot-public-") as scratch:
        destination = Path(scratch) / "tree"
        destination.mkdir()
        shipped = build_tree(destination, files)
        if published_raw != raw:
            promote_tree(destination, published_raw)
        problems = verify_tree(destination, shipped)
        if problems and arguments.allow_missing_scanner:
            problems = [p for p in problems if "gitleaks is not installed" not in p]

        print(f"\n  tracked {len(files)}  ->  shipped {len(shipped)}  "
              f"(excluded {len(excluded)})")
        for path in excluded:
            print(f"    excluded: {path}")

        if problems:
            print("\n  REFUSED, nothing published:")
            for problem in problems:
                print(f"    - {problem}")
            return 1

        print("\n  verification clean")
        if arguments.dry_run or not arguments.remote:
            print("  dry run; nothing published"
                  if arguments.dry_run else
                  "  no --remote given; nothing published")
            return 0

        if is_private_remote(arguments.remote):
            raise Refused("  refusing to publish into the private repository")

        run(["git", "init", "-q", "-b", PUBLIC_BRANCH], cwd=destination)
        run(["git", "add", "-A"], cwd=destination)
        run(["git", "commit", "-q", "-m",
             f"PotatoBot {version_module.display_for(published_raw)}"],
            cwd=destination)
        run(["git", "remote", "add", "origin", arguments.remote], cwd=destination)
        # No force. A public branch that needs rewriting needs a person.
        run(["git", "push", "origin", PUBLIC_BRANCH], cwd=destination)
        run(["git", "tag", f"v{published_raw}"], cwd=destination)
        run(["git", "push", "origin", f"v{published_raw}"], cwd=destination)
        print(f"  published {published_raw} to {arguments.remote} ({PUBLIC_BRANCH})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
