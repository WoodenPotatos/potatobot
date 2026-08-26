# PotatoBot Developer and Operations Guide

## 1. Purpose and current maturity

PotatoBot is a Hungarian-first Discord bot for a private community. It combines community administration with economy, profiles, games, music, activity automation, and a web control plane. It runs on a headless Linux server under systemd, behind a reverse proxy.

The current private deployment is the compatibility target. **Schema version 13** is current; `database.LATEST_SCHEMA_VERSION` is the authority and this sentence is not. Since schema 5's typed guild settings, configurable shop definitions, inventory, gacha banners/pity/pull ledgers, fixed vault reserves, vouchers, timed entitlements, fulfillment, builder drafts and dashboard action outbox: schema 6 added the persistent tenth-pull guarantee counter and voucher subsystem ownership, 7 added ticket claim persistence and action leases, 8 put `guild_id` into the last single-tenant primary keys, 9 added banner display names and the `/work` response pool, 10 added the warning tag, 11 added `instance_settings` for values that have no guild dimension, 12 gave a posted Discord message an identity so the dashboard can edit it later, and 13 widened that table so a plain embed is one too — which retired the builder drafts schema 5 introduced, leaving `dashboard_documents` with no reader. Legacy economy callers still remain effectively single-guild — wallets are keyed by Discord user id alone. Treat `managed` and `self_hosted` profiles as architectural foundations, not as proof of completed production isolation.

The dashboard is **load-bearing, not experimental**: it is the only way an operator configures an installation, so a dashboard defect is a bot defect. Its raw JSON and global price/reward endpoints are gone and every setting is edited through a typed control. The remaining release boundary is complete multi-guild storage integration and service separation.

## 2. Repository map

| Area | Responsibility |
| --- | --- |
| `main.py` | Loads environment configuration, initializes the database, creates the bot, loads cogs, starts monitoring tasks, starts the dashboard thread, and connects to Discord. |
| `cogs/` | Commands, persistent controls, Discord event listeners, scheduled tasks, and feature implementations. |
| `cogs/utils.py` | Localization, shared configuration, checks, user updates, level-role handling, and debounced top-ranker reconciliation. |
| `feature_access.py` | Command-to-feature registry, response visibility policy, early interaction acknowledgement, runtime feature cache, and hybrid context behavior. |
| `settings_registry.py` | Typed feature and setting definitions, categories, constraints, dependencies, scopes, defaults, and apply behavior. |
| `database.py` | SQLite schema, migrations, connection policy, transactions, asynchronous executors, and persistence accessors. |
| `deployment.py` | Deployment profile and dashboard origin validation. |
| `permission_audit.py` | One Discord-permission diagnostic shared by `/checkperms` and the dashboard permissions page. Pure data: it takes a guild plus a feature-state map plus resolved settings and returns coded findings. |
| `item_catalog.py` | The single definition of what a built-in item is; the shop and the gacha both derive from it. |
| `bounded.py` | Bounded containers for every transient in-memory map. |
| `dashboard_api.py`, `dashboard/` | Flask API, Discord OAuth, static dashboard client, and legacy host configuration surfaces. |
| `locales/` | General Hungarian and structurally aligned secondary-language catalogs. |
| `data/` | Stable Everydle datasets and game-specific locale catalogs. |
| `botdata/` | Discord-facing images and runtime game state/assets. |
| `tests/` | `unittest` coverage for loading, localization, migrations, transactions, feature policy, deployment, dashboard security, permission auditing, work responses, and server events. |
| `backups/` | Untracked. Timestamped pre-migration database copies written by `initialize_database`; these are the rollback artefacts. |
| `dashboard-reference/` | Untracked design screenshots for the dashboard layout. |

`CLAUDE.md` carries the full annotated directory tree and the invariants a change has to preserve. Read it before touching the registries, the dashboard, or the schema.

`todo.md` is the only active backlog. Completed incident notes and retired TODO files remain available through Git history.

## 3. Startup and runtime lifecycle

Startup follows this order:

1. `main.py` loads `.env` from the repository directory.
2. `deployment.py` validates the deployment profile, dashboard bind address, external URL, and OAuth callback.
3. `database.initialize_database()` creates or migrates SQLite before cogs or the dashboard access it.
4. The dashboard module starts a daemon Flask thread when enabled.
5. `PotatoBot` is created with the required gateway intents and safe default mention policy.
6. `setup_hook` loads every Python cog except utility/package modules.
7. Discord connects; `on_ready` registers guilds, loads feature state, starts the feature-cache poller and event-loop watchdog, performs private-profile legacy adoption when safe, and synchronizes application commands.

The dashboard and bot currently share one Python process and database module. The dashboard is synchronous Flask code in a separate thread; bot database work must go through the async executor helpers so synchronous SQLite access never blocks the Discord event loop.

## 4. Command and interaction lifecycle

Discord requires an initial interaction response within approximately three seconds. PotatoBot acknowledges every non-modal application command in `PotatoCommandTree.interaction_check()` before its callback performs legacy database or network work.

Every hybrid/application command must have a `COMMAND_POLICIES` entry with:

- an owning feature key or `None` for an internal emergency control;
- `public`, `private`, or `modal` response policy.

Public and private commands are immediately deferred with the declared visibility. Modal-first commands are deliberately not deferred because a modal must be their initial response. Do not add callback-level `ctx.defer()` calls to hybrid command bodies.

Hybrid callbacks should call `ctx.send()`. `PotatoContext` completes the deferred response and, when a specific error branch requests visibility different from the command default, removes the placeholder before sending with the requested visibility. Component, select, button, and modal callbacks are new interactions and acknowledge themselves.

When adding a command:

1. Define its localized Hungarian description and synchronize empty secondary-language keys.
2. Add the command to the appropriate cog and help catalog.
3. Add an explicit command response and feature policy.
4. Gate every component, modal, autocomplete, listener, loop, and external action the feature can reach.
5. Test acknowledgement, intended visibility, disabled behavior, cooldown/concurrency behavior, and error branches.

Disabled features must not mutate SQLite, pay rewards, call Discord, or make external network requests. Persistent controls recheck their current feature state on every click.

## 5. Feature system

`settings_registry.FEATURE_DEFINITIONS` is the typed source of truth for feature keys and dependencies. Missing database rows use each registry default; `shop_gacha` is intentionally disabled by default. Feature updates use optimistic revisions and write audit records. Disabling a dependency atomically disables its enabled dependents; enabling a dependent fails while a required feature is disabled.

The bot keeps guild feature state in memory, so command checks do not perform database I/O. Same-process dashboard changes update that cache immediately. A two-second poll compares the lightweight latest audit revision and reloads full feature state only when the revision changed, allowing a separately running dashboard process to converge as well.

Each definition also declares a `group` and its `required_discord_permissions`. The group is the dashboard switcher's section and is declared rather than inferred, because every casino game and every Everydle game depends on `economy` and grouping by dependency put all of them, the shop and the gacha in one block; `FEATURE_GROUP_ORDER` supplies the render order and reaches the client through `/api/settings/registry`. The permission tuple is what `permission_audit` reports against, so each name must be a real `discord.Permissions` flag with a label in both `admin.permission_names` and `dashboard.permission_names`. Disabling cascades transitively in the database, so the dashboard's confirmation prompt walks the dependency graph too rather than listing only direct dependants.

Maintenance mode sits above normal feature flags. Prefix-only owner operations are internal controls rather than public features.

## 6. Database architecture

SQLite uses the path from `POTATOBOT_DB_PATH`, falling back to `economy.db` in the repository. Connections are short-lived, context-managed, use WAL mode, and apply the schema through one owner: `database.initialize_database()`.

Bot access is divided into:

- `database.run_read()`: synchronous read-only accessor on a concurrent pool;
- `database.run_write()`: write or transaction on one serialized worker;
- `database.run()`: compatibility dispatch that sends only explicitly classified functions to the read pool and treats unknown functions as writes.

Read-worker connections are query-only. Never classify an accessor as read-only if it creates missing rows, repairs state, writes timestamps, or otherwise mutates data. Keep Discord and network calls outside database transactions.

The worker count defaults to four and accepts values from two through eight. Tune it only after queue and execution measurements; more SQLite readers are not automatically faster.

Economy changes must remain atomic. Wagers are reserved before games begin, settlement occurs once, transfers update both parties in one transaction, and reward cooldown checks commit together with payouts. Never reintroduce split read/modify/write sequences.

### Schema changes

For every schema change:

