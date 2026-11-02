# Data inventory (record of processing)

What PotatoBot stores, logs, posts and sends, table by table and sink by sink,
as read from the code on 2026-10-31 (schema 26). This is the engineering record
behind `docs/legal/PRIVACY.md`; the policy is its public summary and must be
re-read whenever a row here changes. GDPR Article 30 asks a controller to keep
a record of this shape; this file is that record for the installations the
maintainer operates. `docs/privacy.md` explains the export and erasure design;
this file lists the facts.

Conventions: **UID** is a Discord user id. **FREE TEXT** is text a member or
staff member typed. **TS** is a timestamp of a member's behaviour. The
canonical declaration of where a member appears is `SUBJECT_TABLES`,
`ERASE_DELETE_ORDER`, `ERASE_REKEY_SUBJECT`, `ERASE_NULL_ACTOR` and
`ERASE_REKEY_ACTOR` in `core/database.py`; a coverage test reads them, so a new
per-member table is added there first and here second.

## 1. Database tables

All DDL is in `core/database.py` (`_create_current_schema`,
`_create_scoped_schema`, `_create_integrity_schema`,
`_create_control_plane_v5_schema`, plus `USER_COLUMNS` applied on every start).
Erasure column: **D** deleted, **R** retained under the tombstone with
behavioural columns blanked, **N** actor column nulled, **T** actor column
re-keyed to the tombstone, **–** holds no member id.

| Table | Member-identifying columns | Free text / behaviour | Erasure | Export |
| --- | --- | --- | --- | --- |
| `users` (installation-wide) | `user_id` PK | balance, xp, level, bj wins/losses, rob/vault modifiers, `bodyguard_until`; TS: `last_daily`, `last_job`, `last_rob`, `last_loldle_*`, `last_valdle`, `last_genshindle`, `last_dbdle_*`, `last_streak_update`, `last_active`; `streak_count`, `inactive_warned`, `rules_read_time` | R (every TS, cooldown, streak and `rules_read_time` blanked) | yes |
| `scoped_accounts` | `user_id` + scope | same columns as `users` | R | yes |
| `voice_settings` | `user_id`, `guild_id` | FREE TEXT `channel_name`; limit, lock, bitrate | D | yes |
| `voice_permissions` | `owner_id`, `target_id` | allow/deny flag | D (either side) | yes |
| `active_channels` | `owner_id` | channel id | D | yes |
| `casino_earnings_window` | `user_id` | TS `window_started_at`, total | D | yes |
| `warnings` | `user_id`, `mod_id`, `guild_id` | FREE TEXT `reason` (staff); `tag`, `date` | D as subject, N as moderator | yes (both roles) |
| `rented_items` | none directly; reached via `timed_entitlements` | item type, Discord item id, expiry | untouched (orphans after the entitlement is deleted) | yes via join |
| `tickets` | `opener_id`, `claimer_id` | `created_at` TS, `ticket_type`, channel id; **no ticket text** | D as opener, N as claimer | yes |
| `shop_prices`, `rewards`, `server_config` | – | configuration | – | – |
| `guilds` | – | guild name, first/last seen | – | – |
| `realms`, `realm_guilds`, `guild_data_scopes` | `created_by`, `approved_by`, `updated_by` (operators) | realm name (operator) | T / N / N | **no** |
| `user_sharing_preferences` | `user_id` | opt-out flag, TS | D | yes |
| `feature_flags`, `guild_settings`, `instance_settings` | `updated_by` (administrator) | setting values; `setting.update` values may embed member ids (`ignored_users`) or operator text | N | **no** |
| `lfg_posts` | `host_id`, `joined_json` (UIDs) | FREE TEXT `game_text`; `created_at` TS | D as host; removed from `joined_json` | yes (host, or member of `joined_json`, matched exactly in Python) |
| `minigame_state` | `last_user_id` | FREE TEXT `value` (current number/word); TS | N | yes |
| `minigame_used_words` | none by design | FREE TEXT `word` (folded), `used_at` | untouched; cleared on chain reset | – |
| `dashboard_access_invalidations` | `user_id` | TS | D; also pruned after 24 h | yes |
| `game_patch_state`, `guild_patch_announcements` | – | public game data | – | – |
| `settings_audit` | `actor_id`; `warning.delete` payload holds subject UID, moderator UID, FREE TEXT reason | `created_at` TS of the actor | T; only `warning.delete` payloads scrubbed; rows older than `audit_retention_days` (default 90, 0 keeps) pruned daily except `PRIVACY_AUDIT_ACTIONS` | yes (as actor, or as subject/moderator inside a `warning.delete` payload) |
| `user_identities` | `user_id` | first/last seen TS | D | yes |
| `activity_events` | `user_id` | category, type, amount, `metadata_json`, TS; only writer is the legacy adoption | D | yes |
| `scope_adoptions` | – | aggregate counts | – | – |
| `casino_wagers` | `user_id` | game, stake, status, outcome, `created_at`/`resolved_at` TS | R | yes |
| `reward_claims` | `user_id` | reward key, `claimed_at` TS | R | yes |
| `shop_item_definitions`, `shop_item_localizations`, `work_responses`, `gacha_banners`, `managed_messages`, `managed_message_entries`, `dashboard_documents` | `updated_by` (administrator) | operator text | N (T for `dashboard_documents`) | **no** |
| `user_inventory` | `user_id` | item, quantity, TS | D | yes |
| `gacha_pity` | `user_id` | pity counters, TS | D | yes |
| `gacha_pulls` | `user_id` | full pull history, TS | D | yes |
| `reward_vouchers` | `user_id` | reward, status, acquired/redeemed/fulfilled TS, Discord item id | D | yes |
| `timed_entitlements` | `user_id`, `granted_by` | key, starts/expires, status, revoked, `discord_missing_since` | D as holder, N as granter | yes (both roles) |
| `fulfillment_requests` | `user_id`, `completed_by`, `ticket_channel_id` | asset type, status, TS | D as requester, N as completer | yes (both roles) |
| `control_actions` | `actor_id`; payloads: `erase_member {user_id}`, `revoke_entitlement {user_id, …}` | action type, status, TS | T; every payload whose `user_id` is the member is re-keyed, whatever its action type; terminal rows pruned after 30 days | yes (as actor, or named in a payload) |

