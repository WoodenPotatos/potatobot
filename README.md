# PotatoBot

![PotatoBot avatar](potatobotpfp.png)

Hi! My name is Woody.

This is my self-created and self-hosted Discord bot written in Python. I originally created this project as a vibe coded bot to skip using multiple different bots on my server and to skip paying for features i want. Later it expanded into an AI assisted setup where i wrote most of the things but an AI assisted me in many things. The version you see here however is a way more expanded version which is now heavily AI coder generated.

That being said i would like to share this project with you all whom are weird enough like me and want to skip using big bots and want something that is ours. The bot is still in active development and has way more bugs and problems that i currently have records of so please be aware of them and report anything that you find to help me fix them.

For the moment i won't share any file that is related to the AI in question. The bot's biggest feature is that it has a central web based dashboard where basically everything is modifiable. The features themselves are all toggleable so even tho the bot has more features than a swiss army knife, everyone can customize it to their own needs. I don't and won't have anything locked behind anything. 

The bot's main language is Hungarian but it has a full English localization, and it is quite easy to add other languages. However i would be really happy if you could help me translate to other languages so that i could also add them to the repo for others to use. 

Currently the bot is built for single guild use however it already has the foundation for multi guild usage with some fancy special features in mind. It runs bare metal or as a container: `container/` holds a Containerfile and a compose.yaml for the two-process split, `deploy/` holds the systemd units and a rootless podman Quadlet unit, and CI builds the image on every push.

<!-- BEGIN GENERATED: version -->
**Version 2.18.4b** &nbsp;·&nbsp; channel `beta`

Early access. Expect breaking changes between releases.
<!-- END GENERATED: version -->

## Quick start

<!-- BEGIN GENERATED: install -->
```bash
git clone https://github.com/WoodenPotatos/potatobot.git /opt/potatobot
cd /opt/potatobot
python3 -m venv venv
./venv/bin/python -m pip install --require-hashes --requirement requirements.lock
cp .env.example .env        # add your bot token
POTATOBOT_DB_PATH=$PWD/economy.db ./venv/bin/python update_db.py
./venv/bin/python main.py
```

Runs on a headless Linux server under systemd. Requires Python 3.12–3.14. Every setting lives in SQLite from the start and is editable from the dashboard once it is running. The full walkthrough — Discord application, intents, OAuth, HTTPS, systemd and the guild setup check — is in [docs/installation.md](docs/installation.md).
<!-- END GENERATED: install -->

## Highlights

- Hybrid prefix and slash commands with explicit public/private response policies
- Atomic economy rewards, transfers, purchases, wagers, and crash-safe game settlement
- Complete Hungarian and English localization of the bot, the dashboard and the
  Everydle minigame data, with Hungarian as the source language (LoLdle's hard
  mode is Hungarian-only so far)
- SQLite WAL storage with ordered, in-place migrations and asynchronous read/write execution
- Guild feature flags with dependency checks, audit revisions, and runtime cache refresh
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
  timeout, a permission diagnostic, and the release changelog on their own pages
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

## Requirements

- A **Linux server, headless**, supervised by systemd and reached through a
  reverse proxy. That is what it is developed and run on; `deploy/` holds the
  units and `container/` holds a Containerfile and compose.yaml for a
  container build. Nothing here is written for Windows or macOS.
- Python 3.12, 3.13, or 3.14
- A Discord application and bot token
- Discord privileged Member and Message Content intents
- FFmpeg for music playback
- SQLite support (included with normal Python builds)
- Optional Twitch API credentials for stream notifications
- For the dashboard, an HTTPS reverse proxy in front of loopback: Tailscale
  Serve, a Cloudflare Tunnel, nginx or Caddy

Runtime dependencies are pinned in `requirements.lock`; development and security tooling is pinned in `requirements-dev.lock`. Both are generated with hashes for every transitive package, so install with `--require-hashes`.

