# Changelog

## 2.2.0-beta.2

- Stopped writing every log line twice. `waitress.serve` calls
  `logging.basicConfig()`, which adds a root handler when nothing else has one,
  so every record that propagated was emitted again in Python's default format;
  and `bot.run()` was configuring the `discord` logger the bot had already set
  up. A 500 MB journal cap was being spent at twice the rate, and any count of
  errors or reconnects in it read double.

- No setting is edited as hand-written JSON any more. The level ladder, the LFG
  map and the faction map were the last three, and they looked like editing a
  config file that happened to live in a form. Each has a row editor now — a
  level and the role it grants, a channel and the role to ping, a faction name
  with its leader role and the roles it manages — built from one engine driven by
  a declared spec rather than four near-copies, and each shape is validated
  server-side so a malformed map is refused instead of failing later inside a
  button callback. The level ladder still accepts a role *name* as well as an id,
  because it always has.

- Retired `config.json` as a source the bot writes. Every cog reads its settings
  through the in-memory cache now, so a dashboard save is visible without
  `?reloadconfig` and a setting can be genuinely per-guild. The mirror is gone
  with it: `_apply_legacy_config_values`, `reconcile_legacy_config_mirror` and
  the lock that made their read-modify-write safe are deleted, and `/maintenance`
  writes the same row the dashboard writes instead of rewriting the file. The
  file survives as the fallback for a setting an installation has never saved,
  which is what makes the one-time import something an operator does when they
  choose rather than a prerequisite for upgrading.
- Fixed three things the conversion turned up. A role-menu button had its role id
  baked in at construction on a view shared by every message, so it could only
  ever be right for one guild; it resolves the role per click now. Both social
  polling loops read one notification channel for the whole installation and now
  iterate guilds. And the inactivity scanner read its log channel outside the
  guild loop, so one guild's channel received every guild's report.
- Fixed the inactivity ignore list, which would have stopped ignoring anyone the
  moment it was saved from the dashboard. It holds Discord ids, is a string list
  because a snowflake cannot cross to a browser as a number, and was compared
  against `member.id` directly — true only while the value came from
  `config.json`, where it is stored as integers. It is compared as ids now, and
  the one-time import converts the legacy shape and says that it did.
- Made YouTube notifications reachable. `socials.youtube_channels` was read by
  the polling loop and existed in neither the registry nor the example config, so
  it always resolved empty — the feature could not be switched on from anywhere.
  It is a typed setting now.

- Gave installation-wide settings a home. **Schema 11** adds
  `instance_settings`, keyed by setting alone, and `language`, `currency_emoji`,
  `maintenance`, `command_prefix` and `data_retention_days` now live there rather
  than being stored per guild while being installation-wide in fact. The scope is
  a structural fact now, not a convention: an instance setting cannot be written
  per guild at all. Pre-existing rows are moved across, and a key that was stored
  for several guilds keeps its most recent value and says which it discarded.
- Added `settings_cache`, so a command path no longer reads SQLite for a setting.
  `is_channel` resolves a setting key through it — one change that moved all 22
  channel gates — and `maintenance_blocks` reads it too. It falls back to
  `config.json` and then the registry default rather than to nothing, which is
  what keeps maintenance failing *open*: an unreadable setting must not be an
  outage, and that is the opposite of how the feature gate fails.
- A dashboard save in a separate process is now visible to the bot without
  `?reloadconfig`. The rows are projected back into the in-process configuration
  on every poll, so every remaining reader is live; a key with no row keeps what
  the file holds, and the file itself is never written by the bot.
- Added `scripts/import_config.py`, which gives every value in `config.json` a
  database row once — idempotent, audited, with a dry run, never overwriting a
  row somebody saved, and writing through the same validated path the dashboard
  uses instead of being a second way in.
