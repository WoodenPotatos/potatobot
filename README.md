# PotatoBot

![PotatoBot avatar](potatobotpfp.png)

Hi! My name is Woody.

This is my self-created and self-hosted Discord bot written in Python. I originally created this project as a vibe coded bot to skip using multiple different bots on my server and to skip paying for features i want. Later it expanded into an AI assisted setup where i wrote most of the things but an AI assisted me in many things. The version you see here however is a way more expanded version which is now heavily AI coder generated.

That being said i would like to share this project with you all whom are weird enough like me and want to skip using big bots and want something that is ours. The bot is still in active development and has way more bugs and problems that i currently have records of so please be aware of them and report anything that you find to help me fix them.

For the moment i won't share any file that is related to the AI in question. The bot's biggest feature is that it has a central web based dashboard where basically everything is modifiable. The features themselves are all toggleable so even tho the bot has more features than a swiss army knife, everyone can customize it to their own needs. I don't and won't have anything locked behind anything. 

The bot's main language is Hungarian but it has a full English localization, and it is quite easy to add other languages. However i would be really happy if you could help me translate to other languages so that i could also add them to the repo for others to use. 

Currently the bot is built for single guild use however it already has the foundation for multi guild usage with some fancy special features in mind. Also it is currently a bare metal build, a Docker build is in plans however i need to do some testing and fixing first.

<!-- BEGIN GENERATED: version -->
**Version 2.14.0-beta.3** &nbsp;·&nbsp; channel `beta`

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
POTATOBOT_DB_PATH=$PWD/economy.db ./venv/bin/python update_db.py
./venv/bin/python main.py
```

Runs on a headless Linux server under systemd. Requires Python 3.12–3.14. `config.json` is optional: it is only a fallback for a setting an installation has never saved, and `python scripts/import_config.py` retires it. The full walkthrough — Discord application, intents, OAuth, HTTPS, systemd and the guild setup check — is in [docs/installation.md](docs/installation.md).
<!-- END GENERATED: install -->

## Highlights

- Hybrid prefix and slash commands with explicit public/private response policies
- Atomic economy rewards, transfers, purchases, wagers, and crash-safe game settlement
- Complete Hungarian and English localization of the bot, the dashboard and the
  Valdle/DbDle minigame data, with Hungarian as the source language
- SQLite WAL storage with ordered, in-place migrations and asynchronous read/write execution
- Guild feature flags with dependency checks, audit revisions, and runtime cache refresh
- Moderation, warnings, faction management, tickets, onboarding, role menus, and temporary voice channels
- LoLdle, Valdle, DbDle, blackjack, dice, roulette, slots, and mines
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

## Requirements

- A **Linux server, headless**, supervised by systemd and reached through a
  reverse proxy. That is what it is developed and run on; `deploy/` holds the
  units and `Containerfile`/`compose.yaml` hold a container build. Nothing here
  is written for Windows or macOS.
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

The schema is at version 11. Prices, rewards, voice preferences, leaderboards, ranks, rental cleanup, `/work` responses and gacha banners are per guild, with `guild_id` 0 holding the installation default; installation-wide settings live in their own table with no guild dimension at all. Wallets are not: `users` is still keyed by Discord user ID alone, so the live economy is not multi-guild-safe yet and cooldowns are deliberately installation-wide. Dashboard access still uses a single `ADMIN_DISCORD_ID`.

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
gacha configuration, the content builders, queued Discord publishing, fulfillment, and audit
history. Raw full-JSON configuration and legacy price/reward endpoints are not exposed.

The interface uses a dark sidebar with a light content area: an overview page with
quick actions and counters, feature toggles grouped by dependency, settings split into
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
python -m unittest discover -s tests
python scripts/locale_audit.py --brief
```

Migrations are rehearsed against a copy, never the live file, and the full
procedure — snapshots, row-count comparison and the acceptance matrices — is in
[docs/development.md](docs/development.md) and
`docs/performance_recovery_plan.md`.

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
### 2.14.0-beta.1