**Automatic pruning**: `lfg_posts` after 8 days (`prune_lfg_posts`,
`LFG_PRUNE_DAYS`), `control_actions` 30 days after settling
(`prune_control_actions`, `CONTROL_ACTION_RETENTION_DAYS`),
`dashboard_access_invalidations` after 24 hours
(`ACCESS_INVALIDATION_RETENTION_HOURS`), `settings_audit` after
`audit_retention_days` (instance setting, default 90, 0 keeps; erasure
records exempt; `prune_settings_audit`, run by the daily sweep). **Nothing
else is pruned**: not `activity_events`, `gacha_pulls`, `casino_wagers`,
`reward_claims`, `warnings` or `user_identities`.

**Retention sweep**: `Privacy.retention_sweep` (`cogs/privacy.py`), daily,
only when some guild resolves `data_retention_days` > 0, using the shortest
window; candidates from `get_retention_candidates` (`last_active` older than
the cutoff, positive id), skipped while present in any served guild; at most
`RETENTION_BATCH` (25) per pass; each erased through `anonymize_user` with
reason `retention_policy`.

**Export**: `export_user_data`, `EXPORT_ROW_LIMIT` 5,000 per table,
`truncated_tables` names the capped ones; the rented-items join is capped but
not flagged. Delivered by `/mydata` as a DM only; 900 s per-user cooldown.

**Erasure**: `anonymize_user`: allocate negative tombstone → refund pending
wagers → delete / re-key / null / scrub → `user.erase` audit row naming the
tombstone and per-table counts. Discord grants are revoked first by
`revoke_all_entitlements`. `/deletemydata` needs one confirmation within 60 s;
the host can queue the same erasure from the dashboard's Privacy page.

## 2. Files outside the database