- Role menus are edited as rows now, not as hand-written JSON: a label, a real
  role picker and an emoji per entry, with the shape declared in the registry so
  the API refuses a malformed menu instead of storing one that fails later in a
  button callback. The role ids inside that JSON cross the wire as strings, for
  the same reason every other snowflake does.
- A setting that applies to the whole installation now says so on the form. The
  API cannot reject a legitimate save, so an operator changing the language on
  one server's page and finding it changed everywhere had no way to know.

- Added tagged warnings and per-tag consequences. **Schema 10** is purely
  additive: `warnings.tag`, gated on table shape so re-running repairs itself,
  with every existing row keeping a NULL tag and counting under the default one
  — it is what those warnings have always effectively been. `/warn` takes a kind
  from a fixed list, `/modlogs` shows it, and each kind carries its own
  threshold, consequence and timeout length as typed settings. Every threshold
  ships at 0, meaning never act, so an upgrade applies nothing nobody asked for.
- Split alerting from acting: `moderation_warn_alerts` posts a crossed threshold
  to the new moderation log channel, `moderation_warn_actions` applies the
  consequence, and alerting works with actions off. Actions ship **disabled**,
  never touch the guild owner or an administrator, and never attempt somebody
  above the bot. The warning and the count it is compared against are written in
  one transaction, so two moderators warning at once cannot both miss the
  threshold.
- Added a word filter under `moderation_word_filter`, disabled by default. It
  deletes the message, files a warning under a configured kind and lets that
  kind's threshold decide what follows, so escalation exists in one place. Text
  is matched after casefolding, stripping accents, mapping digit substitutions,
  removing separators and collapsing repeats — `B.A.D`, `b a d`, `baaad` and
  `b4d` all fold together. Staff are exempt by permission and by role, the
  matched term is never repeated in a public channel, and the configuration is
  cached per guild because this runs on every message.
- Added the streak freeze to the item catalog, so the shop and the four-star
  gacha tier are two ways to obtain one item. It forgives one day beyond the
  grace an Everydle streak already had, is spent retroactively by the claim that
  needs it rather than by a scheduled job, and is consumed in the same
  transaction as that claim. A longer absence resets the streak and does not
  spend the item.
- A constrained setting's choice labels now come from a prefix the registry
  declares, instead of the dashboard matching on the setting's key — which it
  did for `language`, and which is how an interface starts keeping its own copy
  of a list.


- Added a faction lock to the temporary-room control panel. It denies `connect`
  to `@everyone` and the member role, then grants it back to every role the
  owner's faction claims, behind the new `temporary_voice_faction_lock` feature
  flag — which ships disabled and cascades off with either `temporary_voice` or
  `factions`. A member in no faction is refused rather than locking the room to
  nobody, and `Unlock` withdraws the grants by reading the channel's own
  overwrites, so nothing about how a room was locked has to be stored.
- Split Factions out of the Moderation category. That page held the faction map
  and the inactivity ignore list and was named after neither; Factions is now its
  own sidebar entry and its own feature group, and Moderation keeps the ignore
  list.

- Fixed the shop menu's currency symbol. A Discord select option's label is
  plain text, so a custom emoji rendered there as its raw `<:name:id>`; the
  currency now travels on the option's `emoji=` instead, and is dropped rather
  than guessed when the configured symbol is not something Discord can resolve.
  A test forbids any caller passing `coin` to `t()`, which is how the original
  hard-coded symbol survived the sweep.
- Fixed channel and role pickers being unusable on a phone. Focusing the search
  field opened the on-screen keyboard, the keyboard resized the viewport, and the
  popover closed on any resize — so it dismissed itself immediately. The search
  field is no longer autofocused on a touch device, and a resize repositions the
  popover rather than closing it. A popover anchored to a field in the page is
  now positioned in document coordinates by CSS, so the browser carries it with
  the page instead of a scroll handler chasing it — a fixed surface repositioned
  from JavaScript always trails a compositor-thread scroll. It closes once its
  trigger passes under the sticky topbar rather than floating over the header.