- **A word chain now knows Hungarian letters.** `sz`, `gy`, `ny`, `cs`, `ly`, `ty`, `zs`, `dz` and `dzs` are single letters to anyone playing, but the bot compared one character at a time — so after `busz` it deleted `szék` and accepted `zebra`. It now joins words by the letter a player would name, and a doubled digraph like `rossz` ends in `sz` as it should. Servers set to English keep single letters, because `only` and `many` end in `y` there.
- **A word only counts once in a chain.** Repeating a word from earlier is refused with a note saying so; only the word immediately before it was caught until now. `/minigame_reset` clears the used words along with the chain, and a chain already in progress keeps every word it played before this arrived.
- **Only the bot's host can switch maintenance mode.** `/maintenance` stops the bot in every server it is in, and it used to accept anyone with a staff role. It now refuses everyone but the installation's owner, the same rule the dashboard already applied, and says so.
- **Erasing a member now reaches the LFG parties they joined, the last-player mark in the counting and word-chain channels, and the queued erasure itself.** Those records arrived after the privacy work and were being left behind.
- **Text settings have a length, and list settings have a size.** The currency symbol, the command prefix, filter words, ignored users, streamers and channels each carry a limit the form shows and the server enforces, because an over-long currency symbol would have broken every message that prints a balance.
- **Amounts have a ceiling.** `/pay`, `/award`, `/awardall` and every casino stake refuse anything above a trillion with a message, instead of the command silently doing nothing on an absurd number.
- **Moderation commands say why they could not act.** `/kick`, `/ban` and `/timeout` now tell the moderator when the target's role is above the bot's, and a reason can no longer ping roles or overflow the log embed.
- **Cooldowns and daily resets keep the right length across a clock change.** Every timestamp is stored in one clock now, and a `/daily` countdown says how long until local midnight rather than until a moment that shifted with daylight saving.
- …and 3 more, in [CHANGELOG.md](CHANGELOG.md).

### 2.13.1-beta.1

- **The Genshindle grid's headings line up again.** A column was removed from the game a while back but not from the heading row, so everything to the right of "Nem" was labelled with the wrong name — the weekly boss under "Alkat", the version under "Heti boss".
- **The daily bonus now follows the streak you are actually shown.** Losing a streak and starting again still paid the old streak's bonus, so the same "1 nap" could be worth different amounts on different days. A second Everydle game on the same day was also paid one step too high.
- **The coin symbol is no longer printed as a code in game footers.** Crash, higher-or-lower, the wheel and Russian roulette showed `<:potatocoins:…>` instead of the symbol, because a footer cannot draw a custom emoji. A custom symbol is now left out of those footers; a plain one still shows.
- **The gacha can run with the shop switched off.** It never needed the shop — nothing in it reads that switch — so a server can now sell nothing and hand every item out through the gacha. The premium role and the item values stay editable when the shop is off, because the gacha grants those items too. Switching the shop off now stops the shop and rentals, and nothing else.
- **The feature switch says which feature needs which.** Turning something off used to list what would go with it as bare names; each one now names the feature that requires it.
- **The bot notices when it has stopped being connected and restarts itself.** On 31 August the connection to Discord went away at 21:59 and nothing rebuilt it. The process stayed alive and idle, so the server saw a perfectly healthy service and the dashboard kept working — the bot was simply absent from Discord until somebody noticed by hand the next day. A watchdog now checks that the connection is really there and that the bot is still doing work, and ends the process if either has been false for a few minutes, which makes the server start it again within ten seconds.
- The wait before it acts is deliberately several minutes, so a Discord outage or an ordinary reconnection is waited out rather than restarted through.
- **And it can now be asked what it is doing.** The cause of that outage could not be established, because there was no way to see where the bot was stuck. `kill -USR1` now writes every thread's position into the log. See `docs/performance_recovery_plan.md`.
- …and 14 more, in [CHANGELOG.md](CHANGELOG.md).

### 2.10.0-beta.1

- **Your own vouchers and timed roles can be gacha rewards.** Make an item that grants an emoji, sticker or sound, or a role for N days, and put it in a banner. A member wins a voucher and redeems it with `/redeem` as usual — the role lands with its expiry, and an asset opens the staff ticket with the right kind.
- Previously a custom voucher was refused on redemption, because what it was for was worked out from its *name* rather than from the item. It is recorded when you win it now, so it also keeps working if you delete or rename the item afterwards.
- If Discord refuses the role — it was deleted, or sits above the bot — the voucher stays unspent rather than vanishing.
- **Permanent roles are deliberately not offered.** A reward needs a duration, and a permanent role won by chance could not be taken back by anything. A 3650-day timed role does the same job and can still be removed.
- **Genshindle no longer asks about body type.** It sounded like a good clue and was not: five values across 120 characters, most of them the same two, so a guess almost never learned anything from it while it took up a column. Gone from the game and from the weekly update check.

The full history is in [CHANGELOG.md](CHANGELOG.md).
<!-- END GENERATED: changelog -->

## Development status

PotatoBot is production-bound for one private deployment, so database compatibility and safe rollout take priority over rapid breaking changes. Remaining major work includes fully guild-scoping runtime storage, separating dashboard supervision for non-private deployments, rehearsing managed deployment, packaging, and publishing only a sanitized clean-history release.

New sanitized releases are licensed under AGPL-3.0-only; see [LICENSE](LICENSE).
