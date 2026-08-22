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
| HTTPS | required *only* if you want the dashboard — see step 7 |

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
them. Leave the redirect URI for step 7; you cannot fill it in correctly yet.

**Invite it.** Build an invite URL with the `bot` and `applications.commands`
scopes. Grant the permissions the features you intend to use need, or grant
Manage Server and narrow it afterwards — the setup check in step 9 will tell you
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

## 5. Point it at your guild

```bash
cp config.json.example config.json
chmod 640 config.json
```

Every `0` is a placeholder for a Discord id. You do **not** have to fill them in
by hand — almost all of them are editable from the dashboard once it is running,
which is far less error-prone. If you are running without a dashboard, fill in at
least the channels for the features you enable.

`level_roles` is the one thing with no sensible default, because a role id only
exists in your guild. See **[level_setup.md](level_setup.md)** for a ladder that
is known to work and the maths behind it.

## 6. Create the database

```bash
POTATOBOT_DB_PATH=/opt/potatobot/economy.db ./venv/bin/python update_db.py
```

This creates the schema, or upgrades an existing one. It is idempotent: running
it twice reports the same version and changes nothing.

**The bot owns the schema.** If you later split the dashboard into its own
service, the bot must start first and the dashboard must never create the schema.

## 7. The dashboard (optional, but you want it)

Skip this and everything is configured by editing `config.json`. With it, every
channel, role, price and feature flag is a form.

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

## 8. Run it under supervision

`deploy/potatobot.service` is a starting point. Two things about it:

- It specifies `User=potatobot`/`Group=potatobot`. If you have no such account,
  either create one and `chown -R` the tree, or change those two lines to the
  account that owns it. Do not run it as root.
- It carries the sandboxing (`ProtectSystem=strict`, `ProtectHome`,
  `NoNewPrivileges`, …) with `ReadWritePaths=/opt/potatobot`. Keep them. On the
  reference deployment adopting this unit moved `systemd-analyze security` from
  9.2 UNSAFE to 6.2 MEDIUM.

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

## 9. Check the guild, then configure it

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

`shop_gacha` is disabled by default and depends on `economy` and `shop`. Turning
`economy` off takes the gacha with it — the cascade prompt lists what goes.

## 10. Before you rely on it

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
POTATOBOT_DB_PATH=/opt/potatobot/economy.db ./venv/bin/python update_db.py
sudo systemctl start potatobot
```

Pull **before** rehearsing a migration — `scripts/rehearse_migration.py` runs
`update_db.py` from its own checkout, so rehearsing first exercises the old code
and proves nothing. `docs/performance_recovery_plan.md` has the full procedure
with snapshots and comparison.

Restart even when no Python file changed: the command prefix is read from
`config.json` when the bot is constructed.

## When something is wrong

| Symptom | Look at |
| --- | --- |
| Slash commands missing | Invited without `applications.commands`; re-invite |
| Commands time out (`10062`) | Host DNS. Measure with `curl -4 -w '%{time_namelookup}'`; `?ping` only reports gateway heartbeat and will look fine |
| A command works for you, not for members | Setup check → member findings |
| Level roles do nothing | The bot's role sits below them |
| Dashboard refuses to start | Deployment validation; the message names the exact mismatch |
| Dashboard login loops | The redirect URI differs from the portal by a character |
