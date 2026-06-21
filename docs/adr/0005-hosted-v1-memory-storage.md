# Hosted v1 memory storage starts SQLite-compatible and Postgres-ready

Status: accepted

Hosted v1 stores each customer tenant's memory data in one isolated
SQLite-compatible Customer Memory Database. The hosted adapter may use managed
libSQL-compatible storage for this boundary, while local SQLite remains the
reference and self-host adapter shape. Shared Postgres tables with `tenant_id`
and RLS remain deferred.

## Considered Options

- Per-customer SQLite/libSQL: selected because it preserves the current
  structural isolation model, keeps export/delete/restore customer-sized, and
  avoids a premature Postgres rewrite.
- Postgres database per customer: valid future migration target when concrete
  write-concurrency, compliance, VPC, backup, audit, or operational requirements
  justify it.
- Postgres project per customer: stronger infrastructure isolation, but too
  heavy for hosted v1.
- Postgres schema per customer: rejected for v1 because it shares a database
  boundary and depends on schema/search-path discipline.
- Shared Postgres tables with RLS: deferred until Vexic has explicit
  shared-storage threat modeling, isolation tests, audit posture, and operations
  maturity.

## Consequences

Hosted storage code must keep the memory database behind the public
`MemoryService` contract and adapter boundary. New storage-sensitive decisions
should preserve a straightforward path to Postgres database-per-customer:
canonical rows must remain exportable/replayable, rebuildable projections must
not become source of truth, and SQLite-specific behavior must not leak into
hosted API semantics.

Customer tenant is the hosted tenant selected by authentication or authorized
tenant selection. Project, user, and session scopes may share a Customer Memory
Database and remain security-critical `MemoryScope` filters inside that
database; database-per-customer only removes cross-customer shared-table risk.

Tenant isolation tests should prove that authenticated tenant identity maps to
one database handle, caller payload cannot switch tenants, cross-customer
search/export/replay/delete attempts fail, tombstones stay inside the target
customer database, and restoring one customer database cannot expose another
customer's memory.

Hosted v1 launch gates should include storage adapter conformance tests for the
local SQLite reference adapter and the hosted SQLite-compatible adapter. Those
tests must cover contract behavior, FTS/vector retrieval parity, export/import,
replay, rebuild, tombstones, and redaction behavior. If hosted vector search
uses libSQL-native vector behavior rather than local `sqlite-vec`, the adapter
must prove equivalent retrieval semantics through the same conformance suite.
Current local tenant-database tests do not replace these hosted adapter gates.

The backup and restore unit is the Customer Memory Database. Hosted operations
must also back up the separate routing catalog that maps customers to databases,
and restore should create or verify an isolated replacement database before
repointing that catalog. The routing catalog must preserve a one-customer to
one-active-database invariant, detect orphaned databases and stale handles, and
make catalog repointing atomic from the hosted API's perspective.

Schema migrations fan out across customer databases. The hosted layer needs a
catalog of database id, schema version, migration status, and backup checkpoint
so a failed customer migration can stop safely without corrupting other
customers. The adapter must tolerate a mixed-version fleet during expand/contract
migrations, and failed migrations must retry or roll forward per customer
without requiring cross-customer rollback. Cross-backend moves should use
export/import/replay plus projection rebuild instead of depending on physical
file compatibility. Hosted v1 should treat each customer database as independent
rather than relying on provider shared-schema features.

Postgres database-per-customer becomes the preferred migration target only when
measured requirements justify it, such as sustained write contention, hosted
database count or migration fan-out limits, customer VPC/data-residency demands,
backup or audit SLA gaps, or vector/search behavior that cannot be met by the
SQLite-compatible adapter.