1. Add an ordered, transactional, idempotent migration in `database.py`.
2. Keep `update_db.py` on the identical initialization path.
3. Back up the deployed database before migration.
4. Test a clean database, a representative older schema, repeated initialization, and data preservation.
5. Assign every database-mutating test its own temporary `database.DB_PATH` and restore it in teardown.
6. Compare deployed checksum, integrity, schema version, and important row counts before and after the complete suite.

Never require deleting or recreating the live database.

## 7. Configuration and localization

Configuration has four ownership classes:

- Environment/deployment: credentials, database path, profile, dashboard bind/origin, session secret, and proxy policy.
- Instance: language, release metadata, logging, and other host-wide behavior.
- Guild: channels, roles, factions, role menus, rewards, shop prices, social destinations, feature flags, and moderation behavior.
- Runtime/user data: economy, cooldowns, warnings, tickets, rentals, voice settings, and daily-game state.

`cogs.utils.config` is one shared in-memory dictionary loaded from `config.json`. `save_config()` atomically replaces the file and mutates the dictionary in place. Most lookups therefore change immediately, but command prefix, intents, task intervals, import-time persistent labels, Everydle language selection, and OAuth environment settings require reload or restart. Every new setting must declare `live`, `subsystem_reload`, or `restart` behavior.

Hungarian is the complete source language. Every user-visible string belongs in a locale file. When adding a key to `locales/hu.json`, add the same structure to every other catalog but leave new non-Hungarian text empty for a human translator. The same policy applies to `data/*/locales/`.

Run:

```bash
python scripts/sync_locale_keys.py
```

The localization tests reject structure mismatches and inappropriate hard-coded user-visible text.

## 8. Feature areas and background work

The command surface covers help/version, LFG, profiles and leaderboards, economy and transfers, shop, casino games, Everydle games, music, moderation, faction management, tickets, onboarding, role-menu setup, temporary voice rooms, and administrative setup tools.

Background behavior includes:

- Twitch and YouTube checks every five minutes;
- voice reward batching every minute;
- daily inactivity scanning;
- rental cleanup;
- member join/leave, ban, boost, chat activity, and voice-state listeners;
- feature revision polling every two seconds;
- event-loop lag monitoring every second;
- top-ranker Discord role reconciliation coalesced to at most once per guild per 30 seconds.

High-frequency reward paths must batch database work. XP changes mark top-ranker state dirty rather than performing Discord leaderboard work per user.

## 9. Dashboard and OAuth

The private deployment should use one stable HTTPS origin. Flask binds only to loopback, normally `127.0.0.1:5000`, and Tailscale Serve or another trusted reverse proxy terminates HTTPS.

These values must match exactly:

```dotenv
POTATOBOT_DASHBOARD_HOST=127.0.0.1
POTATOBOT_DASHBOARD_PORT=5000
POTATOBOT_DASHBOARD_EXTERNAL_URL=https://potatobot.example-tailnet.ts.net
DISCORD_REDIRECT_URI=https://potatobot.example-tailnet.ts.net/api/callback
POTATOBOT_TRUSTED_PROXY_HOPS=1
```

`POTATOBOT_TRUSTED_PROXY_HOPS` defaults to 1 whenever an external URL is configured, which matches one Tailscale Serve or nginx front end. It controls how many forwarded hops `ProxyFix` honours; without it every request appears to come from loopback and the login rate limit becomes installation-wide. Set it explicitly only for a different topology.

The callback URI must also be registered in the Discord Developer Portal. Raw LAN or Tailscale IP addresses are not substitutes for the canonical browser origin.

Sessions have two expiries. `SESSION_IDLE_TIMEOUT` is ten minutes and is the cookie's own lifetime, refreshed on every request by `SESSION_REFRESH_EACH_REQUEST`, so any interaction slides it forward and an unattended browser logs itself out. `SESSION_LIFETIME` is the twelve-hour absolute cap, measured from the `authenticated_at` recorded at login and enforced by `enforce_absolute_session_lifetime`; a session with no recorded login instant is expired once rather than trusted indefinitely.

Current security behavior includes OAuth state validation, a stable session secret, secure cookie flags under HTTPS, POST logout, session-bound CSRF tokens, strict JSON shapes, optimistic revisions, and host-only deployment controls. OAuth access and refresh tokens remain in server memory rather than the cookie, and non-host mutations refresh current Discord guild permissions. Secrets remain environment-owned.

