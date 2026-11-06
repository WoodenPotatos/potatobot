# PotatoBot Privacy Policy

**Effective date:** 2026-11-06
**Last updated:** 2026-11-06
**Version:** 1.1

This document is written in English and Hungarian. The English text is the
binding one; the Hungarian text (`PRIVACY.hu.md`) is a translation provided
for convenience. If the two differ, the English text prevails.

---

## 1. Who is responsible, and what this policy covers

### 1.1 The data controller

The data controller for the installations listed in section 1.2 is:

> **Máté Domina**, a private individual resident in **Hungary**
> Contact for privacy matters: **privacy@potatobot.win**
> Discord: **@woodenpotato**

Referred to below as "we", "us" or "the operator". We have not appointed a
data protection officer; the size and nature of this hobby project do not
require one. Write to the contact address above for any question or request
about your data.

### 1.2 Installations this policy covers

PotatoBot is open-source software. Anybody may download it and run their own
copy. **This policy covers only the installations that we personally run**,
each of which is a separate Discord application with its own database and
its own servers:

| Discord application | Dashboard address | Status |
| --- | --- | --- |
| **Potato Bot** (private application) | `https://devboard.potatobot.win` | Serves our own community server by invitation. It can serve more than one server. |

The installations never exchange data with each other. A copy of PotatoBot
run by somebody else is **their** service: they are the data controller for
it, and they must publish their own privacy policy. If you are not sure which
installation you are talking to, the bot's `/version` command names it, and
links the current version of this policy and of the Terms of Service.

### 1.3 Relationship to Discord

PotatoBot runs on Discord and receives data from Discord's API. **We are not
affiliated with, endorsed by or sponsored by Discord Inc.** Discord's own
Privacy Policy (`https://discord.com/privacy`) governs what Discord does with
your data; this policy governs what we do with the data Discord passes to us.

### 1.4 Server administrators

A server owner or administrator who adds PotatoBot to a Discord server decides
which features are switched on, which channels they use, and what the
moderation thresholds are. Section 12 explains what that means for them. The
operator remains the controller for the storage and technical processing
described here.

---

## 2. In short

- We store your **Discord user ID**, and under it: coin balance, experience,
  level, game and gacha history, inventory, cooldown and last-activity
  timestamps, warnings issued to you, tickets you opened, and the settings of
  your temporary voice room. The full list is in section 4.
- We **do not** store your username, display name, email address, IP address
  or avatar. We **do not** store the text of your messages, with two small
  exceptions described in section 5 (word-chain words and the current
  counting value).
- Your data is stored on a server we operate ourselves, and backed up nightly
  in **encrypted** form to Google Drive, where copies are kept for 30 days.
- The only text you type that leaves our installation is a **music search**
  (sent to YouTube). Nothing else you type is sent to a third party.
- We never sell data, never show advertising, never use your data to train AI
  models, and never share it with data brokers.
- You can download everything we hold about you with **`/mydata`** and erase
  it with **`/deletemydata`**, at any time, without asking anyone.
- Server administrators may enable automatic moderation (a word filter and
  warning thresholds that can time out, kick or ban). Section 10 explains your
  rights when that happens.

---

## 3. Where your data comes from

| Source | What | How |
| --- | --- | --- |
| **Discord's API** (the main source) | Your user ID, the servers and channels you share with the bot, your roles, your voice state, your join and leave events, your server boosts, and the content of messages in channels the bot can read. Your username, display name, avatar and account creation date are shown to the bot but, except where section 4 says so, not stored. | Discord sends these to the bot as events over its gateway and REST API, because the bot has been added to a server you are in. This is data we did not get from you directly (GDPR Article 14). |
| **You** | Commands you run, buttons you press, text you type into the bot's modals (a game name for a group search, a voice room name, a music search), your confirmation of a data erasure. | Directly, when you use a feature. |
| **Server staff** | The reason attached to a warning, kick, ban or level change about you; whether you are excluded from the inactivity report. | Typed by a moderator of the server. |
| **Discord's audit log** | Who kicked or banned a member and why. | Read only to tell a kick or ban apart from a voluntary leave, when that announcement feature is on. |

