# The legal documents, and how to keep them true

This directory holds the Privacy Policy and the Terms of Service for the
installations the maintainer operates, in English (binding) and Hungarian
(translation):

| File | What |
| --- | --- |
| `PRIVACY.md` | Privacy Policy (English, prevails) |
| `PRIVACY.hu.md` | Adatkezelési tájékoztató (Hungarian translation) |
| `TERMS.md` | Terms of Service (English, prevails) |
| `TERMS.hu.md` | Felhasználási feltételek (Hungarian translation) |

They were written from a line-by-line inventory of the code, the schema, the
log sinks and every outbound network call, recorded in
`docs/data_inventory.md`. **That inventory is the record of processing; these
documents are its public summary.** When the inventory changes, these change.

## Before publishing

Discord's Developer Terms (section 5(a)) oblige every application to "provide
and adhere to a privacy policy" and to "maintain publicly available,
up-to-date links to your privacy policy in the Developer Portal". The Terms of
Service URL is optional there but shown on the OAuth authorisation screen,
which the dashboard uses.

1. **Fill every placeholder**, in all four files, with the same values:

   | Placeholder | Meaning |
   | --- | --- |
   | (filled 2026-10-31) operator name | Máté Domina (Hungarian order, Domina Máté, in the `.hu.md` files). The GDPR (Art. 13(1)(a)) requires the identity of the controller; a pseudonym alone is weak. |
   | (filled 2026-10-31) operator country | Hungary; also the governing law in `TERMS.md` §12. |
   | (filled 2026-10-31) contact email | `privacy@potatobot.win`, a Cloudflare Email Routing alias forwarding to a mailbox the operator reads; the policy's Cloudflare row says so. Never a work address belonging to an employer. Delivery verified with a test mail on 2026-10-31. |
   | (filled 2026-10-31) Discord handle | `@woodenpotato`, the operator's Discord username. |
   | (filled 2026-10-31) hosting country | Hungary, the maintainer's own hardware. |
   | (filled 2026-10-31) effective date | 2026-10-31, version 1.0 of both documents; the draft notes are gone. A material change sets a new date and bumps the version. |
   | (removed 2026-10-31) the public application | Its row in §1.2 is deleted until the application exists and serves a server. Adding it back is a material change: name, dashboard address, new effective date, both languages. |
   | (filled 2026-10-31) alert email | Whether `POTATOBOT_ALERT_EMAIL_TO` is set on that host, and the provider. Currently "not enabled"; if it is ever switched on, name the provider in §6.2 of both files. |
   | (filled 2026-10-31) retention | The `data_retention_days` instance setting's live value: 365 days, set by the maintainer on 2026-10-31. Change the number in §7 of both files if the setting changes. |
   | (filled 2026-10-31) migration backups | "90 days after the migration they protect", enforced by `--prune-local-backups-days 90` on the offsite backup unit's `ExecStart` (`deploy/potatobot-offsite-backup-podman.service`), installed on the host and verified byte-identical on 2026-10-31. Changing the number means changing §7 in both files. |
   | (resolved 2026-10-31) disk encryption | The marker is gone and the sentence stays: the host disk is unencrypted. When an encrypted volume holds the data directory, delete the sentence in §14 of both files. |

   Every placeholder is one bracketed `UPPER_CASE` token, so this finds them
   all, and must come back empty before the effective date is set:

   ```
   grep -rnE '\[[A-Z][A-Z_]+\]' docs/legal/*.md
   ```

   `tests/test_legal_drift.py` enforces the same rule the other way round: a
   file whose `Effective date` is no longer a placeholder may contain no
   placeholder at all.

2. **Publish** the beta snapshot as usual (`/release`); `docs/legal/` ships
   with it. The public URLs are then

   ```
   https://github.com/WoodenPotatos/potatobot/blob/beta/docs/legal/PRIVACY.md
   https://github.com/WoodenPotatos/potatobot/blob/beta/docs/legal/TERMS.md
   ```

3. **Enter the URLs** in the Discord Developer Portal, on the application's
   *General Information* page, in *Privacy Policy URL* and *Terms of Service
   URL*. Each installation is its own application and needs the same two URLs.

4. **They are reachable from the application** (also Developer Terms 5(a)):
   `/version` links both, through `PRIVACY_POLICY_URL` and
   `TERMS_OF_SERVICE_URL` in `core/version.py`, and a test checks that each URL
   names a file that exists. Moving or renaming a file here means changing
   those constants in the same commit. Server administrators should also link
   the policy from their rules channel.

## Keeping them true

Three layers, from the one that works when nobody remembers to the one that
needs a person:

1. **Detection is mechanical.** `scripts/legal_drift.py check` states the
   facts the policy makes claims about (member tables and erasure lists, the
   schema, outbound hosts, log lines naming a user, `tasks.loop`s, features,
   the privacy-relevant settings, intents, the dashboard session, the
   retention constants) and compares them to `drift_stamp.json` in this
   directory, which records them as of the last review. `tests/test_legal_drift.py`
   runs the check in CI and `/release` runs it before publishing. A drift
   report names the fact and the section to re-read.
2. **The procedure is the `/legal-update` skill**: read the report, decide
   whether a *statement* changed, edit the English, mirror the Hungarian in the
   same change, move the version and effective date only for a material
   change, update `docs/data_inventory.md`, re-stamp. The stamp is committed
   with the edits, never alone; nothing automatic may run `stamp`.
3. **Facts the repository cannot see** are checked by hand, yearly and before a
   public instance opens, and the date recorded here:

   | Fact | Where it lives | Last checked |
   | --- | --- | --- |
   | Discord Developer Terms and Developer Policy (sections 5(a)–(c)) | support-dev.discord.com | 2026-10-31 (effective 2024-07-08) |
   | EU-US Data Privacy Framework status of Google LLC and Cloudflare, Inc. | dataprivacyframework.gov; providers' own pages | 2026-10-31 (providers' statements only) |
   | NAIH postal address, phone and email | naih.hu | 2026-10-31 |
   | Host disk encryption | the host | 2026-10-31: none |
   | `data_retention_days` live value; alert email on or off | the instance settings and `potatobot.env` | 2026-10-31: 365 days (set that day, from 0); alert email not configured |

The Hungarian files are hand-translated; the English text prevails, so a
disputed sentence is settled by `PRIVACY.md` / `TERMS.md`.

## What these documents are not

They are not legal advice and were not reviewed by a lawyer. They are written
to be accurate about what the software does, which is the part a lawyer cannot
check; a lawyer's review of the legal framing is still worth having before the
public instance opens.
