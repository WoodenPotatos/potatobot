# PotatoBot

![PotatoBot avatar](potatobotpfp.png)

Hi! My name is Woody.

This is my self-created and self-hosted Discord bot written in Python. I originally created this project as a vibe coded bot to skip using multiple different bots on my server and to skip paying for features i want. Later it expanded into an AI assisted setup where i wrote most of the things but an AI assisted me in many things. The version you see here however is a way more expanded version which is now heavily AI coder generated.

That being said i would like to share this project with you all whom are weird enough like me and want to skip using big bots and want something that is ours. The bot is still in active development and has way more bugs and problems that i currently have records of so please be aware of them and report anything that you find to help me fix them.

For the moment i won't share any file that is related to the AI in question. The bot's biggest feature is that it has a central web based dashboard where basically everything is modifiable. The features themselves are all toggleable so even tho the bot has more features than a swiss army knife, everyone can customize it to their own needs. I don't and won't have anything locked behind anything. 

The bot's main language is Hungarian but it has a full English localization, and it is quite easy to add other languages. However i would be really happy if you could help me translate to other languages so that i could also add them to the repo for others to use. 

Currently the bot is built for single guild use however it already has the foundation for multi guild usage with some fancy special features in mind. It runs bare metal or as a container: `container/` holds a Containerfile and a compose.yaml for the two-process split, `deploy/` holds the systemd units and a rootless podman Quadlet unit, and CI builds the image on every push.

<!-- BEGIN GENERATED: version -->
**Version 3.0.5b** &nbsp;·&nbsp; channel `beta`

Early access. Expect breaking changes between releases.
<!-- END GENERATED: version -->

<!-- BEGIN GENERATED: body -->
## Quick start

```bash
git clone https://github.com/WoodenPotatos/potatobot.git /opt/potatobot
cd /opt/potatobot
python3 -m venv venv
./venv/bin/python -m pip install --require-hashes --requirement requirements.lock
cp .env.example .env        # add your bot token and the PG* settings
./venv/bin/python update_db.py   # against the PostgreSQL 18 database you created
./venv/bin/python main.py
```

Runs on a headless Linux server under systemd. Requires Python 3.12–3.14 and PostgreSQL 18. Every setting lives in the database from the start and is editable from the dashboard once it is running. The full walkthrough — Discord application, intents, OAuth, HTTPS, systemd and the guild setup check — is in [docs/installation.md](docs/installation.md).

## Highlights

- Hybrid prefix and slash commands with explicit public/private response policies
- Atomic economy rewards, transfers, purchases, wagers, and crash-safe game settlement
- Complete Hungarian and English localization of the bot, the dashboard and the
  Everydle minigame data, with Hungarian as the source language
- PostgreSQL storage with numbered, transactional migrations, a dump before each one,
  and asynchronous read/write execution
- 62 guild feature switches in eleven groups, with dependency checks,
  audit revisions, and runtime cache refresh
- Moderation, warnings, faction management, tickets, onboarding, role menus, and temporary voice channels
- LoLdle, Valdle, DbDle and Genshindle daily puzzles; counting and word-chain
  channels
- Casino games: blackjack, dice, roulette, slots, wheel, mines, hilo, crash and
  Russian roulette
- HTTPS YouTube-only music playback through bounded `yt-dlp` and FFmpeg workers
- OAuth-authenticated dashboard designed to run behind private HTTPS
- Typed, categorized guild dashboard with feature-aware navigation, safe Discord
  selectors, audit history, gacha/banner controls, content builders, and safe shop templates
- Every guild setting editable from the dashboard, with a ten-minute idle
  timeout, a permission diagnostic, and an overview showing recent changes beside
  the release changelog
- Toggleable Potato Gacha in its own cog with several named banners per guild,
  explicit one/ten-pull choices, every-tenth-pull 4-star guarantee, persistent
  100-pull hard pity, configurable soft pity after pull 75, fixed-reserve vaults,
  vouchers, and consumables
- One shared item catalog behind the shop and the gacha, so the same consumable
  or vault means the same thing however a member obtained it
- Per-guild `/work` outcome odds, payouts and response text, editable as plain
  rows; a tier a guild has not touched uses the shipped set
- Patch-notes announcements per game, per guild, for eleven supported titles,
  with a configurable channel and an optional role ping for each
- An optional invite log naming who invited each new member, posted to the bot
  log channel or a separate one; it needs the Manage Server permission
- Join, leave, kick, ban and boost messages each with their own switch,
  channel and editable text (title, random variants, colour, image, plain text
  with placeholders), a live preview and a test send on the dashboard; kicks
  and bans are told apart from leaves through the audit log
- A Privacy Policy and Terms of Service in English and Hungarian, linked from
  `/version` and the dashboard's sign-in card, with a drift check in CI that
  holds the policy to what the code stores, logs and sends

## Requirements