---

## 4. What we process, feature by feature

Every feature below can be switched on or off by each server's
administrators. If a feature is off in your server, the data in its row is not
collected there. "Installation-wide" means a value is shared across every
server that installation serves; everything else is kept per server.

The legal bases are those of GDPR Article 6(1): **(b)** performing the service
you asked for by using the bot, **(f)** our legitimate interest, named in
each row, and **(c)** a legal obligation.

| Feature | Data stored (always under your Discord user ID) | Purpose | Basis | Kept until |
| --- | --- | --- | --- | --- |
| **Account and economy** (installation-wide) | Coin balance, experience, level, blackjack win/loss counts, purchased modifiers (rob bonus, rob defence, vault protection, protected reserve), bodyguard expiry; timestamps of your last daily claim, last work, last robbery, last streak update and last activity; streak count; whether the inactivity report has mentioned you. | Running the economy and the games; preventing double payouts. | (b); anti-abuse (f) | Erasure or the retention sweep (section 7). The balance and level survive erasure under an anonymous ID (section 8.3). |
| **Activity tracking** | One "last active" timestamp, updated at most once a day when you send a message or join a voice channel. No content. | Chat and voice rewards; the inactivity report; the retention sweep. | (b); (f) running a community | As above; blanked on erasure. |
| **Levels and roles** | Derived from experience. Role changes are made in Discord, not stored. | Level rewards. | (b) | As above. |
| **Casino games** | Every wager: game, stake, outcome, credit, when placed and settled; a rolling winnings counter used to detect abnormal earning rates. | Settling bets safely across restarts; detecting exploits. | (b); (f) game integrity | Wager rows are kept under an anonymous ID after erasure (section 8.3); the counter is deleted. |
| **Shop, inventory, gacha, vouchers** | Items you own and their quantity; every gacha pull (banner, rarity, reward, pity counters); vouchers you hold and when you redeemed them; timed grants such as a premium role or a rented emoji, sticker or soundboard sound, including which staff member granted or completed it; fulfilment requests for assets you redeemed. | Delivering what you bought or won; expiring rentals; preventing duplicate payouts. | (b) | Erasure. Grants are withdrawn in Discord first. |
| **Daily puzzles (Everydle: LoLdle, Valdle, DbDle, Genshindle)** | One timestamp per puzzle variant recording when you last played. Your guesses are **not** stored. | Enforcing the once-a-day rule and paying rewards. | (b) | Erasure. |
| **Counting and word-chain channels** | The current chain value and the ID of the last member who played; every accepted word, stored without any user ID. See section 5. | Running the game and refusing repeated words. | (b) | Last-player ID is removed on erasure; words until the chain is reset. |
| **Looking for group** | Posts you host: the game name you typed, how many players you need, the IDs of members who joined, when it was created. | Showing and updating the party post. | (b) | Eight days after creation, or erasure. |
| **Temporary voice rooms** | The name you gave your room, its user limit, lock state and bitrate; the member IDs you allowed or blocked; the ID of your current room. | Recreating your room the way you left it. | (b) | Erasure (also when another member erases themselves, their entry on your list is removed). |
| **Tickets** | That you opened a ticket (channel ID, type, when), and which staff member claimed it. **No ticket text is stored in the database**; see "Ticket transcripts" in section 6. | Limiting one open ticket per member; staff workflow. | (b); (f) support | Erasure. |
| **Moderation: warnings** | Each warning: your ID, the moderator's ID, the warning's tag, the reason the moderator typed, the date. Per-tag counts are compared against thresholds the server configured. | Moderating the server. | (f) keeping the community safe; the server's rules | Until staff remove it or you are erased. A removed warning's content stays in the staff audit log until erasure (section 6). |
| **Moderation: word filter** | Nothing about you is stored beyond a warning (above) whose reason names the channel, never the word or your message. | Enforcing server rules. | (f) | As warnings. |
| **Onboarding and rules** | How many seconds passed between your joining and your accepting the rules. | Moderation analytics for staff. | (f) | Blanked on erasure. |
| **Inactivity report** | Uses the last-active timestamp. Staff may exclude member IDs from the report through a setting. | Letting staff see who has been inactive for 14 days. | (f) community management | As above. |
| **Announcements** (join, leave, kick, ban, boost) | Nothing is stored. The post includes your mention, username, display name, user ID, account age and, for kicks and bans, the moderator and reason from Discord's audit log. Your avatar is downloaded into memory and attached to the post, or drawn onto an optional image card, so it stays visible after you change it. | Welcoming and informing the community. | (f) running a community | Only as a Discord message, which the server controls. |
| **Invite log** | Nothing is stored. A staff-channel post names the new member, their account age, the inviter and the invite code. | Letting staff see how members arrive. | (f) anti-raid, community safety | Only as a Discord message. |
| **Music** | Nothing is stored. The queue (what was requested and by whom) exists in memory while the bot plays. | Playing audio you requested. | (b) | Lost on restart. |
| **Twitch and YouTube notifications** | No member data. Streamer and channel names are server configuration. | Announcing streams and videos. | (b) for the server | Not applicable. |
| **Game patch notes** | No member data. | Announcing updates. | Not applicable | Not applicable. |
| **Data-sharing preferences** | Any opt-out you record for a data category, with a timestamp. (Foundation for a future multi-server economy; today nothing reads it.) | Honouring your choice. | (b) | Erasure. |
| **Erasure and export** | Your request itself is not stored under your ID: the audit record of an erasure names only the anonymous replacement ID and the number of rows deleted per table. | Proving what was deleted. | (c) GDPR accountability | Indefinitely, anonymised. |

