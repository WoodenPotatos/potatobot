# Changelog

## 2.6.0-beta.1

- **Four casino items, and the loaded die now works in roulette.** A stacked deck
  deals your blackjack hand twice and keeps the better one, a lucky charm spins
  the slots twice and keeps the better payout, and a metal detector marks a
  minesweeper tile safe before you start. All four are buyable and drawable, and
  each is spent only by a paid round that actually resolves — win or lose. Getting
  there meant moving roulette's and slots' outcomes into their settlement
  transactions, which makes those two games atomic whether or not you own
  anything.
- **`/pity`** shows your pity on a banner and the last five 5-stars you pulled,
  with the pity each one landed at. `/profile` gains a pity line too. The data has
  been recorded since the gacha shipped and nothing had ever read it.
- **The item creator is rebuilt.** Four of its six kinds used to hand you an empty
  JSON box and expect you to type a role id and a shape nothing told you about;
  now every kind has real fields, a role picker, and no JSON anywhere. Items can
  be edited rather than only enabled, disabled or deleted.
- **One list of every item.** Built-in items were visible only as a price field
  called "Loaded die price" — there was no way to see what an item does, or which
  ones the gacha can give, without reading the bot's own code. The page now reads
  the way `/shop` does, in your dashboard language, with your own items alongside.
- **A custom item can have English text.** It was stored under Hungarian whatever
  your language setting said, so an English server showed Hungarian for its own
  items while every built-in had both. English is optional per field and falls
  back, so you are never made to translate.
- **Redeems is its own page**, with the queue waiting on staff and a list of
  everything the server is currently granting and how long is left on each. It is
  not tied to the shop feature: a redemption may have come from the gacha, and a
  member has paid for it either way.
- **Redeeming a voucher for an emoji, sticker or sound opens a ticket**, so you
  and the member can agree what to make instead of a request id appearing in a
  queue with no conversation attached. With tickets off it behaves as before.
- Ticket creation lived in two hand-written copies that had already drifted apart;
  there is one now, and rental tickets are finally typed as rentals.

- **`/mydata` never worked.** It acknowledged an interaction the command tree
  had already acknowledged, so it raised on every invocation and the export
  never ran — a member asking for their own data got a command error. A test now
  walks every command body for a second `defer`, because that failure is
  invisible until somebody runs the command.
- **"Message could not be loaded" above the bot's embeds.** `/gacha` declared
  itself a private command, so the tree acknowledged it ephemerally and the
  public result then forced the reply to be deleted and re-sent — leaving
  Discord's "used /gacha" header pointing at a message that no longer existed,
  on every single pull. `/gacha` is a public command now. `/modlogs` had the same
  shape and posts to the channel instead, carrying the requesting moderator in
  the footer. A test forbids the combination outright.
- **Turning the shop off hid the shop editor.** It also hid the fulfillment
  queue — including gacha-sourced requests that members had already paid for —
  because the page was gated on the feature it configures. A feature flag decides
  what members can do, not whether staff can configure it, so those pages stay
  reachable and say the feature is off instead. Six pages had the same wiring.
- **A switched-off feature stopped things expiring.** Premium roles, rented
  emoji, stickers, sounds and timed roles were only revoked while the feature
  that created them was enabled, so turning the gacha or the shop off left
  members holding grants that could never run out. Expiry no longer asks.
- The dashboard could not tell "Discord rejected this token" from "Discord did
  not answer" — both produced the same warning and both kept the session alive
  on its cached permissions. A rejection now ends the session; only an outage
  serves the last known answer, and the log says which it was.
- Logging configuration moved out of `main.py`, so a dashboard running as its own
  service gets the project's format and rotating file instead of whatever
  `waitress` set up for it. Its thread pool went from four to eight, since a
  request can block for the length of a Discord call.

