"""Generate everything in `README.md` below the maintainer's introduction.

The introduction — the picture, "Hi! My name is Woody" and the paragraphs up
to the version block — is hand-written and is the only part a person checks.
Everything from the version block down is generated here, from templates in
this file and facts read from the code: the version and channel, the install
steps and Python range, the feature summary with its pity numbers and game
counts, the requirements with the intents `main.py` actually asks for, the
configuration with the schema version and the read-worker bounds, the
dashboard notes, the documentation list from `docs/`, and the recent
changelog sections.

Why: the README once said the schema was at version 24 while the code was at
26, and that a dataset was Hungarian-only a month after English was complete,
and the stable release shipped saying so. A sentence that states a fact the
code holds is a sentence that will lie the day the code moves, unless the code
writes it. So it does, and `--check` (run by the suite and by the publisher
inside the built tree) fails when the file and the generator disagree.

Two marked blocks, and nothing outside them is touched:

    <!-- BEGIN GENERATED: version -->  ...  <!-- END GENERATED: version -->
    <!-- BEGIN GENERATED: body -->     ...  <!-- END GENERATED: body -->

A missing marker is an error rather than an excuse to append — appending to
somebody's README is how a tool earns being removed from the pipeline. Prose
lives in the `render_*` functions below; a factual number never does, it is
read from where the code keeps it.

Usage:
    python scripts/update_readme.py            # rewrite in place
    python scripts/update_readme.py --check    # exit 1 if it would change
    python scripts/update_readme.py --dry-run  # show what would change
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import version as version_module  # noqa: E402

README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"

# How many releases the README carries. Enough to see the direction of travel,
# few enough that the file stays readable; the rest is one link away.
CHANGELOG_RELEASES = 3
CHANGELOG_ENTRIES = 8

# Documents that describe this deployment or this repository's history and
# are not published (`scripts/publish_public.py` excludes them; it cannot be
# imported here because it does not ship either). Listed so the private
# README does not advertise them; in a published tree they are absent anyway.
PRIVATE_DOCS = {
    "config_retirement_plan.md", "deployment_host.md", "guild_message.md",
    "lessons.md", "performance_recovery_plan.md", "podman_runbook.md",
}

PRIVILEGED_INTENTS = {
    "message_content": "Message Content",
    "members": "Server Members",
    "presences": "Presence",
}

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
         7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
         12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
         16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
         20: "twenty"}


def _word(number: int) -> str:
    return WORDS.get(number, str(number))


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ------------------------------------------------------------------- facts

def python_minors() -> list[str]:
    """`3.12`, `3.13`, `3.14` from pyproject's `requires-python`."""
    with open(ROOT / "pyproject.toml", "rb") as handle:
        spec = tomllib.load(handle)["project"]["requires-python"]
    low = int(re.search(r">=3\.(\d+)", spec).group(1))
    below = int(re.search(r"<3\.(\d+)", spec).group(1))
    return [f"3.{minor}" for minor in range(low, below)]


def idle_minutes() -> int:
    # Read from the source: importing dashboard_api generates a session secret.
    return int(re.search(r"^SESSION_IDLE_TIMEOUT = timedelta\(minutes=(\d+)\)",
                         _source("dashboard_api.py"), re.MULTILINE).group(1))


def read_workers() -> tuple[int, int, int]:
    """(default, lowest, highest) from `_read_worker_count`."""
    text = _source("core/database.py")
    default = int(re.search(r'POTATOBOT_DB_READ_WORKERS", "(\d+)"', text).group(1))
    low, high = re.search(r"if not (\d+) <= workers <= (\d+)", text).groups()
    return default, int(low), int(high)


def privileged_intents() -> list[str]:
    enabled = re.findall(r"intents\.(\w+) = True", _source("main.py"))
    return [label for key, label in PRIVILEGED_INTENTS.items() if key in enabled]


def schema_version() -> int:
    from core import database
    return database.LATEST_SCHEMA_VERSION


def pity() -> dict:
    from core import database
    return database.DEFAULT_GACHA_CONFIG


def supported_game_count() -> int:
    from core.supported_games import SUPPORTED_GAME_KEYS
    return len(SUPPORTED_GAME_KEYS)


def feature_counts() -> tuple[int, int]:
    from core.settings_registry import FEATURE_DEFINITIONS
    groups = {definition.group for definition in FEATURE_DEFINITIONS.values()}
    return len(FEATURE_DEFINITIONS), len(groups)


def deployment_profiles() -> list[str]:
    text = _source("core/deployment.py")
    return re.findall(r'^\s+[A-Z_]+ = "([a-z_]+)"$', text, re.MULTILINE)


