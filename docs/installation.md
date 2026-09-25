# Installing PotatoBot

From a bare host to a working guild. Written from the deployment that actually
runs it, so the awkward parts are the ones that were awkward in practice rather
than the ones that look awkward on paper.

Roughly forty minutes, most of it waiting for Discord's UI.

**This is early-access software.** The version you install carries a `beta`
channel marker and `/version` says so. Expect breaking changes between releases,
read `CHANGELOG.md` before upgrading, and keep the backups the migration writes.

---

## 1. What you need first

| | |
| --- | --- |
| Python | 3.12, 3.13 or 3.14 |
| FFmpeg | only for music; everything else works without it |
| A host | any Linux box that can stay online; the reference deployment is AlmaLinux 10 |
| HTTPS | required *only* if you want the dashboard — see step 6 |

SQLite ships with Python. There is no separate database server, no Redis, no
message bus: the bot and the dashboard coordinate through one SQLite file.

## 2. Get the code and its dependencies

```bash
git clone https://github.com/WoodenPotatos/potatobot.git /opt/potatobot
cd /opt/potatobot
python3 -m venv venv
./venv/bin/python -m pip install --requirement requirements.lock
```

Use `requirements.lock`, not `pip install .` — the lockfile is what CI tests
against and what the reference deployment runs.

## 3. Create the Discord application

At <https://discord.com/developers/applications> → **New Application**.

**Bot tab.** Add a bot, then **Reset Token** and keep the token somewhere safe;
Discord shows it once. Under *Privileged Gateway Intents* enable:

- **Server Members Intent** — joins, leaves, level roles, member cleanup
- **Message Content Intent** — prefix commands and chat XP

Both are required. The bot requests exactly these two beyond the defaults
(`main.py`), and it will fail to start cleanly without them.

**OAuth2 tab.** Copy the *Client ID* and *Client Secret* — the dashboard needs
them. Leave the redirect URI for step 6; you cannot fill it in correctly yet.

**Invite it.** Build an invite URL with the `bot` and `applications.commands`
scopes. Grant the permissions the features you intend to use need, or grant
Manage Server and narrow it afterwards — the setup check in step 8 will tell you
precisely what is missing, which is easier than guessing up front.

> Do **not** grant Administrator. It works, and it is reported as a warning by
> the setup check for a reason: it silently masks every permission mistake you
> would otherwise be told about.

## 4. Configure the environment

```bash
cp .env.example .env
chmod 600 .env          # it holds your bot token
```

The minimum for a bot with no dashboard:

```dotenv
DISCORD_TOKEN=...
POTATOBOT_DEPLOYMENT_PROFILE=private
POTATOBOT_DASHBOARD_ENABLED=false
POTATOBOT_DB_PATH=/opt/potatobot/economy.db
```

**Set `POTATOBOT_DB_PATH` explicitly.** Leaving it out is legal — the path then
comes from the process's working directory — but it means a command you run by
hand from the wrong directory quietly creates a second, empty database. The
reference deployment omitted it and this is the trap it left behind.

Do not put the same key in twice. `.env` is last-wins, so a stale line above a
correct one works fine until somebody edits the wrong one; that happened on the
reference deployment with `DISCORD_REDIRECT_URI`.

## 5. Create the database

```bash
(umask 027; POTATOBOT_DB_PATH=/opt/potatobot/economy.db ./venv/bin/python update_db.py)
```

This creates the schema, or upgrades an existing one. It is idempotent: running
it twice reports the same version and changes nothing. Every guild and instance
setting starts at its registry default; there is no file to point at your guild
first — you configure it from the dashboard in step 6.

**The bot owns the schema.** If you later split the dashboard into its own
service, the bot must start first and the dashboard must never create the schema.

## 6. The dashboard (optional, but you want it)

You want it. Settings live only in the database, and the dashboard is what edits
them; without it you are limited to whatever the registry's shipped defaults are
for any channel, role or feature flag you never touch.

The dashboard is **never exposed directly**. It binds loopback and sits behind a
reverse proxy that terminates HTTPS. Deployment validation enforces this and
refuses to start otherwise — it rejects a plain-HTTP origin, an external URL with
a path, mismatched origins, a callback that does not end in `/api/callback`, and
a non-loopback bind behind a proxy.

The reference deployment uses Tailscale Serve, which gets you a real certificate
on a private name with no port forwarding:

```bash
tailscale serve --bg http://127.0.0.1:5000
tailscale serve status          # note the https://<host>.<tailnet>.ts.net name
```

nginx or Caddy in front of `127.0.0.1:5000` works identically.

Then in `.env` — **the origin must be byte-identical in all three places**: here,
and in the Discord Developer Portal's redirect URI:

```dotenv
POTATOBOT_DASHBOARD_ENABLED=true
POTATOBOT_DASHBOARD_HOST=127.0.0.1
POTATOBOT_DASHBOARD_PORT=5000
POTATOBOT_DASHBOARD_EXTERNAL_URL=https://your-host.your-tailnet.ts.net
DISCORD_REDIRECT_URI=https://your-host.your-tailnet.ts.net/api/callback
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
ADMIN_DISCORD_ID=your_own_discord_user_id
POTATOBOT_TRUSTED_PROXY_HOPS=1
```

Add that exact `DISCORD_REDIRECT_URI` to **OAuth2 → Redirects** in the portal.

`ADMIN_DISCORD_ID` is you. It is re-read per request rather than baked into a
session, so changing it takes effect immediately.

`POTATOBOT_TRUSTED_PROXY_HOPS` is how many forwarded hops to believe. One proxy
means `1`. Getting it wrong makes every client share the proxy's address as its
rate-limit identity, which turns the login limit into a guild-wide one.

Leave `POTATOBOT_DASHBOARD_SESSION_SECRET` unset and a private secret file is
generated beside the database.

## 7. Run it under supervision

`deploy/potatobot.service` is a starting point. Two things about it:

- It specifies `User=potatobot`/`Group=potatobot`. If you have no such account,
  either create one and `chown -R` the tree, or change those two lines to the
  account that owns it. Do not run it as root.
- It carries the sandboxing (`ProtectSystem=strict`, `ProtectHome`,
  `NoNewPrivileges`, `UMask=0027`, `CapabilityBoundingSet=`,
  `SystemCallFilter=@system-service`, …) with `ReadWritePaths=/opt/potatobot`.
  Keep them. On the reference deployment the first version of this unit moved
  `systemd-analyze security` from 9.2 UNSAFE to 6.2 MEDIUM; the 2026-09
  additions measured **1.9 OK** on the same host. One directive depends on
  *who* the service runs as: `RemoveIPC=true` deletes every System V and
  POSIX IPC object owned by the unit's user when it stops, which is right for
  a dedicated `potatobot` account and wrong for a human login — if you run the
  service as your own user, drop that line from your copy. `UMask=0027` is why the migration command
  above is wrapped in `(umask 027; …)`: the unit's umask covers what the
  *service* writes, and a migration run by hand inherits your shell's instead —
  which is how a backup of every member's balance once landed world-readable.
  After the first start with `SystemCallFilter`, read the journal for `EPERM`;
  a call outside `@system-service` is refused rather than fatal, so a surprise
  shows up as a logged error and not an outage.

```bash
sudo cp deploy/potatobot.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/potatobot.service   # do this
sudo systemctl daemon-reload
sudo systemctl enable --now potatobot
journalctl -u potatobot -f
```

Run `systemd-analyze verify` before enabling. It is how the reference deployment
found that its `StartLimitIntervalSec` was in `[Service]`, where systemd has
ignored it since v230 — the service starts fine either way, which is exactly what
makes the mistake survive.

Expect `Database ready (path=…, schema=N, users=N)` followed by each cog
reporting ready.

## 7a. Or: run it in containers

`container/Containerfile` and `container/compose.yaml` are an alternative to
steps 2, 5 and 7 above — one image, the bot and the dashboard as two services
sharing one `/data` volume, because WAL needs both writers on one filesystem.
Steps 1, 3, 4, 6, 8 and 9 are unchanged: a container still needs the Discord
application, the `.env` values, and the same post-install setup check.

```bash
cp .env.example .env   # then fill in real values, including a
                        # POTATOBOT_DASHBOARD_SESSION_SECRET -- without one,
                        # the fallback file lives outside /data and a rebuild
                        # silently ends every session
podman-compose -f container/compose.yaml run --rm bot python update_db.py
podman-compose -f container/compose.yaml up -d
```

