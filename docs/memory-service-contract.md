# Memory Service Contract

> Role: human-readable reference for the Vexic public memory contract.
> Executable source of truth: `src/vexic/contract/__init__.py`.

Vexic v0.1 defines a service-shaped memory contract before choosing hosted
adapters. Python models, request/result semantics, and the `MemoryService`
Protocol are the stable core; HTTP, MCP, SDK, and hosted-service adapters can
be built over the same contract later.

## Source Of Truth

The contract code owns exact field names and validation:

- `CONTRACT_VERSION = "0.1.0"`
- `MemoryScope` and `MemoryScopeSelector`
- `Principal`, `PrincipalType`, and `TrustBoundary`
- `MemoryCapability`
- operation request/result models
- `RedactionContext`
- `MemoryService`

Markdown is explanatory. If this document disagrees with
`src/vexic/contract`, update the document or make an explicit contract change
with tests.

## Identity And Scope

Every public request carries an actor/auth `MemoryScope`.

- `tenant_id` is required and nonblank.
- `project_id`, `user_id`, `session_id`, and `agent_id` are optional
  refinements.
- Transcript and `expand_history` operations require `session_id`.
- Scope means the conjunction of all non-null identifier fields.
- `agent_id = None` is the explicit shared agent scope inside the same
  tenant/project/user/session parent scope. It is not a wildcard.
- Agent reads are exact by default. To combine shared and agent-specific memory,
  a caller must issue explicit shared and agent-specific reads, or use an
  adapter helper that performs those reads explicitly.
- `principal`, `trust_boundary`, `capabilities`, and optional
  `correlation_id` travel with the scope for authorization and audit metadata.
  `principal_id` identifies who acted; it is not a memory-scope identifier and
  must not be used as a fallback `agent_id`.

Lifecycle deletion uses two shapes:

- `DeleteScopeRequest.scope` is the actor/auth `MemoryScope`.
- `DeleteScopeRequest.target_scope` is a `MemoryScopeSelector`, an
  identifiers-only target.
- `target_scope.tenant_id` must match `scope.tenant_id`.

Networked adapters should bind `tenant_id` from credentials or an authorized
tenant selection, not arbitrary caller payload. The local trusted core accepts a
validated `MemoryScope`, and storage/query layers must still enforce it.

## Versioning

The v0.1 contract uses `CONTRACT_VERSION = "0.1.0"`. Request and result models
carry the contract version so future adapters can reject unsupported payloads
explicitly. Breaking changes require a new contract version rather than silent
request semantic changes.

`agent_id` remains in the v0.1 contract version because the package has not
shipped through public package registries and omitted values map to the
explicit shared agent scope. A future incompatible request semantic change
should still bump the contract version.

## Capabilities

Capabilities are explicit strings through `MemoryCapability`.

| Capability | Purpose |
| --- | --- |
| `memory:read` | Ordinary read access. Does not grant search by itself. |
| `memory:write` | Transcript writes and write-side telemetry. |
| `memory:search` | Transcript and long-term search. |
| `memory:expand` | Privileged verbatim transcript egress. |
| `memory:fresh-context` | No-query session priming: bounded summary recap plus raw tail, not arbitrary-range verbatim reads. |
| `memory:export` | Privileged export egress. |
| `memory:replay` | Privileged replay egress. |
| `memory:admin:rebuild` | Admin rebuild or dream-phase operations. |
| `memory:admin:lifecycle` | Scope tombstone and lifecycle operations. |
| `memory:dream:trigger` | Schedule a Summarize dream-phase sweep without granting admin rebuild. |

Use `require_capability(scope, capability)` for the common fail-closed check.

## Operation Catalog

The Python request/result models are authoritative. This table summarizes the
behavioral contract and the current `LocalMemoryService` v0.1 surface.

| Operation | Request | Required capability | Current local service |
| --- | --- | --- | --- |
| Append transcript | `AppendTranscriptRequest` | `memory:write` | Implemented |
| Ingest source transcript | `IngestSourceTranscriptRequest` | `memory:write` | Implemented |
| Search transcript | `SearchTranscriptRequest` | `memory:search` | Implemented |
| Expand history | `ExpandHistoryRequest` | `memory:expand` | Implemented |
| Fresh context | `FreshContextRequest` | `memory:fresh-context` | Implemented |
| Search long-term | `SearchLongTermRequest` | `memory:search` | Implemented |
| Record retrieval event | `RecordRetrievalEventRequest` | `memory:write` | Implemented |
| Retire fact | `RetireFactRequest` | `memory:write` | Implemented |
| Run dream phase | `RunDreamPhaseRequest` | `memory:admin:rebuild` | Host-port backed |
| Trigger dream phase | `TriggerDreamPhaseRequest` | `memory:dream:trigger` | Host-port backed (async, summarize-only in v1) |
| Export scope | `ExportScopeRequest` | `memory:export` | Implemented |
| Replay scope | `ReplayScopeRequest` | `memory:replay` | Implemented |
| Rebuild | `RebuildRequest` | `memory:admin:rebuild` | Implemented |
| Delete scope | `DeleteScopeRequest` | `memory:admin:lifecycle` | Implemented |

