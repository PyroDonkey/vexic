# Hosted storage cutover starts Turso-only with a deferred Neon control plane

Status: accepted

## Context

ADR audit AUDIT-002 (COA-264) found the deployed hosted alpha runs plain
SQLite files on a Railway volume for both customer memory databases and the
control-plane catalog, rather than the managed Turso (customer memory) and Neon
Postgres (control plane) posture that ADR 0005 and ADR 0008 name as readiness
targets. COA-232 ran only the Railway-volume drill and recorded Turso PITR and
Neon recovery as blocked follow-ups.

ADR 0008 frames Turso/Neon as readiness targets, not day-one bootstrap, and its
Consequences say the encryption/backup decision closes "without adding provider
SDKs, hosted secrets, billing, dashboards, public HTTP, or production operations
code to `src/vexic`." ADR 0013 records that the hosted FastAPI adapter and
control-plane surface live in `src/vexic` as hosted-adapter code, and that
splitting the memory app and control-plane app into separate processes "must be
recorded as a superseding or updated ADR... Do not split silently."

The cutover therefore needs a decision of record for how it is done: which
providers, where adapter code lives, the trust and secret surface, and the
process topology. This ADR provides that and intentionally scopes the first
cutover smaller than ADR 0008's full readiness target.

## Decision

The hosted storage cutover starts **Turso-only** as a bootstrap posture with no
customer-data-readiness claim.

- **Customer memory databases** move to managed Turso/libSQL, one isolated
  Customer Memory Database per customer tenant (unchanged from ADR 0005).
- **The control-plane catalog, API-key store, and operational telemetry** move
  to a managed Turso/libSQL database, reusing the existing SQLite-shaped catalog
  schema through a shared connection seam. Neon Postgres for the control plane
  is **deferred, not abandoned** (see Deferred).
- Both stores reach managed hosting through one new `connect(target)` boundary
  in `src/vexic` so the local SQLite path, the hosted libSQL path, and the
  catalog share a single provider abstraction.

This is the smallest cutover that closes the managed-hosting and Turso PITR gap
COA-232 recorded, without a SQLite-to-Postgres rewrite of the catalog.

### Adapter boundary and secrets

The pure connection seam and the libSQL-using storage adapter logic live in
`src/vexic`, consistent with the ADR 0013 precedent that hosted adapters live in
`src/vexic`. They accept an already-resolved target — a filesystem path or an
authenticated libSQL DSN — and never read provider secrets. The libSQL client
is added under the `hosted` optional dependency extra.

Provider-credential wiring — reading Turso database tokens from hosted
environment or secret management and constructing the authenticated libSQL DSN —
lives in the repo-root `adapters/` directory, which `docs/ai/AGENTS.md`
designates for provider-secret and live-model wiring rather than `src/vexic`
(alongside the existing `adapters/openrouter_live_adapter.py`). The hosted
service factory may read a non-secret backend-selection flag in `src/vexic`, but
resolves the authenticated DSN through the `adapters/` layer and passes it to the
seam.

This clarifies ADR 0008: a provider storage driver used by an in-package adapter
is in-bounds for `src/vexic`, but raw database tokens, credential reads, and
secret rotation stay out of `src/vexic`, as do billing, dashboards, hosted auth
stacks, and Console runtime. Raw tokens are read from the environment only and
never stored in `src/vexic` or committed.

### Topology

The hosted app remains a single co-deployed FastAPI process
(`vexic.hosted_control_plane_http:create_app`) that calls managed libSQL over
the network. This cutover does not split the memory app and control-plane app
into separate processes or services. A future process split remains an
ADR-0013-governed decision and is coupled to the durable-quota work in COA-263;
it is not pulled forward here.

### Vector and FTS parity

