# Localization: state and rules

Re-generate every figure here with `python scripts/locale_audit.py`. Last
measured 2026-08-22, after the English embargo was lifted and English was filled
in.

---

## 1. The rules in force

1. **Hungarian is the source language.** Every feature is designed in Hungarian
   and Hungarian is what everything else falls back to.
2. **English ships with it.** The maintainer lifted the no-generated-translations
   rule for English on 2026-08-22, because the Hungarian text is generated too.
   `locales/en.json` and `data/*/locales/en.json` are filled in the same change
   as the Hungarian, not left for a translator.
3. **Every language after English keeps the old rule.** Add the identical key
   with an empty value and leave it for a human translator. Nothing else about
   the language rules changed.
4. **All user-visible output comes from a catalog.** Command descriptions,
   embeds, button labels, errors, dashboard text, minigame display data, and
   Discord audit-log reasons. No inline literals.
5. **Code stays English.** Comments, docstrings, technical values and operational
   logs are explicit English, structured, and free of credentials.
6. **Catalogs stay structurally identical**, and `scripts/sync_locale_keys.py` is
   what keeps them that way. It adds new keys, removes deleted ones, and now
   *reports* the English keys it left blank, so unfinished English work is
   visible immediately rather than at the next red build.
7. **`data/loldle/` is off limits.** It is maintained by a named administrator
   who asked that it not be edited automatically. Its English catalog stays
   empty, which means LoLdle is unavailable when the language is English. That is
   a recorded consequence of an ownership decision, not a defect to fix by
   editing the file.

### What enforces them

| Rule | Enforced by |
| --- | --- |
| Identical catalog shape | `test_localization_policy` |
| No Hungarian prose outside catalogs | `test_localization_policy` |
| Literal `t("…")` and `dashboard.*` keys resolve | `test_localization_policy`, `test_locale_coverage` |
| Runtime-composed keys resolve | `test_locale_coverage` |
| Hungarian catalog has no blank value | `test_locale_coverage` |
| **English catalog has no blank value** | `test_locale_coverage` |
| **No English value carries Hungarian text** | `test_locale_coverage` |
| **An identical value is a name or template, never a sentence** | `test_locale_coverage` |
| **Every selectable language is actually complete** | `test_locale_coverage` |
| **The LoLdle exception stays the only one** | `test_locale_coverage` |
| No user-visible literal outside a catalog | `test_locale_coverage` |
| Minigame catalogs align | `test_minigame_localization` |
| Help covers every command | `test_cog_loading` |
| CI gate | `scripts/locale_audit.py --brief` in `.github/workflows/ci.yml` |

Translation *quality* is still the one thing no test can judge. Read the diff.

---

## 2. Current state

### 2.1 Headline

| Surface | Hungarian | English |
| --- | --- | --- |
| General catalog (1393 keys) | **100%** | **100%** |
| Dashboard namespace (542 keys) | 100% | 100% |
| Runtime-composed key families (26) | 100% | 100% |
| Valdle data (63 strings) | 100% | **100%** |
| DbDle data (137 strings) | 100% | **100%** |
| LoLdle data (331 strings) | 100% | **0% — owner-maintained** |

### 2.2 Selecting a language

`language` is a constrained choice (`SUPPORTED_LANGUAGES = ("hu", "en")`), not
free text. Anything outside it is rejected at save time, and the dashboard
renders it as a dropdown so a rejected value cannot be typed in the first place.
A language belongs in that tuple only when its general catalog and the minigame
catalogs it needs are complete; `test_locale_coverage` fails otherwise.

What `language = "en"` does today:

- Every bot message, command description, button label and error → English.
- The dashboard → English (its display language is a separate per-browser
  preference and works independently).
- Valdle and DbDle → English.
- **LoLdle → unavailable.** `load_or_disable` treats a blank entity name as a
  fatal dataset error, so an empty English catalog removes the game rather than
  falling back. `/loldle` answers with `everydle.err_champions_json`, which is a
  clear message rather than a broken command. The fix is for the dataset's owner
  to fill `data/loldle/locales/en.json`; nothing in the code needs to change.

### 2.3 How resolution works

`t()` walks *requested language → English → Hungarian* and treats a
present-but-empty value as a **miss**. That second part is the load-bearing one:
the catalogs are structurally identical, so an untranslated key exists with an
empty string, and treating that as a hit is what used to make a switched language
answer with blank embeds and blank button labels.

