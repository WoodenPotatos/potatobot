"""Refresh the generated sections of `README.md`, and nothing else.

The README is hand-written. It is the first thing a person sees, and it should
read like somebody wrote it, because somebody did. What this script maintains is
the part that goes stale on its own: the current version, a short installation
summary, and the latest changelog entries.

It replaces only what sits between markers:

    <!-- BEGIN GENERATED: changelog -->   ...   <!-- END GENERATED: changelog -->

Everything outside them is untouched. A missing marker is an error rather than an
excuse to append — appending to somebody's README is how a tool earns being
removed from the pipeline.

Usage:
    python scripts/update_readme.py            # rewrite in place
    python scripts/update_readme.py --check    # exit 1 if it would change
    python scripts/update_readme.py --dry-run  # show what would change
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import version as version_module  # noqa: E402

README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"

# How many releases the README carries. Enough to see the direction of travel,
# few enough that the file stays readable; the rest is one link away.
CHANGELOG_RELEASES = 3
CHANGELOG_ENTRIES = 8


def _marker(name: str) -> re.Pattern:
    return re.compile(
        rf"(?P<open><!-- BEGIN GENERATED: {re.escape(name)} -->\n)"
        rf"(?P<body>.*?)"
        rf"(?P<close>\n?<!-- END GENERATED: {re.escape(name)} -->)",
        re.DOTALL,
    )


def render_version() -> str:
    raw = version_module.raw_version()
    channel = version_module.channel_for(raw)
    note = {
        "alpha": "Private development build. Not published.",
        "beta": "Early access. Expect breaking changes between releases.",
        "stable": "Stable release.",
    }[channel]
    return (
        f"**Version {version_module.display_for(raw)}** &nbsp;·&nbsp; "
        f"channel `{channel}`\n\n{note}"
    )


def render_install() -> str:
    return "\n".join([
        "```bash",
        "git clone https://github.com/WoodenPotatos/potatobot.git /opt/potatobot",
        "cd /opt/potatobot",
        "python3 -m venv venv",
        "./venv/bin/python -m pip install --requirement requirements.lock",
        "cp .env.example .env        # add your bot token",
        "cp config.json.example config.json",
        "POTATOBOT_DB_PATH=$PWD/economy.db ./venv/bin/python update_db.py",
        "./venv/bin/python main.py",
        "```",
        "",
        "Requires Python 3.12–3.14. The full walkthrough — Discord application,"
        " intents, OAuth, HTTPS, systemd and the guild setup check — is in"
        " [docs/installation.md](docs/installation.md).",
    ])


def render_changelog() -> str:
    """The most recent releases, read from CHANGELOG.md.

    Parsed rather than duplicated, so the README cannot describe a release the
    changelog does not have.
    """
    text = CHANGELOG.read_text(encoding="utf-8")
    lines, releases = text.splitlines(), []
    for line in lines:
        if line.startswith("## "):
            releases.append({"heading": line[3:].strip(), "entries": []})
        elif line.startswith("- ") and releases:
            releases[-1]["entries"].append(line[2:].strip())
        elif line.startswith("  ") and releases and releases[-1]["entries"]:
            # A bullet wrapped across source lines is one entry, not two.
            releases[-1]["entries"][-1] += " " + line.strip()

    if not releases:
        raise SystemExit("CHANGELOG.md has no '## ' release headings")

    out = []
    for release in releases[:CHANGELOG_RELEASES]:
        out.append(f"### {release['heading']}\n")
        shown = release["entries"][:CHANGELOG_ENTRIES]
        out.extend(f"- {entry}" for entry in shown)
        hidden = len(release["entries"]) - len(shown)
        if hidden > 0:
            out.append(f"- …and {hidden} more, in "
                       f"[CHANGELOG.md](CHANGELOG.md).")
        out.append("")
    out.append("The full history is in [CHANGELOG.md](CHANGELOG.md).")
    return "\n".join(out)


SECTIONS = {
    "version": render_version,
    "install": render_install,
    "changelog": render_changelog,
}


def apply(text: str) -> str:
    for name, render in SECTIONS.items():
        pattern = _marker(name)
        if not pattern.search(text):
            raise SystemExit(
                f"README.md has no '{name}' generated block. Add:\n"
                f"    <!-- BEGIN GENERATED: {name} -->\n"
                f"    <!-- END GENERATED: {name} -->\n"
                "This script never appends to a README it was not invited into."
            )
        body = render()
        text = pattern.sub(
            lambda m, body=body: f"{m.group('open')}{body}\n{m.group('close').lstrip(chr(10))}",
            text,
            count=1,
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the README is out of date")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    original = README.read_text(encoding="utf-8")
    updated = apply(original)

    if original == updated:
        print("  README.md is up to date")
        return 0
    if arguments.check:
        print("  README.md is out of date; run scripts/update_readme.py")
        return 1
    if arguments.dry_run:
        import difflib
        for line in difflib.unified_diff(
            original.splitlines(), updated.splitlines(),
            fromfile="README.md", tofile="README.md (generated)", lineterm="",
        ):
            print(line)
        return 0

    README.write_text(updated, encoding="utf-8")
    print("  README.md generated sections refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