Customer memory vector search currently uses the `sqlite-vec` loadable
extension (`vec0` virtual tables, `serialize_float32`). Managed Turso may not
permit loading arbitrary extensions on a remote connection and may instead
require native libSQL vectors with a different API. The hosted adapter's vector
and FTS implementation is decided by a verification spike against a real Turso
database, not assumed. Whatever path is chosen must prove equivalent retrieval
semantics through the same storage-adapter conformance suite ADR 0005 requires
for both the local SQLite reference adapter and the hosted SQLite-compatible
adapter.

The spike (COA-264 slice 264c) resolved this. `sqlite-vec` cannot load on a
managed remote libSQL connection (no `enable_load_extension`), so the hosted
adapter uses native libSQL vectors: an `F32_BLOB` column with a brute-force
`vector_distance_cos` scan. The native ANN index (`vector_top_k`) returned no
rows in the spike and is not used; an exact scan is correct at per-customer
memory-database scale. FTS5 has full parity and is unchanged. Both are proven
equivalent on the local sqlite-vec and hosted libSQL backends by the
parametrized storage-adapter conformance suite
(`tests/test_storage_conformance.py`), with the backend selected from the live
connection type behind the one `connect()` seam. `PRAGMA journal_mode=WAL` is
skipped on libSQL (Turso rejects it and manages WAL server-side); all other
schema pragmas were verified to work remotely.

### Migration

The cutover is greenfield: fresh empty Turso databases are provisioned and
disposable dogfood data is recreated rather than migrated. Because ADR 0005
makes export/import/replay the canonical cross-backend move mechanism, and the
restore drill needs it, the existing operator-run canonical migration (ADR
0011, `vexic.migration`) is extended to accept a libSQL target and is drilled on
a synthetic fixture. This updates the hosted-migration runbook's "No Postgres
adapter / no physical file copy" expectation to include libSQL-target import.

### Backup, restore, and readiness

Turso PITR becomes the recovery mechanism for both the customer memory databases
and the Turso-hosted control-plane catalog. The customer-readiness restore drill
closes COA-232's Turso PITR row. The Neon control-plane recovery row stays
explicitly deferred with the Neon promotion below. Restore preserves the ADR
0005/0008 path: restore to an isolated replacement database, verify, rebuild
projections, atomically repoint the catalog, quarantine the stale handle, and
hold the one-customer-to-one-active-database invariant.

## Deferred

Neon Postgres for the control plane remains the ADR 0008 readiness target and is
promoted before external-customer memory, or sooner when the control plane needs
Postgres-grade write concurrency, audit/usage analytics, or Neon PITR. That
promotion is its own ticket and its own control-plane recovery drill, and it
re-checks whether external control-plane state plus COA-263 quota motivate a
process split under ADR 0013. AWS S3 Object Lock export hardening (ADR 0008)
also stays deferred until external beta or real customer memory.

## Consequences

- The cutover closes the COA-232 managed-hosting and PITR gap with one provider
  signup and no SQLite-to-Postgres rewrite.
- `src/vexic` remains both the local reference adapter home and the hosted
  adapter home; the new `connect(target)` seam is the single storage boundary.
- The Turso-only posture makes no customer-data-readiness claim; ADR 0008's full
  posture (Neon control plane, verified Neon PITR, S3 export hardening,
  successful drills) is still required before external customer memory.
- A second, well-bounded cutover (libSQL catalog to Neon) is incurred later, by
  design, in exchange for a much smaller first step.
- This ADR does not by itself make hosted Vexic customer-ready.

## References

- ADR 0005 — Hosted v1 memory storage starts SQLite-compatible and Postgres-ready
- ADR 0008 — Hosted data protection uses provider encryption, PITR, and drilled exports
- ADR 0011 — Operator-run canonical migration
- ADR 0013 — Hosted control-plane HTTP API is a console-facing adapter slice
- COA-264 (this cutover), COA-232 (restore drills), COA-263 (durable quota), COA-27 (security-gap umbrella)
- `docs/runbooks/hosted-migration.md`, `docs/runbooks/restore-drills/hosted-restore-drill.md`