The dashboard uses the shared typed registry, categorized feature-aware navigation, live Discord selectors, optimistic revisions, audit views, and explicit live/reload/restart indicators. Navigation groups collapse client-side, a category entry hides only when every setting it owns is hidden, and the view leaves a page whose owning feature was just disabled.

The top right holds the account avatar with the guild switcher immediately to its left. Both use one anchored popover helper in `dashboard/script.js`; its outside-click listener is registered a tick after opening so the click that opened the menu cannot immediately dismiss it. The account menu carries appearance, display language and logout. Guild icons come from `_decorate_guilds` in `dashboard_api.py`, which reads `_dashboard_bot.get_guild(...).icon`; nothing is stored, and a dashboard running without a bot simply reports no icon so the monogram fallback applies. `start()` is idempotent, because binding the shell twice would attach every listener twice.

The dashboard display language is a per-browser preference held in `localStorage.potatobot-language` and applied by `dashboard/theme.js` before the first request, so the interface never re-renders on load. It is independent of the `language` instance setting that controls the bot's Discord output. `get_dashboard_locale_catalog` in `cogs/utils.py` merges the requested language over Hungarian and is deliberately not shared with `t()`, which falls back Hungarian to English for the bot.

Each `SettingDefinition` carries a `page`, and the category form renders one `<fieldset>` section per page value rather than a single flat grid. Adding a page value therefore requires a matching `dashboard.pages.<page>` locale key. Settings saves send only the fields the operator actually changed, so one edit no longer bumps every revision in the category or writes an audit row per untouched setting. `collectSettingChanges` computes that set once and is shared by the save handler and `refreshSettingsDirtyState`, which puts the pending count on the apply button and an unsaved marker on each dirty section; a JSON field mid-edit yields *unknown* rather than zero, because zero would disable the only way to save.

A `CHANNEL`/`CHANNEL_LIST` definition also declares `channel_types`, and `resourceSelect` offers only those kinds — a ticket category must be a category, a voice lobby a voice channel. The constraint is presentational: resolving a channel's type needs live Discord state, and a settings save must not start failing while Discord is unreachable. Channel options carry a type glyph and are grouped by their Discord category with `<optgroup>` in Discord's own order, which is why `/api/guilds/<id>/discord-resources` reports `parent_id` and `position`. A stored id absent from the live resource list is kept as a selected option labelled unavailable; dropping it showed "no channel" and the next save silently cleared a working setting.

Three pages have no settings of their own. `GET /api/changelog` parses `CHANGELOG.md` into release sections server-side, because the front end may not use a markup sink and a bullet wrapped across source lines has to be rejoined; it is read from the checkout rather than a repository host, since the page has no outbound allowance and a remote copy could describe a version this installation is not running. `GET /api/guilds/<id>/permissions` runs `permission_audit.build_report` and needs the in-process bot for channel overwrites and role hierarchy, returning 503 in a standalone dashboard rather than a clean result it could not check. `/api/guilds/<id>/work-responses` is the per-guild `/work` response editor.

**Front-end constraints.** The dashboard is served under `default-src 'self'; style-src 'self'; script-src 'self'` with no `font-src`. There are consequently no CDN fonts, no icon webfonts, no inline `<style>` blocks, no `style=` attributes and no inline `<script>`. Icons come from the inline SVG sprite at the top of `dashboard/index.html` and are referenced with `<use href="#ic-…">`; note that the `hidden` attribute is not mapped for SVG elements, so the sprite is hidden by an explicit `svg[hidden]` rule. Display type uses a system condensed stack.

`dashboard/style.css` defines one semantic token set twice: light under `:root` / `:root[data-theme="light"]`, and the original warm palette under `:root[data-theme="dark"]`, with a `prefers-color-scheme` block for viewers who have made no choice. Sidebar and table headers stay dark in both themes. `dashboard/theme.js` loads synchronously in `<head>` so the stored `localStorage.potatobot-theme` value is applied before first paint; the sidebar button cycles system → light → dark.