Host-port backed means `LocalMemoryService` authorizes and checks lifecycle
state, then executes Light, REM, or Deep only when a host supplies explicit
dream-phase ports. Without those ports it fails closed with
`HostPortNotConfigured` through `missing_host_port`. Inside supplied ports,
embedding may fall back to the optional `vexic[local-embed]` adapter and Deep
contradiction may be deferred; REM runs locally as a deterministic
embedding-centrality heuristic and consumes none of the supplied ports, but
still executes only inside the same gate (ADR 0020). Do not wire this by
importing private host runtime code.

`DreamPhase` has four values: `light`, `rem`, `deep`, and `summarize`.
`DreamPhase.SUMMARIZE` compacts Tier 1 spans into `session_summaries` rows
that back fresh context (ADR 0024); it needs a host-supplied
`build_summary_agent` port and fails closed with `HostPortNotConfigured`
without one, the same gate as Light and Deep.

### Trigger Dream Phase

`TriggerDreamPhaseRequest` is a thin boundary request that carries its own
capability (`memory:dream:trigger`), not `memory:admin:rebuild`, so a
trigger-only key (the recorder or a cron caller) never needs admin-rebuild
just to kick off a sweep. It hard-restricts `phase` to `DreamPhase.SUMMARIZE`
in v1 -- any other value fails validation (surfaced as `400` over HTTP).
`scope.session_id` is not required: the sweep is not scoped to one session.

The service authenticates, binds, and rate-checks this request exactly once
at the trigger boundary, then internally mints a fully-scoped
`RunDreamPhaseRequest` (server-side `memory:admin:rebuild`) and executes it
directly rather than re-entering the ordinary `run_dream_phase` call path --
re-entering would strip the minted capability during capability
intersection and double-count the shared rate bucket. `TriggerDreamPhaseResult`
carries `status` (`"scheduled"` or `"skipped"`) and an optional `reason`
(e.g. `"already_running"` when a sweep for the same tenant+agent is already
in flight -- an in-process, per-(tenant_id, agent_id) lock). The scheduled
sweep runs asynchronously; the caller gets `202` back immediately and does
not await phase completion.

**Scope is tenant(+agent)-wide, not project-scoped.** The request's
authenticated project header binds and authorizes the call the same as any
other route, but `messages` and `session_summaries` carry no `project_id`
column, so the sweep sees every project sharing that tenant's database
regardless of which project triggered it. Within that, `agent_id` scoping is
exact, not "all agents": `list_compactable_session_ids` filters with
`agent_id IS ?`, so a trigger that supplies no agent id matches only
`NULL`-agent sessions, and a trigger that supplies an agent id matches only
sessions recorded with that exact `agent_id`. There is no "sweep every
agent for this tenant" mode -- the operator must align the trigger's agent
header (or its absence) with how the recorder writes transcripts for the
agent(s) they intend to sweep. A tenant whose projects share one database
get one shared sweep and one shared daily span budget per `(tenant_id,
agent_id)`, not per-project isolation.

A daily span budget (`VEXIC_SUMMARIZE_DAILY_SPAN_BUDGET`, default `50`)
caps `session_summaries` writes (leaf and condense both count) per
tenant(+agent) per UTC calendar day, counted against each row's explicit
`created_at`. The budget window (UTC-day) is a distinct clock from the
phase's own 2h-idle/3am-local ripeness heuristic -- spend accounting and
ripeness evaluation are independent concerns on independent clocks.

## Fresh Context

