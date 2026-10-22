# Release checklist

## Code and data

- CI is green on Python 3.12, 3.13 and 3.14: `compileall`, `mypy` (scoped to
  `core/`), the unittest suite (with Node installed, so the dashboard harnesses
  run rather than skip), `scripts/locale_audit.py --brief`, and the container
  build. `.github/workflows/ci.yml` is the authority for the exact commands.
- A production-shaped legacy database migrates to `database.LATEST_SCHEMA_VERSION`
  twice without changes on the second run (`scripts/rehearse_migration.py`);
  integrity check, counts, and checksums are recorded.
- Pending wager, duplicate settlement, booster deduplication, guild isolation,
  realm approval, opt-out, authorization loss, and rollback paths are exercised.
- Gacha soft/hard pity boundaries, sequential ten-pull reset, atomic debit,
  duplicate vault compensation, inventory consumption, voucher activation,
  fulfillment expiry, and custom-shop refund paths are exercised.
- A fresh backup and tested restore procedure exist before deployment.

## Security and operations

- `pip-audit`, Bandit, the gitleaks secret scan, and dependency review are clean
  or have a documented accepted risk.
- Discord permissions are least-privilege; the dashboard binds to loopback and is
  reached only through an HTTPS proxy; `.env`, the database, backups, the session
  secret, and logs are absent from the export.
- OAuth callback/origin values match exactly and production uses Waitress under
  service supervision.
- Monitoring covers process health, event-loop lag, database queue time,
  background-loop failures, disk usage, and backup age.

## Canary and publication

- Deploy the alpha to the private guild and complete
  `docs/performance_recovery_plan.md` without unresolved money or authorization
  failures. The version is whatever `pyproject.toml` says; naming one here is how
  this file goes stale.
- Freeze changes, then publish with `python scripts/publish_public.py --promote`,
  after a `--dry-run`. It is the only thing that builds a snapshot: it derives the
  file list from `git ls-files`, promotes the alpha to the next beta inside the
  built tree, and runs its whole preflight before writing a byte.
- **A published tree has to pass its own CI, and the publisher checks it.** After
  verifying the tree, it runs the whole suite inside a copy of it, which needs
  `npm` on `PATH` and the development lock installed. A failure is a refusal.
- **The README and the changelog are part of the artefact.** Promotion merges the
  leading run of `Unreleased` sections under the published heading and regenerates
  the README inside the tree; the publisher then refuses if any `Unreleased`
  survives, if the README does not name the release, or if it has lost a
  generated marker. Read `README.md` in the built tree once, and confirm what a
  visitor will see on the front page.
- Scan every exported ref and file. Never change this development repository's
  visibility and never mirror its Git history.
