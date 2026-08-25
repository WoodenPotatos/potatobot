# PotatoBot

![PotatoBot avatar](botdata/pfp/potatobotpfp.png)

Hi! My name is Woody.

This is my self-created and self-hosted Discord bot written in Python. I originally created this project as a vibe coded bot to skip using multiple different bots on my server and to skip paying for features i want. Later it expanded into an AI assisted setup where i wrote most of the things but an AI assisted me in many things. The version you see here however is a way more expanded version which is now heavily AI coder generated.

That being said i would like to share this project with you all whom are weird enough like me and want to skip using big bots and want something that is ours. The bot is still in active development and has way more bugs and problems that i currently have records of so please be aware of them and report anything that you find to help me fix them.

For the moment i wont share any file that is related to the AI in question. The bot's biggest feature is that it has a central web based dashboard where basically everything is modifiable. The features itselves are all toggleable so even tho the bot has more features than a swiss army knife, everyone can customize it to their own needs. I don't and won't have anything locked behind anything. 

The bot's main language is Hungarian but it has a full English localization, and it is quite easy to add other languages. However i would be really happy if you could help me translate to other languages so that i could also add them to the repo for others to use. 

Currently the bot is built for single guild use however it already has the foundation for multi guild usage with some fancy special features in mind. Also it is currently a bare metal build, a Docker build is in plans however i need to do some testing and fixing first.

<!-- BEGIN GENERATED: version -->
**Version 2.2.0-beta.2** &nbsp;·&nbsp; channel `beta`

Early access. Expect breaking changes between releases.
<!-- END GENERATED: version -->

## Quick start

<!-- BEGIN GENERATED: install -->
```bash
git clone https://github.com/WoodenPotatos/potatobot.git /opt/potatobot
cd /opt/potatobot
python3 -m venv venv
./venv/bin/python -m pip install --requirement requirements.lock
cp .env.example .env        # add your bot token
cp config.json.example config.json
POTATOBOT_DB_PATH=$PWD/economy.db ./venv/bin/python update_db.py
./venv/bin/python main.py
```

Requires Python 3.12–3.14. The full walkthrough — Discord application, intents, OAuth, HTTPS, systemd and the guild setup check — is in [docs/installation.md](docs/installation.md).
<!-- END GENERATED: install -->

## Highlights

- Hybrid prefix and slash commands with explicit public/private response policies
- Atomic economy rewards, transfers, purchases, wagers, and crash-safe game settlement
- Complete Hungarian and English localization of the bot, the dashboard and the
  Valdle/DbDle minigame data, with Hungarian as the source language
- SQLite WAL storage with ordered, in-place migrations and asynchronous read/write execution
- Guild feature flags with dependency checks, audit revisions, and runtime cache refresh
- Moderation, warnings, faction management, tickets, onboarding, role menus, and temporary voice channels
- LoLdle, Valdle, DBDle, blackjack, dice, roulette, slots, and mines
- HTTPS YouTube-only music playback through bounded `yt-dlp` and FFmpeg workers
- OAuth-authenticated experimental dashboard designed to run behind private HTTPS
- Typed, categorized guild dashboard with feature-aware navigation, safe Discord
  selectors, audit history, gacha/banner controls, content builders, and safe shop templates
- Every guild setting editable from the dashboard, with a ten-minute idle
  timeout, a permission diagnostic, and the release changelog on their own pages
- Toggleable Potato Gacha in its own cog with several named banners per guild,
  explicit one/ten-pull choices, every-tenth-pull 4-star guarantee, persistent
  100-pull hard pity, configurable soft pity after pull 75, fixed-reserve vaults,
  vouchers, and consumables
- One shared item catalog behind the shop and the gacha, so the same consumable
  or vault means the same thing however a member obtained it
- Per-guild `/work` outcome odds, payouts and response text, falling back to the
  shipped Hungarian lines for any tier a guild has not written its own

## Requirements

- Python 3.12, 3.13, or 3.14
- A Discord application and bot token
- Discord privileged Member and Message Content intents
- FFmpeg for music playback
- SQLite support (included with normal Python builds)
- Optional Twitch API credentials for stream notifications
- Optional Tailscale Serve for the private HTTPS dashboard

Runtime dependencies are pinned in `requirements.lock`; development and security tooling is pinned in `requirements-dev.lock`.

## Configuration

Environment variables own credentials and deployment settings. Typed guild settings,
feature revisions, gacha state, inventory, vouchers, and dashboard audit records live in
SQLite, which is the only thing that writes them. `config.json` is a read-only fallback
for a setting an installation has never saved; `python scripts/import_config.py` gives
each of those a row, after which the file is unused.

Important environment variables are documented in `.env.example`. In particular:

- `POTATOBOT_DB_PATH` selects the database file.
- `POTATOBOT_DB_READ_WORKERS` selects 2–8 concurrent read workers; the default is 4.
- `POTATOBOT_DEPLOYMENT_PROFILE` accepts `private`, `managed`, or `self_hosted`.
- `POTATOBOT_LEGACY_GUILD_ID` identifies the original guild when a legacy private database is connected to multiple guilds.
- Dashboard OAuth requires matching HTTPS values for `POTATOBOT_DASHBOARD_EXTERNAL_URL` and `DISCORD_REDIRECT_URI`, with the latter ending in `/api/callback`.

