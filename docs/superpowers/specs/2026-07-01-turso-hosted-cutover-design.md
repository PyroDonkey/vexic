# Turso/libSQL hosted storage cutover (ADR 0019 full posture) — design spec

Status: approved (design), fuse-audited
Date: 2026-07-01
Related: ADR 0019 (hosted storage cutover starts Turso-only), ADR 0005/0008/0011/0013. Tracking issues live in the tracker, not in this spec.

## Goal

Wire managed Turso/libSQL as the hosted storage backend for the full ADR 0019
posture: one isolated Customer Memory Database per tenant AND the shared
control-plane (catalog / API-key store / operational telemetry) on managed
Turso, reached through the single `connect(target, auth_token=...)` seam. Built
test-first; default `uv run pytest` stays green without Turso creds.

## Verified baseline (already true in-repo)

- `connect(target, *, auth_token=None, **kwargs)` routes `libsql://`/`https://`/
  `ws(s)://` to the managed libSQL client; a filesystem path or `:memory:` to
  `sqlite3`. Refuses an auth token over `http://`/`ws://` (plaintext).
- libSQL connection contract: no settable `row_factory` (use `rows_as_dicts`),
  no named/dict params, no `enable_load_extension`; supports
  `execute`/`executemany`/`cursor`/`commit`/`rollback`/`close` and `with conn:`.
- Native libSQL vectors (`F32_BLOB` + brute-force `vector_distance_cos`) and FTS5
  pass on real Turso: `tests/test_storage_conformance.py` 11/11 including the
  `[libsql]` params.
- **Empirical:** a token embedded in the URL (`?authToken=`) returns 401; the
  separate `auth_token` kwarg works. The token cannot ride inside the DSN.
- **Verified safe:** `enable_load_extension` is called only in
  `SqliteVecBackend.prepare()`; `LibsqlVectorBackend.prepare()` is a no-op and
  all schema paths dispatch via `select_vector_backend(conn)`. `init_db` /
  `init_vector_memory` do NOT crash on libSQL.
- `init_db(db_path)` is invoked on **every** storage call (idempotent
  `CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` checks + ALTERs), inside a
  `with conn:` transaction. `WAL` is already guarded behind
  `isinstance(conn, sqlite3.Connection)`.
- Control-plane (`hosted_local.py`) is filesystem-coupled: `db_filename`
  (`root_path / customer-<hex>.db`), `_ensure_control_db_permissions`
  (`os.open(O_EXCL, 0o600)` + `chmod`), `activate_replacement_database` `Path`
  checks under root, `BEGIN IMMEDIATE`, `AUTOINCREMENT`, and an expression index
  on `julianday(recorded_at)`.
- `.env.turso` holds ONE DB URL + ONE DB token. No Platform API token/group yet
  (operator will add `TURSO_PLATFORM_API_TOKEN` + `TURSO_GROUP`).

## Cross-cutting invariants

1. **`StorageTarget` is secret-bearing.** Frozen dataclass `{target: str,
   auth_token: <redacted> | None}`. `auth_token` never appears in `repr`/`str`,
   logs, exceptions, `model_dump`, telemetry, or the redaction forbidden-values
   scan. `__eq__`/`__hash__` key on `target` only. Audit every site that logs or
   serializes `HostedTenant.db_path`.
2. **Secrets live in `adapters/`.** Raw DB tokens and the org Platform API token
   are read from env only in `adapters/`. The hosted factory injects resolved
   `StorageTarget`s / the provisioning port into `src/vexic`; core never reads
   these secrets from env. A non-secret `VEXIC_STORAGE_BACKEND` flag in
   `src/vexic` is allowed.
3. **Connect resilience.** Bounded connect/read timeout + limited retry/backoff
   on the hot path; `connect()` error paths redact the DSN.
4. **CI green without creds.** All libSQL/live tests behind `@pytest.mark.turso`
   + skip. Live provisioning/e2e create and destroy throwaway resources and
   never print tokens.

## Phases (TDD: write and watch the test fail before production code)

### P0 — libSQL portability spike + gate (throwaway, creds-gated; no prod change)
Prove on real Turso, with explicit pass/fail gates, the control-plane constructs
conformance never exercised: `BEGIN IMMEDIATE` executes; `AUTOINCREMENT` PK
works; `CREATE INDEX ... julianday(recorded_at)` creates and a query uses it;
`PRAGMA foreign_keys=ON` rejects a violating INSERT. If any fails, test the
fallback explicitly (generated/stored column may also be version-dependent; else
app-side filtering / portable transaction). Output: concrete catalog
schema-adjustment list feeding P3.