`dashboard/script.js` builds the DOM with `createElement` and `textContent` only. Discord-supplied guild, channel and role names reach the DOM, so `innerHTML` and every other markup sink stay forbidden, and `tests/test_localization_policy.py` fails the build if one appears. That same test rejects Hungarian prose anywhere under `dashboard/` and verifies every `dashboard.*` key the front end references exists in `locales/hu.json`. Run `python scripts/sync_locale_keys.py` after any locale change: it now removes keys dropped from Hungarian as well as adding new ones, keeping the catalogs structurally identical.

Flask request handlers are synchronous and have no event loop, so dashboard reads use `database.run_read_sync()`. It marks the Waitress thread as a reader for one classified accessor, giving it the same `query_only` connection the read pool uses, and refuses anything absent from `READ_ONLY_OPERATIONS`. Mutations remain on the serialized writer. Before this split every page load took the process-wide write lock several times and stalled the bot's writer.

The **Content** group holds five pages — Embeds, Rules panel, Role menus, Ticket launcher and Entry gate — and every one of them reads and writes `managed_messages`. Each lists what exists and offers Save, Post/Update and Delete against the message it already posted; before schema 12 nothing recorded a `message_id`, so a draft could be posted again but never updated, and schema 13 brought the last of them, the plain embed, into the same system. A message the bot posted earlier can be **adopted** by pasting its link, which is the other half of the sentence the schema-12 seed left open. Publishing still enters the durable outbox; the bot worker rechecks the actor, feature, channel, and bot permissions before a Discord send, and `managed_messages.py` renders the row for both the worker and the bot's own `/setup_*` and `/rules_group`. Shop creation accepts only fixed/timed roles, fixed vaults, approved consumables, non-inflationary repeatable coin bundles, and manual-fulfillment vouchers. Hungarian name and description are mandatory and item keys are immutable. The consumable and vault templates pick their item from `GET /api/item-catalog` instead of hand-written JSON, and the API validates against the same catalog. The gacha editor can add and remove reward rows, not only retune the ones a banner already stores — a guild that has saved a banner keeps its own configuration, so a newly shipped default reward reaches it only through that control. Waitress serves the application, but non-private deployment still requires separating and supervising the dashboard process.

The moderation surface has two destinations, and the split matters: `moderation_log_channel` carries the threshold alerts and the filter's matched term and is staff-only, while `warn_announce_channel` carries the `/warn` embed and declares member permissions because members read it. Unset, `/warn` posts where the moderator typed it. `/modlogs` is ephemeral.

### Managed messages

`managed_messages` is keyed by `(guild_id, kind, menu_key)` with `kind` one of
`role_menu`, `rules`, `ticket`, `airlock`; a role menu's buttons live in
`managed_message_entries`. The invariants are in `CLAUDE.md` under **Managed
Messages and the Content Builders**; the ones that bite while developing are
these.

- `record_managed_post` is not part of a content save. Where a message was
  posted is a fact about Discord, and an edit must not claim the message moved.
- The renderer, not the queue, evaluates the feature gate — a feature can be
  switched off while an action waits in the outbox.
- `RoleMenuView()` with no arguments is the routing instance for
  `bot.add_view()`: it unions every guild's labels and carries no role id.
  `RoleMenuView(guild_id, menu_key)` is one guild's menu. Omitting the guild id
  is how a dashboard-posted menu came to show every guild's buttons.
- A rules panel holds 1–10 sections, because a message holds 10 embeds, and its
  256/4096/6000-character limits are validated before the action is queued.
- `menu_key` is immutable: it is the primary key and it addresses the posted
  message. `display_name` is what an operator renames.
- A button label never touches a `custom_id`. The instance registered with
  `bot.add_view` keeps the shipped label; only the instance built for one posted
  message carries the operator's.
- The creator is `MANAGED_FIELDS` + `MANAGED_KINDS` in `dashboard/script.js`, one
  page per kind. `pack` must emit all eight keys the POST route names — the route
  uses `require_exact_keys`.

### Gacha and shop inventory

`shop_gacha` depends on `economy` and `shop`, defaults off, and owns the dedicated `cogs/gacha.py`.

