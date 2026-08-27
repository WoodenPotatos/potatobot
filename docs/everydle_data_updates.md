# Keeping Everydle data current

**Implemented on 2026-08-22.** Every number below was measured against the live
sources that day, and phases 1 to 5 are done — see section 8 for what shipped and
what deliberately did not.

The reasoning is kept as written, because it is the part that explains why the
tooling reports rather than edits.

**League of Legends is out of scope.** `data/loldle/` is maintained by a named
administrator who has asked that it not be edited automatically. Nothing in this
plan touches it, and any tool built from this plan must refuse `loldle` by name
rather than by configuration, so it cannot be enabled by mistake.

---

## 1. The problem, and what it actually costs

Every Everydle dataset is hand-written. When a game ships a new character, the
puzzle silently stays on the old roster until somebody notices and edits JSON in
three places: the mechanics file, the Hungarian catalog, and now the English one.

That is already the situation. Measured today:

| Game | Local | Upstream | Drift |
| --- | --- | --- | --- |
| Valdle (agents) | 28 | 29 | **Miks missing** — a Controller released 2026-03-18 |
| DbDle (killers) | 42 | 43 | **The Slasher missing**; `the_onryo` is spelled `The Onryō` upstream |

There is also a local gap the drift check found on the way: `the_mastermind` has
no `height` field in `data/dbdle/killers.json`, so that killer's height column is
empty in the puzzle.

So the cost is not hypothetical. The interesting part is that *noticing* is the
expensive step, not editing.

---

## 2. What can be automated, and what cannot

This is the crux, and it is why "auto-update the datasets" is the wrong goal.

The attributes these puzzles are built on split cleanly into two kinds. **Game
data** is published by the game or datamined from it, and a machine can fetch it.
**Lore** is a fact about a character that no API carries — nationality, species,
gender for a non-human — and only a person can supply it.

### Valdle — `https://valorant-api.com/v1/agents?isPlayableCharacter=true`

Verified: HTTP 200, 29 playable agents, community-run mirror of Riot's client data.

| Local field | Upstream | Verdict |
| --- | --- | --- |
| roster | `displayName`, `uuid` | **Automatable.** Names are stable and match the local slugs. |
| `role` | `role.displayName` | **Automatable.** Agreed on 28 of 28 shared agents. |
| `year` | `releaseDate` | **Partly.** Older agents carry the epoch placeholder `1970-01-01`; only recent additions have a real date. Propose a year only when the date is not epoch. |
| `gender` | — | Human. |
| `species` | — | Human. |
| `origin` | — | Human. |

### DbDle — `https://dbd.tricky.lol/api/characters`

Verified: HTTP 200, 96 characters (43 killers, 53 survivors); `/api/perks` returns
315 perks. Community-run, datamined.

| Local field | Upstream | Verdict |
| --- | --- | --- |
| roster | `name`, `id`, `role` | **Automatable.** |
| `height` | `height` (`tall`/`average`/`short`) | **Automatable.** Agreed on 40 of 40 comparable killers. |
| `terror_radius` | `tunables.TerrorRadius` ÷ 100 | **Automatable for the base value.** Agreed on 40 of 40. |
| `gender` | `gender` (`male`/`female`/`multiple`/`nothuman`) | **Mostly.** Agreed on 38 of 41 — the three gaps need a human look. |
| `movement_speed` | `tunables.MaxWalkSpeed` ÷ 100 | **Base value only.** Agreed on 38 of 41. Local values list power-state speeds too (`4.6 m/s, 10.12 m/s`); upstream has one base tunable. |
| `country` | — | Human. Nationality is lore. |
| `release` | `dlc` | Needs a mapping. `dlc` is an internal codename (`ApplePie`, `Cannibal`), not a chapter name or a year. |

The disagreements are the most useful part of this table. Three gender values and
three base speeds differ between the local data and upstream. One of the two is
wrong, and a tool cannot tell which — so it must show them, never overwrite them.

### Genshindle — `https://genshin-db-api.vercel.app/api/v5/characters` and `.../talents`

Probed 2026-08-27. Two bulk endpoints, 122 characters and 125 talent records,
answering in one request each rather than one per character.