- **Gacha banners can feature a reward.** A banner may mark one 4-star and one
  5-star as featured, and a `featured_split` per banner — shipped at 50 — decides
  how often a rare pull awards it. Lose that split and you draw from the standard
  banner's pool instead, and the *next* rare of that same tier from that banner is
  guaranteed to be the featured one. The two tiers guarantee independently, so a
  4-star loss never spends a 5-star guarantee. The standard banner cannot feature
  anything — it is the pool a loss draws from — and if it is disabled there is no
  split at all: rares then come only from the banner's own table. The reward table
  shows each row's real chance rather than its share of the tier, which is a
  different number entirely once a split is in play, and warns when a featured
  reward is still in the standard pool, because a loss can then award the very
  thing you were chasing. Schema 14, purely additive.
- **`/modlogs` is a staff-channel command now, not an ephemeral one.** Inside the
  configured admin category the reply is public, so every moderator in the room
  sees the lookup; anywhere else it is refused. The gate binds administrators
  too — a channel an administrator can step around is not a boundary, and the
  whole point is that the reply is readable. An installation with no admin
  category configured keeps the ephemeral reply it had.
- **A member who asked to be erased might not have been.** `revoke_entitlement`
  treated any unrecognised entitlement as a soundboard sound and read an id that
  a custom-shop timed role does not have, so `/deletemydata` raised before the
  erasure ran, the daily retention sweep aborted and skipped every later
  candidate, and an operator-requested erasure settled as an internal error. All
  three now complete, and the path finally has tests.
- **Four background loops could die for good.** A `tasks.loop` stops on an
  unhandled exception; the entitlement and rental cleanups start in `__init__`
  and so had no revival path at all, meaning one transient database error stopped
  premium roles and rented assets expiring until a restart. All four are
  supervised now, through one shared helper.
- **Twitch marked a stream announced before it announced it**, so one bad channel
  permission suppressed that go-live for as long as the stream ran — the same
  defect the YouTube path already documented fixing.
- **A dashboard feature toggle could switch on everything else.** After a failed
  startup load, applying one change marked the whole guild loaded, so every other
  feature began answering from its registry default instead of staying disabled.
  A startup read failure also aborted the rest of `on_ready`, which left the
  control-action outbox with nothing draining it.
- Three admin commands swallowed every exception and logged a class name with no
  traceback, and `/update_rules_group` reported failure for a database write that
  had in fact landed. Narrowed, with the write separated from the Discord edit.
- Five unused economy helpers that took a pre-computed balance and wrote it with
  no guard are deleted, and the one live path that discarded a refused debit now
  honours it. Three silent economy readers finally log.
- Smaller fixes: the LFG join button acknowledges before it announces, the No. 1
  role resolves its new holder before stripping the old one, the music panel no
  longer disables the shared persistent view, and two dead casino branches that
  would have become a phantom payout are closed.

- **Three commands were broken and nobody knew.** One refactor removed three
  names and left four references behind: Twitch go-live announcements (which also
  stopped the poll loop), YouTube new-video announcements (silently), `/checkperms`
  and `/manage`. All four raised `NameError` the moment the line ran. Fixed, and
  a new test walks the whole runtime tree with `symtable` for names that do not
  exist — it reports exactly those four against the previous code.
- **Warnings can have their own channel.** `warn_announce_channel` publishes the
  `/warn` embed where members can read it, separately from the moderation log
  that carries the filtered word and the threshold reports. Unset, the warning
  appears where the moderator used the command, exactly as before.
- **`/modlogs` is private to staff.** It was staff-only to run but its reply was
  not ephemeral, so in a public channel it printed a member's whole history,
  every moderator's name, and the account intel the code marks staff-only. The
  public warning embed still names no moderator — which is the split you wanted:
  members cannot see who warned whom, moderators can.
- **The vault glove is a ⚒️ vault drill.** A glove cannot break a vault. Its
  description also overstated the effect — the 25% is added to the pool the steal
  roll is then taken from, not handed over whole — and the lockpick's break
  message used a wrench where the item is a padlock.
