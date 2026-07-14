# Hosted Memory MVP Shell

Role: deployment and readiness notes for the first hosted boundary around the
Vexic memory core.

The hosted MVP shell is an in-process Python boundary in `vexic.hosted`.
Concrete tenant catalog, API-key provisioning, and internal-alpha transports
live in adapter modules under `vexic`. The `vexic.hosted_local` module is for
local staging and tests. This is not a public HTTP server, dashboard, billing
system, or production customer-data service. A future web/API process can wrap
this boundary without changing the memory contract.

All environment variables referenced below are catalogued in
[`configuration.md`](configuration.md).

## What Exists

- `HostedMemoryService` exposes the public memory contract operation names,
  binds tenant/principal/capability scope from an adapter-supplied auth context,
  and delegates to `LocalMemoryService`.
- `vexic.hosted_local.HostedTenantCatalog` persists local staging tenant routing
  in a SQLite control-plane database and provisions one isolated
  SQLite-compatible Customer Memory Database per tenant.
- `vexic.hosted_local.HostedApiKeyStore` creates high-entropy scoped API keys,
  persists only SHA-256 hashes, scope, and revocation metadata in the local
  SQLite control-plane database, authenticates by non-secret key id with
  constant-time hash comparison, and can revoke keys for local staging.
- `HostedBackgroundJobRunner` runs Light/REM/Deep/Summarize dream phases when
  explicit host model ports are supplied, records job lifecycle and usage
  events, and fails closed with `HostPortNotConfigured` while ports (or, for
  Summarize, `build_summary_agent` specifically) are absent. REM itself
  is a local heuristic that makes no model calls (ADR 0020) but runs inside
  the same ports gate.
- `HostedMemoryService` can send sanitized request audit and usage metadata to
  a telemetry sink without storing tenant metadata in shared service lists.
- The local staging adapter stores sanitized request audit, usage, and
  background job lifecycle metadata in `control-plane.db` without raw API keys
  or request payload text.
- `HostedMemoryService` applies single-process in-memory operation quotas for
  authenticated local staging traffic before delegating to the memory core.
- `vexic.hosted_http` exposes an internal-alpha FastAPI transport over
  `HostedMemoryService` for `append_transcript`, `search_transcript`,
  `search_long_term`, and `expand_history`, with API-key auth, request caps,
  error mapping, and `/health`.
- `vexic.hosted_control_plane_http` is the hosted control-plane HTTP adapter.
  It wraps the hosted memory app and registers `/control/v1/*` without adding
  control-plane routes to the core `vexic.hosted_http` app.
- `vexic.mcp_stdio` stays the local Claude Code stdio MCP process; the
  `vexic.hosted_mcp` adapter lets the supported launcher point
  that MCP process at the hosted HTTP API.
- `vexic.mcp_http` exposes a native read-only Streamable HTTP MCP `/mcp`
  route on the hosted FastAPI app. It is stateless, JSON-only, Bearer-auth
  only, and exposes `recall_conversation_history` and `recall_user_memory`.
- `vexic setup claude-code` installs a SessionStart primer that reuses the
  recorder config and hosted read endpoints to inject capped memory context on
  new/cleared Claude Code sessions.
- `vexic.hosted_http` exposes `POST /v1/fresh_context`, a dedicated hosted
  fresh-conversation context endpoint (capability `memory:fresh-context`,
  `token_budget` validated 1-24,000, rate-limited 30/min, results capped like
  `expand_history`). `vexic setup claude-code`'s SessionStart primer calls it
  first and leads the injected context with a "Prior conversation recap:"
  section when the key carries that capability; keys without it fail open to
  the existing search-only priming.
- The `summarize` dream phase (`vexic.summarize`) compacts Tier 1 transcript
  spans into `session_summaries` rows that back fresh context. Like Light and
  Deep, it needs a host-supplied `build_summary_agent` port and fails closed
  with `HostPortNotConfigured` without one; `run-dream-phase --phase
  summarize` is the CLI entry point.

## Local Staging

Use a throwaway directory for tenant databases:

```python
from pathlib import Path

from vexic.contract import MemoryCapability
from vexic.hosted import HostedMemoryService
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog

catalog = HostedTenantCatalog(Path(".hosted-memory"))
catalog.provision_tenant("tenant-a", project_ids={"project-a"})

keys = HostedApiKeyStore(Path(".hosted-memory"))
api_key = keys.create_key(
    tenant_id="tenant-a",
    principal_id="agent-a",
    capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
    project_ids={"project-a"},
    agent_ids={"agent-memory-a"},
)

service = HostedMemoryService(catalog, keys, telemetry=catalog)
```

`HostedTenantCatalog` stores local tenant-to-database routing in
`control-plane.db` under the provided root path. The returned customer database
paths are generated by the catalog rather than interpolated from tenant ids.
`HostedApiKeyStore` stores local key ids, SHA-256 hashes, principal bindings,
capability/project/agent scopes, creation metadata, and revocation metadata in
the same `control-plane.db`. The returned `api_key.raw_key` is shown once.
Store it in the caller's secret store; raw keys and raw secret material are not
stored by the local adapter. SHA-256 is used here because generated API keys are
high-entropy random tokens; do not reuse this as a password hashing pattern.
Omit `agent_ids` for an unrestricted staging key, or include `None` alongside
agent ids to allow explicit shared-memory reads. `principal_id` stays actor
identity and is never used as a fallback memory `agent_id`.

## Hosted Environment

For one internal hosted environment:

- run a server-owned API process that calls `HostedMemoryService`;
- verify human/session auth outside `src/vexic`;
- issue scoped Vexic API keys for agent callers through the server-owned
  control surface;
- provision one managed SQLite/libSQL-compatible Customer Memory Database per
  tenant;
- replace the repo-local SQLite control-plane with a production control-plane
  store for tenant routing, key hashes, and revocation state;
