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

- Deploy `2.0.0-rc.1` to the private guild for seven days and complete
  `docs/performance_recovery_plan.md` without unresolved money or authorization
  failures.
- Freeze changes, create release notes and rollback instructions, then export a
  sanitized working tree to a separate empty public repository.
- Scan every exported ref and file. Never change this development repository's
  visibility and never mirror its Git history.
