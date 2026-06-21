# Hosted v1 memory storage starts SQLite-compatible and Postgres-ready

Status: accepted

Hosted v1 stores each customer's memory data in one isolated
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

Tenant isolation tests should prove that authenticated tenant identity maps to
one database handle, caller payload cannot switch tenants, cross-customer
search/export/replay/delete attempts fail, tombstones stay inside the target
customer database, and restoring one customer database cannot expose another
customer's memory.

The backup and restore unit is the Customer Memory Database. Hosted operations
must also back up the separate routing catalog that maps customers to databases,
and restore should create or verify an isolated replacement database before
repointing that catalog.

Schema migrations fan out across customer databases. The hosted layer needs a
catalog of database id, schema version, migration status, and backup checkpoint
so a failed customer migration can stop safely without corrupting other
customers. Cross-backend moves should use export/import/replay plus projection
rebuild instead of depending on physical file compatibility. Hosted v1 should
treat each customer database as independent rather than relying on provider
shared-schema features.