Server configuration (channels, roles, prices, templates, filter words) is
stored per server and is not about you, except that **each configuration
row records the ID of the administrator who last changed it**, which is
covered in section 11.

---

## 5. Message content

The bot asks Discord for the **Message Content** privileged intent. It uses it
for exactly these things, all in memory, as each message arrives:

1. **Prefix commands** (messages starting with the bot's prefix, `?` by
   default) are parsed and executed.
2. **The word filter**, where a server enables it, normalises your message and
   compares it against the server's list. The message is deleted if the
   server configured that. **Neither your message nor the matched word is
   stored**; the staff channel is told which list entry matched.
3. **Counting and word-chain channels** read the number or word you posted.
   The current value is stored so the game survives a restart, and every
   accepted word is stored **without your user ID** so it cannot be reused.
   These are the only pieces of message text kept in the database.
4. **Activity rewards** note that you sent a message; the content is not
   read.
5. **Boost detection** looks at the message type only.

Message content is never logged, never sent to a third party, and never used
to train any model.

---

## 6. Where your data goes

### 6.1 Discord (recipient and platform)

Everything the bot posts, edits or attaches goes to Discord Inc. and is then
governed by Discord's terms and the server's own settings. Of note:

- **Ticket transcripts.** When staff close a ticket, the bot assembles the
  ticket channel's messages (timestamp, display name, text, links to
  attachments, up to 50,000 messages in at most five text files) and posts
  them to the server's ticket-log channel, which should be visible to the
  owner only. The transcript is not stored by us; it lives in Discord as long
  as the server keeps it.
- **Moderation posts.** Warnings, escalations, filter alerts, level changes,
  kick and ban notices, your warning history (`/modlogs`, staff only) and the
  casino rate-limit alert are posted to channels the server chose.
- **Direct messages from the bot.** You receive a DM only for: a word-filter
  notice, the reason for a kick or ban, your data export, and your erasure
  receipt.
- **Avatars.** For announcements and image cards the bot downloads your
  current avatar into memory and re-uploads it as an attachment. Nothing is
  written to our disk.
