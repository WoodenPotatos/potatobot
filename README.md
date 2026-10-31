# PotatoBot

![PotatoBot avatar](potatobotpfp.png)

Hi! My name is Woody.

This is my self-created and self-hosted Discord bot written in Python. I originally created this project as a vibe coded bot to skip using multiple different bots on my server and to skip paying for features i want. Later it expanded into an AI assisted setup where i wrote most of the things but an AI assisted me in many things. The version you see here however is a way more expanded version which is now heavily AI coder generated.

That being said i would like to share this project with you all whom are weird enough like me and want to skip using big bots and want something that is ours. The bot is still in active development and has way more bugs and problems that i currently have records of so please be aware of them and report anything that you find to help me fix them.

For the moment i won't share any file that is related to the AI in question. The bot's biggest feature is that it has a central web based dashboard where basically everything is modifiable. The features themselves are all toggleable so even tho the bot has more features than a swiss army knife, everyone can customize it to their own needs. I don't and won't have anything locked behind anything. 

The bot's main language is Hungarian but it has a full English localization, and it is quite easy to add other languages. However i would be really happy if you could help me translate to other languages so that i could also add them to the repo for others to use. 

Currently the bot is built for single guild use however it already has the foundation for multi guild usage with some fancy special features in mind. It runs bare metal or as a container: `container/` holds a Containerfile and a compose.yaml for the two-process split, `deploy/` holds the systemd units and a rootless podman Quadlet unit, and CI builds the image on every push.

<!-- BEGIN GENERATED: version -->
**Version 2.19.29b** &nbsp;·&nbsp; channel `beta`

Early access. Expect breaking changes between releases.
<!-- END GENERATED: version -->

<!-- BEGIN GENERATED: body -->
## Quick start

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

## Highlights

- Hybrid prefix and slash commands with explicit public/private response policies
- Atomic economy rewards, transfers, purchases, wagers, and crash-safe game settlement
- Complete Hungarian and English localization of the bot, the dashboard and the
  Everydle minigame data, with Hungarian as the source language
- SQLite WAL storage with ordered, in-place migrations and asynchronous read/write execution
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

The schema is at version 26. Prices, rewards, voice preferences, leaderboards, ranks, rental cleanup, `/work` responses and gacha banners are per guild, with `guild_id` 0 holding the installation default; installation-wide settings live in their own table with no guild dimension at all. Wallets are not: `users` is still keyed by Discord user ID alone, so the live economy is not multi-guild-safe yet and cooldowns are deliberately installation-wide. Per-guild wallets are planned for 3.0.0, and economies shared between guilds for 3.x. The dashboard has one host, `ADMIN_DISCORD_ID`; under the `self_hosted` profile each guild's owner, administrators and Manage Server holders can also manage that guild.

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

### 2.19.29b

- **A stable release channel exists.** The public repository now has a `stable` branch beside `beta`. A stable release is the last published beta copied with its letter stripped (`2.19.26b` becomes `2.19.26`, and `/version` reports the channel as stable), so the code is byte-identical to a beta that has already been out; betas keep landing on `beta`.
- **The README below the introduction is generated from the code.** It said the schema was at version 24 (it is 26) and that LoLdle's hard mode was Hungarian-only (English has been complete since September), and the stable release shipped saying so. Only the maintainer's introduction is written by hand now; the version, the Python range, the intents, the schema version, the read-worker bounds, the gacha pity numbers, the game and feature counts, the documentation list and the recent releases are read from the code and the repository each time, and the build fails when the file disagrees.

### 2.19.26