- Navigating between dashboard pages now counts as activity. Pages that render
  from already-loaded state made no request, so the session cookie genuinely was
  not refreshed and the idle countdown was telling the truth; a throttled
  keepalive against a new `GET /api/session/touch` makes navigation refresh it.
  That endpoint touches only the session, at roughly half the cost of
  `/auth/status`, which reads and decorates the guild list.

- Moved the version to `pyproject.toml` as its only source, read at runtime. The
  operator-editable `release_version` and `release_date` settings are gone, along
  with `bot_settings.version` and `bot_settings.release_date`: two sources, one of
  them a form, is why the file said 1.9 while the packaging metadata said
  2.0.0-rc.1.
- Derived the release channel from the version rather than declaring it, so a
  build cannot claim to be stable while carrying a prerelease suffix. `/version`
  now shows the channel and the public repository, and no longer shows a release
  date.
- Added `scripts/bump_version.py`, which refuses a bump that would move the
  version backwards and ties `x`/`y`/`z` to breaking changes, features and fixes
  rather than to how large a change felt.
- Made the currency symbol configurable as the `currency_emoji` instance
  setting, defaulting to a Unicode emoji. It was one guild's custom emoji
  hard-coded in 105 places, so every other installation rendered the literal
  `<:potatocoins:…>` text on every balance, price, payout and `/work` line.
  `t()` substitutes it into a `{coin}` token, so no call site changed.
  **Existing `work_responses` rows keep their current text**: seeding is gated on
  absence, and those rows are operator-authored content rather than shipped
  defaults. Casino embed footers no longer carry a currency icon, because a
  Unicode emoji has no image to point at.
- Added the guild setup check: the permission diagnostic now declares what the
  bot needs per setting rather than per channel kind, and checks what *members*
  need as well — a slash command does not appear at all where
  `use_application_commands` is denied. It also reads the legacy configuration,
  without which it resolved every channel and role to nothing and reported clean
  while checking none of them.
- Added `docs/installation.md` and `docs/level_setup.md`, and stopped shipping
  one guild's role ids as the `level_roles` default.

## 2.0.0-rc.1 - Unreleased

- Added transactional schema-4 wager recovery and atomic booster reward claims.
- Made feature policy fail closed until its guild cache is ready.
- Hardened YouTube extraction, music queues, ticket transcripts, moderation
  validation, interaction permissions, and bounded runtime state.
- Added typed data-scope resolution foundations and explicit legacy-guild
  adoption for multi-guild migration.
- Replaced Flask's development server path with Waitress and added dashboard
  rate limits and response security headers.
- Added pinned dependencies, Python 3.12-3.14 CI, security policy, threat model,
  privacy notes, and release checklist.
- Added schema-5 typed settings, configurable shop templates, fixed vault
  reserves, inventory, vouchers, timed entitlements, fulfillment, builder drafts,
  and a permission-rechecked dashboard action outbox.
- Added toggleable Potato Gacha with atomic one/ten pulls, 75-pull soft pity,
  100-pull hard pity, immutable pull history, loaded dice, robbery gloves, and
  duplicate-vault compensation.
- Moved Potato Gacha into its own cog and added an independent persistent
  every-tenth-pull 4-star-or-higher guarantee plus explicit Discord pull choices.
- Replaced the dashboard raw JSON and global price/reward editors with a
  categorized, feature-aware typed control plane and safe Discord selectors.
- Enforced maintenance mode at every interaction entry point. It previously only
  covered prefix and hybrid commands, so components, modals and native
  application commands could still start paid games and purchases during an
  emergency stop.
- Stopped a nickname from injecting a role mention into the LFG ping, and
  restricted that message to the configured LFG role instead of all roles.
- Refused temporary voice ownership claims when no owner row exists, and kept
  the row when the channel delete fails so provenance is not lost.
- Restored the voice reward loop's automatic restart, which a duplicate error
  handler had silently disabled.