A guild may run up to 25 banners, the size of a Discord choice list. `banner_key` is the stable identifier written into every pull and pity row and is what `/gacha` addresses; `display_name` (schema 9) is only what an operator reads, and a banner stored earlier renders as its key. `standard` is the installation default: it is the only banner a pull may create on demand and the only one that cannot be deleted, because `/gacha` with no argument resolves to it. Every other banner is created explicitly and starts **disabled**, so a half-filled reward table is never pullable; `set_gacha_banner` and `perform_gacha_pulls` both refuse an unknown key rather than creating one, which is what stops a member naming an arbitrary key and getting a default-priced banner nobody configured. Deleting a banner leaves its pull rows and pity counters in place: pull history is immutable and a member paid for the pity they hold. Saving without a `display_name` keeps the stored one, so editing rewards cannot silently rename a banner. The `/gacha` banner autocomplete is a separate interaction and carries the same maintenance and feature gates as the command.

The standard banner costs 5,000 PC per pull and permits only atomic one- or ten-pull transactions exposed as explicit Discord choices. Base tier totals are 97.8% / 1.6% / 0.6%. Every tenth pull is floored to 4-star or higher. Pulls 76–99 multiply both 4-star and 5-star totals by three and subtract the increase from 3-star; pull 100 guarantees a 5-star. Both counters are stored by guild, user, and banner and evaluated sequentially inside ten-pulls. A 5-star resets only 5-star pity; the tenth-pull cadence remains fixed. Every pull records its banner revision, outcome, and guarantee markers.

The 3-star pool is loaded die (40%), lockpick (10%), and 250/500/1,000/5,000 PC (30%/10%/7%/0.8%). The six 4-star rewards split 1.6% equally; the five 5-star rewards split 0.6% equally. Loaded dice are consumed only by the next valid paid dice wager and roll twice, keeping the higher result. Lockpicks add 15 percentage points. A vault drill exposes 25% of the protected reserve. Both robbery items are consumed on the next otherwise eligible resolved attempt, win or loss.

#### One item catalog, two ways to obtain it

`item_catalog.py` is the single definition of what a built-in item is: its stable key and the effect it applies. `database.SHOP_DEFAULTS`, `BUILTIN_SHOP_KEYS`, the `shop_price_*` settings, the shop cog's menu, and the dashboard's consumable validator all derive from it, so the Shop and Potato Gacha cannot drift into two versions of the same item. Adding an entry there gives the item a price field on the dashboard and makes it selectable in the shop builder and the banner reward picker without touching a second list.

Identity is shared; acquisition is not. Lockpicks, loaded dice and vault drills are one stackable `user_inventory` row whether bought (`purchase_inventory_item`) or pulled — a bought lockpick and a pulled one are the same object. Vaults use the shop's keys in both systems (`small_vault`, `med_vault`, `big_vault`), and `_validated_gacha_config` requires a catalog vault reward to award the catalog reserve, so `big_vault` protects 500,000 PC however it arrived. What differs is the rule for a duplicate: the shop refuses the purchase and charges nothing, while a pull pays the banner's configured compensation. Banners saved before the keys were shared keep `vault_25000`/`vault_500000`; those keys are absent from the catalog, stay valid, and keep their locale entries so recorded pulls remain readable.

`users.rob_bonus` is a finite compatibility path rather than a second lockpick mechanic. No purchase writes it any more; `resolve_robbery` still reads it so an older buyer keeps what they paid for, and clears it on every resolved attempt. It was not migrated because the column has no guild dimension.

Vault protection is a fixed reserve. Schema 5 maps old tiers to 25,000/100,000/500,000 PC. Normal accessible funds are `max(0, balance - reserve)`; a drill adds `0.25 * min(balance, reserve)` before the existing 2–10% robbery roll. A vault replaces only a smaller reserve; duplicate drops return the configured percentage (10% by default). Premium time starts on voucher redemption. Emoji, sticker, and sound time starts only when staff records fulfillment.

Reward vouchers persist their owning subsystem. `cogs/gacha.py` expires only Gacha-created premium and asset entitlements, while `cogs/shop.py` expires custom-shop assets and timed roles. This keeps cleanup working correctly when `shop_gacha` is disabled and prevents two scheduled loops from acting on the same Discord resource.

### Configurable work responses and member display

