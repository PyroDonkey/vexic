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