| What | Where | Content | Retention |
| --- | --- | --- | --- |
| Bot log | `LOG_DIR/bot.log` (`/data/logs` in the container) | see §3 | `RotatingFileHandler` 5 MB × 10 |
| stdout → journald | host journal | same lines | host `SystemMaxUse=500M`; about two months on the reference host |
| Pre-migration backups | `backups/potatobot-v{n}-{timestamp}.dump` in the data directory (a `pg_dump`, mode 0600, since 2.21), and 2.20's `{DB_PATH}.backup-v{n}-{timestamp}` SQLite copies until they are archived | full database copy | deleted by `scripts/offsite_backup.py --prune-local-backups-days N` (both kinds) after the night's offsite copy verifies, by the date in the file name; the deployed units pass 90; the flag defaults to 0 (keep) |
| Offsite backup | `rclone crypt` remote on Google Drive | full database dump (`pg_dump`), encrypted before upload | `--retention-days` 30 |
| Everydle state | `data/everydle_state.json` or `POTATOBOT_EVERYDLE_STATE_PATH` | daily target ids | no member data |
| Welcome-card backgrounds | `cards/` in the data directory (`POTATOBOT_DATA_DIR`, else beside the database; `core/welcome_card.py` `background_path`) | operator-uploaded PNG per guild | until replaced or deleted from the dashboard |
| Session secret | `.dashboard_session_secret` in the data directory, 0600 | random key | until rotated |

## 3. Operational log content

Format `time | level | logger | message`; INFO and above; loggers `discord`,
`PotatoBot.*`, `waitress`.

- **UIDs appear** on: member join registration and autorole lines
  (`cogs/serverevents.py`), music search/extraction failures
  (`cogs/music.py`), an undeliverable export (`cogs/privacy.py`), a support
  ticket open (`core/support_tickets.py`), a failed level-role move
  (`cogs/utils.py`), failed economy writes in `core/database.py` (`user=%s`
  lines), a failed robbery (attacker, victim, amount).
- **Never**: usernames, display names, message content, music search text or
  URLs (the extractor reduces errors to the exception class), IP addresses,
  OAuth data, dashboard bodies, query strings or sessions.
- **Edge cases**: `cogs/music.py` logs the raw playback error, and ffmpeg's
  own stderr (`-loglevel warning`) is not scrubbed, so a stream URL can reach
  the journal; `main.py`'s unhandled-command-error line includes the exception
  text, which can contain argument values; discord.py's default error handler
  logs tracebacks for slash and component errors.
- **Dashboard**: no access log; slow requests (method, path, status, ms),
  unhandled 500s with traceback, rejected permission refreshes. Paths carry
  guild ids and object ids, never UIDs. `request.remote_addr` is only an
  in-memory rate-limit key.
- **Email alerts**: `AlertEmailHandler` (`core/logging_setup.py`) emails every
  ERROR/CRITICAL record, body = formatted line + traceback, de-duplicated for
  600 s, STARTTLS; enabled only with both `POTATOBOT_ALERT_EMAIL_TO` and
  `POTATOBOT_ALERT_SMTP_HOST`.
- **Privacy lines** name only the tombstone, never the subject.

## 4. Discord channel posts naming a member

| Sink | Setting | Content |
| --- | --- | --- |
| Moderation log | `moderation_log_channel` (staff-only by design) | filter alert (mention, channel, matched list entry, counts), escalation alert (mention, tag, counts, outcome, reason), staff level change (admin, member, levels, reason), kick/ban fallback |
| Warn announcement | `warn_announce_channel` (member-readable by design) or invoking channel | mention, tag, reason |
| `/modlogs` | `admin_category` or ephemeral | full warning history, rules-read seconds, days since last active |
| Invite log | `bot_log_channel` or `invite_log_channel` | avatar, mention + username + id, account creation, inviter (same shape), invite code and uses; nothing stored |
| Announcements | per-event channels | mention, username, display name, user id, account age, joined-ago, moderator and reason from Discord's audit log (`on_audit_log_entry_create`, `guild.audit_logs`), re-uploaded avatar, optional image card; nothing stored |
| Ticket transcript | `ticket_logs` (requires full admin) | `[time] display_name: content`, attachment links, header with claimer and close reason; ≤ 50,000 messages, ≤ 5 files; nothing stored |
| Inactivity report | `bot_log_channel` | display name, mention, avatar, 14 days, last-active time |
| Casino rate limit | `casino_rate_limit_alert_channel` | mention, window, cap |
| Kick/ban/timeout | invoking channel + DM | mention, moderator, reason |
| Level-up | `levels_channels` | mention, level |
| Redeem ticket | ticket channel | mention, request id |
| LFG | `games` channel | host, party, full-party notice |

DMs sent by the bot: filter notice, kick reason, ban reason, data export,
erasure receipt. Nothing else.

## 5. Outbound network calls

