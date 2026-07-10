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
`src/vexic`. They accept an already-resolved target -- a filesystem path or an
authenticated libSQL DSN -- and never read provider secrets. The libSQL client
is added under the `hosted` optional dependency extra.

Provider-credential wiring -- reading Turso database tokens from hosted
environment or secret management and constructing the authenticated libSQL DSN --
lives in the repo-root `adapters/` directory, which `AGENTS.md`
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

- ADR 0005 -- Hosted v1 memory storage starts SQLite-compatible and Postgres-ready
- ADR 0008 -- Hosted data protection uses provider encryption, PITR, and drilled exports
- ADR 0011 -- Operator-run canonical migration
- ADR 0013 -- Hosted control-plane HTTP API is a console-facing adapter slice
- COA-264 (this cutover), COA-232 (restore drills), COA-263 (durable quota), COA-27 (security-gap umbrella)
- Hosted migration and restore-drill runbooks (maintained in the private hosted-ops repository)

## Addendum -- 2026-07-01: implementation clarifications (verification + multi-model audit)

This addendum refines the Decision for implementation; it does not reverse ADR
0019. It records findings from a real-Turso verification spike and a multi-model
design audit. One spike finding that constrains all adapter code: the managed
libSQL connection supports `execute`/`executemany`/`cursor`/`commit`/`rollback`/
`close` and `with conn:`, but has no settable `row_factory` (use dict-row
helpers), no named/dict parameters, and no `enable_load_extension`.

1. **The token is not carried in the DSN.** Empirically, a libSQL token embedded
   in the URL (`?authToken=`) returns 401; the managed client authenticates only
   via the separate `auth_token` argument. So the "already-resolved target ... an
   authenticated libSQL DSN" language above is realized as a secret-bearing
   `StorageTarget{target, auth_token}` handle whose token is passed to
   `connect(target, auth_token=...)`, held only in memory, redacted in
   `repr`/logs, and never embedded in the DSN or persisted raw.
2. **Per-tenant DB tokens are minted short-lived, not persisted raw.** The
   catalog stores non-secret target metadata only (DSN, provider, generation).
   Per-tenant DB auth tokens are minted short-lived and DB-scoped through the
   Turso Platform API in `adapters/` and cached in-process with a TTL. If
   measured latency forces persistence, store them encrypted (AES-GCM) under an
   adapters-only `VEXIC_CONTROL_DB_SECRET_KEY` that never enters `src/vexic`.
3. **Schema init is once-per-target, not per-call.** `init_db` runs on every
   storage call; against remote libSQL that is a per-request DDL round-trip. A
   process-level init-once memo keyed by (target, schema generation), guarded by
   a lock and set only after commit, is required. Local behavior is unchanged.
4. **Filesystem-coupled control-plane ops guard to local targets.**
   `_ensure_control_db_permissions` (`os.open`/`chmod`) and
   `activate_replacement_database` `Path` checks run only for local filesystem
   targets; remote targets use a DSN-based replacement validator.
5. **Restore is verify-gated and generation-stamped.** The PITR restore drill
   activates the replacement only after verification passes (else re-activate the
   original and destroy the replacement); the catalog target carries a generation
   that bumps on repoint so request-scoped services cannot write the quarantined
   handle.
6. **Split-brain window acknowledged.** While customer memory is on Turso and the
   control-plane mapping could be lost, a Platform-API list-databases reconcile
   path recovers tenant->DB mappings; accepted for internal dogfood with a manual
   recovery note.
7. **Verified safe:** `enable_load_extension` is sqlite-vec-only (chosen by
   `select_vector_backend`); `init_db`/`init_vector_memory` do not require it on
   libSQL, so no change is needed there.

## Addendum 2 -- 2026-07-01: implementation landed (COA-273 P0-P5 complete)

All items in the Decision and the addendum above are implemented and, where
noted, live-verified against a real Turso database; see
`docs/hosted-mvp.md#tursolibsql-storage-backend-coa-273` for the
implementation-facing writeup; the operator-facing migration/restore-drill
procedure is maintained in the private hosted-ops repository. Summary of what
landed:

- The `StorageTarget`/`connect(target, auth_token=...)` seam (point 1 above),
  the per-target init-once schema memo (point 3), and the local-only
  filesystem guards on control-plane ops (point 4) are all in place, per this
  addendum's original description.
