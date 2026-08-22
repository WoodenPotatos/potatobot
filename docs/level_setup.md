# Setting up levels

Levels are the one part of the bot with no useful default. Channels and prices
ship with something sensible; a level role is a Discord role id that only exists
in your guild, so the shipped `level_roles` is empty and stays empty. This
document is the substitute: the maths, what it costs a member in practice, and a
ladder that is known to work because it has been running on a real guild.

## How a level is earned

One formula, in `database.py`:

```
level = floor(sqrt(xp / 10)) + 1        which inverts to        xp = 10 x (level - 1)^2
```

Quadratic, so each level costs a little more than the last. Level 2 is ten XP;
level 100 is ninety-eight thousand.

XP comes from these, at the rates a fresh installation ships with. All ten are
typed settings you can change per guild from the dashboard.

| Source | XP | Setting |
| --- | --- | --- |
| Chat message | 2 | `reward_chat_message_xp` |
| Voice, per minute | 5 | `reward_voice_minute_normal_xp` |
| Voice, per minute, premium | 10 | `reward_voice_minute_premium_xp` |
| `/daily` | 50 | `reward_daily_normal_xp` |
| Everydle win (Valdle, DbDle, LoLdle easy/medium) | 100 | `reward_valdle_xp`, … |
| LoLdle hard | 150 | `reward_loldle_hard_xp` |
| `/work`, ordinary | 25 | `work_xp_normal` |
| `/work`, no-payout outcome | 50 | `work_xp_free` |

Chat XP is per message and has the same anti-spam cooldown as everything else,
so it is a slow trickle rather than a lever. Voice is the fast one: an hour in a
call is 300 XP, worth 150 chat messages.

## What each level actually costs

The right-hand column is the honest one — a member who claims `/daily` and sits
in voice for half an hour, most days.

| Level | Total XP | Chat messages | Voice hours | Days at daily + 30 min voice |
| --- | --- | --- | --- | --- |
| 5 | 160 | 80 | 0.5 | 1 |
| 10 | 810 | 405 | 2.7 | 4 |
| 15 | 1 960 | 980 | 6.5 | 10 |
| 25 | 5 760 | 2 880 | 19 | 29 |
| 50 | 24 010 | 12 005 | 80 | 120 |
| 100 | 98 010 | 49 005 | 327 | 490 |
| 150 | 222 010 | 111 005 | 740 | 1 110 |
| 200 | 396 010 | 198 005 | 1 320 | 1 980 |
| 250 | 620 010 | 310 005 | 2 067 | 3 100 |

Read the bottom rows as what they are: level 200 is years of daily presence.
That is the point of a top rung — it should be something almost nobody has.

## The recommended ladder

```json
{
  "5": 0, "10": 0, "15": 0, "25": 0, "50": 0,
  "100": 0, "150": 0, "200": 0, "250": 0
}
```

Replace each `0` with the role id to grant at that level. Nine rungs: four in the
first month, then widening gaps.

This is not a guess. It is the ladder the development guild has run, and on
2026-08-22 its 87 accounts — 73 with any XP at all — were distributed like this:

| Milestone | 5 | 10 | 15 | 25 | 50 | 100 | 150 | 200 | 250 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Members at or above | 65 | 45 | 35 | 27 | 18 | 9 | 4 | 3 | 0 |

Every rung except the last is occupied, the drop between rungs is gradual rather
than a cliff, and the top member sits at 227 — climbing toward 250 rather than
parked above it. The median active member is level 14, which is why the early
rungs are close together: that is where most of your guild lives.

### If your guild is new or small

The upper half assumes a guild that has been running long enough for somebody to
reach level 100. If yours has not, those rungs are decoration. Compress:

```json
{"3": 0, "7": 0, "12": 0, "20": 0, "35": 0, "60": 0}
```

Same shape — dense at the bottom, widening — but the top rung is reachable in a
few months rather than a year. You can always add rungs above later; a member who
already passed level 60 simply receives the new role the next time they level up.

## Configuring it

Dashboard → **Community** → **Levels**:

- **`level_roles`** — the JSON map above. A value may be a role **id** or a role
  **name**; use ids. A renamed role breaks a name and does not break an id.
- **`levels_channels`** — where level-up announcements go. Members need to be
  able to *see* it or the announcement is pointless; the setup check reports it
  if nobody can.

Two behaviours worth knowing:

- **Level 2 is announced but grants nothing.** It is the "the bot noticed you"
  moment and deliberately has no role, so it does not need to be in the map.
- **A malformed entry is skipped, not fatal.** A key that is not a number, or a
  value that is neither an id nor a name, is dropped with a warning. This runs
  inside the level-up path, and raising there would swallow the member's level-up
  along with the bad entry.

Roles are granted **cumulatively downward**: on reaching a milestone the member
receives that milestone's role. Make sure the bot's own role sits **above** every
level role, or it cannot hand them out — `/checkperms` and the dashboard setup
check both report this, and it is the single most common reason a correctly
configured ladder does nothing.

## Tuning the rates instead

If the ladder is right but the pace is wrong, change the rates rather than the
milestones — the milestones are where your role rewards sit, and moving them
re-sorts everyone. Doubling `reward_voice_minute_normal_xp` halves every voice
hour in the table above. Existing XP totals are untouched, so members keep the
levels they have and climb faster from there.