`/work` reads its outcome odds, payouts and XP from the ten `work_*` typed settings and its response text from the `work_responses` table (schema 9). The three tiers — `normal`, `free`, `high` — are mechanics rather than text and keep English identifiers. The shipped weights are 998/1/1, which reproduce the previous hard-coded one-in-a-thousand chances exactly, and a tier the guild has never touched resolves to the shipped rows at `guild_id = 0`, so a guild can override one outcome's flavour without blanking the others. `database.get_work_responses` does that resolution and returns only what is in effect, which is why the dashboard shows one plain editable list: editing or deleting a shipped line adopts that tier into the guild first, in the same transaction. (An earlier version fell back to `casino.job_*` locale lines and kept a `WORK_DEFAULT_RESPONSE_COUNTS` constant in step with them; both are gone, and `scripts/locale_audit.py` lists `casino.job_` as a retired prefix.) Response text is operator authored and reaches message content, so it goes through `discord.utils.escape_mentions`, and `{earnings}` is substituted with a literal `str.replace` rather than `str.format` so a stray brace cannot raise mid-command. Weights compete inside a tier, so the chance the dashboard shows is a response's share of its own tier.

A guild-facing list must never print a raw member id. `cogs.utils.display_member_name` returns the display name when the account resolves and a guild-salted `blake2s` pseudonym when it does not, which is the case for a member who left between the query and the render. Nothing is written and nothing is deleted, so it reverses itself: the real name reappears as soon as the member resolves again. The guild id is the salt, so the same account carries no recognisable label between guilds.

Interactive blackjack and mines create a durable pending wager in the same transaction that reserves funds. Settlement is compare-and-set; delivery failure refunds immediately, and startup refunds any wager orphaned by a process restart. Music is restricted to HTTPS YouTube input and bounded by isolated extraction workers, a 20-second timeout, 25 playlist entries, 100 queued items per guild, and a three-hour duration limit.

## 10. Deployment and service operation

### Running the bot and dashboard as separate services

The two can share one process or run as two supervised services. Two services is
preferred for anything beyond the private deployment: a dashboard restart never
interrupts Discord, and a dashboard fault cannot take the bot with it.

They coordinate only through SQLite. Feature changes converge by revision polling
within about two seconds, and queued Discord publishes travel through the
`control_actions` outbox, which only the bot executes. Nothing needs a message
bus.

Three things make the split work:

- **One schema owner.** The bot runs `initialize_database()`; the dashboard must
  not also create the schema. Set `POTATOBOT_DASHBOARD_ENABLED=false` for the bot
  process and start it first, and order the dashboard unit `After=potatobot.service`.
- **Discord resources over REST.** `/api/guilds/<id>/discord-resources` was the
  only route needing the in-process bot. It now falls back to the Discord REST
  API using `DISCORD_TOKEN`, cached for `RESOURCE_CACHE_SECONDS`, so a standalone
  dashboard still populates its channel and role selectors. That means the
  dashboard process needs `DISCORD_TOKEN` as well as the OAuth values. Without a
  bot member object the role hierarchy is unknown, so `manageable` falls back to
  Discord's `managed` flag and the real check happens again when a role is used.
- **The `config.json` mirror.** `CONFIG_LOCK` is a thread lock and does not span
  processes. While the legacy mirror still exists, only the dashboard writes it,
  and the bot reloads through `?reloadconfig`. Removing the mirror entirely is
  the outstanding backlog item that closes this properly.

`deploy/potatobot.service` and `deploy/potatobot-dashboard.service` are ready to
install; `Containerfile` and `compose.yaml` package the same split in containers,
with the database on a shared volume rather than inside the image.



The live checkout is under `/opt/potatobot` and runs with `/opt/potatobot/venv/bin/python3`. Use that interpreter for diagnostics; invoking the host `python3` can incorrectly report that `discord` is missing.

The private dashboard pattern is:

```text
Browser -> HTTPS MagicDNS origin -> Tailscale Serve -> 127.0.0.1:5000
                                               |
                                      PotatoBot process
                                      Discord gateway/REST
                                      SQLite WAL database
```

Keep the process under service supervision, keep port 5000 off untrusted interfaces, and restart after environment-variable changes because deployment and OAuth settings are read at import time.

### Reviewing the dashboard without a deployment

`scripts/local_dashboard.py` serves the control plane on loopback with no Discord
and no server. It copies `economy.db` to `.local-dev/economy.dev.db` with its WAL
sidecars, fingerprints the copy, runs `database.initialize_database()` against it
and prints the before/after comparison — so pointing it at a stale copy is also a
migration rehearsal. It then builds a stand-in guild whose channels and roles are
named after the ids `config.json` and `guild_settings` already reference, which is
what makes `_resources_from_bot_cache` and `permission_audit.build_report` work
unmodified, and injects a host session ahead of every other request hook so OAuth
is bypassed.

