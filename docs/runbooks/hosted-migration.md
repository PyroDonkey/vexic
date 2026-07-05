# Hosted Migration Runbook

Role: operator procedure for local/self-host to hosted Customer Memory Database
migration.

This runbook implements ADR 0011. Migration is an operator-run
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
- The replacement database is new or contains only Vexic-owned schema tables.
  Host-owned extension tables in the replacement database must fail closed.
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
   - Confirm the local catalog rejects replacement databases imported for a
     different tenant/project.
   - Confirm schema/version mismatches are rejected.
   - Confirm redaction failures leave no artifact or replacement database.
   - Confirm source or replacement host-owned extension tables fail without an
     explicit plan.

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
- If a row id exists with different content, or if the replacement database has
  canonical rows outside the artifact, import fails closed. Investigate the
  replacement database rather than overwriting in place.

## Turso/libSQL Targets (ADR 0019)

`import_canonical_migration` accepts a `vexic.storage.StorageTarget` (a
resolved libSQL DSN plus auth token) as `target_db_path`, in addition to a
local filesystem path. Both `_record_import_metadata` and the pre-import
host-owned-table check go through the shared `connect()` seam, so a Turso
target and a local `.db` file run the same canonical-import logic; the only
branch is that a remote `StorageTarget` has no local file to `Path.exists()`
against, so the host-owned-table probe always connects rather than being
skipped for a "brand-new" file. This closes the "no Postgres adapter / no
physical file copy" note below for the libSQL case specifically — import
still writes canonical rows through SQL, never a raw file or page copy,
against either backend.

Resolving the actual Turso DSN and auth token for a target tenant/database is
an `adapters/` concern (see `docs/hosted-mvp.md#tursolibsql-storage-backend-coa-273`)
and stays outside this runbook's local-file-oriented preconditions section;
an operator migrating into a Turso-backed replacement still needs a
`StorageTarget` resolved out of band before calling `import_canonical_migration`.

## Restore Drill (PITR)

`vexic.restore.run_restore_drill` implements the verify-gated,
generation-stamped restore decision logic this runbook's replacement/repoint
steps depend on: provision a replacement, import the canonical artifact into
it, verify it, and only then activate it (repointing the catalog and bumping
the tenant's `generation` counter so a request-scoped service holding the
pre-repoint handle cannot keep writing the quarantined database); if
verification fails, the replacement is destroyed instead and the original
stays active. This orchestration function is pure — it takes the provision/
import/verify/activate/destroy steps as injected callables, reads no secrets,
and does no I/O of its own — so the decision logic is unit tested with fakes
independent of any real Turso account.

Running this drill against a **real Turso point-in-time-recovery snapshot**
is a separate, manual, operator-run exercise: an operator provisions a
replacement database from a Turso PITR restore point (Turso Platform API,
outside `src/vexic`), then wires that replacement into
`run_restore_drill`'s `provision_replacement`/`import_canonical`/`verify`/
`activate`/`destroy` callables. That live run has not yet been executed and
recorded as an artifact under `docs/runbooks/restore-drills/` — only the
decision logic above is automated and covered by tests today. Record any live
Turso PITR drill the same way the 2026-06-26 Railway alpha volume drill was
recorded, per the "Record evidence" step above.

## What This Does Not Do

- No public import API.
- No Postgres adapter.
- No customer self-serve artifact upload.
- No physical SQLite file/page copy (Turso/libSQL import is SQL-level too —
  see "Turso/libSQL Targets" above).
- No migration of host-owned extension tables without a separate host plan.
- No completed live Turso PITR restore-drill run yet (decision logic only —
  see "Restore Drill (PITR)" above).