- **The dashboard is ready for guild admins.** Installation-wide settings
  (`language`, `currency_emoji`, `maintenance`, `command_prefix`,
  `data_retention_days`) are host-only now; without that, any one guild's admin
  could have stopped the bot everywhere. Guild reads re-check Discord
  permissions too, so a demoted admin loses access in about thirty seconds
  instead of up to twelve hours — and a Discord outage still leaves the dashboard
  readable, because only writes fail closed.
- A rental whose asset is not in this guild is no longer deleted from the
  database while the emoji lives on, and a transient Discord error keeps the row
  for the next pass instead of destroying the record.
- A warning with no guild provenance counts toward no threshold rather than every
  guild's, while staying visible in `/modlogs` and removable. Where the guild is
  unambiguous, the upgrade attributes it.
- Two guilds watching the same streamer or YouTube channel are both notified;
  before, only whichever was polled first ever was. A YouTube video is marked
  seen after the announcement lands, so a failed send is retried.
- Shipping both `.containerignore` and `.dockerignore`, byte-identical. Only the
  first existed, and Docker reads only the second — so a Docker build would have
  baked `.env` into an image layer.

## 2.4.0-beta.1

- **Five public betas shipped with a private heading on the front page.**
  `README.md` renders the three most recent changelog sections, and promotion
  renamed only the top one, so a stale `2.0.0-rc.1 - Unreleased` sat under every
  release and 2.1 and 2.2 were never mentioned at all. Promotion now merges the
  whole leading run of unreleased sections, and the publisher refuses a snapshot
  where an `Unreleased` survives, where the README does not name the release, or
  where it has lost one of its generated blocks.

- **The plain embed sender is a creator like the others.** It was the last thing
  still writing drafts: a name and a JSON textarea, posted and then unreachable
  forever. It is now a list and a creator with 1–10 numbered embeds, a colour, a
  banner image and a live preview — and no buttons, no drafts and nothing else
  around it, because an embed is the message itself. Post it, and it stays
  editable; press Update and the same message changes. It carries **no feature
  toggle**, since there is nothing there anybody would switch off.
- Schema 13 rebuilds `managed_messages` so it can hold an embed. SQLite cannot
  alter a CHECK constraint, so this is a create-copy-drop-rename of the kind
  schema 8 used, gated on the table's own SQL: re-running is a no-op and an
  interrupted upgrade repairs itself. Rehearsed against a copy of the live
  database — every row, every column and every id survives.
- `dashboard_documents` keeps no reader. Dropping the table is a destructive
  migration with nothing to gain, so it stays the way `server_config` does.

- CI installs Node. Four tests drive the dashboard's JavaScript through it and
  skip themselves without a runtime, so all four had been reporting green while
  running nothing — and they are the checks guarding the defects that reached the
  deployment. A test asserts both halves now, because the failure mode of a guard
  is silence.

- **You can take over the panels you have already posted** instead of
  recreating them. Paste a message link into a creator and the bot reads that
  message, fills the form from it and records the link, so Update edits that
  exact message from then on — your rules panel, ticket launcher and entry gate
  stay where they are, with their pins and their place in the channel. The
  schema-12 migration always left this half undone: it says a menu already
  posted keeps working "until it is re-posted **or told which message it is**",
  and only the re-posting half existed.
  Your three role menus need only the link: their content — all 19 role and
  emoji pairs — was already imported by that migration, and a role menu's
  buttons are deliberately *not* read back from the message, because a button
  carries no role id and the database is the only place those live.
  It refuses a message the bot did not post rather than accepting it and failing
  on every Update, refuses a link from another server, and refuses a message
  another item already owns.
- The rules panel takes a **banner image**, which is what `/rules_verify` posts.
  Without it, adopting one of those messages would have stripped the banner on
  the first Update, silently.

- **The `/work` editor says what you may type.** There are two tokens,
  `{earnings}` and `{coin}`, and the hints named only the first, so the currency
  symbol was undiscoverable. The page now lists both with the rules that apply —
  500 characters, line breaks and formatting work, mentions cannot ping anyone,
  and any other braced text stays exactly as written. The hint claiming the
  shipped responses are "not editable" was also out of date; editing one adopts
  that tier into your server.