def documentation() -> list[tuple[str, str]]:
    """(title, path) for every shipped document, title from its first heading."""
    entries = []
    for path in sorted((ROOT / "docs").glob("*.md")):
        if path.name in PRIVATE_DOCS:
            continue
        heading = next((line[2:].strip() for line in
                        path.read_text(encoding="utf-8").splitlines()
                        if line.startswith("# ")), path.stem)
        entries.append((heading, f"docs/{path.name}"))
    return entries


# ---------------------------------------------------------------- renders

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
    minors = python_minors()
    return "\n".join([
        "## Quick start",
        "",
        "```bash",
        "git clone https://github.com/WoodenPotatos/potatobot.git /opt/potatobot",
        "cd /opt/potatobot",
        "python3 -m venv venv",
        "./venv/bin/python -m pip install --require-hashes --requirement requirements.lock",
        "cp .env.example .env        # add your bot token and the PG* settings",
        "./venv/bin/python update_db.py   # against the PostgreSQL 18 database you created",
        "./venv/bin/python main.py",
        "```",
        "",
        "Runs on a headless Linux server under systemd. Requires Python"
        f" {minors[0]}–{minors[-1]} and PostgreSQL 18. Every setting lives in the database"
        " from the start and is"
        " editable from the dashboard once it is running. The full"
        " walkthrough — Discord application, intents, OAuth, HTTPS, systemd"
        " and the guild setup check — is in"
        " [docs/installation.md](docs/installation.md).",
    ])


def render_highlights() -> str:
    config = pity()
    features, groups = feature_counts()
    return "\n".join([
        "## Highlights",
        "",
        "- Hybrid prefix and slash commands with explicit public/private response policies",
        "- Atomic economy rewards, transfers, purchases, wagers, and crash-safe game settlement",
        "- Complete Hungarian and English localization of the bot, the dashboard and the",
        "  Everydle minigame data, with Hungarian as the source language",
        "- PostgreSQL storage with numbered, transactional migrations, a dump before each one,",
        "  and asynchronous read/write execution",
        f"- {features} guild feature switches in {_word(groups)} groups, with dependency checks,",
        "  audit revisions, and runtime cache refresh",
        "- Moderation, warnings, faction management, tickets, onboarding, role menus, and temporary voice channels",
        "- LoLdle, Valdle, DbDle and Genshindle daily puzzles; counting and word-chain",
        "  channels",
        "- Casino games: blackjack, dice, roulette, slots, wheel, mines, hilo, crash and",
        "  Russian roulette",
        "- HTTPS YouTube-only music playback through bounded `yt-dlp` and FFmpeg workers",
        "- OAuth-authenticated dashboard designed to run behind private HTTPS",
        "- Typed, categorized guild dashboard with feature-aware navigation, safe Discord",
        "  selectors, audit history, gacha/banner controls, content builders, and safe shop templates",
        f"- Every guild setting editable from the dashboard, with a {_word(idle_minutes())}-minute idle",
        "  timeout, a permission diagnostic, and an overview showing recent changes beside",
        "  the release changelog",
        "- Toggleable Potato Gacha in its own cog with several named banners per guild,",
        "  explicit one/ten-pull choices, every-tenth-pull 4-star guarantee, persistent",
        f"  {config['hard_pity']}-pull hard pity, configurable soft pity after pull"
        f" {config['soft_pity_start']}, fixed-reserve vaults,",
        "  vouchers, and consumables",
        "- One shared item catalog behind the shop and the gacha, so the same consumable",
        "  or vault means the same thing however a member obtained it",
        "- Per-guild `/work` outcome odds, payouts and response text, editable as plain",
        "  rows; a tier a guild has not touched uses the shipped set",
        f"- Patch-notes announcements per game, per guild, for {_word(supported_game_count())}"
        " supported titles,",
        "  with a configurable channel and an optional role ping for each",
        "- An optional invite log naming who invited each new member, posted to the bot",
        "  log channel or a separate one; it needs the Manage Server permission",
        "- Join, leave, kick, ban and boost messages each with their own switch,",
        "  channel and editable text (title, random variants, colour, image, plain text",
        "  with placeholders), a live preview and a test send on the dashboard; kicks",
        "  and bans are told apart from leaves through the audit log",
        "- A Privacy Policy and Terms of Service in English and Hungarian, linked from",
        "  `/version` and the dashboard's sign-in card, with a drift check in CI that",
        "  holds the policy to what the code stores, logs and sends",
    ])