Two guards matter. It refuses to start when `POTATOBOT_DASHBOARD_EXTERNAL_URL` is
set or the profile is `managed`, and it always binds `127.0.0.1`. And it must not
write the tracked `config.json`, which it would by default: `_legacy_guild_id()`
infers the mirror target from "private profile with exactly one active guild", and
a copy of the server database has exactly one. It therefore sets
`POTATOBOT_LEGACY_GUILD_ID=0` to disable the mirror and repoints
`cogs.utils.CONFIG_PATH` at a throwaway copy. `tests/test_configuration_security.py`
asserts both, and that no development branch exists inside `dashboard_api.py`.

What is not real: channel and role names are labels, channel overwrites are
permissive so overwrite findings cannot appear, and queued Discord publishes stay
pending because no bot consumes the outbox.

### DNS and Discord interaction health

Discord gateway heartbeat latency is not Discord REST latency. `?ping` only reports the former. If all slash commands fail at the initial defer with `discord.NotFound`, measure the host network before rewriting command code:

```bash
curl -4 --connect-timeout 5 -sS -o /dev/null \
  -w 'DNS=%{time_namelookup}s CONNECT=%{time_connect}s TLS=%{time_appconnect}s TOTAL=%{time_total}s HTTP=%{http_code}\n' \
  https://discord.com/api/v10/gateway
```

The August 2026 outage was caused by a retired DNS server listed first in NetworkManager's generated `/etc/resolv.conf`. Lookup fallback consumed roughly three seconds, expiring every Discord interaction. After persistent DNS repair, total curl time fell to about 39 ms.

Manage resolver settings in the active NetworkManager connection and fix the DHCP source; do not make generated `/etc/resolv.conf` immutable. Tailscale DNS acceptance and Tailscale Serve are separate controls.

## 11. Testing and acceptance

### Verification commands

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


The standard local gate is:

```bash
python -m unittest discover -s tests -v
python -m compileall -q . -x './\.git|./venv|./\.venv'
```

Tests must mock Discord and HTTP boundaries. A command addition is incomplete unless cogs load, help coverage remains exact, the command policy registry is complete, and localization structure passes.

For live performance recovery, validate at least:

- `/version` as a simple public command;
- `/bal`, `/daily`, or `/profile` as database-backed commands;
- `/help` or `/shop` as private commands;
- a public command's private validation-error branch;
- a modal-first command;
- a persistent component;
- join and leave announcements;
- a disabled command, component, listener, and scheduled loop;
- voice reward batching and top-ranker reconciliation.

Normal-operation targets are acknowledgement under one second, no-database acknowledgement p95 below 100 ms, simple database command completion p95 below 500 ms, database queue wait below 100 ms, and event-loop lag below 250 ms. The full checklist is in `docs/performance_recovery_plan.md`.

## 12. Security and release boundary

Never commit `.env`, session secrets, databases, backups, logs, tokens, OAuth secrets, or live deployment exports. Review `config.json` because it contains installation-specific IDs and is hot-reloadable.

The private repository history contains a historical token-shaped value. Do not make this repository public or mirror its refs. Export a sanitized working tree into a separate clean-history repository, scan it and all exported refs, remove private deployment data, then add licensing, locked dependencies, packaging, operator documentation, release notes, and stable version tags.

## 13. Documentation and backlog discipline

After a successful change, update documentation when the work creates durable knowledge:

- `CLAUDE.md` for contributor invariants, architecture decisions, deployment lessons, and safety rules;
- `docs/everydle_data_updates.md` for how the minigame datasets drift from the games they describe and what can be automated;
- `docs/config_retirement_plan.md` for the plan to reduce `config.json` to `bot_settings` and move the rest into the database;
- `docs/localization_status.md` for the localization rules, the measured state of every surface, and what selecting a language actually does;
- `docs/development.md` for developer and operator behavior;
- `README.md` for GitHub-facing capabilities and setup;
- `todo.md` for remaining work only.

Do not create per-agent or per-feature TODO files. Delete completed backlog entries instead of retaining them as a changelog; Git history already records completed work.
