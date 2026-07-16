# Configuration Reference

Every environment variable read by code in `src/` and `adapters/`. The local
library and MCP server need none of these by default; variables marked
**operator-only** configure the hosted internal-alpha deployment
(see [`hosted-mvp.md`](hosted-mvp.md)) and are not part of the public surface.

## Local MCP / hosted MCP client

| Variable | Component | Default | Notes |
| --- | --- | --- | --- |
| `VEXIC_API_KEY` | `vexic.mcp_stdio`, `vexic.hosted` CLI | -- | Raw API key for hosted-API mode. The variable *name* is configurable via `--api-key-env`; `VEXIC_API_KEY` is only the default name. Unused in local `--db-path` mode. |

## Hosted service (operator-only)

| Variable | Component | Default | Notes |
| --- | --- | --- | --- |
| `PORT` | `vexic.hosted_entrypoint` | `8000` | HTTP listen port; provided by Railway. |
| `VEXIC_RUNTIME_USER` | `vexic.hosted_entrypoint` | `vexic` | Unprivileged user the server drops to after fixing volume ownership. |
| `VEXIC_HOSTED_ROOT` | `vexic.hosted`, `vexic.hosted_http`, `vexic.hosted_entrypoint` | `.hosted-memory` (CLI); `/data/vexic` (entrypoint) | Root directory for hosted key/tenant state and local per-store databases. |
| `VEXIC_STORAGE_BACKEND` | `vexic.hosted`, `vexic.hosted_http` | `local` | `local` or `turso`. Selects the per-store storage backend (non-secret flag). Routes customer memory only. |
| `VEXIC_CONTROL_PLANE_TARGET` | `vexic.hosted`, `vexic.hosted_http` | `local` | `local` or `turso`. Selects where the control-plane catalog and API-key store live (non-secret flag; ADR 0019). Independent of `VEXIC_STORAGE_BACKEND`: that flag routes only customer memory, this one routes only the control plane. Omitting it silently selects the local filesystem `control-plane.db` under `VEXIC_HOSTED_ROOT` -- the same silent-`local` footgun `VEXIC_STORAGE_BACKEND` has. |
| `VEXIC_CONTROL_PLANE_TOKENS` | `vexic.hosted_control_plane_http` | -- (endpoints disabled) | Comma-separated bearer tokens for the Console control-plane API. |
| `VEXIC_MCP_ALLOWED_ORIGINS` | `vexic.mcp_http` | -- | Comma-separated extra `Origin` values allowed on the hosted MCP endpoint. |
| `VEXIC_DOGFOOD_TENANT_ID` | `vexic.hosted_http` | -- | Tenant id whose telemetry is tagged as dogfood traffic. |
| `VEXIC_PROVISION_EXISTING_TURSO_TARGETS` | `vexic.hosted_http` | -- | Set to `1` to backfill Turso databases for stores provisioned before the Turso backend. |
| `VEXIC_DREAM_PHASE_ADAPTER` | `vexic.hosted` | -- | Filesystem path to the dream-phase adapter module (e.g. `/app/adapters/openrouter_live_adapter.py`). |
| `VEXIC_DREAM_PHASE_MODEL_GROUP` | `vexic.hosted` | `hosted-dream` | Model group name passed to the adapter's agent builders. |
| `VEXIC_SUMMARIZE_DAILY_SPAN_BUDGET` | `vexic.hosted` | `50` | Per-day cap on summarize-phase spans per tenant. |
| `VEXIC_DREAM_SWEEPER` | `vexic.hosted_sweeper` | `on` | Kill switch for the in-server dream sweeper (`off`/`0`/`false`/`no` disables; ADR 0030). |
| `VEXIC_DREAM_SWEEP_TICK_SECONDS` | `vexic.hosted_sweeper` | `1800` | How often the sweeper walks active tenants. |
| `VEXIC_DREAM_INTERVAL_SECONDS` | `vexic.hosted_sweeper` | `86400` | Minimum gap between full Light -> REM -> Deep chains per tenant. |
| `VEXIC_DREAM_FAILURE_BACKOFF_SECONDS` | `vexic.hosted_sweeper` | `3600` | Re-arm interval for a scope whose last chain failed without durably recording (withheld dream stamp); shorter than the success interval so transient faults recover fast but a persistent unrecorded failure retries at this cadence, not every tick. |
| *(names passed via `--secret-env`)* | `vexic.hosted` CLI | -- | `run-dream-phase --secret-env NAME` reads each named variable and threads it to the adapter as a forbidden secret value. |