Unlike the other two, **this source publishes every attribute the puzzle uses**:
`elementText`, `weaponType`, `region`, `rarity`, `gender`, `bodyType` and the
release `version`. There is no lore field, so the adapter reports nothing as
needing a person and the only local knowledge is the aliases.

Two things it does *not* publish, both settled deliberately:

- **Which boss drops a talent material.** The material record's description only
  alludes to it in prose ("the Dragon of the East"), and the domain list carries
  no weekly-boss drops at all. So the attribute value is the material —
  "Dvalin's Sigh" — which a player recognises just as well and which upstream
  actually states. `GENSHIN_WEEKLY_BOSS` in `everydle_sources.py` is an empty map;
  adding a row collapses that material into a boss name, and since a boss's three
  materials map to the same name they remain one value. Verified against the live
  roster: 125 of 125 characters have exactly one cost in the 113000–113999
  material id range at talent level 10, so the drop is identified by id range
  rather than by matching names.
- **Whether a character has actually been released.** `version` is when they
  entered the data, so an upcoming character already carries one — the roster
  cannot tell you. That is the only fact a person supplies, and it is one line in
  `EXCLUDED_ENTITIES` per character, deleted the day they arrive.

What looked at first like a third gap turned out not to be one. `region` is blank
for eleven characters, but every one of them still carries an `associationType`,
so the nation is published under another name: `ASSOC_SNEZHNAYA_STAR` is
Snezhnaya, `ASSOC_NODKRAI_ZIBAI` is Nod-Krai. Four associations name no nation at
all — the Traveller, Aloy, Nicole and Skirk — and they share the value
`Outsider`, which is the fact rather than a placeholder.

### The conclusion

**Automate the diff, not the edit.** A job that says "Valorant added Miks, here is
their role and release date, you still need gender, species and origin" turns a
months-long blind spot into a same-day notification, and it does that without ever
being trusted to author a puzzle. That is where nearly all the value is.

---

## 3. Five mechanics that constrain any tool

These come from reading the code, not from the sources, and each one rules out an
obvious shortcut.

1. **Attribute value ids are opaque and dataset-scoped.** `gender_5767625f95`
   means `Férfi` in `killers`, while the same word in `survivors` is
   `gender_1282126270`. They are not a hash of anything reproducible. A tool must
   therefore *look up* an existing value's id within that dataset and reuse it,
   and mint a fresh unique id only for a genuinely new value. Re-deriving an id
   would orphan every entity in the mechanics file that references the old one.
2. **A new entity is safe; a renamed one is not.** `get_daily_target` filters the
   persisted deck to ids that still exist, so an addition simply joins the next
   reshuffle and today's puzzle is untouched. But if today's answer disappears —
   which is what renaming `the_onryo` to `the_onryō` would do — a new answer is
   drawn mid-day, and anyone with a game open is comparing against a target that
   no longer exists. **Never rename an entity id.** Add an alias instead; the
   locale catalog's `aliases` list exists for exactly this.
3. **Changing an attribute changes an in-flight puzzle.** The colour feedback a
   player already received was computed against the old value. Apply data changes
   at a day boundary, not whenever the job runs.
4. **Datasets load at import time.** `cogs/everydle.py` calls
   `load_game_dataset` at module scope and picks the language then, so a data
   change needs `?reload everydle` or a restart. A tool that writes files without
   saying this leaves the operator wondering why nothing changed.
5. **Both catalogs, always.** Hungarian and English are now both required and
   `tests/test_locale_coverage.py` enforces it, so any proposal has to carry text
   for both — and a blank entity name does not degrade, it disables the whole
   minigame via `load_or_disable`.

---

## 4. The design

Three components, each independently useful. Nothing writes to `data/` without a
human pressing something.

### 4.1 A source adapter per game

One small module per game, with one job: fetch the upstream roster and normalise
it into `{entity_id: {field: value}}` using the *local* vocabulary. The adapter
owns the mapping (`male` → the `Férfi`/`Male` value id, `MaxWalkSpeed: 460` →
`4.6 m/s`) and nothing else. It is the only part that knows about a third-party
API, which is what keeps the fragile part small and testable against a recorded
fixture.