- keep audit and usage ledgers durable outside the tenant memory database;
- supply model-backed host ports before enabling real Light or Deep jobs (REM
  is a local heuristic but rides the same ports gate).

## Internal Alpha HTTP API

Run the hosted HTTP adapter locally:

```powershell
$env:VEXIC_CONTROL_PLANE_TOKENS = "console-secret"
uv run --with-editable . --extra hosted python -m uvicorn vexic.hosted_control_plane_http:create_app --factory --host 127.0.0.1 --port 8000
```

`VEXIC_CONTROL_PLANE_TOKENS` is read only by the repo-local
`vexic.hosted_control_plane_http` adapter as a comma-separated list for
`/control/v1/*` Console service credentials. If it is unset or contains a blank
token, the control plane fails closed. Running `vexic.hosted_http:create_app`
directly starts the hosted memory and MCP app without control-plane routes.

Issue a tester key against the same hosted root:

```powershell
uv run --with-editable . --extra hosted python -m vexic.hosted_http issue-key --root .hosted-memory --tenant-id tenant-a --project-id project-a --principal-id claude-code
```

The raw key is printed once. Store it in the caller secret store or Claude Code
MCP environment, not in repository files.

The HTTP API accepts `Authorization: Bearer <raw-key>` or `X-Vexic-Api-Key` on
`/v1/*`; use Bearer in new examples. It serves:

- `GET /health`
- `POST /v1/append_transcript`
- `POST /v1/ingest_source_transcript`
- `POST /v1/search_transcript`
- `POST /v1/search_long_term`
- `POST /v1/expand_history`
- `POST /v1/fresh_context`
- `POST /v1/load_active_context`
- `POST /v1/trigger_dream_phase`
- `POST /v1/setup/exchange`
- `POST /mcp`
- `/control/v1/*` when started through `vexic.hosted_control_plane_http`

`POST /mcp` is the native read-only Streamable HTTP MCP route. It differs from
the `/v1/*` routes deliberately:

- it requires `Authorization: Bearer <raw-key>`;
- it rejects query strings and does not accept `X-Vexic-Api-Key`;
- it returns `application/json` only and no SSE;
- it is stateless and does not issue `MCP-Session-Id`;
- it accepts missing `Origin` for CLI agents, but rejects present origins that
  are not listed in `VEXIC_MCP_ALLOWED_ORIGINS`;
- it binds `project_id`, `session_id`, and optional `agent_id` from
  `X-Vexic-Project-Id`, `X-Vexic-Session-Id`, and `X-Vexic-Agent-Id`;
- it exposes only `recall_conversation_history` and `recall_user_memory`.

Native HTTP MCP explicitly defers OAuth discovery/PKCE/audience handling,
redirect/SSRF hardening, SSE/resumability, stateful sessions, write/admin
tools, public marketplace distribution, and production customer-data readiness.

Hosted transcript writes are separate from MCP and use scope-free bodies. The
tenant comes from the Agent API key; `X-Vexic-Project-Id` and
`X-Vexic-Session-Id` are required; `X-Vexic-Agent-Id` is optional. The adapter
rejects body `scope`, `user_id`, and `correlation_id`, plus
`X-Vexic-User-Id` and `X-Vexic-Correlation-Id`.

Console-created projects return `tenantId`, and Agent API Key create/list
responses return `scopeTemplate`. That template is the caller's source for the
correct `/v1/search_*` `scope.tenant_id`; do not derive it from the Clerk org id
client-side.

Append a cleaned model-message row:

```powershell
curl.exe -s http://127.0.0.1:8000/v1/append_transcript `
  -H "Authorization: Bearer <raw-key>" `
  -H "X-Vexic-Project-Id: project-a" `
  -H "X-Vexic-Session-Id: session-a" `
  -H "Content-Type: application/json" `
  -d "{\"messages_json\":[\"<clean-model-message-json>\"],\"redaction\":{\"forbidden_values\":[]}}"
```

Ingest cleaned source transcript rows:

```powershell
curl.exe -s http://127.0.0.1:8000/v1/ingest_source_transcript `
  -H "Authorization: Bearer <raw-key>" `
  -H "X-Vexic-Project-Id: project-a" `
  -H "X-Vexic-Session-Id: session-a" `
  -H "Content-Type: application/json" `
  -d "{\"messages\":[{\"source_host\":\"claude-code\",\"source_session_id\":\"sessionId\",\"source_message_id\":\"uuid\",\"message_json\":\"<clean-model-message-json>\"}],\"redaction\":{\"forbidden_values\":[]}}"
```

Direct `/v1/search_*` calls include a body scope copied from the key's
`scopeTemplate`. Add `session_id` for transcript search:

```powershell
curl.exe -s http://127.0.0.1:8000/v1/search_transcript `
  -H "Authorization: Bearer <raw-key>" `
  -H "Content-Type: application/json" `
  -d "{\"scope\":{\"tenant_id\":\"tenant_from_console\",\"project_id\":\"project-a\",\"session_id\":\"session-a\",\"agent_id\":\"agent-a\",\"principal\":{\"principal_id\":\"agent-a\",\"principal_type\":\"agent\"},\"trust_boundary\":\"networked\",\"capabilities\":[\"memory:search\"]},\"query\":\"cedar\",\"limit\":5}"

curl.exe -s http://127.0.0.1:8000/v1/search_long_term `
  -H "Authorization: Bearer <raw-key>" `
  -H "Content-Type: application/json" `
  -d "{\"scope\":{\"tenant_id\":\"tenant_from_console\",\"project_id\":\"project-a\",\"agent_id\":\"agent-a\",\"principal\":{\"principal_id\":\"agent-a\",\"principal_type\":\"agent\"},\"trust_boundary\":\"networked\",\"capabilities\":[\"memory:search\"]},\"query\":\"cedar\",\"limit\":5}"