- Stopped the shared persistent ticket view from leaking one ticket's claimer
  into another ticket's transcript.
- Tied custom shop refunds to the amount actually debited and the purchased
  duration rather than a price snapshotted when the menu was rendered.
- Made `is_enabled` fail closed on an unknown feature key, bounded the
  interaction timing map and the daily activity cache, and gave the per-command
  profile views a finite timeout.
- Rejected gacha banners whose soft-pity expansion cannot fit in 100 percent at
  save time instead of failing on every pull from 76 onward.
- Held `CONFIG_LOCK` across the dashboard's whole `config.json` read-modify-write
  so concurrent saves cannot discard each other's keys.
- Routed dashboard reads through a synchronous read path so Waitress threads no
  longer take the database writer lock on every page load.
- Dropped the unused `file` ffmpeg protocol, screened autoroles like every other
  role path, removed the victim's vault reserve from the public robbery embed,
  and stopped unpacking an already-settled wager.
- Rebuilt the dashboard around the reference layout: dark sidebar with a pinned
  footer, light content canvas, condensed uppercase headings, dark-headed tables,
  registry-driven form sections, an overview page, and a light/dark theme toggle.
- Fixed the builders page, which threw on every render because a callback
  parameter shadowed `document`, and let a draft be saved more than once.
- Added front-end error handling for unreachable, non-JSON and unauthorized
  responses, plus empty, loading and no-guild states.
- Moved the account controls to the top right as an avatar menu holding
  appearance, display language and logout, with the guild switcher immediately
  to its left showing each guild's Discord icon.
- Added a per-browser dashboard display language, served merged over Hungarian
  so a partially translated interface never shows raw locale keys. It is
  separate from the instance language that controls the bot's Discord output.
- Reserved the built-in shop keys. A dashboard-defined item named `premium` or
  `big_vault` previously replaced the built-in entry in the live shop menu,
  taking over its purchase handler at an operator-chosen price.
- Replaced the message-matched revision-conflict checks with a typed
  `RevisionConflictError`, so rewording a database error cannot turn a 409
  into a 500.
- Rejected requests now name the reason from the locale catalogs instead of one
  generic message, and malformed field types return 400 rather than escaping as
  unhandled 500s.
- Hardened the dashboard session: an absolute lifetime, rotation on login, host
  status re-derived from ADMIN_DISCORD_ID per request instead of trusted from the
  cookie, and only the user id plus display fields stored in it.
- Added a 30-second permission snapshot cache with bounded, session-keyed eviction
  shared with the server-held OAuth tokens, which previously were only ever
  removed by an explicit logout.
- Added ProxyFix with a configurable trusted-hop count, so the login rate limit is
  no longer installation-wide behind a reverse proxy, and rate-limited reads.
- Added revision-checked edit, disable and delete for custom shop items and
  builder drafts, with confirmations and a 409 that reloads the server's state.
  Built-in keys are not addressable and the stable item key stays immutable.
- Added an action-status endpoint and client polling, so a queued publish reports
  its outcome instead of stopping at "queued", and settled outbox rows are pruned.
- Reduced `/api/locale` to the dashboard namespace, which is all the interface
  reads; it previously disclosed every command and moderation string pre-login.
- Validated embed fields and colour, and the publish channel's guild membership,
  before an action enters the outbox; bounded the fulfillment identifier length;
  moved the shop-item audit row inside its transaction; and added `connect-src`,
  `base-uri`, `form-action` and `object-src` to the dashboard CSP.
- Added schema 7: a durable ticket claimer that survives a restart, a control
  action lease so a slow multi-section publish is no longer re-queued and posted
  to Discord twice, and guild provenance on the voice tables. Purely additive,
  with no row rewritten and a clean rollback to schema 6.
- Added `scripts/db_snapshot.py` and `scripts/rehearse_migration.py` so a
  migration can be rehearsed on a copy and proven not to have lost data, plus
  the deployment procedure and acceptance matrices in the recovery plan.