Rules for an adapter: it never writes, it never invents a lore attribute, it
reports "unknown" rather than guessing, and it fails loudly on an unexpected
payload shape rather than producing a half-empty roster. A community API changing
its schema must produce an error, not a proposal to delete forty killers.

### 4.2 A drift report

`scripts/everydle_drift.py`, in the shape of the existing `scripts/locale_audit.py`
— read-only, prints a report, exits non-zero when it finds something. For each
dataset it reports:

- **new upstream** entities, with every field the source could supply and an
  explicit list of the lore fields a human still has to fill;
- **missing upstream** entities, which usually means a name change rather than a
  removal, and which must never be auto-deleted;
- **field disagreements** on shared entities, with both values side by side;
- **local gaps** — a field absent from an entity, which is how
  `the_mastermind`'s missing height surfaced.

This alone closes the "months later" problem, and it is a day's work.

### 4.3 A proposal generator

`scripts/everydle_propose.py` takes the drift report and emits a **patch file**,
not an edit: the exact JSON fragments to add to the mechanics file and to both
locale catalogs, with lore fields left as `null` and a `TODO` list at the top. The
operator fills the lore in, then applies it. Applying is deliberately a separate
step, and it should refuse to run when the current answer for that dataset would
be affected — that is constraint 2 and 3 above, enforced rather than documented.

Value ids are minted here, once, using the same `<field>_<10 hex>` shape, checked
for collisions inside the target dataset.

### 4.4 Scheduling

Once the report is trustworthy, run it on a timer and have it post to the bot log
channel when it finds drift. It is a read-only outbound HTTP fetch on a schedule,
so it belongs in the same place as the existing social-notification polling, with
the same failure discipline: a source being down is a logged warning, not an
error, and never a reason to change data.

---

## 5. Phases

| Phase | Work | Outcome |
| --- | --- | --- |
| 1 | Fix the two known gaps by hand: add Miks and The Slasher, give `the_mastermind` a height, add `The Onryō` as an alias of `the_onryo`. | The data is correct today, and doing it by hand once shows exactly what the tool must generate. |
| 2 | Valdle adapter + drift report, with a recorded fixture in `tests/`. | Same-day notice of a new agent. |
| 3 | DbDle adapter, reusing the same report. | Same for killers; also surfaces the gender/speed disagreements for a decision. |
| 4 | Proposal generator with id minting and the day-boundary guard. | A new character becomes "fill in three lore fields and apply". |
| 5 | Scheduled run posting to the bot log channel. | Nobody has to remember to look. |

Phase 1 is worth doing regardless of whether any of the rest happens.

---

## 6. Risks, and what to do about them

- **Community APIs are not contracts.** Both sources are volunteer-run and can
  change or vanish. Mitigation: adapters are tiny and fixture-tested, the report
  is read-only, and a fetch failure is a warning. Nothing downstream depends on a
  source being reachable.
- **Licence and attribution.** Datamined community data may carry terms. Check
  each source's terms before shipping a scheduled fetch, and record the source and
  the date in the drift report so provenance is never guessed.
- **Upstream can be wrong.** It disagrees with the local data on six fields right
  now, and the local data may well be the correct one in some of them. This is
  precisely why nothing auto-applies.
- **Scope creep into LoLdle.** The most likely way this plan causes harm is
  someone generalising the tool and pointing it at `data/loldle/`. The adapter
  registry should hard-code the two supported games and have no mechanism for
  adding a third without a code change and a conversation.
- **A new attribute value needs both catalogs.** Adding, say, a nationality that
  does not exist yet means a new value id plus Hungarian and English text. The
  proposal generator has to produce all three or the minigame will not load.

---

## 7. Sources, as verified

| Source | Endpoint | Checked | Result |
| --- | --- | --- | --- |
| valorant-api.com | `/v1/agents?isPlayableCharacter=true` | 2026-08-22 | HTTP 200, 29 agents, `role` and `releaseDate` present |
| dbd.tricky.lol | `/api/characters` | 2026-08-22 | HTTP 200, 96 characters, `gender`/`height`/`dlc`/`tunables` present |
| dbd.tricky.lol | `/api/perks` | 2026-08-22 | HTTP 200, 315 perks |
| api.nightlight.gg | `/v1/characters` | 2026-08-22 | HTTP 404 — not a usable endpoint |

