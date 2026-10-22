# PotatoBot threat model

## Assets and trust boundaries

The protected assets are Discord/OAuth/Twitch credentials, the dashboard session
secret, member balances and moderation records, guild configuration, and the
ability to act through the bot's Discord role. Discord, YouTube/yt-dlp, Twitch,
the reverse proxy or tunnel in front of the dashboard, the private network used
for host administration, package registries, and administrators' browsers are
external trust boundaries.

The bot and the dashboard run in one process or two, decided by
`POTATOBOT_DASHBOARD_ENABLED`. In one process, a dashboard compromise reaches the
same filesystem, database and Discord token as the bot; in two, it still shares
the database. Either way:

- Bind the dashboard to loopback (`127.0.0.1:5000` by default) and publish it
  only through an HTTPS proxy or tunnel, such as a Cloudflare tunnel. State the
  proxy with `POTATOBOT_TRUSTED_PROXY_HOPS` so rate limits see real client
  addresses.
- Expose no host port to the internet. Reach the host for administration over a
  private network such as Tailscale, with SSH keys only.
- Run the service under an account that holds nothing else of value. A rootless
  container under a dedicated account (`deploy/potatobot.container`) also keeps
  the code read-only and every write on the data volume.

Who can sign in to the dashboard depends on the deployment profile. Under
`private` only the host (`ADMIN_DISCORD_ID`) is admitted; under `managed` and
`self_hosted` each active guild's owner, Administrator or Manage Server holder is
admitted for that guild, so those Discord accounts should require two-factor
authentication.

## Primary abuse cases and controls

- Stolen credentials: environment-only secrets, redacted logs, ignored runtime
  files, secure session cookies with an idle and an absolute lifetime, OAuth
  state and PKCE, a session-bound CSRF header, and clean-history public exports.
- Unauthorized guild changes: Discord permission-aware OAuth, guild-scoped
  authorization on every route, host-only instance settings, optimistic
  revisions, typed setting and feature keys, and audit records.
- Duplicate or lost money: serialized SQLite writes, conditional debits, atomic
  claims, persistent interactive-wager identities, compare-and-set settlement,
  and startup refunds.
- Cross-guild data disclosure: guild-scoped configuration and guild-filtered
  leaderboards. Wallets are still one per Discord account across the whole
  installation (see Residual risks).
- Remote media abuse: HTTPS YouTube-only input, no credentials or ports, bounded
  extraction workers/timeouts/playlists/queues, live and duration limits, isolated
  yt-dlp instances, and constrained FFmpeg protocols.
- Resource exhaustion: body limits, request rate limits, command cooldowns,
  bounded in-memory maps/views/transcripts, database worker limits, and rotating
  logs.
- Dependency compromise: exact pins with hashes, weekly Dependabot review, CI
  vulnerability, static and secret scans, and a separate sanitized release
  workflow.

## Residual risks

SQLite is local single-host state and is not protected from an attacker who
controls the service account. Economy state (balances, XP, cooldowns) has no
guild dimension yet, so one wallet per Discord account serves every guild the bot
is in; do not run one installation for unrelated communities until per-guild
wallets ship. The `DataContext`/`scoped_accounts` foundation for that is tested
but has no runtime caller.