- **A Privacy Policy and Terms of Service now ship with the bot**, in `docs/legal/`, in English (binding) and Hungarian, written from a line-by-line inventory of every table, log line, Discord post and outbound network call the software makes (`docs/data_inventory.md`). They describe what is stored under a member's Discord id and for how long, what `/mydata` returns and `/deletemydata` keeps, which third parties receive anything (Discord, Cloudflare in front of the dashboard, encrypted nightly backups to Google Drive, a music search's text to YouTube), the dashboard's cookie, and the automatic moderation actions a server can configure. The operator's identity, the effective date and three deployment facts were filled in over the same day; both documents are in force from 2026-10-31 as version 1.0, and reach their public addresses with this release.
- **`/version` and the dashboard's sign-in card link the Privacy Policy and the Terms of Service**, so both are reachable from inside the bot and from the dashboard as Discord's Developer Terms require. Both read the same two addresses, so when the documents move to the planned `welcome.potatobot.win` pages, one edit moves every link.
- **`/mydata` now returns everything an erasure touches.** It missed five places: the casino earnings counter, group-search posts you host or joined, the last turn you took in a counting or word-chain channel, staff audit rows where you are the administrator or the subject of a removed warning, and queued Discord actions about you.
- **Erasure now scrubs every queued Discord action that names you.** It rewrote only the erasure's own queue entry; a revocation of one of your grants queued from the dashboard kept your id for up to 30 days afterwards. Any queued action whose payload names you is re-keyed to the anonymous id.
- **The Privacy Policy is now held to the code by the test suite.** `scripts/legal_drift.py` records the facts the policy describes (which tables hold a member, which hosts the bot contacts, which log lines carry a user id, which loops act on their own, how long a dashboard session lives) and CI fails when any of them changes until the policy has been re-read and re-stamped, naming the section to look at.
- **Members who leave are forgotten after a year.** The maintainer set the installation's data retention to 365 days: a member whose last activity is older than that and who is in none of the bot's servers is erased by the daily sweep, with their balance kept under an anonymous record as always. The Privacy Policy now states the window, and that error-alert email is not enabled on this installation.
- **The Privacy Policy and Terms name their operator**, Máté Domina, resident in Hungary, with the bot hosted in Hungary and Hungarian law governing the terms. The contact address is `privacy@potatobot.win`, forwarded through Cloudflare Email Routing, which the policy's Cloudflare entry now says. The Discord contact is `@woodenpotato`, pre-migration database backups are promised deleted 90 days after their migration, the planned public application is left out until it exists, and the policy keeps the sentence that the host disk is not encrypted.
- **The dashboard's audit history is no longer kept forever.** A new installation-wide setting, "Audit history retention (days)" on the Administration page, deletes staff audit rows (setting changes, warning removals) older than the window each day; it ships at 90, and 0 keeps them forever as before. Data-erasure records are never pruned: they name only the anonymous replacement id and are the proof of what was deleted.
- …and 1 more, in [CHANGELOG.md](CHANGELOG.md).

### 2.19.17b

- **Music logs when a song starts and where in it a gap fell.** "Ran empty" now says how many seconds into the song it happened, and each song logs how long it waited and how much it had buffered, so whether gaps cluster at the start (which a larger reserve would fix) can be read off the log.
- **Queueing songs no longer makes you wait.** The music panel's search and `/play` each had a 15-second cooldown, so queueing a second song meant waiting it out. Now there is no timer: each person can have one lookup running at a time, and the next one can start as soon as it finishes.
- **A song now builds a short reserve before it starts**, about half a second of audio, so the start of a song no longer runs dry for a moment while the stream gets going.
- **Songs no longer start with a sped-up blip.** While a song was still loading, the player counted the wait as falling behind and then played the first half second or so too fast to catch up. It now plays silence on time while the song loads, so it starts at normal speed. A stream that runs short mid-song is now a short gap instead of a gap followed by a rush.
- **Music logs when its player falls behind.** A gap followed by a sped-up rush can come from the stream, from the bot being busy, or from the network to Discord. The log now says which of the first two it was, so the next one can be traced instead of guessed at.
- **The dashboard's and the bot's main files are now type-checked in CI**, as the shared core already was, and a linter (ruff) now catches unused imports, undefined names and the like everywhere. Its 57 findings and mypy's six were all fixed; no behaviour changed.
- **The container is locked down further.** Its root filesystem is now read-only, so the bot cannot change its own code, and files it creates are readable only by its own account, however the container is started (started through compose, it would have written the database readable by every account on the host).
- **Join and leave messages can show an image card.** Switch it on in the event's settings: the member's avatar and name, with a line of your own underneath, drawn on a background image. Upload any picture as the background (it is cropped to fit), or keep the built-in one. The card takes the message's big image spot, and the dashboard shows a live preview. If many people join at once, the bot sends the normal message instead of drawing a card for each of them.
- …and 16 more, in [CHANGELOG.md](CHANGELOG.md).

The full history is in [CHANGELOG.md](CHANGELOG.md).

## Development status

PotatoBot is production-bound for one private deployment, so database compatibility and safe rollout take priority over rapid breaking changes. Remaining major work is guild-scoped wallets (3.0.0) and then shared economies between guilds (3.x), followed by rehearsing a managed deployment. Public releases are sanitised snapshots with no history from the private development line: betas on the `beta` branch, and each stable release on the `stable` branch, which is the last beta copied with its letter stripped.

New sanitized releases are licensed under AGPL-3.0-only; see [LICENSE](LICENSE).
<!-- END GENERATED: body -->