Riot publishes no official endpoint carrying agent lore attributes, and Behaviour
Interactive publishes no public Dead by Daylight API at all, which is why both
usable sources are community-run.

---

## 8. What shipped

| Phase | Status | Where |
| --- | --- | --- |
| 1 — correct the known drift by hand | **done** | `data/valdle/`, `data/dbdle/` |
| 2 — Valdle adapter + drift report | **done** | `scripts/everydle_sources.py`, `scripts/everydle_drift.py` |
| 3 — DbDle adapter | **done** | same |
| 4 — proposal generator | **done** | `scripts/everydle_propose.py` |
| 5 — scheduled run | **done, differently** | `deploy/potatobot-everydle-drift.{service,timer}` |

### Phase 1, as applied

- **Valdle** gained `miks` (Miks). `role` and `year` came from the source;
  `gender` is from the bio's pronouns, `origin` from "Straight from Croatia", and
  `species` is the one inferred value — sonic powers read as Radiant, which is
  what every other agent with innate powers is. Croatia was a new origin value,
  so an id was minted and text added to both catalogs.
- **DbDle** gained `the_slasher` (The Slasher). Gender, height, base speed,
  terror radius and the release year all came from the source; the chapter is the
  Jason Chapter, so `country` is American.
- `the_mastermind` gained the `height` it was missing.
- `The Onryō` was added as an **alias** of `the_onryo`, not as a rename.

Valdle is in step with upstream. DbDle's movement speeds were then corrected —
The Blight is a 110% killer, so his base is 4.4 m/s, and The Shape's and The Pig's
labels now list the base first as every other killer's does, which removed the
confusable `4.6m/s` pair as a side effect. The three gender divergences are
recorded in `ACCEPTED_DIVERGENCES` with a reason each.

### Fields the game owns

`SOURCE_AUTHORITATIVE` marks the fields the game publishes and changes with a
balance patch: a killer's `movement_speed`, `terror_radius` and `height`, and an
agent's `role`. A disagreement there is a **balance change** — something to apply,
not to argue with — so the drift report proposes it and `everydle_propose.py`
drafts it already filled in. `merge_base_value` replaces only the leading
component of a multi-value label, so a base-speed change keeps the power-state
speeds a person maintains: `4.6 m/s, 9.2 m/s` with upstream at `4.4 m/s` becomes
`4.4 m/s, 9.2 m/s`, not `4.4 m/s`.

Applying one *does* change an in-flight puzzle's colour feedback, so the
day-boundary guard covers updates as well as additions, and a patch drafted
against a value that has since moved again is refused rather than applied.

### Divergences somebody has already decided about

`ACCEPTED_DIVERGENCES` records a local value that knowingly disagrees with
upstream, with a reason. Those stay visible in the report but do not count as
findings and are never drafted. This exists because the failure mode of a drift
check is not missing something — it is crying wolf until an operator stops
reading it. Deleting an entry makes it a finding again.

### Phase 5, and why it is a timer

The plan said this belonged beside the social-notification polling inside the
bot. It ships as a systemd timer instead. The check is worth running weekly at
most, and keeping it out of the bot means no third-party HTTP dependency sits on
the event loop that has to meet Discord's three-second interaction deadline. The
report already exits 1 for drift and 2 for an unreachable source, so the unit
distinguishes them with `SuccessExitStatus=1 2` and the journal is the record.

### Running it

```bash
python scripts/everydle_drift.py                      # live sources
python scripts/everydle_drift.py --fixtures tests/fixtures/everydle
python scripts/everydle_propose.py draft -o patch.json
python scripts/everydle_propose.py apply patch.json --dry-run
python scripts/everydle_sources.py record tests/fixtures/everydle   # re-record
```

`apply` refuses three ways, each of which is a silent failure otherwise: a field
nobody filled in, a new attribute label with no text in every language, and an
entity that already exists — because renaming one re-draws the day's answer.
After applying, run `?reload everydle` or restart; the datasets load at import.