def render_requirements() -> str:
    minors = python_minors()
    intents = privileged_intents()
    return "\n".join([
        "## Requirements",
        "",
        "- A **Linux server, headless**, supervised by systemd and reached through a",
        "  reverse proxy. That is what it is developed and run on; `deploy/` holds the",
        "  units and `container/` holds a Containerfile and compose.yaml for a",
        "  container build. Nothing here is written for Windows or macOS.",
        f"- Python {', '.join(minors[:-1])}, or {minors[-1]}",
        "- A Discord application and bot token",
        f"- Discord privileged {' and '.join(intents)} intents",
        "- FFmpeg for music playback",
        "- PostgreSQL 18, on the host or as the container beside the bot",
        "- Optional Twitch API credentials for stream notifications",
        "- For the dashboard, an HTTPS reverse proxy in front of loopback: Tailscale",
        "  Serve, a Cloudflare Tunnel, nginx or Caddy",
        "",
        "Runtime dependencies are pinned in `requirements.lock`; development and security"
        " tooling is pinned in `requirements-dev.lock`. Both are generated with hashes for"
        " every transitive package, so install with `--require-hashes`.",
    ])


def render_configuration() -> str:
    default, low, high = read_workers()
    profiles = deployment_profiles()
    return "\n".join([
        "## Configuration",
        "",
        "Environment variables own credentials and deployment settings. Typed guild settings,",
        "feature revisions, gacha state, inventory, vouchers, and dashboard audit records live in",
        "PostgreSQL, and the bot is the only thing that creates the schema.",
        "",
        "Important environment variables are documented in `.env.example`. In particular:",
        "",
        "- `PGHOST`, `PGDATABASE`, `PGUSER` and `PGPASSWORD` (libpq's own) select the"
        " database; the role is the bot's own, not a superuser.",
        "- `POTATOBOT_DATA_DIR` selects where the dashboard session secret and welcome-card"
        " backgrounds live; unset, they sit beside the database.",
        f"- `POTATOBOT_DB_READ_WORKERS` selects {low}–{high} concurrent read workers;"
        f" the default is {default}.",
        "- `POTATOBOT_DEPLOYMENT_PROFILE` accepts "
        + ", ".join(f"`{p}`" for p in profiles[:-1]) + f", or `{profiles[-1]}`.",
        "- `POTATOBOT_LEGACY_GUILD_ID` names the guild a 2.x database's wallets belong to"
        " when schema 27 cannot tell (more than one active guild and no adoption marker).",
        "- Dashboard OAuth requires matching HTTPS values for `POTATOBOT_DASHBOARD_EXTERNAL_URL`"
        " and `DISCORD_REDIRECT_URI`, with the latter ending in `/api/callback`.",
        "",
        f"The schema is at version {schema_version()}. Prices, rewards, voice preferences,"
        " leaderboards, ranks, rental cleanup, `/work` responses and gacha banners are per"
        " guild, with `guild_id` 0 holding the installation default; installation-wide"
        " settings live in their own table with no guild dimension at all. Wallets are not:"
        " `users` is still keyed by Discord user ID alone, so the live economy is not"
        " multi-guild-safe yet and cooldowns are deliberately installation-wide. Per-guild"
        " wallets are planned for 3.0.0, and economies shared between guilds for 3.x. The"
        " dashboard has one host, `ADMIN_DISCORD_ID`; under the `self_hosted` profile each"
        " guild's owner, administrators and Manage Server holders can also manage that guild.",
        "",
        "Members can export their data with `/mydata` and erase it with `/deletemydata`; the"
        " host can erase on a member's behalf from the dashboard. Erasure keeps the economy"
        " row under an anonymous tombstone so installation totals never change silently."
        " See `docs/privacy.md` for the design and `docs/legal/` for the policy members read.",
    ])


