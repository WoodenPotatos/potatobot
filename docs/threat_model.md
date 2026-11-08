# PotatoBot threat model

## Assets and trust boundaries

The protected assets are Discord/OAuth/Twitch credentials, the dashboard session
secret, member balances and moderation records, guild configuration, and the
ability to act through the bot's Discord role. Discord, Twitch,
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
- Duplicate or lost money: serialized writes (one advisory lock across
  processes), conditional debits, atomic
  claims, persistent interactive-wager identities, compare-and-set settlement,
  and startup refunds.
- Cross-guild data disclosure: guild-scoped configuration and guild-filtered
  leaderboards, and since 3.0.0 one wallet per member per guild (schema 27),
  so coins, XP and cooldowns earned in one guild cannot be spent in another.
- Resource exhaustion: body limits, request rate limits, command cooldowns,
  bounded in-memory maps/views/transcripts, database worker limits, and rotating
  logs.
- Dependency compromise: exact pins with hashes, weekly Dependabot review, CI
  vulnerability, static and secret scans, and a separate sanitized release
  workflow.

## Residual risks

The database is single-host state (PostgreSQL in a container under the
service account, reachable only on its private network) and is not protected
from an attacker who controls the service account. Wallets are per guild since
3.0.0, but moderation is not shared across guilds and one installation's
operator can read every guild's data; the `DataContext`/`scoped_accounts`
foundation for shared economies is tested but has no runtime caller.