## Configuration

Environment variables own credentials and deployment settings. Typed guild settings,
feature revisions, gacha state, inventory, vouchers, and dashboard audit records live in
SQLite, which is the only thing that writes them.

Important environment variables are documented in `.env.example`. In particular:

- `POTATOBOT_DB_PATH` selects the database file.
- `POTATOBOT_DB_READ_WORKERS` selects 2–8 concurrent read workers; the default is 4.
- `POTATOBOT_DEPLOYMENT_PROFILE` accepts `private`, `managed`, or `self_hosted`.
- `POTATOBOT_LEGACY_GUILD_ID` identifies the original guild when a legacy private database is connected to multiple guilds.
- Dashboard OAuth requires matching HTTPS values for `POTATOBOT_DASHBOARD_EXTERNAL_URL` and `DISCORD_REDIRECT_URI`, with the latter ending in `/api/callback`.

The schema is at version 24. Prices, rewards, voice preferences, leaderboards, ranks, rental cleanup, `/work` responses and gacha banners are per guild, with `guild_id` 0 holding the installation default; installation-wide settings live in their own table with no guild dimension at all. Wallets are not: `users` is still keyed by Discord user ID alone, so the live economy is not multi-guild-safe yet and cooldowns are deliberately installation-wide. Per-guild wallets are planned for 3.0.0, and economies shared between guilds for 3.x. The dashboard has one host, `ADMIN_DISCORD_ID`; under the `self_hosted` profile each guild's owner, administrators and Manage Server holders can also manage that guild.

Members can export their data with `/mydata` and erase it with `/deletemydata`; the host can erase on a member's behalf from the dashboard. Erasure keeps the economy row under an anonymous tombstone so installation totals never change silently. See `docs/privacy.md`.

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

- [Installation and deployment guide](docs/installation.md)
- [Setting up levels](docs/level_setup.md)
- [Developer and operations guide](docs/development.md)
- [Localization status and plan](docs/localization_status.md)
- [Keeping Everydle data current](docs/everydle_data_updates.md)
- [Threat model](docs/threat_model.md)
- [Privacy and data lifecycle](docs/privacy.md)
- [2.0 release checklist](docs/release_checklist.md)
- [Changelog](CHANGELOG.md)
- [Localized minigame data format](data/README.md)

## Recent releases

<!-- BEGIN GENERATED: changelog -->
### 2.18.4b

- **Join, leave and boost messages can be written by each server.** The dashboard has a new Announcements page. Every event has its own switch and channel, and its message can have a title, several text variants (one is picked at random), a colour, an image, and plain text above it. Placeholders such as `{user}`, `{server}` and `{member_count}` are filled in. A live preview shows the result, and "Send test" posts the saved message with you as the member. Anything left empty keeps the old text.
- **Join, leave, kick and ban messages always say who it was.** They used to show only a mention, which Discord turns into a bare ID once the member has left. The messages now also give the display name, the username and the ID as plain text, and the new `{user_id}` placeholder lets your own templates do the same. The member's picture is now attached to the message instead of linked, so it no longer disappears when they change it or delete their account. It is not stored anywhere on the server.
- **Settings pages list their fields in a sensible order.** Every page was sorted alphabetically by internal name, which is why the boost message's channel sat at the bottom instead of first. Fields now appear in the order they were designed in: channels first, and related fields together (for example each warning type's threshold, action and timeout side by side).
- **Announcement variants each get their own box.** Instead of one box with a variant per line, every variant is now a multi-line box of its own, and "Add variant" adds another, the way the rules editor adds a section. A message can have up to five. The preview shows the variant you are editing, rendered with markdown, and the plain text above the embed is a multi-line box too.
- **Message previews render nested formatting correctly.** Bold with italic inside (`**bold *and italic* inside**`) used to fall apart and show its asterisks, and `2 * 3 * 4` came out partly italic. Every preview now renders these as Discord does.
- **The dashboard keeps its two languages apart.** Messages it shows you — saved, queued, done, errors — are now always in the dashboard's language; before, some arrived in the bot's language, so one action could answer in Hungarian and then English. Everything that previews what members will see in Discord — the announcement texts, the default buttons on panels, shop item names and the shop preview — is now always in the bot's language, even when the dashboard is set to another one.
- **The Announcements page is easier to use.** Every message editor opens and closes like the sidebar groups and starts closed; the ones you open stay open next time. Empty boxes show the built-in text in grey, and the preview shows it filled in, so it is clear what each message says before you change anything.
- **Kicks and bans are no longer announced as leaves.** A ban used to post a farewell *and* a ban message, and a kick looked like someone leaving. Now each produces one message of its own kind, with the moderator and reason. When the bot runs `/kick` or `/ban`, the moderator who ran it is named, not the bot. Kick and ban messages go to the moderation log unless they are given a channel of their own; **this moves ban messages out of the leave channel.** Telling a kick from a leave needs the View Audit Log permission.
- …and 12 more, in [CHANGELOG.md](CHANGELOG.md).