- **The content builders' Edit buttons did nothing and no creator ever
  appeared** — and it was one missing property, not a missing feature. Every
  picker on the settings form is built from a registry definition; the two in
  the builder editor were written by hand and omitted the locale key the picker
  reads, so building one threw. The throw landed after the card had been emptied
  and before the form was attached, which is why the card looked blank, and it
  aborted the rest of the page load, which is why the New button was missing and
  every Edit click did nothing. `tr` no longer throws on an absent key name, and
  a test walks every hand-written picker definition for what the picker reads.

- **Four content pages, each named for what it makes**: Rules panel, Role menus,
  Ticket launcher, Entry gate. "Panels" held two unrelated systems in one page,
  which is exactly why it read as the same thing as role menus — the real
  difference is that a role menu is the only one whose buttons you write. The
  ticket launcher and the entry gate each get their own menu, and each page says
  in one sentence what pressing its button does.
- **New and Edit open a creator in place of the list**, laid out as numbered
  steps: the message, then the sections or the buttons, then the button. Rules
  sections are numbered blocks you add and remove rather than a count you keep in
  sync. Every field has a hint. Back returns to the list, and asks first if you
  have unsaved text.
- **A preview beside the form**, redrawn as you type: the embeds, the colour, the
  server icon and the buttons with their real labels. It tells you what it cannot
  show — markdown and emoji from another server appear as typed — rather than
  letting you find out after posting. A rules panel also shows a running
  character count, because passing Discord's 6000 fails the whole send.
- **You can write the button text.** All three one-button panels hardcoded their
  label, and the dashboard had been collecting a rules accept-label for a while
  and throwing it away. Leave it empty for the bot's own wording.
- `/setup_tickets` and `/setup_enter` now record what they posted, so a panel put
  up from Discord is visible and editable in the dashboard instead of invisible
  to it — and `/update_enter` stops asking you to copy a message id by hand.
- Fixed a dashboard-published role menu, and the "New" button multiplying: every
  save added another one.

- **Schema 12** gives a posted message an identity. `message_id` appeared
  nowhere in the schema before, so everything the dashboard published was
  fire-and-forget: a draft could be posted a second time but never updated, and
  the bot's own `/update_games` worked only because you typed the id by hand.
  `managed_messages` and `managed_message_entries` record what was posted, where,
  and the buttons it carries. Purely additive — the upgrade adds two tables and
  moves no row — and the three existing role menus are seeded from their settings
  with every pair intact.

- Fixed the settings form reporting three changes nobody made, every single time
  the Role menus page loaded — and still reporting them after a save. Flask sets
  `app.json.sort_keys`, so an entry reaches the browser with its fields in
  alphabetical order, while a row editor rebuilds them in the order its columns
  are declared. For a role menu that is `{emoji, id}` against `{id, emoji}`:
  identical values, different text, and the dirty check compared the text. Every
  Community save therefore also wrote three phantom audit rows and bumped three
  revisions for settings nobody had touched. The comparison is canonical now —
  key order and number-versus-string can no longer manufacture a difference for
  any shape — and when two values still differ the console names which setting
  and shows both sides.
- **Content builders that can edit what they posted.** They were a name and one
  JSON textarea, and the rules type could not express a button at all: its
  validator demanded exactly `{"sections": [...]}` and the worker sent one bare
  embed per section with no view, so it was strictly less capable than
  `/rules_group`. There is a **Content** group now with four pages — Embeds,
  Rules panel, Role menus, Panels — and the last three list what exists with
  Save, Post/Update and Delete. Update edits the message that is already up
  instead of posting a second one, and Delete removes the message with the row.
