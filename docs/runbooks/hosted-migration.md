# Hosted Migration Runbook

Role: operator procedure for local/self-host to hosted Customer Memory Database
migration.

This runbook implements ADR 0011 for COA-202. Migration is an operator-run
canonical row move, not a public `MemoryService` operation, hosted HTTP
endpoint, or customer self-serve import API.

## Preconditions

- The operator has a hosted tenant and project already provisioned.
- The source database is a Vexic local/self-host SQLite database.
- The replacement database path is a new file directly under the hosted root.
- Forbidden values for the tenant, host secrets, provider keys, database tokens,
  and support-only markers are available for redaction checks.
- Any host-owned extension table has an explicit host migration plan. Without
  that plan, canonical export must fail closed.
- The old active Customer Memory Database remains available for rollback until
  the migration evidence is accepted.

## Procedure

1. Preflight the source database.
   - Confirm the source is the intended tenant/project source.
   - Confirm no raw provider secrets or configured forbidden values are present.
   - Confirm host-owned extension tables are either absent or covered by a
     separate host-owned migration plan.

2. Export the canonical artifact.

   ```powershell
   uv run python -c "from vexic.migration import export_canonical_migration; export_canonical_migration('source.db', 'canonical-migration.json', tenant_id='tenant-a', project_id='project-a', forbidden_secret_values=('secret-value',))"
   ```

   The artifact contains only Vexic canonical rows and excludes rebuildable FTS
   and vector projection tables.

3. Import into a replacement Customer Memory Database.

   ```powershell
   uv run python -c "from vexic.migration import import_canonical_migration; import_canonical_migration('canonical-migration.json', 'replacement.db', tenant_id='tenant-a', project_id='project-a', forbidden_secret_values=('secret-value',))"
   ```

   Import validates the artifact version and hosted operator tenant/project
   scope before creating or writing the replacement database. Re-running the
   same import is idempotent; conflicting existing rows fail closed.

4. Verify the replacement database before repoint.
   - Search transcript for the synthetic fixture.
   - Search long-term memory for the promoted fact fixture.
   - Re-ingest the same source transcript key and confirm it is skipped.
   - Confirm tenant/project spoofed artifacts are rejected.
   - Confirm schema/version mismatches are rejected.
   - Confirm redaction failures leave no artifact or replacement database.
   - Confirm host-owned extension tables fail without an explicit plan.

5. Repoint the local hosted catalog.

   ```powershell
   uv run python -c "from vexic.hosted_local import HostedTenantCatalog; HostedTenantCatalog('.hosted-memory').activate_replacement_database('tenant-a', 'replacement.db')"
   ```

   The catalog activation step is separate from import. Until it succeeds, the
   old database remains active. The local catalog stores one active database
   handle per tenant.

6. Smoke the normal hosted route.
   - `search_transcript` returns the migrated transcript fixture.
   - `search_long_term` returns the migrated durable fact fixture.
   - Another tenant/project cannot read the migrated fixture.

7. Record evidence in `docs/runbooks/migration-drills/`.
   Evidence must use redacted tenant/database identifiers. Do not store raw API
   keys, provider secrets, database tokens, unredacted database handles, or raw
   customer memory payloads.

## Rollback And Retry

- Before catalog activation, rollback is deleting the replacement database and
  rerunning export/import after fixing the failure.
- After catalog activation, rollback is a catalog repoint back to the old
  database handle while both files are retained.
- Re-running the same import against the same replacement database imports zero
  new rows and leaves existing canonical rows unchanged.
- If a row id exists with different content, import fails closed. Investigate the
  replacement database rather than overwriting in place.

## What This Does Not Do

- No public import API.
- No Postgres adapter.
- No customer self-serve artifact upload.
- No physical SQLite file/page copy.
- No migration of host-owned extension tables without a separate host plan.