- Replaced the LICENSE summary with the canonical AGPL-3.0-only text and added a
  sanitized `config.json.example` that carries no Discord identifiers.
- Fixed the failing secret scan. The reported finding was a false positive on the
  gacha reward pool, whose entries use a field named `key`; `.gitleaks.toml` now
  allowlists those identifiers narrowly and nothing else.
- Recorded the credentials that exist in Git history in `.gitleaksignore` and
  `SECURITY.md`, each with a rotation status, and gave the secret scan its own
  job with full history plus a weekly and on-demand run.
- Moved every workflow action off Node 20, which GitHub removes from hosted
  runners on 2026-09-16: checkout v4 to v7, setup-python v5 to v7, and
  gitleaks-action v2 to v3. Pinned the gitleaks version for reproducibility.
- Counted disabled rows against the custom shop item cap, which previously
  counted only enabled rows and so could be exceeded by re-enabling.
- Gave database.py's validators stable reason codes, so gacha, settings and
  voucher rejections name themselves instead of returning one generic message.
- Replaced the gacha rewards JSON textarea with a per-row editor showing each
  reward's in-tier chance, and added a per-row disable flag. Disabled rows keep
  their history and weight but are never drawn, and a tier cannot be emptied.
- Reconciled the config.json mirror against committed typed settings at startup,
  repairing drift left by a failed file write.
- Made the dashboard runnable as its own service: the one route that needed the
  in-process bot now falls back to the Discord REST API. Added a Containerfile,
  a compose file and systemd units for the bot and dashboard separately.
- Gave the Shop and Potato Gacha one shared item catalog. Lockpicks, loaded dice
  and vault gloves are now the same stackable inventory item whichever system
  granted them, and the shop sells all three; gacha vaults use the shop's
  `small_vault`/`med_vault`/`big_vault` keys, so one key always means one
  protected reserve. Each system keeps its own acquisition rule: a duplicate
  vault refuses a purchase and charges nothing, while a duplicate pull still
  pays the configured compensation. The 100,000 PC vault, previously
  shop-only, joins the 4-star pool.
- Retired the shop's column-backed lockpick. Nothing writes `users.rob_bonus`
  any more, but robbery still honours and clears it, so a member who bought one
  before the change keeps it and the column drains; it was not migrated because
  it has no guild dimension. A shop lockpick and a gacha lockpick could
  previously stack to +30% and both be consumed by one robbery.
- Let the dashboard add and remove gacha reward rows, not only retune existing
  ones. A guild that had already saved a banner kept its own configuration
  forever, so a newly shipped default reward could never reach it.
- Gave the shop item builder a catalog-driven picker for the consumable and
  vault templates instead of hand-written JSON, backed by a new
  `GET /api/item-catalog`, and validated the API against the same catalog.
- Derived the custom shop item cap from the Discord select limit rather than
  hard-coding 16. Adding two built-in items would otherwise have allowed a
  26-option menu, which Discord rejects outright, taking `/shop` down for that
  guild. `ShopView` now also trims and logs rather than failing.
- Rejected a banner that renames a shared vault to a different reserve, lists
  one reward twice in a tier, or uses an empty or malformed reward key.
- Gave a guild more than one gacha banner. A banner now has an operator-facing
  name beside its stable key, the dashboard picks which one it edits, and
  `/gacha` takes a banner argument with autocomplete over the enabled ones. A new
  banner starts disabled so a half-filled reward table is never pullable, an
  unknown banner key is refused rather than created — previously naming any key
  conjured a default-priced banner nobody configured — and deleting a banner
  keeps its immutable pull history and the pity members paid for.
- Made `/work` per guild. Its three outcome tiers now draw on configurable
  weights, payout ranges and XP, and each tier's response text is editable from
  the dashboard. A tier with no stored responses keeps the shipped Hungarian
  lines, so overriding one outcome does not blank the others, and the shipped
  weights reproduce the previous one-in-a-thousand odds exactly. Operator text is
  escaped and its `{earnings}` placeholder substituted literally.