def render_dashboard() -> str:
    config = pity()
    soft, hard = config["soft_pity_start"], config["hard_pity"]
    return "\n".join([
        "## Dashboard",
        "",
        "Bind the Waitress-served dashboard to `127.0.0.1:5000` and expose it through one"
        " HTTPS reverse proxy: Tailscale Serve for a private name, a Cloudflare Tunnel for a"
        " public one, or nginx. With Tailscale Serve:",
        "",
        "```bash",
        "tailscale serve --bg http://127.0.0.1:5000",
        "tailscale serve status",
        "```",
        "",
        "Use the exact same stable HTTPS origin in the environment and Discord Developer"
        " Portal. Never expose port 5000 directly to an untrusted network.",
        "",
        "The dashboard supports Discord OAuth, server-held refreshable OAuth tokens,",
        "session-bound CSRF protection, live permission checks for mutations, typed/revisioned",
        "settings, feature flags, data-scope foundations, safe channel/role selectors, shop and",
        "gacha configuration, the content builders, queued Discord publishing, fulfillment, and audit",
        "history. Raw full-JSON configuration and legacy price/reward endpoints are not exposed.",
        "",
        "The interface uses a dark sidebar with a light content area: an overview page with",
        "quick actions and counters, feature toggles grouped by area with each game family"
        " under a master toggle, settings split into",
        "labelled sections, and tables for shop items, fulfillment requests, posted messages and",
        "the audit log. The account avatar sits at the top right, with the guild switcher next to",
        "it showing each server's icon; its menu selects the appearance mode (follow the operating",
        "system, light, or the original dark potato palette) and the dashboard's display language,",
        "both stored in the browser. The display language only affects the dashboard — the bot's",
        "Discord language stays the instance setting on the Administration page. Everything is"
        " self-hosted with no external fonts, scripts or icons, so the",
        "page works unchanged behind a strict Content-Security-Policy and on an isolated network.",
        "",
        "Gacha is disabled by default. Enable `shop_gacha` only after configuring and reviewing",
        "the banner. `/gacha` offers explicit one-pull and ten-pull choices; ten pulls are atomic",
        f"and have no discount. Every tenth pull is at least 4-star, pulls {soft + 1}–{hard - 1}"
        " triple both",
        f"rare-tier totals while reducing the 3-star pool, and pull {hard} guarantees a 5-star.",
        "`/inventory` lists consumables and vouchers; `/redeem` activates premium or creates an",
        "asset-fulfillment request.",
        "",
        "The shop and the gacha hand out the same goods. Lockpicks, loaded dice and vault drills",
        "are one stackable inventory item either way, and a vault protects the same reserve",
        "whichever system granted it. Only the way you get one differs: buying a vault you already",
        "own is refused and costs nothing, while pulling a duplicate pays the configured",
        "compensation instead.",
    ])


def render_verification() -> str:
    return "\n".join([
        "## Verification",
        "",
        "Before relying on a change:",
        "",
        "```bash",
        "npm ci --no-audit --no-fund       # the dashboard tests drive script.js through Node",
        "./venv/bin/python -m pip install --require-hashes -r requirements-dev.lock",
        "./venv/bin/python -m unittest discover -s tests",
        "./venv/bin/python scripts/locale_audit.py --brief",
        "```",
        "",
        "Migrations are rehearsed against a copy, never the live file, and the full",
        "procedure — the CI gate, snapshots, row-count comparison and the acceptance",
        "matrices — is in [docs/development.md](docs/development.md).",
    ])


def render_documentation() -> str:
    lines = ["## Documentation", ""]
    lines.extend(f"- [{title}]({path})" for title, path in documentation())
    lines.append("- [Privacy Policy](docs/legal/PRIVACY.md) and"
                 " [Terms of Service](docs/legal/TERMS.md), with Hungarian translations"
                 " ([PRIVACY.hu.md](docs/legal/PRIVACY.hu.md),"
                 " [TERMS.hu.md](docs/legal/TERMS.hu.md)) beside them")
    lines.append("- [Changelog](CHANGELOG.md)")
    lines.append("- [Localized minigame data format](data/README.md)")
    return "\n".join(lines)


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

    out = ["## Recent releases", ""]
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


def render_status() -> str:
    return "\n".join([
        "## Development status",
        "",
        "PotatoBot is production-bound for one private deployment, so database compatibility"
        " and safe rollout take priority over rapid breaking changes. Remaining major work is"
        " guild-scoped wallets (3.0.0) and then shared economies between guilds (3.x),"
        " followed by rehearsing a managed deployment. Public releases are sanitised"
        " snapshots with no history from the private development line: betas on the"
        " `beta` branch, and each stable release on the `stable` branch, which is the last"
        " beta copied with its letter stripped.",
        "",
        "New sanitized releases are licensed under AGPL-3.0-only; see [LICENSE](LICENSE).",
    ])


def render_body() -> str:
    return "\n\n".join([
        render_install(),
        render_highlights(),
        render_requirements(),
        render_configuration(),
        render_dashboard(),
        render_verification(),
        render_documentation(),
        render_changelog(),
        render_status(),
    ])


SECTIONS = {
    "version": render_version,
    "body": render_body,
}


def _marker(name: str) -> re.Pattern:
    return re.compile(
        rf"(?P<open><!-- BEGIN GENERATED: {re.escape(name)} -->\n)"
        rf"(?P<body>.*?)"
        rf"(?P<close>\n?<!-- END GENERATED: {re.escape(name)} -->)",
        re.DOTALL,
    )


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
