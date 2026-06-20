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
- `project_id`, `user_id`, and `session_id` are optional refinements.
- Transcript and `expand_history` operations require `session_id`.
- Scope means the conjunction of all non-null identifier fields.
- `principal`, `trust_boundary`, `capabilities`, and optional
  `correlation_id` travel with the scope for authorization and audit metadata.

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

## Capabilities

Capabilities are explicit strings through `MemoryCapability`.

| Capability | Purpose |
| --- | --- |
| `memory:read` | Ordinary read access. Does not grant search by itself. |
| `memory:write` | Transcript writes and write-side telemetry. |
| `memory:search` | Transcript and long-term search. |
| `memory:expand` | Privileged verbatim transcript egress. |
| `memory:export` | Privileged export egress. |
| `memory:replay` | Privileged replay egress. |
| `memory:admin:rebuild` | Admin rebuild or dream-phase operations. |
| `memory:admin:lifecycle` | Scope tombstone and lifecycle operations. |

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
| Search long-term | `SearchLongTermRequest` | `memory:search` | Implemented |
| Record retrieval event | `RecordRetrievalEventRequest` | `memory:write` | Deferred |
| Retire fact | `RetireFactRequest` | `memory:write` | Deferred |
| Run dream phase | `RunDreamPhaseRequest` | `memory:admin:rebuild` | Deferred |
| Export scope | `ExportScopeRequest` | `memory:export` | Deferred |
| Replay scope | `ReplayScopeRequest` | `memory:replay` | Deferred |
| Rebuild | `RebuildRequest` | `memory:admin:rebuild` | Deferred |
| Delete scope | `DeleteScopeRequest` | `memory:admin:lifecycle` | Deferred |

Deferred means the protocol surface exists, but the local v0.1 adapter is not
the implementation point yet. Do not wire these by importing Coalescent runtime
code.

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
- Candidates are promoted, retired, marked stale, or marked for review.
- Long-term facts are retired or superseded, not physically removed by ordinary
  retrieval or promotion.
- Scope deletion is modeled as a tombstone/scope-deny contract.
- Physical purge is backend and SLA specific, and remains deferred.

Audit records for lifecycle operations should retain actor, scope, operation,
and correlation metadata without retaining deleted payload text unnecessarily.

## Storage Posture

The contract is storage-neutral. The current local reference implementation uses
one SQLite database per opened memory context.

| Backend posture | Contract requirement |
| --- | --- |
| Current SQLite | Tenant identity is validated from the opened local context and `MemoryScope`; memory tables do not need `tenant_id` columns while the database file is the isolation boundary. |
| Local/self-host SQLite | Default v0.1 adapter shape. Strong simple isolation is one SQLite file per customer or scope boundary. |
| Early hosted storage | Prefer isolated per-customer storage before shared tables. |
| Future shared storage | Shared tables require explicit tenant-isolation tests, audit logging, lifecycle guarantees, and operational maturity. |

Future adapters must pass the same behavior and scope tests. Physical schema
parity is not required.

## Host Boundary

Vexic core does not:

- authenticate network callers
- read provider secrets from the environment
- build provider-backed model clients directly
- load or download embedding models directly
- choose hosted storage backends
- own host-specific extension tables

Those are adapter or host responsibilities. Model-backed operations, including
embedding text for vector search, use host ports from `src/vexic/ports.py`.

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
| Light, REM, Deep | `vexic.pipeline`, `vexic.rem`, and `vexic.deep` primitives with host-supplied model ports |
| Per-tenant SQLite `memory.db` | local SQLite adapter opened through validated scope/context |

Coalescent remains a private host and first-party consumer. Vexic must stay
usable without importing Coalescent runtime modules.