(`docker compose` works the same way if that is what you have.) Expect
`Database ready at schema version N (/data/economy.db)` from the migration
step, then both containers `Up` and the dashboard answering on
`127.0.0.1:5000`. The database, logs and Everydle's daily state all live on
the `/data` volume, so a rebuild never resets them; nothing else in the image
is meant to hold state. There is no container equivalent of the weekly
Everydle drift timer — run it from the host instead, on the same schedule
`deploy/potatobot-everydle-drift.timer` uses:

```bash
podman-compose -f container/compose.yaml run --rm bot python scripts/everydle_drift.py
```

An ad-hoc backup or inspection the way `docs/deployment_host.md` describes for
the bare-metal host works the same way, `sqlite3` ships in the image for it:

```bash
podman exec potatobot-bot-1 sqlite3 -readonly /data/economy.db "PRAGMA integrity_check;"
```

## 8. Check the guild, then configure it

Open the dashboard at your HTTPS origin and sign in with Discord.

Run the **setup check** — the tile on the overview, or `/checkperms` in Discord.
Both run the same diagnostic. It reports:

- permissions a feature needs and the bot does not hold, guild-wide **and per
  channel** — a channel overwrite that denies the bot is invisible to Discord's
  own permission list
- permissions **members** need, which is the half most setups get wrong: a slash
  command does not appear at all where `use_application_commands` is denied
- configured channels and roles that no longer exist
- roles the bot is expected to grant but sits below

Work it until it is clean, then enable features one at a time on the **Features**
page and fill in each one's settings. A finding appears under the field it
concerns, so you can fix as you go.

`level_roles` is the one setting with no sensible default, because a role id
only exists in your guild. See **[level_setup.md](level_setup.md)** for a
ladder that is known to work and the maths behind it.

`shop_gacha` is disabled by default and depends on `economy` and `shop`. Turning
`economy` off takes the gacha with it — the cascade prompt lists what goes.

Then post the messages your members actually press, from the **Content** group.
Each page is a list and a creator: fill it in, pick a channel, press **Post**.
Afterwards the same page edits the message in place — add a role, press
**Update**, and the message already in the channel changes.

| Page | What it posts |
| --- | --- |
| Embeds | An announcement. 1–10 embeds, a colour, a banner, no buttons. |
| Rules panel | Your rules, optionally ending in a button that grants the onboarding role. |
| Role menus | Buttons members press to give themselves a role. |
| Ticket launcher | One button that opens a private channel with your moderators. |
| Entry gate | One button that swaps the onboarding role for the member role — the last step of joining. |

A welcome flow is usually rules panel → role menus → entry gate, each in its own
channel.

**Already posted some of these from Discord?** Do not post them again. Open the
creator, paste the message link into *Take over a message you already posted*,
and the bot reads that message and takes it over where it stands, pins and all.
Only messages the bot itself posted can be taken over — Discord lets a bot edit
nothing else.

## 9. Before you rely on it

- **Back up `economy.db`.** Use `sqlite3 economy.db ".backup out.db"`, not `cp`:
  it is consistent against a running writer.
- **Test the restore.** Open the copy and run `PRAGMA integrity_check`. An
  untested backup is a guess.
- Migrations write their own timestamped backup before the first upgrade. Keep
  them; they are the only rollback for a schema change that rebuilds tables.

## Upgrading

```bash
sudo systemctl stop potatobot
git -C /opt/potatobot pull
./venv/bin/python -m pip install --requirement requirements.lock   # if it changed
(umask 027; POTATOBOT_DB_PATH=/opt/potatobot/economy.db ./venv/bin/python update_db.py)
sudo systemctl start potatobot
```

Pull **before** rehearsing a migration — `scripts/rehearse_migration.py` runs
`update_db.py` from its own checkout, so rehearsing first exercises the old code
and proves nothing. `docs/performance_recovery_plan.md` has the full procedure
with snapshots and comparison.

Restart even when no Python file changed: the command prefix is read once when
the bot object is constructed, so changing it in the dashboard does nothing until
the next start. Everything else converges on its own within a couple of seconds.

## When something is wrong

| Symptom | Look at |
| --- | --- |
| Slash commands missing | Invited without `applications.commands`; re-invite |
| Commands time out (`10062`) | Host DNS. Measure with `curl -4 -w '%{time_namelookup}'`; `?ping` only reports gateway heartbeat and will look fine |
| A command works for you, not for members | Setup check → member findings |
| Level roles do nothing | The bot's role sits below them |
| Dashboard refuses to start | Deployment validation; the message names the exact mismatch |
| Dashboard login loops | The redirect URI differs from the portal by a character |