Prices, rewards, voice preferences, leaderboards, ranks and rental cleanup are per guild as of schema 8, where `guild_id` 0 holds the installation default. Wallets are not: `users` is still keyed by Discord user ID alone, so the live economy is not multi-guild-safe yet and cooldowns are deliberately installation-wide. Dashboard access still uses a single `ADMIN_DISCORD_ID`.

Members can export their data with `/mydata` and erase it with `/deletemydata`; the host can erase on a member's behalf from the dashboard. Erasure keeps the economy row under an anonymous tombstone so installation totals never change silently. See `docs/privacy.md`.

## Dashboard

For private deployment, bind the Waitress-served dashboard to `127.0.0.1:5000` and expose it through an HTTPS reverse proxy such as Tailscale Serve:

```bash
tailscale serve --bg http://127.0.0.1:5000
tailscale serve status
```

Use the exact same stable HTTPS origin in the environment and Discord Developer Portal. Never expose port 5000 directly to an untrusted network.

The dashboard supports Discord OAuth, server-held refreshable OAuth tokens,
session-bound CSRF protection, live permission checks for mutations, typed/revisioned
settings, feature flags, data-scope foundations, safe channel/role selectors, shop and
gacha configuration, builder drafts, queued Discord publishing, fulfillment, and audit
history. Raw full-JSON configuration and legacy price/reward endpoints are not exposed.

The interface uses a dark sidebar with a light content area: an overview page with
quick actions and counters, feature toggles grouped by dependency, settings split into
labelled sections, and tables for shop items, fulfillment requests, builder drafts and
the audit log. The account avatar sits at the top right, with the guild switcher next to
it showing each server's icon; its menu selects the appearance mode (follow the operating
system, light, or the original dark potato palette) and the dashboard's display language,
both stored in the browser. The display language only affects the dashboard — the bot's
Discord language stays the instance setting on the Administration page. Everything is self-hosted with no external fonts, scripts or icons, so the
page works unchanged behind a strict Content-Security-Policy and on an isolated network.

Gacha is disabled by default. Enable `shop_gacha` only after configuring and reviewing
the banner. `/gacha` offers explicit one-pull and ten-pull choices; ten pulls are atomic
and have no discount. Every tenth pull is at least 4-star, pulls 76–99 triple both
rare-tier totals while reducing the 3-star pool, and pull 100 guarantees a 5-star.
`/inventory` lists consumables and vouchers; `/redeem` activates premium or creates an
asset-fulfillment request.

The shop and the gacha hand out the same goods. Lockpicks, loaded dice and vault gloves
are one stackable inventory item either way, and a vault protects the same reserve
whichever system granted it. Only the way you get one differs: buying a vault you already
own is refused and costs nothing, while pulling a duplicate pays the configured
compensation instead.

### Running the dashboard locally

When the deployment is unreachable and you only want to look at the control
plane:

```bash
python scripts/local_dashboard.py          # then open http://127.0.0.1:5001/
python scripts/local_dashboard.py --fresh  # re-copy the database first
```

It copies `economy.db` into `.local-dev/`, migrates and fingerprints the copy so
a stale database is also a migration rehearsal, builds a stand-in Discord guild
from the ids in `config.json` so the selectors resolve, and signs you in as the
host without OAuth. It refuses to start against a proxied or managed environment,
always binds loopback, and never writes the tracked `config.json`. Everything it
fakes is printed at startup.

## Verification

Run the complete test and syntax checks before committing:

```bash
python -m unittest discover -s tests -v
python -m compileall -q . -x './\.git|./venv|./\.venv'
```

Before deploying a schema change, rehearse it on a copy of the deployed database
and prove no data was lost:

```bash
python scripts/rehearse_migration.py /opt/potatobot/economy.db
```

It copies the database, fingerprints it, migrates the copy, fingerprints again and
compares, exiting non-zero if any table's row count changes. The copy it leaves
behind is the rollback artefact. `scripts/db_snapshot.py` does the fingerprint and
compare steps on their own. Never point either at the live file while the bot runs.

When adding Hungarian localization keys, synchronize empty placeholders into the other general catalogs:

```bash
python scripts/sync_locale_keys.py
```

Do not machine-translate the generated empty values.

## Documentation

- [Contributor invariants and repository layout](CLAUDE.md)
- [Installation and deployment guide](docs/installation.md)
- [Setting up levels](docs/level_setup.md)
- [Developer and operations guide](docs/development.md)
- [Localization status and plan](docs/localization_status.md)
- [Keeping Everydle data current](docs/everydle_data_updates.md)
- [Retiring config.json](docs/config_retirement_plan.md)
- [Security policy](SECURITY.md)
- [Threat model](docs/threat_model.md)
- [Privacy and data lifecycle](docs/privacy.md)
- [2.0 release checklist](docs/release_checklist.md)
- [Changelog](CHANGELOG.md)
- [Performance recovery and acceptance plan](docs/performance_recovery_plan.md)
- [Active backlog](todo.md)
- [Localized minigame data format](data/README.md)