- The rules panel takes 1–10 sections rather than a hard-coded seven, which is
  Discord's embeds-per-message limit, and carries its colour, the guild-icon
  thumbnail and the accept button as toggles. The 256, 4096 and 6000-character
  limits are checked before you press Post rather than surfacing as a send that
  failed.
- Role menus are the flow you asked for: create one, add rows of label, role and
  emoji, post it, and later add a role and press update. The three menus that
  shipped as fixed settings are ordinary menus now, so a guild may have as many
  as it wants; `/setup_games` and friends still work and write through the same
  row, and `/update_games` and `/update_rules_group` no longer ask you to copy a
  message id out of Discord.
- Fixed a dashboard-published role menu showing **every guild's** buttons. The
  view was built without a guild id, which is the routing constructor, so a
  click on a foreign button answered "role not found".
- **"Games and prices" is gone.** It priced nothing — every price in the registry
  is a shop item price under Economy — and owned one channel, which was the LFG
  channel with no role. That channel is `lfg_default_channel` in a new **LFG**
  category now, and it finally respects the LFG toggle, which it did not before.
  Your stored value moves with the rename.
- Removed the Music page from the sidebar. It owns no settings and no
  sub-toggles, so it has always been hidden — an empty page pretending to be a
  destination. The music switch is on the Features page like every other one. A
  new test walks the sidebar and fails on an entry that cannot render anything,
  which is exactly how the Builders page went missing.
- The published snapshot no longer carries the game icons. `botdata/` holds
  eleven icons uploaded to Discord by hand and read by no code; they are other
  people's artwork and every public release shipped them. The bot's own avatar
  moved to the repository root so it still ships.

## 2.1.0-beta.1 to 2.3.0-beta.1

- These notes cover five public betas — `v2.1.0b1` through `v2.3.0b1` — as one
  block. The changelog was a single running section then and the publisher
  renamed only its top heading, so the per-release boundaries were never
  recorded and inventing them now would be a guess; the tags are the authority
  on what each build contained. The 2.4.0 boundary below it *is* exact, because
  `git blame` can place every line against the commit that wrote it.

- The README is an overview and a quick start again. The verification commands
  and the local-dashboard walkthrough moved to `docs/development.md`, which
  already owned that ground, and it says what it runs on: a headless Linux
  server under systemd behind a reverse proxy, which the README never stated.
  Corrected the stale facts too — it claimed schema 8 while announcing schema 11
  fourteen lines below, called the dashboard experimental where `CLAUDE.md`
  explicitly says it is load-bearing, and said `/work` falls back to shipped
  *Hungarian locale lines* when the shipped set is English database rows.
- `docs/development.md` said schema 6, called the dashboard a typed alpha, and
  documented a `/work` fallback deleted some time ago. `docs/release_checklist.md`
  pinned schema 6 in a step that has to be true at every release, so it names the
  constant now instead of a number.

- A stored gacha banner can pick up rewards the bot shipped after it was saved.
  Nothing ever reconciled the two, so a banner was frozen at the shipped set of
  the day it was first saved — which is how the streak freeze reached the shop
  and the shipped 4-star tier while being unobtainable from the banner a guild
  actually pulls on. **Add missing rewards** appends only what the table lacks and
  leaves your weights and your deliberate omissions alone; **Reset rewards**
  replaces the whole table with the shipped one. Both exist because neither can
  stand in for the other.
- A new banner starts with one placeholder reward per tier instead of a copy of
  all eighteen, so the first thing you do with it is not pruning. It cannot be
  literally empty: a tier can still be rolled and has to have something to award.
- The banner key says what it wants. The message you got was the browser's own
  "please match the requested format", which blocks submitting and so never let
  the server's descriptive message through; the field now carries the same words
  as a hint and a tooltip.
- A reward row on a banner nobody has saved yet is editable, like one you added.
  The synthesised standard banner rendered its rows as committed, which is why
  they behaved differently from your own. And the reward table explains what
  "amount" means, which depends on the kind.

