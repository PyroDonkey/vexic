# Hosted migration is operator-run canonical row migration

Status: accepted

Hosted local/self-host migration uses an operator-run canonical row migration
path, not a public `MemoryService` operation or hosted HTTP endpoint in v0.1.
The migration path preserves source memory records and provenance through a
versioned, backend-neutral canonical migration artifact, imports into an
isolated replacement Customer Memory Database, verifies schema, canonical rows,
tenant isolation, redaction behavior, and provenance, rebuilds derived
projections, then atomically repoints the hosted routing catalog.

The export payload used by `export_scope` is not the migration artifact. It is
a scoped privileged egress artifact and must not be treated as sufficient for
lossless local/self-host to hosted migration. A migration artifact must avoid
SQLite-specific semantics such as physical pages, `rowid` dependence, FTS
implementation details, and vector index internals. FTS, vector tables, and
other projections remain rebuildable after import.

Tenant and project authority comes from the hosted operator context and routing
catalog, not from the artifact payload. Redaction is a fail-closed integrity
gate: if configured forbidden values are found during export, import,
verification, logging, or evidence generation, the migration stops without
making the imported database active or persisting raw payload evidence.

Host-owned extension tables remain host-owned. The migration path must preserve
known Vexic canonical rows and either carry an explicit host-owned extension
migration plan or fail closed rather than silently dropping or claiming
ownership of extension-table semantics.

## Consequences

- COA-202 should deliver an operator runbook and drill, not a public import API.
- A public import API is deferred until there is a concrete self-serve migration
  requirement and separate decisions for artifact storage, resumability,
  authorization, rate limits, audit, support, and compatibility guarantees.
- The drill must prove lossless canonical round-trip, payload tenant/project
  spoofing rejection, cross-tenant isolation, redaction fail-closed behavior,
  retry/idempotency, rollback before catalog repoint, atomic one-active-database
  catalog state, projection rebuild, and schema/version mismatch handling.

## Addendum — 2026-07-16: v1 artifact compatibility is additive-tolerant (COA-387)

The original import required each artifact row's column set to exactly equal
the current target schema's columns, so every additive `_ensure_column`
schema migration silently invalidated all previously exported v1 artifacts
while `ARTIFACT_VERSION` still claimed they were supported.

Decision: v1 artifacts are additive-tolerant.

- The importer accepts artifact rows that are missing columns the target
  schema has. Missing columns are omitted from the insert so the storage
  backend fills the schema `DEFAULT` (or `NULL` for a nullable column with no
  default).
- Import fails closed when a missing column is `NOT NULL` with no default
  (no safe fill exists), when artifact rows carry columns unknown to the
  target schema, and when rows within one table carry mixed column sets.
- `ARTIFACT_VERSION` stays `vexic.canonical-migration.v1` across additive
  schema changes. A version bump is reserved for column renames, removals, or
  semantic changes that additive tolerance cannot absorb.
- The canonical-row insert loop runs inside one explicit `BEGIN`/`COMMIT`
  transaction. A bare `with conn:` opens no transaction on libSQL/Hrana (each
  statement auto-commits), so only the explicit transaction makes a failed
  import leave zero canonical rows behind on both backends.

Cross-version behavior (export at schema N, import at schema N+1) is pinned by
tests in `tests/test_operator_migration.py` and
`tests/test_migration_libsql.py`.