## Recent releases

<!-- BEGIN GENERATED: changelog -->
### 2.2.0-beta.2

- Stopped writing every log line twice. `waitress.serve` calls `logging.basicConfig()`, which adds a root handler when nothing else has one, so every record that propagated was emitted again in Python's default format; and `bot.run()` was configuring the `discord` logger the bot had already set up. A 500 MB journal cap was being spent at twice the rate, and any count of errors or reconnects in it read double.
- No setting is edited as hand-written JSON any more. The level ladder, the LFG map and the faction map were the last three, and they looked like editing a config file that happened to live in a form. Each has a row editor now — a level and the role it grants, a channel and the role to ping, a faction name with its leader role and the roles it manages — built from one engine driven by a declared spec rather than four near-copies, and each shape is validated server-side so a malformed map is refused instead of failing later inside a button callback. The level ladder still accepts a role *name* as well as an id, because it always has.
- Retired `config.json` as a source the bot writes. Every cog reads its settings through the in-memory cache now, so a dashboard save is visible without `?reloadconfig` and a setting can be genuinely per-guild. The mirror is gone with it: `_apply_legacy_config_values`, `reconcile_legacy_config_mirror` and the lock that made their read-modify-write safe are deleted, and `/maintenance` writes the same row the dashboard writes instead of rewriting the file. The file survives as the fallback for a setting an installation has never saved, which is what makes the one-time import something an operator does when they choose rather than a prerequisite for upgrading.
- Fixed three things the conversion turned up. A role-menu button had its role id baked in at construction on a view shared by every message, so it could only ever be right for one guild; it resolves the role per click now. Both social polling loops read one notification channel for the whole installation and now iterate guilds. And the inactivity scanner read its log channel outside the guild loop, so one guild's channel received every guild's report.
- Fixed the inactivity ignore list, which would have stopped ignoring anyone the moment it was saved from the dashboard. It holds Discord ids, is a string list because a snowflake cannot cross to a browser as a number, and was compared against `member.id` directly — true only while the value came from `config.json`, where it is stored as integers. It is compared as ids now, and the one-time import converts the legacy shape and says that it did.
- Made YouTube notifications reachable. `socials.youtube_channels` was read by the polling loop and existed in neither the registry nor the example config, so it always resolved empty — the feature could not be switched on from anywhere. It is a typed setting now.
- Gave installation-wide settings a home. **Schema 11** adds `instance_settings`, keyed by setting alone, and `language`, `currency_emoji`, `maintenance`, `command_prefix` and `data_retention_days` now live there rather than being stored per guild while being installation-wide in fact. The scope is a structural fact now, not a convention: an instance setting cannot be written per guild at all. Pre-existing rows are moved across, and a key that was stored for several guilds keeps its most recent value and says which it discarded.
- Added `settings_cache`, so a command path no longer reads SQLite for a setting. `is_channel` resolves a setting key through it — one change that moved all 22 channel gates — and `maintenance_blocks` reads it too. It falls back to `config.json` and then the registry default rather than to nothing, which is what keeps maintenance failing *open*: an unreadable setting must not be an outage, and that is the opposite of how the feature gate fails.
- …and 20 more, in [CHANGELOG.md](CHANGELOG.md).

### 2.0.0-rc.1 - Unreleased

- Added transactional schema-4 wager recovery and atomic booster reward claims.
- Made feature policy fail closed until its guild cache is ready.
- Hardened YouTube extraction, music queues, ticket transcripts, moderation validation, interaction permissions, and bounded runtime state.
- Added typed data-scope resolution foundations and explicit legacy-guild adoption for multi-guild migration.
- Replaced Flask's development server path with Waitress and added dashboard rate limits and response security headers.
- Added pinned dependencies, Python 3.12-3.14 CI, security policy, threat model, privacy notes, and release checklist.
- Added schema-5 typed settings, configurable shop templates, fixed vault reserves, inventory, vouchers, timed entitlements, fulfillment, builder drafts, and a permission-rechecked dashboard action outbox.
- Added toggleable Potato Gacha with atomic one/ten pulls, 75-pull soft pity, 100-pull hard pity, immutable pull history, loaded dice, robbery gloves, and duplicate-vault compensation.
- …and 73 more, in [CHANGELOG.md](CHANGELOG.md).

The full history is in [CHANGELOG.md](CHANGELOG.md).
<!-- END GENERATED: changelog -->

## Development status

PotatoBot is production-bound for one private deployment, so database compatibility and safe rollout take priority over rapid breaking changes. Remaining major work includes fully guild-scoping runtime storage, separating dashboard supervision for non-private deployments, rehearsing managed deployment, packaging, and publishing only a sanitized clean-history release.

New sanitized releases are licensed under AGPL-3.0-only; see [LICENSE](LICENSE).
