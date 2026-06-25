# Migration Drill: Local Canonical Migration 2026-06-25

Status: pass

## Summary

- Drill ID: COA-202-local-canonical-2026-06-25
- Environment: local test harness
- Runbook: `docs/runbooks/hosted-migration.md`
- Source: synthetic local Vexic SQLite database
- Target: synthetic hosted replacement Customer Memory Database
- Customer-data readiness claim: no

## Evidence

The drill is backed by `tests/test_operator_migration.py`:

- canonical export -> hosted import -> projection rebuild -> catalog repoint;
- transcript search returns the migrated transcript fixture;
- long-term search returns the migrated durable fact fixture;
- source transcript ledger prevents duplicate replay on re-ingest;
- tenant/project spoofed artifact scope is rejected before target DB creation;
- local catalog activation rejects a replacement DB imported for another tenant;
- unsupported artifact version is rejected before target DB creation;
- configured forbidden values fail before artifact or target DB persistence;
- overwrite export redaction failure removes a stale artifact;
- failed import leaves the catalog on the old active database;
- re-running the same import is idempotent;
- extra target canonical rows fail closed instead of being blessed as idempotent;
- source or replacement host-owned extension tables fail closed without a
  migration plan;
- canonical export does not create vector projection tables in the source DB;
- local hosted catalog has one active database handle for the tenant after
  activation.

Fresh command for this drill:

```powershell
uv run pytest tests/test_operator_migration.py
```

Recorded result after adversarial review fixes: `13 passed`.

## Sanitization

The fixture uses synthetic tenant, project, source, transcript, and fact values.
No raw API keys, provider secrets, database tokens, or customer memory payloads
are stored in this evidence record.

## Limits

This local drill proves the operator path and failure behavior for the local
SQLite/libSQL-compatible adapter. It does not prove a production hosted restore
SLA, cross-region recovery, Postgres migration, customer self-serve import,
billing/account migration, or host-owned extension table migration.
