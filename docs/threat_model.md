# PotatoBot threat model

## Assets and trust boundaries

The protected assets are Discord/OAuth/Twitch credentials, the dashboard session
secret, member balances and moderation records, guild configuration, and the
ability to act through the bot's Discord role. Discord, YouTube/yt-dlp, Twitch,
Tailscale or another reverse proxy, package registries, and administrators'
browsers are external trust boundaries.

The bot process currently contains both the Discord client and dashboard WSGI
application. A dashboard compromise can therefore reach the same filesystem and
database as the bot. Bind it to loopback, publish it only through an authenticated
HTTPS proxy, and run the service as an unprivileged dedicated account.

## Primary abuse cases and controls

- Stolen credentials: environment ownership, redacted logs, ignored runtime
  files, stable secure cookies, OAuth state, CSRF tokens, and clean-history public
  exports.
- Unauthorized guild changes: Discord permission-aware OAuth, guild-scoped
  authorization, optimistic revisions, typed feature keys, and audit records.
- Duplicate or lost money: serialized SQLite writes, atomic claims, persistent
  interactive-wager identities, compare-and-set settlement, and startup refunds.
- Cross-guild data disclosure: explicit `DataContext`, guild-local defaults,
  approved realms, user opt-out, and provenance on guild-owned records. Full
  caller migration remains a release blocker in `todo.md`.
- Remote media abuse: HTTPS YouTube-only input, no credentials or ports, bounded
  extraction workers/timeouts/playlists/queues, live and duration limits, isolated
  yt-dlp instances, and constrained FFmpeg protocols.
- Resource exhaustion: body limits, request rate limits, command cooldowns,
  bounded in-memory maps/views/transcripts, database worker limits, and rotating
  logs.
- Dependency compromise: exact direct pins, weekly update review, CI vulnerability
  and static scans, and a separate sanitized release workflow.

## Residual risks

SQLite and `config.json` are local single-host state and are not protected from an
attacker who controls the service account. The dashboard still has legacy raw
configuration routes pending its typed rewrite. Runtime economy callers are not
all routed through scoped accounts yet, so managed multi-guild operation must stay
disabled until the backlog isolation tests pass.