```

Minimal client config shape for Claude Code, Codex, OpenClaw, and Hermes Agent:

```text
transport: streamable-http
url: https://api.vexic.dev/mcp
headers:
  Authorization: Bearer <raw-key>
  X-Vexic-Project-Id: project-a
  X-Vexic-Session-Id: session-a
  X-Vexic-Agent-Id: agent-a  # optional
```

Before pointing an agent runtime at hosted Vexic, suppress that runtime's
native durable memory where possible.
[ADR 0004](adr/0004-native-agent-memory-is-host-integration-policy.md) defines
this as host integration policy, not Vexic core behavior: Vexic cannot stop
Claude Code, Codex, or another runtime from writing its own local memory. Use
the local setup guidance in
[usage.md](usage.md#native-agent-memory); if suppression is unavailable,
treat Vexic as authoritative only for memory that reaches Vexic through the
hosted HTTP append route, recorder, or importer path. Claude Code hosted
auto-recording uses `vexic setup claude-code`; the command installs user-local
Claude Code hook config and Vexic recorder config, then prints a vendor
`claude mcp add` command for the user to run
([ADR 0027](adr/0027-agent-mcp-connect-uses-vendor-add-commands.md)). Vexic
writes no `.mcp.json`. The recorder sends cleaned transcript rows to
`/v1/ingest_source_transcript`; the SessionStart primer injects capped hosted
memory context on `startup` and `clear`; once the user runs the connect command,
MCP reads go through the read-only hosted `/mcp` route for targeted on-demand
search. The Claude Code host transcript
recorder flow is documented in
[usage.md](usage.md#claude-code-transcript-import) and
[ADR 0002](adr/0002-host-recorders-ingest-complete-cleaned-transcripts.md).

Smoke each configured client with the same sequence:

1. `initialize` succeeds and returns protocol version `2025-11-25`.
2. `tools/list` returns exactly `recall_conversation_history` and
   `recall_user_memory`.
3. `tools/call recall_conversation_history` returns scoped transcript hits as
   prose.
4. `tools/call recall_user_memory` returns facts as prose or a configuration
   tool error if no embedding port is configured.
5. Missing or invalid Bearer key returns `401`.
6. `expand_history`, write, and admin tools are absent and unreachable.

For Claude Code alpha testing, run the stdio MCP shim against the hosted API:

```powershell
$env:VEXIC_API_KEY = "<raw-key>"
uv run python scripts/vexic-mcp-stdio.py --api-base-url http://127.0.0.1:8000 --tenant-id tenant-a --project-id project-a --session-id session-a
```

For the internal Railway alpha, use `https://api.vexic.dev` as the
`--api-base-url` with a throwaway scoped API key.

`append_transcript` or `ingest_source_transcript` is verified through the
hosted HTTP API. Claude Code then searches the hosted memory through the stdio
MCP tools.

For hosted auto-recording, run `vexic setup claude-code` with the hosted base
URL, raw key, project ID, and session ID. It installs user-local Claude Code
hook config plus Vexic recorder config, then prints a `claude mcp add` command
for the user to run (ADR 0027). The Stop hook posts cleaned Claude Code
transcript rows to `/v1/ingest_source_transcript`; the SessionStart hook primes
new/cleared sessions through hosted read endpoints using the same recorder
config; once the user runs the connect command, targeted reads go through the
stdio proxy to hosted `/mcp`. The raw API key stays in the user-local recorder
config, never in Claude settings; the connect command is derived from the
recorder config path, so no raw key appears in it.

Passing the raw key on the setup command line is the interim path. The target
flow is a console-minted, single-use setup token that the CLI exchanges for the
scoped key ([ADR 0026](adr/0026-agent-setup-token-exchange.md)). That exchange
has landed: the Console mints a single-use token through the control-plane
setup-token route, and the CLI redeems it against `POST /v1/setup/exchange`.

## Turso/libSQL Storage Backend