`FreshContextRequest` is session-scoped and redaction-required, with a
`token_budget` (default `6_000`). It requires `memory:fresh-context` rather
than `memory:expand`: fresh context returns a bounded recap plus tail for
priming a new conversation, not an arbitrary-range verbatim read. Result
`FreshContextResult` carries `summaries` (the `SessionSummary` frontier read),
`recent` (the raw tail `TranscriptHit`s past the frontier's covered prefix),
the assembled `text`, and `truncated`.

`PRIME_CONTEXT_HEADER = "Vexic memory priming:"` marks host-injected priming
context (fresh-context recap plus any long-term/transcript search results) so
recorders can recognize and skip it. A host recorder must not re-ingest text
containing this header as Tier 1 transcript; `ingest_source_transcript`
independently rejects any row containing it
(`reason="prime context is not transcript text"`), so injected priming never
re-enters Tier 1 and is never re-summarized or re-extracted.

## Redaction

`RedactionContext` is mandatory for write operations and privileged egress
operations that persist or return bulk text.

The core policy is fail-closed:

- reject payloads containing configured forbidden values
- do not sanitize payloads in place
- do not persist or return forbidden values after a redaction violation
- source-ledger transcript ingestion rejects polluted rows per row before
  persistence and creates no ledger entry for rejected rows

Direct or offline database modes that cannot load host secrets must make that
limitation explicit. Vexic core accepts forbidden values supplied by the host;
it does not discover provider secrets itself.

## Lifecycle

Memory is retained by default.

- Transcript rows are append-only.
- Existing transcript rows are never backfilled to assign an `agent_id`.
  Pre-agent rows keep `agent_id = NULL` and are shared-scope rows.
- Candidates are promoted, retired, marked stale, or marked for review.
- Long-term facts are retired or superseded, not physically removed by ordinary
  retrieval or promotion.
- Scope deletion is modeled as a tombstone/scope-deny contract.
- The local SQLite adapter records tombstones in `scope_tombstones` and blocks
  retrieval, export, replay, and rebuild for matching scopes.
- Physical purge is a second deliberate step (`purge_scope`, ADR 0022): it
  requires an existing tombstone for exactly the target scope, irreversibly
  deletes the scope's canonical rows, projections, and content-bearing
  telemetry from the primary database in one transaction, and records
  `purged_at` plus per-table counts on the tombstone. Provider backups retain
  residual copies until their own retention expires; wording must not promise
  instantaneous global erasure.
- Content-bearing retrieval telemetry supports age-based expiry
  (`expire_retrieval_queries`): query text is blanked in place, rows and
  derived counters survive.

Audit records for lifecycle operations should retain actor, scope, operation,
and correlation metadata without retaining deleted payload text unnecessarily.

## Storage Posture

The contract is storage-neutral. The current local reference implementation uses
one SQLite database per opened memory context.

| Backend posture | Contract requirement |
| --- | --- |
| Current SQLite | Tenant identity is validated from the opened local context and `MemoryScope`; memory tables do not need `tenant_id` columns while the database file is the isolation boundary. |
| Local/self-host SQLite | Default v0.1 adapter shape. Strong simple isolation is one SQLite file per customer or scope boundary. |
| Hosted v1 storage | One isolated SQLite-compatible Customer Memory Database per customer tenant. The hosted adapter binds tenant identity from credentials or authorized tenant selection to exactly one database handle. |
| Future shared storage | Shared tables require explicit tenant-isolation tests, audit logging, lifecycle guarantees, and operational maturity. |

Future adapters must pass the same behavior and scope tests. Physical schema
parity is not required.

Hosted v1 storage should remain Postgres-ready without making Postgres a v1
dependency. Storage-sensitive API, migration, export, and rebuild decisions
should keep canonical memory rows portable through the public contract, so a
future Postgres database-per-customer adapter can be introduced for concrete
operational requirements without changing Vexic memory semantics.

Hosted adapters must not turn local SQLite details into public API semantics.
Before launch, the hosted storage adapter should pass conformance tests against
the local SQLite reference behavior, including FTS/vector retrieval, export,
replay, rebuild, tombstones, and redaction. Project, user, and session scopes
remain `MemoryScope` filters inside a Customer Memory Database.

## Agent Scope Test Matrix

Agent-scoped adapters must prove:

- contract JSON round trips and blank-value validation for `agent_id`
- fresh and pre-existing database migration with unchanged transcript rows
- exact-read isolation between Agent A, Agent B, and shared rows
- source-ledger ingest and idempotency within the decided scope
- scoped Light watermarks, including retry/no-op behavior
- scoped candidate insertion, merge, retirement, promotion, and supersession
- long-term keyword, vector, fused retrieval, and candidate fallback isolation
- retrieval and candidate-retrieval telemetry scoped enough for audit/rebuild
- tombstones, export, replay, rebuild, and summaries do not leak other agents
- local MCP and hosted-shell adapters bind configured agent scope and reject
  caller widening

## Host Boundary

Vexic core does not:

- authenticate network callers
- read provider secrets from the environment
- build provider-backed model clients directly
- require embedding model dependencies unless the optional local embedding extra
  is installed
- choose hosted storage backends
- own host-specific extension tables

Those are adapter or host responsibilities. LLM-backed operations use host ports
from `src/vexic/ports.py`; embedding text for vector search can use a host port
or the optional lazy local adapter from ADR 0016.

## Coalescent Compatibility Map

Vexic was extracted from Coalescent. These mappings are compatibility context,
not runtime dependencies.

| Coalescent surface | Vexic mapping |
| --- | --- |
| `engine.memory_contract` | `vexic.contract` |
| `engine.memory_service` local behavior | `vexic.service.LocalMemoryService` where implemented |
| `search_memory` transcript behavior | `SearchTranscriptRequest` / `search_transcript` over scoped clean Transcript |
| `search_long_term` | `SearchLongTermRequest` / `search_long_term` with durable facts first and candidate fallback on zero Tier 3 hits |
| `expand_history` | `ExpandHistoryRequest` / privileged, session-scoped verbatim egress |
| Light, REM, Deep | `vexic.pipeline`, `vexic.rem`, and `vexic.deep` primitives; host-supplied agent ports cover Light extraction and Deep contradiction only (REM is a local heuristic), with optional local embeddings and deferrable Deep contradiction |
| Per-tenant SQLite `memory.db` | local SQLite adapter opened through validated scope/context |

Coalescent remains a private host and first-party consumer. Vexic must stay
usable without importing Coalescent runtime modules.