- Rewrote `/checkperms` and gave it a dashboard page. Both now run one shared
  diagnostic that reports whether this guild's *enabled* features can actually
  work, instead of comparing five hand-written permission groups. It catches
  three things a guild-wide permission list cannot see: a configured channel whose
  own overwrites deny the bot, a role above the bot's top role while
  `manage_roles` is held, and an integration role nobody can grant.
- Split the feature switcher into declared groups. Every casino game and every
  Everydle game depends on the economy, so grouping by dependency had collapsed
  them, the shop and the gacha into one undifferentiated block. The cascade
  confirmation is now transitive too, so disabling the economy names everything
  it will take with it rather than only its direct dependants.
- Added a ten-minute idle timeout to the dashboard, refreshed by any interaction,
  alongside the existing absolute session cap. An unattended logged-in browser is
  the realistic exposure, and nothing ended such a session before its twelfth
  hour.
- Improved the channel and role selectors. Channel options carry a type glyph and
  are grouped under their Discord category in Discord's own order, each setting
  offers only the channel kinds that can actually work for it, and a stored id
  that no longer resolves is kept as a selected option labelled unavailable —
  before, it rendered as "no channel" and the next save silently cleared a
  working setting. The settings form now shows its pending change count on the
  apply button and marks which sections are unsaved.
- Made every `config.json` value editable from the dashboard, enforced by a test.
  The command prefix, the release version and date, and the per-guild `No. 1`
  role were reachable only by editing the file; the prefix was a literal in
  `main.py`, so it is now read from configuration and applies on restart.
- Added a changelog page to the dashboard, parsed server-side from this file
  because the front end may not use a markup sink.
- Stopped printing a departed member's Discord id on leaderboards. An
  unresolvable account now shows a guild-salted pseudonym instead, derived rather
  than stored, so it reverts to the real display name the moment the member is
  resolvable again.
- Renamed `AGENTS.md` to `CLAUDE.md` and documented the repository layout in it.
  Removed the Aider working files, moved the pre-migration database backups out
  of the repository root into `backups/`, and dropped the space out of the
  design-reference directory name.
- Added `scripts/local_dashboard.py`, which runs the control plane on loopback
  with no Discord and no server: it migrates and fingerprints a copy of the
  database, builds a stand-in guild so the selectors resolve, and signs you in as
  the host without OAuth. It refuses a proxied or managed environment and cannot
  write the tracked `config.json`, which it otherwise would have — the mirror
  target is inferred from "private profile with one active guild", and a copy of
  the server database has exactly one.
- Added `scripts/locale_audit.py` and `docs/localization_status.md`, which measure
  the localization state of the bot, the dashboard and the minigames. The audit
  found that `locales/en.json` is 0% translated rather than the 43% its fill rate
  suggested — 599 values are verbatim Hungarian — that six of those copies
  describe behaviour retired two schemas ago, and that eighteen catalog keys are
  unreachable, several of them error messages whose failure path now tells the
  user nothing.
- Enforced the localization rules a grep cannot check. `tests/test_locale_coverage.py`
  verifies every key the code composes at runtime from a registry, an enum or a
  reason code, and fails when a user-visible string literal appears outside a
  locale catalog. CI runs the audit as a gate.
- Made English a first-class language. The maintainer lifted the
  no-generated-translations rule for English, so `locales/en.json` and the Valdle
  and DbDle minigame catalogs are now complete: 1393 general keys and 200
  minigame strings, up from 0% actually translated. Hungarian stays primary and
  every language after English keeps the old human-translator rule.
- Left `data/loldle/` untouched at its maintainer's request. LoLdle is therefore
  unavailable when the language is English, which the command reports clearly
  rather than failing oddly; it is recorded as the single, tested exception.
