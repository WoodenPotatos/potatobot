# PotatoBot

![PotatoBot avatar](potatobotpfp.png)

Hi! My name is Woody.

This is my self-created and self-hosted Discord bot written in Python. I originally created this project as a vibe coded bot to skip using multiple different bots on my server and to skip paying for features i want. Later it expanded into an AI assisted setup where i wrote most of the things but an AI assisted me in many things. The version you see here however is a way more expanded version which is now heavily AI coder generated.

That being said i would like to share this project with you all whom are weird enough like me and want to skip using big bots and want something that is ours. The bot is still in active development and has way more bugs and problems that i currently have records of so please be aware of them and report anything that you find to help me fix them.

For the moment i won't share any file that is related to the AI in question. The bot's biggest feature is that it has a central web based dashboard where basically everything is modifiable. The features themselves are all toggleable so even tho the bot has more features than a swiss army knife, everyone can customize it to their own needs. I don't and won't have anything locked behind anything. 

The bot's main language is Hungarian but it has a full English localization, and it is quite easy to add other languages. However i would be really happy if you could help me translate to other languages so that i could also add them to the repo for others to use. 

Currently the bot is built for single guild use however it already has the foundation for multi guild usage with some fancy special features in mind. Also it is currently a bare metal build, a Docker build is in plans however i need to do some testing and fixing first.

<!-- BEGIN GENERATED: version -->
**Version 2.7.0-beta.2** &nbsp;·&nbsp; channel `beta`

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
### 2.7.0-beta.2

- **The shop item editor follows the kind you pick.** Choosing "Vault" and still being asked which role to grant is fixed properly this time: the redraw was wired through the surrounding form and matched on an attribute, which had already broken twice in the same way, so it is wired straight to the Kind dropdown instead. There is nothing left in between to go wrong.
- **Editing an item now shows what the item is set to.** The fields were the right ones but arrived empty — a vault opened reading "Nothing selected", and saving it wrote that back. All six kinds keep their stored values now.

### 2.7.0-beta.1

- **The five new casino items can be pulled at all now.** The lucky charm, stacked deck, marked card, metal detector and parachute were marked drawable but were never added to the shipped gacha table, so no banner could award them — and because "add missing rewards" compares your banner against that shipped table, it correctly reported nothing missing. The only route to them was "reset rewards", which throws away your own table. They are in the shipped 3-star tier now, so the button offers them.
- **The gacha page's buttons follow the banner you are looking at.** They were built once when you opened the page, so switching banners in the picker left them describing the previous one — a banner that was missing rewards showed no "add missing" button at all.
- **The audit log shows its entries again.** It drew the count in the header and then stopped, leaving the list loading forever, because the code that turns a timestamp into "3 hours ago" used a table that was never defined.
- **Editing a shop item opens the right kind.** Every existing item opened as "Permanent role" asking which role to grant, whatever it actually was — so editing a vault and saving would have turned it into a role grant.
- **Russian roulette.** `/russian` opens a lobby other members join; when the host starts it, one player takes the round and everybody else splits the pot. Up to ten players, and the house takes its 2% off the pot once rather than per player, so a bigger table is better odds and not worse.
- Every ante is its own reserved wager, so a lobby nobody starts costs nothing: the antes come back when it expires, and again at the next start if the bot went down while it was open.
- **Two channel games the bot keeps honest.** A **counting** channel and a **word chain** channel: people play, and the bot removes a message that breaks the rule and tells only its author why, so the channel still reads as the chain. Accents and capitals are ignored when words are compared, and a message that is not an attempt at all is left alone.
- The same person cannot take two turns in a row unless you allow it, two people posting at once cannot both count, and every hundredth turn gets a 🏆.
- …and 11 more, in [CHANGELOG.md](CHANGELOG.md).

### 2.6.0-beta.1

- **Four casino items, and the loaded die now works in roulette.** A stacked deck deals your blackjack hand twice and keeps the better one, a lucky charm spins the slots twice and keeps the better payout, and a metal detector marks a minesweeper tile safe before you start. All four are buyable and drawable, and each is spent only by a paid round that actually resolves — win or lose. Getting there meant moving roulette's and slots' outcomes into their settlement transactions, which makes those two games atomic whether or not you own anything.
- **`/pity`** shows your pity on a banner and the last five 5-stars you pulled, with the pity each one landed at. `/profile` gains a pity line too. The data has been recorded since the gacha shipped and nothing had ever read it.
- **The item creator is rebuilt.** Four of its six kinds used to hand you an empty JSON box and expect you to type a role id and a shape nothing told you about; now every kind has real fields, a role picker, and no JSON anywhere. Items can be edited rather than only enabled, disabled or deleted.
- **One list of every item.** Built-in items were visible only as a price field called "Loaded die price" — there was no way to see what an item does, or which ones the gacha can give, without reading the bot's own code. The page now reads the way `/shop` does, in your dashboard language, with your own items alongside.
- **A custom item can have English text.** It was stored under Hungarian whatever your language setting said, so an English server showed Hungarian for its own items while every built-in had both. English is optional per field and falls back, so you are never made to translate.
- **Redeems is its own page**, with the queue waiting on staff and a list of everything the server is currently granting and how long is left on each. It is not tied to the shop feature: a redemption may have come from the gacha, and a member has paid for it either way.
- **Redeeming a voucher for an emoji, sticker or sound opens a ticket**, so you and the member can agree what to make instead of a request id appearing in a queue with no conversation attached. With tickets off it behaves as before.
- Ticket creation lived in two hand-written copies that had already drifted apart; there is one now, and rental tickets are finally typed as rentals.
- …and 24 more, in [CHANGELOG.md](CHANGELOG.md).

The full history is in [CHANGELOG.md](CHANGELOG.md).
<!-- END GENERATED: changelog -->

## Development status

PotatoBot is production-bound for one private deployment, so database compatibility and safe rollout take priority over rapid breaking changes. Remaining major work includes fully guild-scoping runtime storage, separating dashboard supervision for non-private deployments, rehearsing managed deployment, packaging, and publishing only a sanitized clean-history release.

New sanitized releases are licensed under AGPL-3.0-only; see [LICENSE](LICENSE).