- **Images from other hosts.** Server administrators may configure image URLs
  (announcement pictures, rule banners). Discord's media proxy fetches those,
  not our server, and not with your data.

### 6.2 Processors we use

| Recipient | What they receive | Why | Where | Safeguard |
| --- | --- | --- | --- | --- |
| **Google LLC** (Google Drive) | A nightly **encrypted** copy of the whole database. Google cannot read it: the file is encrypted on our server with rclone's crypt layer before upload, and the keys never leave our server. | Off-site backup. | United States | Encryption before transfer; Google's certification under the EU-US Data Privacy Framework, with the Standard Contractual Clauses as fallback. |
| **Cloudflare, Inc.** | Every request to the dashboard passes through Cloudflare's edge and a Cloudflare Tunnel to our server. Cloudflare sees the **IP address, request metadata and TLS handshake** of each dashboard visitor, provides DNS for `potatobot.win`, and forwards email sent to our contact address to the mailbox we read (Cloudflare Email Routing), so it handles the content of anything you write to us. It does not see bot traffic to Discord. | Publishing the dashboard over HTTPS without exposing our home connection. | EU edge locations and United States | Cloudflare's Data Processing Addendum, its EU-US Data Privacy Framework certification, and the Standard Contractual Clauses. |
| **Google LLC** (YouTube) | When you use the music feature: the **search text you typed, or the YouTube URL you pasted**, and our server's IP address. No Discord data is sent. The audio stream is fetched by our server. | Finding and playing the song you asked for. | United States | YouTube acts as an independent controller under its own privacy policy; we send nothing beyond the query. |
| **Twitch Interactive, Inc.** | Only the streamer names a server administrator configured, with our application credentials. No member data. | Stream notifications. | United States | None needed for member data; none is sent. |
| **Game publishers' public endpoints** (Valve/Steam, Riot Games, HoYoverse, Mojang) | Nothing about anyone; our server polls public patch-notes feeds. | Patch-notes notifications. | Various | Not applicable. |
| **GitHub, Inc.** | A plain request for the project's changelog when a dashboard user opens the changelog page. No user data. | Showing release notes. | United States | Not applicable. |
| **An email provider (optional)** | If the operator enables error alerting, lines of the bot's error log are emailed to the operator's own address through an SMTP server. Such lines can contain **Discord user IDs and server IDs**, never message text or usernames. **This installation: not enabled.** | Being told when the bot breaks. | Depends on provider | Disabled unless configured; TLS in transit. |

We have no other processors. There is no analytics service, no error-tracking
service, no advertising network, no AI provider and no update telemetry.

### 6.3 Hosting

The bot and dashboard run on hardware we own, in **Hungary**, inside
a rootless container under a dedicated system account, reachable for
administration only over a private network with SSH keys. The database is a
PostgreSQL server in its own container under that same account, reachable
only from the bot's container over a private network, and readable by that
account alone.

### 6.4 International transfers

The recipients in section 6.2 marked "United States" are outside the European
Economic Area. Transfers rely on the European Commission's adequacy decision
for the **EU-US Data Privacy Framework** for recipients certified under it,
and otherwise on the **Standard Contractual Clauses** (Commission Decision
2021/914). For the database backup, the data is additionally encrypted before
it leaves our server, so the recipient holds only ciphertext. If the Data
Privacy Framework were invalidated, the Standard Contractual Clauses continue
to apply. Discord itself is a US company; data we receive from Discord is a
transfer Discord makes under its own terms, not one we make.

### 6.5 Who else can see your data

- **Administrators of your server** (the owner, members with Administrator or
  Manage Server) can sign in to the dashboard for that server. There they see
  that server's audit history (which administrator changed what, with
  usernames resolved live from Discord), redeem and fulfilment requests
  naming members, and the record of past erasures (anonymous IDs only). They
  do not see other servers' data, and they cannot read balances or warnings
  in bulk; those are reached through the bot's own staff commands in Discord.