`get_dashboard_locale_catalog` reaches the same outcome by a different route,
overlaying only non-empty leaves over Hungarian. It stays separate because it
hands a whole catalog to the browser rather than resolving a single key.

A missing format argument is now its own error: it is logged by name and returns
the unformatted template, so the braces are visible. Previously the same handler
caught it as a missing key and returned the other catalog's value or an empty
string — `t("system.ping")` with no `latency` returned `''`.

### 2.4 Minigame data structure

This is the best-organised part of the project and it did not need changing.
Stable English entity ids and numeric mechanics live in `data/<game>/*.json`;
display names, aliases and attribute labels live in
`data/<game>/locales/<lang>.json`. The English catalogs generated for Valdle and
DbDle copy the entity names — they are already English proper nouns — and
translate only the attribute labels, which is exactly the split the format is
for. LoLdle hard mode remains deliberately unfinished and must stay unexposed.

---

## 3. What was fixed getting here

- **599 verbatim Hungarian copies** in `locales/en.json` were replaced with
  actual English, and the 803 empty values filled. English went from 0% to 100%.
- **Six stale copies** describing retired behaviour were corrected: the +10%
  single-use lockpick (now +15% and stackable) and the three percentage vaults
  (now fixed reserves). `casino.rob_win_desc` lost the `{vault}` placeholder the
  Hungarian had deliberately dropped — it would have raised on `.format()` and
  re-exposed the victim's vault percentage.
- **Eighteen unreachable keys** were deleted. Nine were duplicates of
  `general.cmd_*`; the other nine were error messages superseded by variants that
  deliberately do **not** interpolate the exception text (`music.join_failed` →
  `music.join_failed_safe`, `music.search_error` → `music.search_failed`, and so
  on). None may be revived: they were retired precisely because echoing
  extractor and exception text to users is forbidden.
- **Four Discord audit-log reasons** moved into the catalogs
  (`roleselect.audit_reason`, `tickets.audit_reason_setup_failed`,
  `shop.audit_reason_setup_failed`). Server staff read those in the audit log, so
  the project's own rule covers them.
- **`t()` was rewritten** for the fallback chain and the format-argument split
  described above.
- **`language` became a constrained choice** with a dashboard dropdown.

One thing is deliberately *not* fixed. `cogs/music.py:445` logs a playback
failure and tells the member nothing; the track auto-advances. The key that used
to report it (`music.playback_error`) was deleted rather than revived, because it
interpolated the raw error. If that silence should become a message, it needs a
new key that names the failure without quoting the exception.

---

## 4. Remaining work

### Adding a key
Add it to `locales/hu.json` **and** `locales/en.json` in the same change, then run
`python scripts/sync_locale_keys.py`. It will tell you if English is still blank.
Keep every `{placeholder}`, emoji and custom emoji id byte-identical between the
two — three tests check that, and a dropped placeholder breaks `.format()` at
runtime.

### LoLdle in English
`data/loldle/locales/en.json` needs its 173 entity names, their aliases, and the
attribute labels for champion class, species, resource, range and region. It is
the dataset owner's call. Once filled, `test_locale_coverage` will require it to
stay filled and the `KNOWN_INCOMPLETE_MINIGAMES` exception in that test should be
removed.

### A per-guild language
One `language` setting still decides what the bot says everywhere, while the
dashboard's display language is per-browser. A managed deployment needs the guild
to choose. That means a new typed setting plus a `t()` that takes a guild
context, and it is the largest remaining item — tracked in `todo.md`.

### A third language
Only after there is a reason for one. It follows the old rule: identical keys,
empty values, human translator. `t()` will degrade it to English and then
Hungarian, so a partial third catalog is readable rather than blank — but
`SUPPORTED_LANGUAGES` must not list it until its minigame catalogs are complete,
or selecting it silently removes minigames.

---

## 5. Running the audit

```bash
python scripts/locale_audit.py            # full report
python scripts/locale_audit.py --brief    # counts and problems only
python scripts/locale_audit.py --json     # machine-readable
```

Exit code is non-zero only when a referenced key is missing — the one class of
finding that is unambiguously a defect. Everything else is a number to read.