- Casino and Everydle are one master toggle each, with the individual games as
  sub-toggles on their own settings page. Eight near-identical games in the flat
  Features list pushed everything else off it; the list is 27 entries instead of
  35. A child depends on its master, so the existing cascade switches the games
  off with it — `parent` is only where it renders.
- Everydle has its own settings category. Its channel and its five reward
  settings shared a "Games" page with the general other-games channel, which the
  two had nothing to do with beyond both being games.
- Removed the backpack from `/profile`. It showed one legacy flag off a column
  nothing has written since consumables became guild-scoped inventory rows, so it
  read "Empty" for everyone who had not bought a lockpick before that change.
  `/inventory` is the real view, and it is gated on `economy` now rather than on
  the gacha — both the shop and the gacha put items there, so a guild with the
  shop on and the gacha off could not see what it had bought.

- Fixed the settings form marking itself unsaved with nothing touched, and
  refusing to save when it did. Three ways the editor's output could differ from
  what the API sent: an unset role was serialised as the id `"0"`, which the API
  rejects as not a snowflake — and it rejects the whole patch, so one half-filled
  row made every change in that category fail to save; an entry stored without
  one of its fields, or as a legacy bare id, was sent in a shape the editor never
  writes; and a faction's managed roles were compared in the picker's order
  rather than a canonical one. Incomplete rows are now left out and marked as the
  thing to finish, the wire format carries every field of a shape, and role lists
  are sorted on both sides.
- Made the Builders page reachable. It carried a `data-category` that no setting
  declares, and the sidebar hides a category page that owns no settings — so the
  embed, rules and panel builders were hidden on every load while being fully
  implemented. A test now catches a page that renders from its own section but is
  gated on settings anyway.
- Fixed the sidebar's last entry being unreachable on a phone. The height was
  `100vh`, which on a browser with a bottom tab bar is taller than the visible
  viewport, so the foot of the nav sat under the chrome with nothing to scroll.
- Fixed a long label pushing its input out of line. Every field is its own
  column flex box in a grid, so a label that wrapped to two lines dropped its
  input below the neighbouring one.
- The row editors have column headers, so a populated role menu is no longer
  three anonymous boxes — a text cell's placeholder disappears once it has a
  value and a picker never had one. Their add button is styled as a button
  again; it was missing the class that gives it padding, a radius and the page's
  font, and the remove button carried a class defined nowhere.
- The gacha banner selector no longer resizes when you switch banners.

- Renamed the currency in every shipped string. "PC" and "Potatocoin" are Potato
  Empire's coin, and they were in 37 strings per catalog — a dashboard label
  reading "Daily normal PC reward" and a Discord embed reading "1 pull - 5,000 PC"
  ship one guild's fact to every guild. The word is "coins" / "érme" now; the
  symbol stays the `currency_emoji` setting, and a test forbids either name coming
  back.
- Word lists are edited one entry per line. Filtered terms, watched Twitch
  channels, YouTube channel ids and the inactivity ignore list all rendered as a
  raw JSON box, so an empty list showed the two characters `[]` and it was
  anybody's guess whether the brackets and quotes were part of what you type.
- Explained the numbers that cannot be read from their label. A weight is
  relative to its siblings, a tier total is a share of every pull, and the
  duplicate refund applies to exactly one reward kind — each now carries a hint,
  and the three `/work` outcome weights show their computed share the way the
  gacha reward table already did.
- Removed `general_channels`, a configured channel list that nothing has ever
  read, along with the `channels.general` key it mirrored.
- Removed six gacha settings that nothing read. `gacha_roll_cost`,
  `gacha_hard_pity`, `gacha_soft_pity_start`, `gacha_soft_pity_multiplier`,
  `gacha_four_star_guarantee_interval` and `gacha_duplicate_percent` were a
  name-for-name duplicate of the banner's own config, which is what the runtime
  actually reads — so the Economy page carried a second "Gacha" section that could
  disagree with the banner page and change nothing either way. Banners are
  configured in one place now.

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

## 2.0.0-rc.1

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