- Per-tenant Turso provisioning (`adapters/turso_adapter.TursoProvisioningPort`,
  `TenantTokenCache`, `make_customer_target_resolver`) replaced the earlier
  single-shared-database dogfood override entirely; the catalog stores a
  per-tenant `customer_target` DSN and a `generation` counter (point 2's
  "non-secret target metadata only" and point 5's generation stamp).
  `TenantTokenCache` mints short-lived, DB-scoped tokens and caches them
  in-process with a TTL shorter than the mint expiration; nothing is
  persisted raw, and the encrypted-persistence fallback point 2 allows for
  was not needed and was not built.
- The split-brain reconcile path (point 6) is implemented as a pure function,
  `adapters/turso_adapter.reconcile_tenant_databases`, over an
  already-fetched platform database list and the catalog's tenant mapping.
- The restore drill (point 5) is implemented as `vexic.restore.run_restore_drill`,
  a verify-gated, generation-stamped, pure orchestration function unit tested
  with fakes.
- Shared cross-backend exception classifiers
  (`src/vexic/storage/errors.py`: `is_unique_violation`, `is_operational_error`,
  `is_retryable_operational_error`) were added and adopted at every affected
  sqlite3-typed catch site, closing the libSQL bare-`ValueError` gap this
  addendum's verification spike surfaced.
- Live-verified on a real Turso database: the storage-adapter conformance
  suite, a customer-memory round-trip, and a full per-tenant
  provision -> round-trip -> destroy cycle. These live tests are
  creds-gated (`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`/the optional `libsql`
  extra) and skip cleanly without credentials, so the default test run stays
  green with zero Turso setup.

Known, deliberately deferred follow-ups (not blocking this ADR's acceptance,
tracked as fix-soon items rather than open decisions):

- `connect()` has no explicit timeout or retry/backoff on the hot path against
  remote libSQL.
- A live run of the restore drill against a real Turso point-in-time-recovery
  snapshot has not been executed; only the drill's decision logic is
  automated and tested. Recording that live run as a restore-drill artifact
  (in the private hosted-ops repository) remains outstanding.
- `TenantTokenCache` has no size-bounded eviction (an unbounded in-process
  dict).
- Some adapter type-annotation precision cleanup is outstanding.
- `run_restore_drill`'s best-effort compensating `destroy()` on an
  import/verify failure swallows its own exception so it can never mask the
  original failure, which means a broken teardown could silently leave a
  replacement database behind. Documented and accepted, not fixed.

## Addendum 3 -- 2026-07-06: read-then-write serialization on libSQL (COA-311)

The promotion pipeline has two atomic read-then-write paths where a stale read
followed by a dependent write would corrupt Tier-2 state under concurrent Light
runs: the watermark compare-and-set in `commit_dream_cycle` (COA-310) and the
per-candidate liveness recheck in `backfill_missing_candidate_embeddings`
(COA-311). Both open their transaction through the shared
`storage.candidates._begin_write_txn` helper.

The two backends reach serialization differently, and this is a deliberate
reliance, not an oversight:

- **sqlite** gets `BEGIN IMMEDIATE`, which takes the write lock before the read,
  so a second concurrent writer blocks or fails busy rather than reading stale
  state. This branch is covered by the sqlite regression tests in
  `tests/test_pipeline.py`.
- **managed libSQL/Turso** gets a plain `BEGIN` (it has no local pre-read write
  lock; see the transaction caveat above). Concurrent-Light safety there relies
  on the Turso server rejecting the stale write at commit via its conflict
  detection. This branch is exercised only by a creds-gated live test and is
  therefore unverified in the default creds-free CI run. Both edges bite only
  under multi-worker Light, which v0.1 does not run.

## Addendum 4 -- 2026-07-10: the control-plane catalog stayed local (COA-359)

Correction of record. The Decision above (and Addendum 2's "All items in the
Decision ... are implemented" summary) is inaccurate about *one* store: the
control-plane catalog did **not** move to managed Turso/libSQL. What actually
shipped, and what the deployed Railway alpha runs today, is a split:

- **Customer memory** -- one isolated Turso/libSQL database per tenant,
  addressed by the catalog's `tenants.customer_target` DSN. This half of the
  Decision landed as written (Addendum 2).
- **The control-plane catalog and API-key store** -- a single local SQLite
  `control-plane.db` on the Railway volume, rooted at `VEXIC_HOSTED_ROOT`
  (`/data/vexic`). `create_service_from_env` (`src/vexic/hosted_http.py`)
  builds `HostedTenantCatalog(root)` / `HostedApiKeyStore(root)` from that
  filesystem path under `VEXIC_STORAGE_BACKEND=turso`, exactly as under the
  `local` backend; the docstring there is explicit that the turso backend
  "keeps the control-plane ... LOCAL/filesystem-rooted." The tenant registry,
  API keys, operational telemetry, and the dream sweeper's `dream_sweep_state`
  scheduling table therefore all live in that local file, not on Turso.

So the Decision's "move to a managed Turso/libSQL database" for the control
plane, and the Consequences line naming "customer memory and the control-plane
catalog both on managed libSQL," describe an intended posture that was
deliberately narrowed to customer-memory-only during implementation. The
narrowing is not itself a reversal of ADR 0019's direction -- a managed
control-plane store remains a readiness target -- but it was never recorded
until now. `docs/hosted-mvp.md` (corrected under COA-353) is the accurate
as-shipped description; this addendum brings the ADR and the ADR index
(`docs/adr/README.md`) into line with it.

The `connect(target)`/`StorageTarget` seam still applies to the control plane
in principle -- the catalog and API-key store open through it, and a
`control_plane_target(env)` helper that builds a Turso `StorageTarget` from
`TURSO_DATABASE_URL` exists in `adapters/turso_adapter.py`. But that helper is
referenced only by tests; no runtime path wires it into the service factory.
It is dead code embodying the never-shipped catalog-on-Turso leg, and whether
to delete it or wire it is a separate decision (tracked on COA-359), not part
of this correction.

The eventual managed control-plane store remains ADR 0008's readiness target
and this ADR's deferred Neon Postgres promotion; that work, when taken, is the
place to actually move `control-plane.db` off the Railway volume.

## Addendum 5 -- 2026-07-10: control-plane cutover executed (COA-360)

Addendum 4 recorded that the control-plane catalog had stayed local. That is no
longer true: the catalog was moved to managed Turso/libSQL and the deployed
Railway alpha now runs on it. This addendum supersedes Addendum 4's
"stayed local" status and the Decision's original narrowing.

What shipped and was executed:

- **Wiring (COA-360).** A `VEXIC_CONTROL_PLANE_TARGET` selection flag
  (`local` default, `turso`) routes the catalog and API-key store through
  `control_plane_target(env)` in `create_service_from_env`, independent of the
  customer-memory `VEXIC_STORAGE_BACKEND` flag. The `control_plane_target`
  helper is no longer dead code -- it is the wired runtime path. Setting the
  flag to `turso` is reversible: unset it and the service reads the local
  `control-plane.db` again.
- **Auth cache.** With the catalog remote, each API-key check is a network
  round-trip, so `HostedApiKeyStore` gained a short-TTL in-process auth cache
  (active only against a `StorageTarget`), evicted on revoke. Its bounded
  multi-replica stale-revocation window is documented in code as an accepted
  risk that is zero on the current single instance and must be revisited before
  a second replica.
- **Migration.** `vexic.migrate_control_plane` copies every control-plane table
  from the local `control-plane.db` into an empty Turso target, parents-first
  (foreign-key-safe), with plain `INSERT` and exact row-count verification, and
  emits counts only (never key hashes). The dogfood catalog (3 tenants, their
  API keys, and operational telemetry) was migrated this way and verified.

Consequently the tenant registry, API keys, operational telemetry, and
`dream_sweep_state` now live in the managed Turso control-plane database, not in
the Railway-volume `control-plane.db`. That local file is retained, unwritten,
as the instant rollback target. This realizes clean frontend / backend /
database tier separation: the database tier (customer memory and control plane
both on Turso) no longer lives inside the backend's compute volume.

Direction change: the eventual managed control-plane store is now **Turso**, the
same provider as customer memory, decided in favor of provider consolidation and
the already-built libSQL seam over ADR 0008's deferred Neon Postgres target.
Neon is no longer the planned control-plane home; the "libSQL catalog to Neon"
second cutover named in the Consequences is retired.

Backup posture (current tier reality): the deployment is on Turso's free tier,
which has no point-in-time recovery, so the Turso PITR recovery mechanism named
in "Backup, restore, and readiness" above is not available here. DR is instead
scripted `turso db dump` exports of the control-plane and per-tenant databases
(a scheduled GitHub Actions workflow), plus the retained local `control-plane.db`
as a rollback handle. Turso PITR remains the intended mechanism if the
deployment moves to a paid tier.