- **The operator** can read everything in the database and the logs.
- **Nobody else.** We disclose data to third parties only in the cases in
  section 6.2, when the law requires it, or when you expressly ask us to.

---

## 7. How long we keep data

| Data | Retention |
| --- | --- |
| Everything stored under your user ID (section 4) | Until you erase it (`/deletemydata`), the operator erases it at your request, or the **retention sweep** removes it. The sweep runs daily when the operator has set a retention window (`data_retention_days`; 0 means indefinitely; **this installation: 365 days**), and erases a member only if their last recorded activity is older than the window **and** they are no longer in any server the installation serves. A member who leaves and returns within a year keeps their progress; after that they start afresh. |
| Economy row, settled wagers and reward-claim records | Kept after erasure under an anonymous negative ID with every behavioural column blanked (section 8.3). |
| Looking-for-group posts | Eight days. |
| Dashboard access-change markers | 24 hours. |
| Queued Discord actions (the "outbox") | 30 days after they complete. An erasure replaces your ID in any queued action about you with the anonymous ID. |
| Staff audit history (dashboard changes, warning removals) | Deleted after the audit window (`audit_retention_days`; 0 means indefinitely; **this installation: 90 days**). While kept, the acting administrator's ID is replaced by an anonymous ID on erasure and a removed warning's reason blanked. **Erasure records are exempt** and kept indefinitely: they name only the anonymous ID and are the proof of what was deleted. |
| Operational logs (section 9) | The bot's log file rotates at 10 × 5 MB (about 55 MB). The system journal is capped by size at 500 MB on this host, about two months; the log lines naming members it held until November 2026 expire under that cap. |
| Off-site encrypted backups | 30 days, then deleted automatically. |
| Pre-migration backups on the server | A full copy taken before each database upgrade, kept on the server as the rollback artefact. An erasure does not reach them. The nightly backup job deletes each one once it is older than the window, after that night's off-site copy has been verified. **This installation: 90 days after the migration they protect.** |
| In-memory data (music queue, invite snapshot, cooldown maps, dashboard tokens and rate-limit counters) | Lost on restart; dashboard tokens at most 12 hours. |

Erasing your data does not reach into backups. A backup restored after your
erasure would bring your rows back; our procedure is to re-run every erasure
recorded in the audit history after any restore. Off-site copies age out
within 30 days in any event.

---

## 8. Your rights, and how to use them

### 8.1 Access and portability: `/mydata`

Run `/mydata` in any server where the bot is. Within a few seconds the bot
sends you a JSON file in a direct message containing every row the
installation holds under your user ID, across every server, including rows
where you appear as a moderator, claimer, granter or blocked member. Each
table is capped at 5,000 rows and tells you if it was cut. You can use the
command once every 15 minutes. Your DMs must be open to server members.

The export covers every table in section 4, including the staff audit rows
that name you as an administrator or as the subject of a removed warning, and
the queued Discord actions about you.

### 8.2 Erasure: `/deletemydata`

Run `/deletemydata`, read the warning, and press the confirmation button
within 60 seconds. Erasure is immediate and **installation-wide**, because the
economy has no per-server copy. You receive a receipt listing what was
deleted and what was kept. You can also ask the operator to erase you, and
the operator can do it from the dashboard without your Discord account being
present.

### 8.3 What erasure keeps, and why

Erasure is an anonymisation. We delete everything personal and keep three
things under a fresh, random, negative ID that can never be a Discord ID:

- **your economy row** (balance, experience, level, blackjack counts,
  purchased modifiers) with every timestamp, cooldown and streak blanked;
- **settled casino wagers**;
- **reward-claim records**.

These are kept so the installation's total coin supply can be shown to be
unchanged by an erasure, and so a returning member cannot be paid a one-off
reward twice. Nothing links the anonymous ID back to you. Any wager still
pending is refunded to the balance first. Active premium roles and rentals
are withdrawn in Discord before their records go. Where other members' rows
pointed at you (a warning you issued, a ticket you claimed, a voice list you
were on), the pointer is removed or replaced by the anonymous ID.