The hosted storage cutover decided by
[ADR 0019](adr/0019-hosted-storage-cutover-starts-turso-only.md) is implemented.
`src/vexic/storage/connection.py` exposes one `connect(target, *, auth_token=None)`
seam used by every storage module (local SQLite and hosted libSQL alike); a
`StorageTarget(target, auth_token)` handle carries the resolved DSN plus an
auth token that is redacted from `repr`/logs and never embedded in the DSN
itself (a libSQL client rejects a token passed via `?authToken=`; it must go
through the connection's separate `auth_token` argument). `src/vexic` never
reads Turso credentials from the environment; the repo-root `adapters/`
directory does that, per ADR 0008/0013 precedent.

- **Non-secret backend flags.** `resolve_storage_backend` (in `vexic.hosted`)
  reads `VEXIC_STORAGE_BACKEND` (`"local"` default, or `"turso"`) and routes
  customer-memory storage: `local` is the filesystem-SQLite path, `turso`
  routes it to per-tenant Turso databases. The control-plane catalog and
  API-key store are routed *separately* by `resolve_control_plane_target`
  reading `VEXIC_CONTROL_PLANE_TARGET` (`"local"` default, or `"turso"`, ADR
  0019 Addendum 5); `turso` sends the catalog to a managed Turso
  control-plane database via `control_plane_target(env)`. Both flags carry no
  credential, so both stay in `src/vexic`. A hosted deployment sets both to
  `turso`; see "Required Railway config" below.
- **Per-tenant provisioning, not a shared dogfood override.** `adapters/turso_adapter.py`
  provides `TursoProvisioningPort` (`create_database`/`mint_token`/`destroy_database`/
  `provision`, all against the Turso Platform API, mocked HTTP transport in
  tests) and `make_customer_target_resolver`, which `create_service_from_env`
  (`vexic.hosted_http`) wires in when `VEXIC_STORAGE_BACKEND=turso`. Each
  tenant gets its own isolated Turso database; the catalog stores only the
  non-secret DSN (`customer_target` column) plus a `generation` counter, never
  a raw token. An earlier single-shared-database dogfood override (from the
  P2 milestone) has been fully replaced by this per-tenant path.
- **Token store decision: mint short-lived, cache in-process, never persist
  raw.** `TenantTokenCache` mints a fresh, DB-scoped token via
  `TursoProvisioningPort.mint_token` on cache miss/expiry and holds it only in
  an in-memory `OrderedDict` keyed by database name, bounded by LRU eviction
  (default 512 entries, ADR 0019 Addendum 6), with an injectable clock for
  deterministic TTL tests. TTL governs freshness and the bound governs size;
  neither replaces the other. The cache TTL (default 600s) is kept shorter than
  the minted token's own expiration (default `15m`) so a cached token is
  always re-minted well before Turso would reject it. Nothing is written to
  the catalog, disk, or any persistent store; a process restart or GC simply
  drops the cache and the next call re-mints. This is the accepted answer to
  ADR 0019's open token-store question for the current scale -- if measured
  latency ever forces persistence, the ADR addendum records the fallback
  (encrypted at rest under an `adapters/`-only key that never enters
  `src/vexic`), but that fallback is not built.
- **Schema init is once per target, not per call.** `src/vexic/storage/schema.py`
  keeps a process-level, lock-guarded, target-keyed memo (`_memo_key`/
  `_reset_init_memo`) so `init_db`/`init_vector_memory` run their DDL exactly
  once per distinct target (a token rotation is not a schema change and does
  not bust the memo); the memo is populated only after the guarded DDL commits,
  so a failed init never poisons it. Local SQLite behavior is unchanged; this
  matters for libSQL because every DDL statement against a remote database is
  a network round-trip, and re-running `init_db` on every storage call would
  be a per-request latency tax.
- **Control-plane over the same seam, local-only filesystem guards.** The
  control-plane catalog and API-key store open through the same `connect()`
  seam (`StorageTarget`-aware). Filesystem-coupled operations --
  `_ensure_control_db_permissions` (`os.open`/`chmod`) and the `Path`-based
  half of `activate_replacement_database` -- run only when the target is a
  local filesystem path; a DSN-shaped replacement (Turso) instead validates as
  a well-formed libSQL URL distinct from the tenant's current
  `customer_target`, and repointing bumps the catalog row's `generation`
  rather than swapping a filename, so a request-scoped service holding the
  pre-repoint handle cannot keep writing the quarantined database.
- **Split-brain reconcile.** Because the control-plane mapping and the Turso
  Platform API's own database list are two independent sources of truth,
  `adapters/turso_adapter.reconcile_tenant_databases` compares the platform's
  list-databases response against the catalog's tenant -> `customer_target`
  mapping and reports matched, orphaned (platform-only), and dangling
  (catalog-only) entries. It is a pure function over two already-fetched
  collections -- no network I/O, no secrets -- so recovery from a lost or
  stale mapping is a documented, tested reconcile pass rather than manual
  Turso-console archaeology. This is accepted as adequate for the internal
  dogfood posture; it does not remove the split-brain window ADR 0019's
  addendum describes.
- **Cross-backend exception classifiers.** `src/vexic/storage/errors.py`
  provides `is_unique_violation`, `is_operational_error`, and
  `is_retryable_operational_error`, which recognize both typed `sqlite3.*`
  exceptions and the bare `ValueError` libSQL raises for the equivalent
  server-side errors (its message carries a Hrana/`code:` payload instead of a
  typed exception). Every previously sqlite3-typed catch on a shared code path
  (control-plane persistence, transcript ingest/search, candidates, longterm,
  operators) now goes through these classifiers, re-raising when the
  classifier returns `False` so an unrelated `ValueError` is never silently
  swallowed.
- **Creds-gated live tests.** Tests that exercise a real Turso database
  (conformance parity, customer-memory round-trip, per-tenant
  provision -> round-trip -> destroy, the `turso` pytest marker) check for
  `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`/the optional `libsql` package at
  collection time and skip (not fail) when any is absent. `uv run pytest -q`
  is green with zero Turso credentials configured; live verification requires
  loading real Turso credentials and running the `turso`-marked suite
  separately.
- **Restore drill.** `src/vexic/restore.py` provides `run_restore_drill`, a
  pure orchestration function (provision -> import -> verify -> activate-or-destroy)
  over caller-injected callables -- it reads no secrets and does no I/O itself.
  It activates the replacement (repointing the catalog and bumping
  `generation`) only when `verify` returns `True`; otherwise it destroys the
  replacement and leaves the original active. The decision logic is unit
  tested with fakes. Actually restoring from a real Turso point-in-time-recovery
  snapshot against production data remains a manual/operator-run step (the
  hosted-migration runbook is maintained in the private hosted-ops repository);
  only the automated decision logic above is exercised in CI.

Remote libSQL round trips use the wall-clock deadline from ADR 0019 Addendum 7.
A read-only timeout, or worker-capacity exhaustion before a call starts, is a
retryable storage fault and produces a sanitized 503 `storage_unavailable`
without a speculative `Retry-After`. A mutation or commit that times out may
still land after the response, so it produces the non-retryable,
sanitized `MutationOutcomeUnknown` failure instead. At most 64 abandoned remote
workers are retained process-wide; once that cap is occupied, new work fails
before starting a driver call.

Because `libsql.connect()` is lazy, a transient edge fault (for example the
Hrana `api error` 502 `connect to upstream failed` observed live 2026-07-13)
first surfaces on a new connection's first round-trip. Both connection seams
absorb one such fault at acquisition time: `storage.connection.connect` probes
a remote target with `SELECT 1` through the deadline wrapper and rebuilds once
on a classified retryable fault, and the control-plane `_connect_control_db`
does the same around its setup PRAGMA. A second consecutive fault propagates;
business statements and mutations are never replayed.

Known follow-ups, deliberately not built in this cutover:

- Some adapter type annotations (e.g. around the injected HTTP transport and
  provisioning seams) are looser than ideal and are flagged for a precision
  pass.
- In `run_restore_drill`, the best-effort compensating `destroy()` call made
  after an `import_canonical`/`verify` failure swallows its own exception so
  it can never mask the original failure; this means a broken teardown can
  silently leave the replacement database behind rather than surfacing a
  second error. Documented and accepted for now, not fixed.

## Railway Alpha Deploy

Use the committed `Dockerfile`; do not rely on Railway Nixpacks for this slice.
The image installs Python 3.13 dependencies with `uv` and includes
`sqlite-vec`.

> **This file does not record what the live service currently runs** (ADR 0033).
> Deployment state is not a property of the repo: a sentence asserting it is
> correct the day it is written and rots silently afterwards, which is exactly
> how an empty-volume reading was once mistaken for an empty database. What
> follows is the *recipe* a hosted deployment must satisfy. To see what a
> deployment actually has set, read it from the deployment -- `railway variables`
> for the names, and the control-plane database for tenant routing.

Storage split (ADR 0019, see "Turso/libSQL Storage Backend" above): a hosted
deployment sets `VEXIC_STORAGE_BACKEND=turso` and
`VEXIC_CONTROL_PLANE_TARGET=turso`. Customer memory then lives in per-tenant
Turso databases addressed by the control-plane `tenants.customer_target` DSN,
and the control-plane catalog and API-key store live in a managed Turso database
(ADR 0019 Addendum 5), migrated off the volume with
`vexic.migrate_control_plane`. The Railway persistent volume stays mounted at
`/data/vexic` (`VEXIC_HOSTED_ROOT=/data/vexic`), but its `control-plane.db` is
then only a rollback handle -- setting `VEXIC_CONTROL_PLANE_TARGET=local` falls
back to it -- not the catalog the service reads.

**Operator hazard.** Under that configuration the volume's databases are not
live state. Any `customer-*.db` files on it are vestigial artifacts of the
pre-cutover layout, and its `control-plane.db` is neither read nor written by
the serving app. Reading them tells you nothing about the service and has
already produced one confident, wrong diagnosis: empty files on the volume are
what a correctly-working Turso deployment looks like. Resolve
`tenants.customer_target` for customer memory, or query the Turso control-plane
database for the catalog, keys, and `dream_sweep_state`.

> **The operator CLI resolves the control plane from the environment, not from
> `--root`.** `python -m vexic.hosted_http issue-key|revoke-key` and
> `run-dream-phase` go through the same store-building seam as
> `create_service_from_env`, so they honor `VEXIC_CONTROL_PLANE_TARGET` (and
> `run-dream-phase` also honors `VEXIC_STORAGE_BACKEND` for customer memory).
> To manage keys on a Turso-backed deployment, run the CLI with the service's
> environment loaded (e.g. `railway run ...`, or export the flag plus the Turso
> connection variables). Run without those variables and the CLI operates on
> the local `control-plane.db` under `--root`, which a Turso-backed service
> never reads. The CLI examples below are written for a local root.

Required Railway config (variable names only; values are set in the Railway
service, never committed):

- `PORT`: provided by Railway.
- `VEXIC_HOSTED_ROOT=/data/vexic`
- `VEXIC_CONTROL_PLANE_TOKENS=<comma-separated Console service tokens>`
- `VEXIC_STORAGE_BACKEND=turso`
- `VEXIC_CONTROL_PLANE_TARGET=turso`
- `TURSO_ORG`, `TURSO_GROUP`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`,
  `TURSO_PLATFORM_API_TOKEN` (see "Turso storage backend" in
  `docs/configuration.md`)
- `VEXIC_PROVISION_EXISTING_TURSO_TARGETS=1` to backfill Turso databases for
  stores provisioned before the Turso backend
- `VEXIC_DREAM_PHASE_ADAPTER=/app/adapters/openrouter_live_adapter.py` and the
  provider credential it reads (`OPENROUTER_API_KEY`). These are optional for
  the process to boot -- leaving them unset keeps model-backed operations
  failing closed, as detailed under "Dream-phase / embedding model port config"
  below -- but the in-server dream sweeper (ADR 0030) cannot run without them,
  so a deployment that is meant to dream must set them.
- Persistent volume mounted at `/data/vexic`
- Health check path: `/health`

`VEXIC_DOGFOOD_TENANT_ID` is optional; when set, `create_service_from_env`
provisions a per-tenant Turso database for that tenant if it has no
`customer_target` yet.

Both backend flags default to `local` and neither raises when omitted, so each
has the same silent-downgrade failure mode. Set both explicitly:

- Omitting `VEXIC_STORAGE_BACKEND` serves tenant memory from local
  `customer-*.db` files on the volume, ignoring `tenants.customer_target`.
- Omitting `VEXIC_CONTROL_PLANE_TARGET` routes the tenant catalog and API-key
  store to the volume's stale `control-plane.db`, so the service authenticates
  against a control plane the Console does not write. A deployment provisioned
  from a config list that omits it will look healthy and reject every live key.

Dream-phase / embedding model port config (optional; unset keeps every
model-backed operation, including the `search_long_term` vector path, failing
closed with `HostPortNotConfigured`):

- `VEXIC_DREAM_PHASE_ADAPTER=/app/adapters/openrouter_live_adapter.py` -- path
  to a host adapter module baked into the image; loading it wires
  `DreamPhasePorts` (embedding plus the Light extraction and Deep
  contradiction agents; REM needs no agent) into the deployed service at
  startup. A configured-but-unloadable adapter fails the deploy loudly at app
  startup.
- `VEXIC_DREAM_PHASE_MODEL_GROUP` -- optional model group name, default
  `hosted-dream`.
- `OPENROUTER_API_KEY=<platform key>` -- read only by the adapter module,
  never by `src/vexic`.
- Optional model selection read by the adapter: `VEXIC_LIVE_EMBEDDING_MODEL`
  (default `openai/text-embedding-3-small`), `VEXIC_LIVE_MODEL` (default
  `deepseek/deepseek-v4-pro`), or a per-group override such as
  `VEXIC_LIVE_HOSTED_DREAM_MODEL` for the `hosted-dream` group. These defaults
  are the code's, not a record of what any deployment has set (ADR 0033).

GitHub Actions deploy trigger:

- `.github/workflows/deploy-hosted.yml` runs on pushes to `main` and manual
  `workflow_dispatch` runs against `main`.
- The workflow keeps one hosted deploy active per ref, lets an in-progress
  deploy finish before the next pending run, runs `uv run pytest`, builds the
  hosted Docker image, deploys with Railway CLI `5.23.1`, then checks
  `https://api.vexic.dev/health` with bounded curl timeouts and retries, and
  finally asserts that an unauthenticated `/control/v1/*` request returns `401`
  so a deploy cannot silently expose the control plane.
- The deploy job runs in the `railway-alpha` GitHub environment; environment
  protection rules therefore gate every hosted deploy.
- Required GitHub secret: `RAILWAY_TOKEN`, a Railway project token scoped to
  the `production` environment.
- Required GitHub variable: `RAILWAY_PROJECT_ID=<railway-project-id>`.
- Railway GitHub autodeploys for the service should stay disabled so GitHub
  Actions is the test gate before deploy.
- Roll back from the Railway service deployments tab by selecting a previous
  successful deployment and using Railway's rollback action. Railway restores
  that deployment's Docker image and custom variables, subject to retention.

Internal-alpha smoke has exercised this shell end to end (concrete
deployment identifiers and drill evidence are tracked in the private ops
repository, not here):

- `/health` returns `200` with the current contract version.
- The persistent volume survives a redeploy; append/search state persists.
- API-key auth rejects missing and invalid keys, and cross-agent scoped MCP
  search does not leak another agent's markers.
- A full Light/REM/Deep promotion/search path passed with tenant isolation
  intact and hosted job usage counters recorded. (REM is a local heuristic per
  ADR 0020, so a fresh smoke records zero REM model usage.)
- Tester keys are alpha-only and should be revoked after each check. Revoke them
  through the Console control plane or run the CLI with the service's
  control-plane environment loaded, as described below.

One-off key issuance against a local hosted root:

```powershell
uv run --no-sync python -m vexic.hosted_http issue-key --root .hosted-memory --tenant-id tenant-a --project-id project-a --principal-id claude-code --capability memory:write --capability memory:search --capability memory:admin:rebuild
```

Issuing no `--capability` at all yields a key with `memory:write` and
`memory:search`.

Run one hosted dream phase through a host-owned adapter:

```powershell
$env:VEXIC_API_KEY = "<raw-key>"
uv run --no-sync python -m vexic.hosted_http run-dream-phase --root .hosted-memory --api-key-env VEXIC_API_KEY --adapter ./adapters/openrouter_live_adapter.py --model-group hosted-dream --tenant-id tenant-a --project-id project-a --session-id session-a --agent-id agent-a --phase light
```

These examples use a local root deliberately. The operator CLI uses the same
environment-driven store builder as the hosted service: it honors
`VEXIC_CONTROL_PLANE_TARGET`, and `run-dream-phase` also honors
`VEXIC_STORAGE_BACKEND` for customer memory. With those variables unset it
falls back to the local databases under `--root`; load the target deployment's
environment before operating on its managed stores.

`--adapter` defaults to `VEXIC_DREAM_PHASE_ADAPTER` and `--model-group` to
`VEXIC_DREAM_PHASE_MODEL_GROUP` (then `hosted-dream`), so when the invocation
environment carries the dream-phase configuration both flags may be omitted.
The adapter file must define `embed_texts`, `build_extraction_agent`,
and `build_contradiction_agent`. `build_summary_agent` is optional: an
adapter that omits it can still run `light`/`rem`/`deep`, but
`run-dream-phase --phase summarize` (and the trigger endpoint below) fails
closed with a `HostPortNotConfigured` error until the adapter exposes it (a
CLI error for the CLI path, `503 host_port_not_configured` for the HTTP path).
Provider secrets stay in the host environment; pass
secret variable names with `--secret-env NAME` when Vexic should include those
values in redaction checks.

Priming keys need the fresh-context capability to get the recap leg of
`vexic setup claude-code`'s SessionStart priming: add `--capability
memory:fresh-context` to `issue-key` (repeatable per capability, alongside
`--capability memory:search`). A key issued without it still authenticates
against `/v1/fresh_context` calls made by the primer, gets a `403`, and the
primer falls back to its existing search-only priming -- it does not fail the
session.

### Dream-phase trigger endpoint (automatic summarize)

`POST /v1/trigger_dream_phase` schedules the Summarize dream phase and
returns immediately -- this is now how summarize runs in practice, replacing
the old manual `run-dream-phase --phase summarize` CLI invocation as the
day-to-day trigger. The body is `{"phase": "summarize"}`; v1 hard-rejects any
other phase value with `400` (light/rem/deep triggering has a different
cost/abuse profile and is a separate decision). A header-bound scope
authenticates the same way as the other `/v1/*` routes.

- Requires capability `memory:dream:trigger`, a capability distinct from
  `memory:admin:rebuild`: trigger-only keys (such as the recorder's) never need
  admin-rebuild just to kick off a sweep. Issue a trigger key with, for example:

  ```powershell
  uv run --no-sync python -m vexic.hosted_http issue-key --root .hosted-memory --tenant-id tenant-a --project-id project-a --principal-id recorder --capability memory:dream:trigger
  ```

  A priming key that should also self-trigger from `recorder prime` needs
  both capabilities: `--capability memory:fresh-context --capability
  memory:dream:trigger`.
- Returns `202` with `{"status": "scheduled"}`, or `{"status": "skipped",
  "reason": "already_running"}` when a sweep for the same (tenant, agent) is
  already in flight. The guard is a durable control-plane lease with a
  heartbeat (ADR 0032), so it holds across processes and across the overlapping
  containers of a rolling deploy, not just within one process.
- Missing/invalid key: `401`. Key without `memory:dream:trigger`: `403`.
  `phase` other than `"summarize"`: `400`. No `build_summary_agent` port
  configured: `503 host_port_not_configured`, checked synchronously before
  any task is scheduled. Exceeding the shared rate rule: `429`.
- Shares the existing `run_dream_phase` rate rule (6 requests/hour) with the
  CLI/admin dream-phase path -- one bucket per tenant, consumed once per
  trigger call, not once per session summarized.
- **Sweep scope is tenant(+agent)-wide, not project-scoped.** The project
  header still authenticates and binds the request the same way as every
  other hosted route, but `messages`/`session_summaries` have no `project_id`
  column today, so the sweep itself sees every project sharing that tenant's
  database. `list_compactable_session_ids` matches on `agent_id IS ?`
  (exact equality, including SQL `NULL`-safe comparison) -- it is NOT "all
  agents for the tenant." A trigger that omits `X-Vexic-Agent-Id` (or sends
  no agent id in scope) sweeps only sessions recorded with a `NULL`
  `agent_id`; a trigger that sends an agent id sweeps only sessions recorded
  with that exact `agent_id`. Operators must align the trigger's agent
  header with however the recorder writes transcripts for that agent, or
  those sessions will never be swept. A tenant with multiple projects
  sharing one database gets one shared summarize budget and sweep per
  `(tenant_id, agent_id)`, not per-project isolation. Project-scoped storage
  is a separate future change if a multi-project tenant ever needs it.
- Execution itself never blocks the request or the serving event loop: the
  phase runs on its own worker thread with its own event loop
  (`asyncio.to_thread(asyncio.run, ...)`), so a slow summarize call cannot
  stall other hosted traffic.

Daily span budget: `VEXIC_SUMMARIZE_DAILY_SPAN_BUDGET` (default `50`) caps
how many `session_summaries` rows (leaf writes and condense writes both
count) a tenant(+agent) can accumulate per UTC calendar day. Once the budget
is reached, the phase stops adding new spans/condensations for the rest of
that UTC day and returns cleanly -- `/v1/fresh_context` still serves whatever
frontier-plus-tail recap is available, it just stops growing until the next
UTC day. The budget window is UTC-day and is a different clock than the
2h-idle/3am-local "is this session ripe to summarize" heuristic the phase
uses elsewhere -- spend is bounded on a calendar-day clock, ripeness is
evaluated on a wall-clock heuristic; the two are intentionally independent.

Model used for summarization: `VEXIC_SUMMARY_MODEL`, read by
`adapters/openrouter_live_adapter.py`'s `build_summary_agent`, defaulting to
`deepseek/deepseek-v4-pro`.

### In-server dream sweeper (ADR 0030)

The hosted app starts its own periodic sweeper in the FastAPI lifespan
(`vexic.hosted_sweeper.DreamSweeper`) -- this replaced the earlier
single-tenant `dream-cron.yml` GitHub workflow and its three repo secrets.
Every tick (`VEXIC_DREAM_SWEEP_TICK_SECONDS`, default 1800) it walks every
active tenant in the catalog and, per recorded agent scope:

- schedules a summarize sweep when the tenant has new transcript rows since
  the last swept watermark (cheap `MAX(id)` check before opening anything
  else), and
- schedules a full Light -> REM -> Deep -> Summarize chain when the tenant's
  dream interval (`VEXIC_DREAM_INTERVAL_SECONDS`, default 86400) has elapsed
  since the last completed chain.

Scheduling reuses the trigger endpoint's machinery through
`HostedMemoryService.schedule_system_dream`: pre-bound, server-minted
requests under a `system` principal (`dream-sweeper`), the per-(tenant,
agent) in-flight dedup lock, worker-thread event-loop isolation, per-phase
job events, and the daily span budget all apply unchanged. Tenants are
staggered within a tick; a broken tenant or missing dream ports skips
content-free without affecting the rest of the tick. Per-tenant opt-out:
`dream_scheduling = 0` on the tenant catalog row
(`HostedTenantCatalog.set_dream_scheduling`). Kill switch:
`VEXIC_DREAM_SWEEPER=off`. Sweeper bookkeeping (last completed summarize
watermark and last dream completion, per (tenant, agent) scope, advanced
monotonically and only after the scheduled job finishes) lives in the control
database's `dream_sweep_state` table.

### Recorder-side backstop trigger

`recorder prime` (invoked from the Claude Code SessionStart hook) spawns a
detached, fire-and-forget `vexic recorder trigger-dream` subprocess before
doing its normal priming work, as a backstop between in-server sweeper ticks (ADR 0030). This
adds no serial latency to the hook: the subprocess is spawned with
`stdin`/`stdout`/`stderr` all `DEVNULL` and `start_new_session=True` (an
inherited stdout pipe would keep the hook's own stdout open until the child
exits, which would defeat the "zero added latency" goal) and prime does not
wait on it. Credentials travel to the child via `--config <path>` only, never
as an `--api-key` argv value, to avoid exposure in `ps` output for the
child's lifetime. Spawn failures, trigger timeouts (5s), and non-2xx
responses are all swallowed with a stderr warning -- the subcommand always
exits `0` and never affects prime's own output or exit code.

**Known limitations, accepted for v1** (see ADR 0025):

- The 6/hour rate limiter is in-process, so under multiple replicas the rate
  cap becomes per-replica rather than global. Revisit with a shared limiter
  before scaling out. The in-flight dedup guard is no longer in this bucket:
  ADR 0032 made it a durable control-plane lease after the single-replica
  assumption broke on ordinary rolling deploys, where the old and new
  containers overlap and both swept the same tenant.
- A scheduled trigger task is in-memory: it does not survive a process
  restart or redeploy mid-sweep. The next trigger (cron or prime) re-runs
  idempotently, so no data is lost, but an in-flight sweep at deploy time is
  simply abandoned rather than resumed.
- Prime's pre-existing serial-timeout budget (up to three sequential 15s
  `urlopen` calls against the SessionStart hook's 30s kill) is unchanged by
  this work and remains a known follow-up to tighten or parallelize
  separately.

Revoke a throwaway key by key id, not by raw key:

```powershell
uv run --no-sync python -m vexic.hosted_http revoke-key --root .hosted-memory --key-id <key-id> --revoked-by <operator>
```

This revokes against the control plane selected by `VEXIC_CONTROL_PLANE_TARGET`
in the CLI's environment (unset or `local` falls back to the local
`control-plane.db` under `--root`). To revoke a key on a Turso-backed
deployment, run the command with the service's environment loaded (e.g.
`railway run ...`) so the revocation lands on the same control plane the
service authenticates against.

This is internal-alpha infrastructure for throwaway data. It is not a
production customer-data launch, public MCP endpoint, billing portal, dashboard,
or enterprise auth surface.

## Readiness

External customer-memory readiness is blocked by the hosted readiness gate.
This hosted shell remains internal-only until that gate is satisfied or an
explicit security/engineering owner risk acceptance is recorded.

Internal-only code surface:

- in-process Python API boundary and internal-alpha HTTP adapter;
- per-tenant Turso databases for customer memory, and a managed Turso
  control-plane database holding the tenant catalog, API-key/revocation records,
  and the sanitized audit, usage, and job lifecycle ledgers, when the backend
  flags select Turso (ADR 0019 and its Addendum 5). The local SQLite path
  remains supported and is what the unset flags select;
- deployments using Turso's free tier must use the scripted `turso db dump`
  recipe (`.github/workflows/turso-backup.yml`); that tier has no PITR and
  therefore does not meet ADR 0008's point-in-time recovery target;
- single-process in-memory authenticated request limiter;
- one `LocalMemoryService` instance is created per hosted request;
- hosted Light/REM/Deep/Summarize jobs run only with injected host model
  ports and fail closed without them (REM itself makes no model calls; see
  ADR 0020).

### Production Telemetry Policy

The v1 telemetry vocabulary is intentionally narrow:

- `HostedAuditEvent`;
- `HostedUsageEvent`;
- `HostedJobEvent`;
- `retrieval_events`; and
- `candidate_retrieval_events`.

`HostedAuditEvent`, `HostedUsageEvent`, `HostedJobEvent`, and non-content
operational aggregates are control-plane operational telemetry. They are used
to run, audit, meter, debug, and plan capacity for the hosted memory API. They
must not store raw memory payloads, prompt payloads, hidden instructions,
thinking traces, tool bodies, retrieval query text, raw API keys, provider
secrets, database tokens, or configured forbidden values.

V1 retains control-plane operational telemetry and non-content operational
aggregates for 400 days, then deletes them. These records are not part of a
tenant memory export. After a customer or scope deletion, Vexic may retain only
the minimized operational records needed for deletion evidence, security,
abuse, metering support, incident response, or audit, under the same 400-day
retention window and the same no-content rule.

`retrieval_events` and `candidate_retrieval_events` are tenant-scoped memory
telemetry inside the Customer Memory Database. They are part of replayable
memory behavior, not cross-tenant product analytics. They may contain
query-bearing telemetry, so they are retained with the Customer Memory Database,
included in scoped export artifacts, excluded from transcript-only replay
responses, and removed from active access through the same scope tombstone
behavior as memory rows. Physical purge remains
backend/SLA-specific and must not be promised before provider backup or Object
Lock retention expires.

The Support View may expose account, project, key, usage, audit, job, and
incident metadata needed to operate hosted Vexic. It must not expose raw memory,
transcript text, fact text, retrieval query text, prompt or tool bodies, hidden
instructions, thinking traces, raw keys, provider secrets, database tokens, or
configured forbidden values. Privileged inspection of tenant-scoped memory
telemetry requires a purpose-bound operator procedure with approval, audit
logging, time-boxing, and post-incident review.

Product-improvement use of customer-data-derived content is default off.
Content-bearing or query-bearing memory telemetry, including `retrieval_events`
and `candidate_retrieval_events`, must not be used for cross-tenant product
improvement unless a separate consent, retention, deletion, security, and legal
gate is accepted. Non-content operational aggregates may be used for capacity,
reliability, and product planning within the 400-day retention window.

### Incident Response And Security Review

The hosted incident response and pre-beta security review runbook, and the
first synthetic scoped-key tabletop artifact, are maintained in the private
hosted-ops repository.
These artifacts satisfy documentation/tabletop evidence only; they do
not close the hosted readiness gate or make hosted Vexic external/customer-data
ready.

### Production Customer-Data Readiness Requirements

Before a deployment handles production customer data, its operators must:

- select backup capabilities that meet ADR 0008 for both the control plane and
  customer-memory databases. A Turso tier without point-in-time recovery does
  not meet that bar through the scripted `turso db dump` recipe alone;
- execute and sign off an end-to-end restore drill against the deployment's
  actual managed backup/PITR mechanism, plus the required incident tabletop
  and security review. `vexic.restore.run_restore_drill` supplies unit-tested
  provision -> import -> verify -> activate-or-destroy decision logic, but the
  live drill and its evidence remain operator-owned. S3 Object Lock export
  restore needs the same evidence before it can support a readiness claim;
- configure network hardening, distributed rate limiting, a support-access
  workflow, Cloudflare/WAF or equivalent origin protection, auth-failure
  throttling, alerting, and an abuse-override workflow;
- implement any billing, dashboard, portal, enterprise SSO, and compliance
  surfaces required by the product launch; and
- define and verify a backend/SLA-specific physical-purge mechanism beyond the
  local service implementation.

The private hosted-ops repository, not this versioned document, records whether
a particular deployment has completed those steps and the evidence for them
(ADR 0033).