### 2.18.2b

- **Corrected the 2.18.1b notes.** They said the schema was unchanged, which is wrong: that release adds schema 25, as its first paragraph says.

### 2.18.1b

- **An optional invite log shows who invited each new member.** Turn it on under Features ("Invite log"). It posts to the bot log channel, or to a channel of its own if you tick "Use a separate channel" under Administration → Logging and pick one. The bot needs the **Manage Server** permission, because Discord only lists a server's invites to members who have it. When the inviter cannot be told apart, for example when two people join at the same moment, the log says so instead of guessing. The post never pings anyone, and nothing is stored.
- **Losing dashboard rights takes effect straight away.** When someone loses Administrator or Manage Server, leaves the server, or a role's permissions change, the dashboard re-checks with Discord on their next click. Before, this could take up to 30 seconds. Discord still decides who gets in, and the 30-second check stays as a backstop.
- **The audit log names who made each change**, as `name (id)` instead of a bare Discord id. Names are looked up when the page loads and are never stored. If a name cannot be found, the id is shown on its own.
- **Security updates for two dependencies.** aiohttp, the HTTP library discord.py uses to talk to Discord, moves to 3.14.4 for eight published vulnerabilities, and certifi moves to 2026.10.19 for one. Reinstall from `requirements.lock` with `--require-hashes`; the container image picks them up on its next build.
- **Dashboard actions now run when the bot and the dashboard are separate services.** In the two-service layout (`container/compose.yaml`), posting or updating a message, deleting one, repairing a channel permission, notifying a redeem ticket and erasing a member were queued but never carried out. The bot now processes that queue whether or not it also serves the dashboard. A single-process installation was not affected.
- **Music uses less of the bot's CPU.** ffmpeg now encodes the audio for Discord itself, so the bot process no longer runs the encoder for every server that is playing.
- **A podman unit ships in `deploy/potatobot.container`**, for running the bot as a rootless container under systemd. It publishes the dashboard on loopback only and keeps new files private to the bot's account.
- **The public snapshot must pass its own tests before it is published.** The publishing script now runs the whole test suite inside the snapshot it built, and refuses to publish on any failure.
- …and 1 more, in [CHANGELOG.md](CHANGELOG.md).

The full history is in [CHANGELOG.md](CHANGELOG.md).
<!-- END GENERATED: changelog -->

## Development status

PotatoBot is production-bound for one private deployment, so database compatibility and safe rollout take priority over rapid breaking changes. Remaining major work is guild-scoped wallets (3.0.0) and then shared economies between guilds (3.x), followed by rehearsing a managed deployment. Public releases are sanitised snapshots on the `beta` channel, with no history from the private development line.

New sanitized releases are licensed under AGPL-3.0-only; see [LICENSE](LICENSE).