## Turso backends (operator-only)

The two Turso flags gate different variables. Each variable below is listed
under the flag that actually causes it to be read; setting one flag does not
make the other flag's variables required.

### Customer-memory provisioning -- required when `VEXIC_STORAGE_BACKEND=turso`

Read by `TursoProvisioningPort.from_env` (and `vexic.hosted_http` for the org
slug) to create and address per-tenant customer databases.

| Variable | Component | Default | Notes |
| --- | --- | --- | --- |
| `TURSO_ORG` | `vexic.hosted_http`, `adapters/turso_adapter.py` | -- (required) | Turso organization slug used for provisioning and for resolving per-tenant DSNs. |
| `TURSO_GROUP` | `adapters/turso_adapter.py` | -- (required) | Turso database group new per-store databases are created in. |
| `TURSO_PLATFORM_API_TOKEN` | `adapters/turso_adapter.py` | -- (required) | Platform API token used to create databases and mint DB tokens. |

### Control-plane database -- required when `VEXIC_CONTROL_PLANE_TARGET=turso`

Read by `control_plane_target()` to address the managed control-plane database.
Also read by `vexic.migrate_control_plane --target-from-env`, which resolves the
same target regardless of `VEXIC_STORAGE_BACKEND`.

| Variable | Component | Default | Notes |
| --- | --- | --- | --- |
| `TURSO_DATABASE_URL` | `adapters/turso_adapter.py` | -- (required) | libSQL DSN of the control-plane database (tenant catalog, API keys, telemetry). |
| `TURSO_AUTH_TOKEN` | `adapters/turso_adapter.py` | -- (required) | Auth token for `TURSO_DATABASE_URL`. |

### Remote query deadline -- read when either Turso flag is `turso`

| Variable | Component | Default | Notes |
| --- | --- | --- | --- |
| `VEXIC_REMOTE_QUERY_DEADLINE_SECONDS` | `adapters/turso_adapter.py` | `30.0` | Wall-clock deadline on each remote libSQL driver call (ADR 0019 Addendum 7). A hang past the deadline surfaces as a retryable 503 `storage_unavailable`. Absent or malformed falls back to the default. Local SQLite is never bounded. |

## OpenRouter live adapter (operator-only)

Read by `adapters/openrouter_live_adapter.py` when it is configured as the
dream-phase adapter.

| Variable | Component | Default | Notes |
| --- | --- | --- | --- |
| `OPENROUTER_API_KEY` | live adapter | -- (required) | OpenRouter platform key; read only inside the adapter module. |
| `VEXIC_LIVE_MODEL` | live adapter | `deepseek/deepseek-v4-pro` (dream agents only) | Fallback agent model when a model group has no per-group override. The default applies to the dream agents; the recall judge has no implicit default -- with neither the per-group name nor `VEXIC_LIVE_MODEL` set it raises `RuntimeError` rather than fall through, so a silent fallback cannot misattribute recall scores to the wrong judge model. |
| *(pattern)* `VEXIC_LIVE_<GROUP>_MODEL` | live adapter | -- | Name pattern, not a literal variable: the model group is upper-snake-cased into the name, e.g. `VEXIC_LIVE_HOSTED_DREAM_MODEL` for the `hosted-dream` group. Wins over `VEXIC_LIVE_MODEL`. |
| `VEXIC_SUMMARY_MODEL` | live adapter | `deepseek/deepseek-v4-pro` | Model for the summarize phase (not routed by model group). |
| `VEXIC_LIVE_EMBEDDING_MODEL` | live adapter | `openai/text-embedding-3-small` | Embedding model. |
| `VEXIC_LIVE_MAX_OUTPUT_TOKENS` | live adapter | `8192` per dream agent (extraction, contradiction, summary); the recall judge is uncapped | Per-request output token cap. Set it to override the extraction, contradiction, and summary defaults with one value; the recall judge stays uncapped even when this is set, so a long structured verdict reason cannot truncate into a judge error. The default model reasons before it emits and thinking tokens count against this cap, so size it for the reasoning, not the visible output -- a cap sized to the output truncates the agent into `finish_reason=length`. |
| `VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS` | live adapter | `60.0` | Per-request timeout. |