### P1 — StorageTarget seam + init-once discipline
Tests first: redacted `repr`; `connect(StorageTarget)` local + libsql-gated;
`init_db` issues DDL **once** across N calls (spy/count); plaintext+token
rejected; local `str` path unchanged. Then: `StorageTarget`; one
`_coerce_target(db_path) -> (dsn, auth_token)` used by both `connect` and
`init_db`; widen storage entry points `str -> str | StorageTarget`. Init-once
memo: per-process, keyed by `(target-DSN, schema-generation)`, guarded by a
`Lock`, set only after commit, `force_init` for tests/migration. (Token rotation
is orthogonal to schema — not part of the memo key.)

### P2 — adapters/turso_adapter + backend flag + customer-memory cutover + e2e
Tests first: `adapters/turso_adapter.py` env→redacted `StorageTarget`
(monkeypatched env; missing-var error; plaintext+token refused); factory selects
target **per store** from `VEXIC_STORAGE_BACKEND`; conformance extended to drive
libsql **through a `StorageTarget`** + assert `init_db` ran on libsql; mocked
connect-failure (timeout/503/401)→redacted error; latency micro-benchmark p95
gate proving the memo helps. Then implement adapter + factory wiring;
`HostedTenant.db_path` becomes a `StorageTarget`. Live-gated e2e:
`VEXIC_STORAGE_BACKEND=turso` → POST `/v1/ingest_source_transcript` → POST
`/v1/search_transcript` returns the row; assert no token leaks. First
user-visible payoff.

### P3 — control-plane on Turso
Add a creds-free `FakeLibsqlConn` test double (documented libSQL contract: no
`row_factory`/named-params/`enable_load_extension`; `with conn:` rollback) so
control-plane TDD runs without creds. Apply P0 adjustments. Route
`_connect_control` via `connect(target, auth_token)`; skip
`_ensure_control_db_permissions` on non-local targets (assert not called on a
DSN); replace `db_filename` with a per-tenant target column; abstract
`activate_replacement_database` into `ReplacementTarget` (local-path vs Turso-DSN
validators). Add a Platform-API list-databases **reconcile** helper; document the
P2→P3 split-brain window (customer on Turso, catalog mapping could be lost) as
accepted for dogfood with a manual recovery note.

### P4 — provisioning port + token store + live verify
`TursoProvisioningPort` in `adapters/`: `create_database` / `mint_token` /
`destroy_database` via Turso Platform API (org token + group from env, injected
by factory). **Token store (resolved):** mint short-lived, DB-scoped per-tenant
tokens; cache in-process with TTL; never persist raw tokens; catalog stores only
non-secret target metadata (DSN, provider, generation). Fallback if latency
bites: encrypted-at-rest token with an adapters-only `VEXIC_CONTROL_DB_SECRET_KEY`
— measured and recorded in an ADR 0019 addendum. Tests: mocked httpx Platform
API (create/mint/destroy, error, idempotent create, compensating destroy on
failure); catalog `provision_tenant` calls the port + stores the target; one
live throwaway create→round-trip→assert-destroyed (gated). Least-privilege token
scope + expiry.

### P5 — migration + PITR restore drill + docs
Clarify: greenfield = no bulk Railway→Turso data migration; `vexic.migration` +
canonical export/import serve cross-backend moves + the restore drill (DR), not
initial load. Extend `vexic.migration` to accept a libSQL `StorageTarget`
(guard `Path()` ops). Restore drill: PITR → provision isolated replacement →
canonical import → verify (row/FTS/vector counts) → **activate only on verify
pass** (else re-activate original + destroy replacement) → quarantine stale
handle with a **generation bump** so request-scoped services cannot write the old
DB after repoint. Update `docs/hosted-mvp.md`, ADR 0019 consequences, and the
hosted-migration runbook. Assign the existing single `.env.turso` DB as the
control-plane DB (persistent singleton); tenant DBs are provisioned. Separate
track: give the recorder Stop hook a Turso deadline + fire-and-forget so a slow
Turso cannot block it.

## Sequencing

P0 → P1 → P2 → P3 → P4 → P5. Control-plane (P3) precedes provisioning (P4)
because provisioning writes tenant targets into the catalog; restore (P5) depends
on the P3 target model. P1/P2 deliver the customer-memory round-trip first for
early value and to exercise the seam before the catalog rewrite.

## Out of scope (separate tickets)

Connection pooling / durable quota (known perf follow-up, tracked separately);
Neon control-plane; S3 Object Lock export hardening; SessionStart primer install.

## Fuse audit trail

Profile `deep`, GPT `xhigh`. Answerers: GPT, Gemini-BU, GLM (primary Gemini
malformed → fell back; Composer timed out). Auditor: QWEN. Accepted
high-severity: init_db memo+lock, StorageTarget redaction, activate_replacement
remote break, per-tenant token store, split-brain reconcile, control-plane libSQL
spike, conditional restore + generation bump. Rejected: `enable_load_extension`
crash (verified already guarded via `select_vector_backend`).