### 8.4 Rectification

Most of what we hold is produced by the software and cannot be "wrong" in the
sense of a misspelled name. A warning's reason is a moderator's statement; ask
the server's staff to amend or remove it, or ask us.

### 8.5 Restriction and objection

You can object to processing based on our legitimate interests (the rows
marked (f) in section 4). The practical form of objection for most of them is
to leave the server, which stops every new processing, and to run
`/deletemydata`. Write to us for anything else.

### 8.6 Rights towards Discord

Your Discord account, username, avatar and messages are Discord's processing.
Exercise rights over those with Discord.

### 8.7 Complaints

You have the right to lodge a complaint with a supervisory authority, in
particular in the EU member state where you live or work. The authority for
the operator is:

> **Nemzeti Adatvédelmi és Információszabadság Hatóság (NAIH)**
> Hungarian National Authority for Data Protection and Freedom of Information
> 1055 Budapest, Falk Miksa utca 9-11, Hungary
> Postal address: 1363 Budapest, Pf. 9
> Telephone: +36 1 391 1400
> Email: ugyfelszolgalat@naih.hu
> Website: https://www.naih.hu

We would appreciate the chance to answer first, at the contact address in
section 1.1.

---

## 9. Logs

The bot and dashboard write operational logs to a rotating file on the
encrypted volume (section 14). The system journal receives only what the
process prints when it fails (a crash trace, a stack dump, the reason for an
automatic restart) and the database server's errors without their values;
none of these names you. The log file contains:

- the bot's own account name and ID, server IDs, channel IDs, command names,
  timings and error types;
- **your Discord user ID** on a small number of lines: when you join a server
  and the bot registers your account or assigns automatic roles, and when a
  feature fails while handling your request (for example a music search that
  could not be completed or an export that could not be delivered);
- never your username, message content, music search text, IP address or
  dashboard session data. The music extractor's own errors are reduced to a
  bare error type before logging. A failure inside the audio player can, in
  rare cases, quote the stream URL that was being played.

The dashboard keeps **no access log**: it records only slow requests (method,
path, status, duration) and unhandled errors. Visitor IP addresses are held in
memory for a few minutes to rate-limit login attempts and are never written
down.

If the operator enables error-alert emails (section 6.2), error lines leave
the server by email. Everything else stays on it.

---

## 10. Automated decisions

Server administrators can configure the following automatic actions. The
software applies them without a human looking at the individual case:

- **Word filter**: a message matching the server's list can be deleted and a
  warning recorded.
- **Warning thresholds**: when the number of warnings carrying a tag reaches a
  threshold the server set, the bot can time out, kick or ban the member.
  Every shipped threshold is zero (off); a server has to choose to enable one.
- **Casino rate limit**: abnormal winnings produce an alert to staff; nothing
  is taken from you automatically.
- **Inactivity report**: 14 days without a recorded message or voice join
  produces a staff post naming you; no action is taken against you.
- **Retention sweep** (section 7).

Where such a decision affects you, you have the right to obtain human
intervention, to express your point of view and to contest it: contact the
server's staff, who can remove a warning and reverse a timeout, kick or ban,
or contact us. The thresholds are the server's rules, and the server's
administrators are responsible for them.

---

## 11. The dashboard

The dashboard is for server administrators and the operator. Members do not
sign in. If you are an administrator:

- **Sign-in** is by Discord OAuth2 with the `identify` and `guilds` scopes
  only. We never ask for your email. The sign-in page links this policy and
  the Terms of Service, and Discord shows the same links on its authorisation
  screen.
- **What we keep about you**: during a session, a signed **cookie** holding
  your Discord user ID, username and avatar hash, the IDs of the servers you
  may manage, a CSRF token and session timestamps. The cookie is `HttpOnly`,
  `Secure`, `SameSite=Lax`, expires after 10 minutes of inactivity and 12
  hours at most. Your OAuth access and refresh tokens are held **in server
  memory only**, never on disk, and are discarded at logout or expiry.
  Nothing about your login is written to the database.
