# Release checklist

## Code and data

- All tests and compile checks pass on Python 3.12, 3.13, and 3.14.
- A production-shaped legacy database migrates to `database.LATEST_SCHEMA_VERSION` twice without changes
  on the second run; integrity check, counts, and checksums are recorded.
- Pending wager, duplicate settlement, booster deduplication, guild isolation,
  realm approval, opt-out, authorization loss, and rollback paths are exercised.
- Gacha soft/hard pity boundaries, sequential ten-pull reset, atomic debit,
  duplicate vault compensation, inventory consumption, voucher activation,
  fulfillment expiry, and custom-shop refund paths are exercised.
- A fresh backup and tested restore procedure exist before deployment.

## Security and operations

- `pip-audit`, Bandit, secret scan, and dependency review are clean or have a
  documented accepted risk.
- Discord permissions are least-privilege; dashboard is loopback-only behind
  HTTPS; `.env`, database, backups, session secret, config, and logs are absent
  from the export.
- OAuth callback/origin values match exactly and production uses Waitress under
  service supervision.
- Monitoring covers process health, event-loop lag, database queue time,
  background-loop failures, disk usage, and backup age.

## Canary and publication

- Deploy the alpha to the private guild and complete
  `docs/performance_recovery_plan.md` without unresolved money or authorization
  failures. The version is whatever `pyproject.toml` says; naming one here is how
  this file goes stale.
- Freeze changes, then publish with `python scripts/publish_public.py --promote`.
  It is the only thing that builds a snapshot: it derives the file list from
  `git ls-files`, promotes the alpha to the next beta inside the built tree, and
  runs its whole preflight before writing a byte.
- **The README and the changelog are part of the artefact.** Promotion merges the
  leading run of `Unreleased` sections under the published heading and regenerates
  the README inside the tree; the publisher then refuses if any `Unreleased`
  survives, if the README does not name the release, or if it has lost a
  generated marker. Five public betas shipped with a stale heading before that
  check existed — read `README.md` in the built tree once, and confirm what a
  visitor will see on the front page.
- Scan every exported ref and file. Never change this development repository's
  visibility and never mirror its Git history.