| Recipient | Trigger | Sent | Stored |
| --- | --- | --- | --- |
| Discord gateway/REST/CDN | always | everything the bot posts; avatar bytes fetched into memory for announcements; `guild.invites()` on join | – |
| Discord OAuth2 + REST (dashboard) | operator action | `identify guilds` scopes; `/users/@me`, `/users/@me/guilds`, token refresh; bot-token lookups of `/users/{id}` (audit actor names, cached 600 s in memory), channels/roles, emoji/sticker/sound names | tokens in memory only |
| YouTube / googlevideo (`core/music_extract.py`, ffmpeg) | member `/play` or search modal | **member's search text verbatim** (`ytsearch1:`) or pasted URL; server IP | in-memory queue |
| Twitch `id.twitch.tv`, `api.twitch.tv/helix/streams` | 5-min loop | app credentials, configured streamer logins | in memory |
| YouTube RSS `www.youtube.com/feeds` | 5-min loop | configured channel ids | in memory |
| Steam, Riot (LoL, Valorant), HoYoLAB, Mojang/minecraft.net | 45-min loop, regardless of flags | nothing personal | `game_patch_state` |
| `raw.githubusercontent.com` | dashboard changelog page | nothing personal | cached 300 s |
| SMTP (optional) | ERROR log record | log line incl. UIDs/guild ids | – |
| Google Drive via rclone (timer) | nightly | encrypted full database | 30 days |
| Everydle sources (`valorant-api.com`, `dbd.tricky.lol`, `genshin-db-api.vercel.app`, `ddragon.leagueoflegends.com`) | weekly drift timer, not the bot | nothing personal | journal only |

Browser side: the dashboard's CSP allows images only from
`cdn.discordapp.com` and `media.discordapp.net`; the visitor's browser fetches
avatars and guild icons from Discord directly. Cloudflare terminates TLS for
the dashboard hostname and sees visitor IPs.

## 6. Dashboard session

Signed client-side Flask cookie: `logged_in`, `user_id`, `display`
(`username`, avatar hash), `authorized_guild_ids`, `full_admin_guild_ids`,
`csrf_token`, `authenticated_at`, `server_session_id`; pre-login
`oauth_state`, `oauth_code_verifier`, optional `dashboard_language`. Flags
`HttpOnly`, `SameSite=Lax`, `Secure` behind https; 10-minute sliding idle
expiry, 12-hour absolute cap. OAuth tokens in `_oauth_tokens` (memory, capped
at 2,048 sessions). In-memory `_permission_cache` (30 s), `_resource_cache`,
`_rate_limit_events` keyed by UID or IP. `localStorage`: theme, language,
open sidebar sections. No login table, no email scope.

## 7. Automated actions against members

Word filter delete + warning; warning thresholds → timeout/kick/ban (all
shipped at 0); casino rate-limit alert (no action); inactivity report (no
action); retention sweep. Discord's own audit log receives the reason for
threshold actions.

## 8. Known gaps (2026-10-31)

Each is either disclosed in the policy or listed in `todo.md` as a fix, or
both:

1. Export omits the `updated_by` operator columns on configuration tables
   (`feature_flags`, `guild_settings`, `instance_settings`, the shop, gacha,
   work-response and managed-message tables) and `realms`/`realm_guilds`
   attribution. These name an administrator, not a member, and are nulled on
   erasure. Not disclosed as an omission; add them if an administrator asks.
2. `settings_audit`: closed 2026-10-31 by `audit_retention_days` (default
   90). A `setting.update` payload holding UIDs (`ignored_users`) now ages
   out with the rest; erasure records are exempt by design.
3. Pre-migration backups: closed 2026-10-31 by the offsite backup unit's
   `--prune-local-backups-days 90`; true on the host once the script copy and
   unit are reinstalled there.
4. `rented_items` rows orphan after erasure (no UID, so not personal data;
   tidiness).
5. `PRIVACY_AUDIT_ACTIONS` lists `user.retention_sweep` but the sweep writes
   `user.erase` with reason `retention_policy`.
6. The host disk is unencrypted; Discord Developer Terms 5(c) expect
   encryption at rest. Disclosed in §14.

Closed the same day: the export now reaches `casino_earnings_window`,
`lfg_posts` (host or party member), `minigame_state`, `settings_audit` (actor
or removed-warning subject) and `control_actions` (actor or payload subject);
the outbox scrub re-keys every payload naming the member rather than one
action type; `/version` links both documents (Developer Terms 5(a)).