- **Changes you make** are recorded in that server's audit history with your
  user ID, the setting changed, its old and new value and the time. Other
  administrators of the same server can see them, with your username resolved
  from Discord at display time. These records are deleted after the audit
  window in section 7 (90 days on this installation); while kept, an erasure
  replaces your ID in them with an anonymous ID.
- **Browser storage**: the dashboard uses `localStorage` for your appearance
  mode, display language and which sidebar sections you left open. It sets no
  analytics or tracking cookies and loads no third-party scripts or fonts.
  Your browser fetches your avatar and server icons directly from Discord's
  CDN.
- **Cloudflare** sits in front of the dashboard (section 6.2).

---

## 12. If you administer a server that uses PotatoBot

By adding the bot and enabling features you decide what is collected in your
server. In particular you decide whether the word filter, warning thresholds,
the invite log, the announcements, the ticket transcripts and the inactivity
report run, and where their output is posted. You are responsible for:

- enabling only what your community needs, and posting outputs that name
  members (transcripts, invite log, moderation log, `/modlogs`) to channels
  limited to staff;
- making this policy known to your members, for example by linking it in your
  rules;
- the lawfulness of the reasons your staff type into warnings and of the
  images you configure;
- using the dashboard only for the server you manage.

Under the GDPR you may be a controller alongside us for the configuration
decisions you take. The storage and technical processing described in this
policy remain ours.

---

## 13. Children

Discord requires its users to be at least 13 years old, and older where the
law of their country says so. PotatoBot is not directed at anyone below
Discord's minimum age for their country, and we do not knowingly process the
data of such a person. If you believe we hold data of a child below that age,
write to us and we will erase it.

---

## 14. Security

- The database and logs are readable only by the service's own system
  account; the bot runs in a rootless container with all capabilities dropped,
  a read-only code image and a system-call filter.
- Secrets live in the environment, never in the code, and the public source
  repository is scanned for leaked credentials on every change.
- Off-site backups are encrypted before leaving the server.
- Administration happens over a private network with SSH keys only; the
  dashboard is reachable only through HTTPS.
- **Your data is encrypted at rest.** The database, the copies taken before
  each upgrade, the nightly backup while it is being prepared, and the bot's
  log file are kept on an encrypted volume (LUKS2), which is unlocked by hand
  after the server starts. Memory the operating system pages out goes to
  compressed RAM, never to disk. Since November 2026 no log line naming you is
  written to the system journal, which is outside that volume; older ones
  expire under its size limit (section 7).
- Should we learn of unauthorised access to your data, we will notify the
  supervisory authority within 72 hours where the GDPR requires it, notify
  Discord as its Developer Terms require, and inform affected members through
  the servers the bot serves.

---

## 15. What we never do

We do not sell personal data, rent it, or share it with data brokers,
advertising networks or any monetisation service. We do not use it for
profiling beyond the game mechanics described above, do not use message
content to train machine-learning or AI models, do not contact you outside
Discord using data obtained through Discord, and do not send unsolicited
direct messages.

---

## 16. The open-source software

PotatoBot's source code is published under the GNU Affero General Public
License v3.0 at `https://github.com/WoodenPotatos/potatobot`. The code is
public; your data is not, and never is part of the repository. The published
copy of this policy lives in that repository under `docs/legal/`, so every
change to it is visible in the project's history.

---

## 17. Changes to this policy

When the processing changes (a new feature that stores something, a new
processor, a new installation) we update this policy, move the effective date
and record the change in the project changelog. Material changes are
announced in the servers the installation serves. The Hungarian translation is
updated in the same change.

---

## 18. Contact

**Máté Domina** · **privacy@potatobot.win** · Discord:
**@woodenpotato**

For a security vulnerability, write to the contact address privately and do
not post personal data or a working exploit publicly. For a data request,
`/mydata` and `/deletemydata` are faster than email, and we answer every email
request within one month as the GDPR requires.