- A **Linux server, headless**, supervised by systemd and reached through a
  reverse proxy. That is what it is developed and run on; `deploy/` holds the
  units and `container/` holds a Containerfile and compose.yaml for a
  container build. Nothing here is written for Windows or macOS.
- Python 3.12, 3.13, or 3.14
- A Discord application and bot token
- Discord privileged Message Content and Server Members intents
- FFmpeg for music playback
- PostgreSQL 18, on the host or as the container beside the bot
- Optional Twitch API credentials for stream notifications
- For the dashboard, an HTTPS reverse proxy in front of loopback: Tailscale
  Serve, a Cloudflare Tunnel, nginx or Caddy

Runtime dependencies are pinned in `requirements.lock`; development and security tooling is pinned in `requirements-dev.lock`. Both are generated with hashes for every transitive package, so install with `--require-hashes`.

## Configuration

Environment variables own credentials and deployment settings. Typed guild settings,
feature revisions, gacha state, inventory, vouchers, and dashboard audit records live in
PostgreSQL, and the bot is the only thing that creates the schema.

Important environment variables are documented in `.env.example`. In particular:

- `PGHOST`, `PGDATABASE`, `PGUSER` and `PGPASSWORD` (libpq's own) select the database; the role is the bot's own, not a superuser.
- `POTATOBOT_DATA_DIR` selects where the dashboard session secret and welcome-card backgrounds live; unset, they sit beside the database.
- `POTATOBOT_DB_READ_WORKERS` selects 2–8 concurrent read workers; the default is 4.
- `POTATOBOT_DEPLOYMENT_PROFILE` accepts `private`, `managed`, or `self_hosted`.
- `POTATOBOT_LEGACY_GUILD_ID` names the guild a 2.x database's wallets belong to when schema 27 cannot tell (more than one active guild and no adoption marker).
- Dashboard OAuth requires matching HTTPS values for `POTATOBOT_DASHBOARD_EXTERNAL_URL` and `DISCORD_REDIRECT_URI`, with the latter ending in `/api/callback`.

The schema is at version 27. Prices, rewards, voice preferences, leaderboards, ranks, rental cleanup, `/work` responses and gacha banners are per guild, with `guild_id` 0 holding the installation default; installation-wide settings live in their own table with no guild dimension at all. Wallets are not: `users` is still keyed by Discord user ID alone, so the live economy is not multi-guild-safe yet and cooldowns are deliberately installation-wide. Per-guild wallets are planned for 3.0.0, and economies shared between guilds for 3.x. The dashboard has one host, `ADMIN_DISCORD_ID`; under the `self_hosted` profile each guild's owner, administrators and Manage Server holders can also manage that guild.

Members can export their data with `/mydata` and erase it with `/deletemydata`; the host can erase on a member's behalf from the dashboard. Erasure keeps the economy row under an anonymous tombstone so installation totals never change silently. See `docs/privacy.md` for the design and `docs/legal/` for the policy members read.

## Dashboard

Bind the Waitress-served dashboard to `127.0.0.1:5000` and expose it through one HTTPS reverse proxy: Tailscale Serve for a private name, a Cloudflare Tunnel for a public one, or nginx. With Tailscale Serve:

```bash
tailscale serve --bg http://127.0.0.1:5000
tailscale serve status
```

Use the exact same stable HTTPS origin in the environment and Discord Developer Portal. Never expose port 5000 directly to an untrusted network.

The dashboard supports Discord OAuth, server-held refreshable OAuth tokens,
session-bound CSRF protection, live permission checks for mutations, typed/revisioned
settings, feature flags, data-scope foundations, safe channel/role selectors, shop and
gacha configuration, the content builders, queued Discord publishing, fulfillment, and audit
history. Raw full-JSON configuration and legacy price/reward endpoints are not exposed.

The interface uses a dark sidebar with a light content area: an overview page with
quick actions and counters, feature toggles grouped by area with each game family under a master toggle, settings split into
labelled sections, and tables for shop items, fulfillment requests, posted messages and
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

The shop and the gacha hand out the same goods. Lockpicks, loaded dice and vault drills
are one stackable inventory item either way, and a vault protects the same reserve
whichever system granted it. Only the way you get one differs: buying a vault you already
own is refused and costs nothing, while pulling a duplicate pays the configured
compensation instead.

## Verification

Before relying on a change:

```bash
npm ci --no-audit --no-fund       # the dashboard tests drive script.js through Node
./venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
./venv/bin/python -m unittest discover -s tests
./venv/bin/python scripts/locale_audit.py --brief
```

Migrations are rehearsed against a copy, never the live file, and the full
procedure — the CI gate, snapshots, row-count comparison and the acceptance
matrices — is in [docs/development.md](docs/development.md).

## Documentation

- [Data inventory (record of processing)](docs/data_inventory.md)
- [PotatoBot Developer and Operations Guide](docs/development.md)
- [Keeping Everydle data current](docs/everydle_data_updates.md)
- [Installing PotatoBot](docs/installation.md)
- [Setting up levels](docs/level_setup.md)
- [Localization: state and rules](docs/localization_status.md)
- [Patchbot data sources](docs/patchbot_data_sources.md)
- [Privacy and data lifecycle](docs/privacy.md)
- [Release checklist](docs/release_checklist.md)
- [PotatoBot threat model](docs/threat_model.md)
- [Privacy Policy](docs/legal/PRIVACY.md) and [Terms of Service](docs/legal/TERMS.md), with Hungarian translations ([PRIVACY.hu.md](docs/legal/PRIVACY.hu.md), [TERMS.hu.md](docs/legal/TERMS.hu.md)) beside them
- [Changelog](CHANGELOG.md)
- [Localized minigame data format](data/README.md)

## Recent releases

### 3.0.5b

- **3.0.0: every server has its own economy.** Your coin balance, experience, level, cooldowns, `/daily`, Everydle streaks and casino limits are now kept separately in each server the bot is in, so coins earned in one server cannot be spent in another and a member of two servers no longer earns twice into one wallet. Everything you had is kept, in the server where you earned it; in any server you join later you start at 0 (a brand-new account still gets its starting coins in its first server). Leaderboards, `/profile` and robbery read the current server's wallets.
- **Privacy Policy 1.2.** Sections 2, 4, 7 and 8 say that this data is kept per server, that `/mydata` and `/deletemydata` cover every server, and that the retention sweep only removes a member inactive in every server.
- **Operators: schema 27, the first migration that cannot be undone by running an older version.** It adds `guild_id` to every wallet and keys `users` and `casino_earnings_window` by (`guild_id`, `user_id`). Existing wallets are attributed to the one guild the installation served: the `POTATOBOT_LEGACY_GUILD_ID` environment value if set, otherwise the single active guild, otherwise the guild recorded by the 2.x scope adoption. If none of those answers, the bot refuses to start and changes nothing; set `POTATOBOT_LEGACY_GUILD_ID` and start again. Rolling back means restoring the `pg_dump` the bot takes before migrating, which loses every coin earned after the upgrade. `adopt_legacy_database` no longer runs at start.
- **The casino rate-limit alert no longer misses its first post after a reboot.** For the length of the window after the host started, the first throttle of each member was silently not reported to staff.

### 2.21.37

- **SQLite support is removed.** Installations still on SQLite (2.19.x and earlier) stay on the last SQLite stable, 2.19.29, which the public repository keeps on the `stable-2.19` branch; there is no upgrade path from it. The one-time copy tool, `POTATOBOT_DB_PATH` and the offsite backup's `--legacy-sqlite` are gone. Unset `POTATOBOT_DATA_DIR` now means beside the code, where the session secret and card backgrounds already sat by default; an installation that set only `POTATOBOT_DB_PATH` sets `POTATOBOT_DATA_DIR` to that file's directory instead.

### 2.21.34b

- **Privacy Policy 1.1: your data is encrypted at rest.** The database, its backups on the server and the bot's log file now sit on an encrypted disk volume, memory the server pages out stays in compressed RAM, and no log line naming you goes to the system journal any more; the older ones expire within about two months. Nothing the bot collects or shares changed. (English and Hungarian, sections 7, 9 and 14.)
- **Keep member-naming log lines out of the system journal (operators).** A new `POTATOBOT_LOG_CONSOLE` setting (default `true`, see `.env.example`): set it to `false` and the bot writes its log lines only to `bot.log` in its data directory, so a journal on an unencrypted disk no longer holds them. Crash traces and stack dumps still reach the journal. The reference Quadlet unit sets it, `deploy/potatobot-db.container` makes PostgreSQL log errors without their values, and `deploy/potatobot-log` is a small reader for the log file that a sudoers rule can grant without wider access.
- **The nightly offsite backup can stage its dump on the encrypted volume.** `scripts/offsite_backup.py --workdir DIR` writes the plaintext dump and its verification copy there instead of the system temporary directory, and refuses to run if the directory is missing. The reference unit passes it and no longer passes `--legacy-sqlite` or sets `POTATOBOT_DB_PATH`.
- **Faster test suite for contributors.** Each test resets its database with ordered deletes instead of `TRUNCATE`, and `scripts/run_tests_parallel.py` runs the suite across several PostgreSQL databases for quick iteration (the serial run stays the gate).

The full history is in [CHANGELOG.md](CHANGELOG.md).

## Development status

PotatoBot is production-bound for one private deployment, so database compatibility and safe rollout take priority over rapid breaking changes. Remaining major work is guild-scoped wallets (3.0.0) and then shared economies between guilds (3.x), followed by rehearsing a managed deployment. Public releases are sanitised snapshots with no history from the private development line: betas on the `beta` branch, and each stable release on the `stable` branch, which is the last beta copied with its letter stripped.

New sanitized releases are licensed under AGPL-3.0-only; see [LICENSE](LICENSE).
<!-- END GENERATED: body -->
