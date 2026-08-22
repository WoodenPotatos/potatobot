"""Bump the version in `pyproject.toml`, which is the only place it lives.

The scheme is `x.y.z` with an optional PEP 440 prerelease suffix, and the parts
are tied to something objective rather than to how big a change felt:

    x  a breaking change to configuration, the database schema, or a command's
       name or arguments — anything an existing installation must react to.
    y  a feature, or a schema migration. A migration is never a z: it changes
       what a rollback means, and that is not a bugfix.
    z  a fix that requires nothing of the operator.

The private repository is the alpha line, so a normal bump here produces an
alpha. The publish script converts the current alpha into the next beta for the
public tree — the channel is derived from the artefact, so promoting is a real
change to the version rather than a label somebody sets.

Every check runs before the file is touched, so a rejected bump leaves nothing
half-written.

Usage:
    python scripts/bump_version.py major|minor|patch [--channel alpha|beta|stable]
    python scripts/bump_version.py prerelease        # 2.1.0a3 -> 2.1.0a4
    python scripts/bump_version.py --set 2.1.0b1
    python scripts/bump_version.py --show
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import version as version_module  # noqa: E402

PYPROJECT = ROOT / "pyproject.toml"

# Matched against the file rather than a parsed document: tomllib cannot write,
# and a round trip through any writer would reformat a file a person maintains.
VERSION_LINE = re.compile(r'^(?P<prefix>version\s*=\s*")(?P<value>[^"]+)(?P<suffix>")$',
                          re.MULTILINE)

PARTS = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
                   r"(?:(?P<kind>a|b|rc)(?P<serial>\d+))?$")

CHANNEL_KIND = {"alpha": "a", "beta": "b", "stable": None}


def read_current() -> str:
    match = VERSION_LINE.search(PYPROJECT.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"no version line found in {PYPROJECT}")
    return match.group("value")


def parse(raw: str) -> dict:
    match = PARTS.match(raw)
    if match is None:
        raise SystemExit(
            f"cannot parse {raw!r}: expected x.y.z with an optional a/b/rc suffix"
        )
    return {
        "major": int(match.group("major")),
        "minor": int(match.group("minor")),
        "patch": int(match.group("patch")),
        "kind": match.group("kind"),
        "serial": int(match.group("serial")) if match.group("serial") else None,
    }


def render(parts: dict) -> str:
    base = f"{parts['major']}.{parts['minor']}.{parts['patch']}"
    if parts["kind"] is None:
        return base
    return f"{base}{parts['kind']}{parts['serial']}"


def sort_key(parts: dict) -> tuple:
    """Order two versions the way PEP 440 does.

    A prerelease sorts *before* its own release, which is what makes the
    backwards check meaningful: 2.1.0a4 < 2.1.0b1 < 2.1.0rc1 < 2.1.0.
    """
    rank = {"a": 0, "b": 1, "rc": 2, None: 3}[parts["kind"]]
    return (parts["major"], parts["minor"], parts["patch"], rank,
            parts["serial"] if parts["serial"] is not None else 0)


def bump(current: dict, step: str, channel: str | None) -> dict:
    parts = dict(current)
    kind = CHANNEL_KIND[channel] if channel else current["kind"]

    if step == "prerelease":
        if current["kind"] is None:
            raise SystemExit(
                f"{render(current)} is not a prerelease; bump major/minor/patch "
                "or pass --channel to open one"
            )
        parts["kind"] = kind or current["kind"]
        parts["serial"] = (current["serial"] or 0) + 1
        return parts

    if step == "major":
        parts.update(major=current["major"] + 1, minor=0, patch=0)
    elif step == "minor":
        parts.update(minor=current["minor"] + 1, patch=0)
    elif step == "patch":
        parts.update(patch=current["patch"] + 1)

    parts["kind"] = kind
    parts["serial"] = 1 if kind else None
    return parts


def write(new_raw: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    updated, count = VERSION_LINE.subn(
        lambda m: f"{m.group('prefix')}{new_raw}{m.group('suffix')}", text, count=1
    )
    if count != 1:
        raise SystemExit("refusing to write: the version line moved under us")
    PYPROJECT.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", nargs="?",
                        choices=("major", "minor", "patch", "prerelease"))
    parser.add_argument("--channel", choices=tuple(CHANNEL_KIND))
    parser.add_argument("--set", dest="explicit",
                        help="set an exact version instead of computing one")
    parser.add_argument("--show", action="store_true",
                        help="print the current version and channel, change nothing")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    current_raw = read_current()
    current = parse(current_raw)

    if arguments.show:
        print(f"{current_raw}  ({version_module.display_for(current_raw)}, "
              f"{version_module.channel_for(current_raw)})")
        return 0

    if arguments.explicit:
        new = parse(arguments.explicit)
    elif arguments.step:
        new = bump(current, arguments.step, arguments.channel)
    else:
        parser.error("give a step, --set, or --show")

    new_raw = render(new)

    # Preflight, entirely before the write.
    if sort_key(new) <= sort_key(current):
        raise SystemExit(
            f"refusing to go backwards: {current_raw} -> {new_raw}"
        )

    channel = version_module.channel_for(new_raw)
    print(f"  {current_raw} -> {new_raw}")
    print(f"  display: {version_module.display_for(new_raw)}   channel: {channel}")
    if arguments.dry_run:
        print("  dry run; nothing written")
        return 0

    write(new_raw)
    print(f"  written to {PYPROJECT.relative_to(ROOT)}")
    print("  next: add the CHANGELOG section, then commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