- Rewrote `t()`. It now resolves *requested language → English → Hungarian* and
  treats a present-but-empty value as a miss, so a partly translated catalog
  degrades to readable text instead of blank embeds and blank button labels. A
  missing format argument is logged as its own error and returns the unformatted
  template, rather than being caught by the missing-key handler and silently
  returning an empty string.
- Constrained the `language` setting to languages the installation can actually
  speak, with a dashboard dropdown instead of a text box. It was free text, so an
  operator could select a language that blanked most of the bot and disabled
  every Everydle game.
- Corrected six English strings that described behaviour retired two schemas ago
  — the single-use +10% lockpick and the three percentage vaults — and removed
  the `{vault}` placeholder one of them still carried, which would have raised on
  `.format()` and re-exposed the victim's vault reserve.
- Deleted eighteen unreachable catalog keys and localized four Discord audit-log
  reasons. Nine of the keys duplicated `general.cmd_*`; the other nine were error
  messages superseded by variants that deliberately do not quote the exception
  text, so none may be revived.
- Made `scripts/local_dashboard.py` start cleanly from any interpreter. It now
  runs its whole preflight before writing anything, re-executes into the
  repository's virtual environment when the current interpreter lacks the
  dependencies, and reports a busy port instead of failing with a bare
  `Address already in use` after the database had already been migrated.
- Implemented the Everydle data-currency plan. `scripts/everydle_drift.py` compares
  the Valdle and DbDle datasets with their upstream sources and reports what
  changed; `scripts/everydle_propose.py` turns a finding into a reviewable patch
  and applies it after refusing three silent failures — an unfilled field, a new
  attribute label with no text in every language, and an entity that already
  exists. A systemd timer runs the check weekly. Nothing edits the datasets
  without a person, because the sources carry the game data but never the lore.
- Corrected the drift the check found. Valdle gained the agent **Miks** and is now
  in step with upstream; DbDle gained the killer **The Slasher**, `the_mastermind`
  gained the height it was missing, and `The Onryō` became an alias of
  `the_onryo` rather than a rename — renaming an entity id re-draws the day's
  answer for anyone mid-game.
- Left League of Legends data untouched, and made the tooling refuse `loldle` by
  name rather than by configuration, so it cannot be pointed there by mistake.
- Corrected the Dead by Daylight movement speeds and made them track the game.
  The Blight's base was 4.6 m/s when he is a 110% killer at 4.4; The Shape's and
  The Pig's labels now list the base first as every other killer's does, which
  also removed the `4.6m/s` typo that read the same as another label but compared
  as different. `SOURCE_AUTHORITATIVE` now marks speed, terror radius, height and
  agent role as fields the game owns, so a buff or a nerf is proposed as a patch
  rather than reported as an argument, and `merge_base_value` keeps the
  power-state speeds a person maintains.
- Recorded three deliberate gender divergences from upstream instead of reporting
  them every run. `ACCEPTED_DIVERGENCES` carries a reason for each and they no
  longer count as findings, so the drift report is silent when nothing is wrong.
- Moved every `/work` response into the database. The responses used to live in
  the shipped locale catalog, which meant one installation's jokes travelled with
  every copy of the bot. The defaults are now generic English rows at
  `WORK_DEFAULT_GUILD_ID`, a guild's own rows replace them per tier, and the
  dashboard shows the defaults read-only with a control that copies them in for
  editing. `scripts/import_work_responses.py` moves the original Hungarian set
  into the guild it belongs to.
- Registered `level_roles`. The level milestones were read from a key that was in
  neither `config.json` nor the registry, so they were the one setting reachable
  from nowhere. The registered default reproduces the hard-coded milestones
  exactly, a value may be a role id or a role name, and malformed operator JSON is
  now skipped with a warning instead of raising inside the level-up path.
- Gave `level_roles` the deployment's own nine role ids as its default, so the
  level milestones resolve by id instead of by role name and survive a rename.

