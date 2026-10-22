# Localization: state and rules

Re-generate every figure here with `python scripts/locale_audit.py`. Last
measured **2026-10-16**: every catalog, general and minigame, is 100% filled in
both languages, and the audit reports 0 missing keys and 0 unlocalized
user-visible literals. LoLdle became fully bilingual across both datasets on
2026-09-27, when its ownership exemption ended, `champions` was filled for
English and `hard_mode` gained its last two champions (Yunara, Zaahen) and was
filled for English too.

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
7. **`data/loldle/`'s ownership exemption ended 2026-09-27.** Its external
   maintainer left, and `champions` was filled for English in the same change
   — no dataset is owner-exempt any more. `hard_mode` (the redacted-lore hard
   mode) was filled for English the same day, so it loads in both languages; a
   dataset added later that is Hungarian-only would be taken offline in English
   by `load_or_disable`, like any unfinished-for-one-language dataset.

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
| **The LoLdle exception is lifted, and stays lifted** | `test_locale_coverage` |
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
| General catalog (2056 keys) | **100%** | **100%** |
| … of which the dashboard namespace (965 keys) | 100% | 100% |
| Runtime-composed key families (33) | 100% | 100% |
| Literal keys referenced in source (1364) | 0 missing | 0 missing |
| Valdle data (65 strings) | 100% | **100%** |
| DbDle data (136 strings) | 100% | **100%** |
| Genshindle data (191 strings) | 100% | **100%** |
| LoLdle data, both datasets (677 strings) | 100% | **100% — ownership exemption ended 2026-09-27** |

The audit also lists **56 unreferenced catalog keys**. They are not a gate and
not automatically dead: some belong to a computed family the audit does not
model, and some were retired deliberately (see section 3). Read why a key was
orphaned before wiring it back up or deleting it.

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
- Valdle, DbDle, Genshindle and every `/loldle` difficulty, `hard` included →
  English.

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
`data/<game>/locales/<lang>.json`. The English catalogs generated for Valdle,
DbDle, Genshindle and both LoLdle datasets copy the entity names — they are already
English proper nouns — and translate the attribute labels (`hard_mode`'s
"attribute" is a full lore quote per champion rather than an enum, but the
same opaque-id mechanism carries it without any loader change).

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
- **Discord audit-log reasons** moved into the catalogs
  (`roleselect.audit_reason`, `tickets.audit_reason_setup_failed`,
  `shop.audit_reason_setup_failed`, and later
  `moderation.escalation_audit_reason`). Server staff read those in the audit
  log, so the project's own rule covers them. `shop.audit_reason_setup_failed`
  has since lost its caller — ticket setup moved to `core/support_tickets.py`,
  which uses the tickets key — and is one of the unreferenced keys above.
- **`t()` was rewritten** for the fallback chain and the format-argument split
  described above.
- **`language` became a constrained choice** with a dashboard dropdown.

A playback failure used to be logged and told the member nothing. The key that
once reported it (`music.playback_error`) was deleted rather than revived,
because it interpolated the raw error; the `after` callback in `cogs/music.py`
now posts the generic `music.playback_failed` instead, which names the failure
without quoting the exception, and the track still auto-advances.

---

## 4. Remaining work

### Adding a key
Add it to `locales/hu.json` **and** `locales/en.json` in the same change, then run
`python scripts/sync_locale_keys.py`. It will tell you if English is still blank.
Keep every `{placeholder}`, emoji and custom emoji id byte-identical between the
two — three tests check that, and a dropped placeholder breaks `.format()` at
runtime.

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
