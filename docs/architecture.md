# Vexic Architecture

> Role: current Vexic memory-core design reference.
> Contract reference: `docs/memory-service-contract.md`.
> Extraction provenance: `docs/provenance.md`.

Vexic is a provenance-first, replayable memory core for long-running agents. It
keeps a lossless transcript, derives short-term candidates, promotes durable
facts, and records retrieval telemetry so memory can be audited and rebuilt.

## Current Code Boundary

Vexic v0.1 is a local Python package, not a hosted service.

Key modules:

- `vexic.contract` - public request/result models and `MemoryService` Protocol
- `vexic.service` - local SQLite reference service
- `vexic.storage` - schema, transcript, candidates, long-term facts,
  promotion, labels, and summaries
- `vexic.pipeline` - Light extraction phase
- `vexic.rem` - REM boost phase
- `vexic.deep` - Deep promotion/supersession phase
- `vexic.subagents.retrieval` - hybrid Tier 3 retrieval and candidate fallback
- `vexic.mcp_stdio` - read-only local stdio MCP MVP
- `vexic.ports` - host-supplied model-agent ports
- `vexic.redaction` - persistence and egress secret guard

The package must not import Coalescent `engine.*` modules. Coalescent is a host
consumer, not a dependency.

## Goals

- Lossless transcript: Tier 1 rows are never updated or deleted.
- Glass-box facts: every durable fact carries provenance, confidence, category,
  and editability metadata.
- Replayable projections: FTS, vectors, and summaries are rebuildable from
  canonical rows and code.
- Explicit scope: requests carry `MemoryScope`, and the local adapter validates
  the opened SQLite context against it.
- Host-neutral core: providers, embeddings, secrets, auth, and managed
  operations live in adapters or hosts.

## Non-goals

- Coalescent AgentOS runtime wiring.
- Tenant-folder management, Telegram, Blog Writer, skills, teammates, or model
  routing.
- Hosted auth, billing, dashboards, public HTTP, or remote MCP in the v0.1 core.
- External vector databases for the local core.
- Destructive chat-window compression.
- Physical purge semantics before a backend/SLA decision exists.

## Memory Tiers

### Tier 1: Transcript

`messages` is the ground-truth transcript table.

- Writers append serialized Pydantic AI messages.
- Existing rows are never updated or deleted.
- Stored text is cleaned replay material, not raw provider payload.
- `source_transcript_ledger` records idempotent host-recorder source keys and
  points to `messages`; source columns do not live on `messages`.
- `messages_fts` is a rebuildable FTS5 projection over clean user/assistant
  text.
- Session-scoped transcript search maps to `SearchTranscriptRequest`.
- Verbatim egress maps to `ExpandHistoryRequest` and requires privileged
  capability plus redaction context.

### Tier 2: Candidates

`memory_candidates` is short-term reinforcement staging.

- Light extraction inserts or reinforces candidates.
- Candidates carry fact text, subject, category, importance, confidence, source
  message ids, lifecycle flags, and reinforcement counters.
- Candidate embeddings live in `memory_candidate_embeddings`.
- `memory_dedup_events` records vector dedup decisions.
- Candidates may be promoted, retired, marked stale, or marked for review.
- Candidate fallback retrieves active unpromoted candidates only when Tier 3
  retrieval returns no durable facts.

Candidate fallback must be presented as tentative `[unverified note]` material,
never as durable memory.

### Tier 3: Long-term Facts

`long_term_memory` stores durable promoted facts.

- Every row has `source_message_ids`.
- `long_term_memory_fts` and `long_term_memory_embeddings` are rebuildable
  projections for hybrid retrieval.
- Superseded facts are retired in place with lifecycle metadata.
- Retrieval returns facts with provenance and logs observations to
  `retrieval_events`.

## Dream Pipeline

The memory pipeline has three named phases. The phase functions exist in the
package, but model-backed work requires host-supplied agents through ports.

### Light

`vexic.pipeline.run_light_phase` reads transcript rows since the last
watermark, renders stable message ids, asks a host-supplied extraction agent for
structured `FactCandidate` output, validates source ids, embeds fact text
through a host-supplied embedding port, and commits candidate inserts/merges
with a `dream_runs` audit row.

### REM

`vexic.rem.run_rem_phase` loads active unpromoted candidates and asks a
host-supplied clustering/boost agent for bounded boost values. REM writes
`rem_boost` only. It does not promote, retire, or insert durable facts.

### Deep

`vexic.deep.run_deep_phase` scores candidates, promotes selected candidates to
Tier 3, and performs contradiction/supersession checks through a host-supplied
judge agent. Promotion is idempotent and non-destructive.

## Retrieval

Vexic has two retrieval families.

### Transcript Search

Transcript search reads `messages_fts`, scoped by session, and returns clean
message hits with message-id provenance.

### Long-term Search

Long-term search uses hybrid retrieval:

1. Optional query rewrite through a host-supplied agent when available.
2. FTS5 keyword search over `long_term_memory_fts`.
3. sqlite-vec KNN over `long_term_memory_embeddings` using host-supplied query
   embeddings.
4. Reciprocal Rank Fusion.
5. Top facts returned with provenance.
6. One `retrieval_events` row per surfaced fact plus `retrieved_count`
   increment in the same transaction.

If no durable Tier 3 facts match, candidate fallback searches active unpromoted
Tier 2 candidates and logs `candidate_retrieval_events`. Candidate fallback
does not write Tier 3 retrieval events.

## Redaction

Redaction is a persistence and egress guard. Callers pass configured forbidden
values. Vexic checks relevant write and privileged egress surfaces and raises
on violations.

The guard is intentionally simple and fail-closed. It rejects exact non-empty
forbidden values; it does not sanitize payloads or discover secrets itself.

## Storage

The v0.1 local core uses SQLite:

- WAL mode is enabled by `init_db`.
- FTS5 backs transcript, candidate, and long-term keyword search.
- sqlite-vec backs candidate and long-term vector search.
- `embedding_metadata` guards embedding model, dimension, and distance metric
  compatibility.
- Vexic core stores and validates vectors but does not load embedding models.
  Missing embedding adapters fail with `HostPortNotConfigured`.

Current local isolation is one opened SQLite database per memory context, with
`LocalMemoryService` validating `MemoryScope.tenant_id` against the service's
configured tenant id. Future hosted storage must satisfy the same behavior and
scope contract, but does not need to match the physical SQLite schema.

## v0.1 Service Surface

`LocalMemoryService` implements the local read/write core for transcript ingest,
source-ledger transcript ingest, long-term search, retrieval telemetry, fact
retirement, export, replay, rebuild, and scope tombstones. Dream phase
orchestration is deliberately host-port backed: the local adapter authorizes and
checks lifecycle state, then fails closed with `HostPortNotConfigured` when no
host execution adapter is supplied. This is not an invitation to import
Coalescent runtime code.

## Data Flow

```text
agent turn
  -> append Transcript rows
  -> Light extracts Candidates from Transcript
  -> REM writes boost signals to Candidates
  -> Deep promotes selected Candidates to Long-term facts
  -> Long-term retrieval returns durable facts with provenance
  -> retrieval telemetry records what was surfaced and judged used
```

Tier 1 is the source of truth. Tier 2 and Tier 3 are higher-level memory state
with lifecycle and telemetry. Rebuildable projections may be repaired; canonical
rows are retained.

## Repair And Rebuild Posture

Vexic v0.1 includes storage primitives, service operations, and tests for
export, replay, rebuild, and lifecycle tombstones. Repair/rebuild work preserves
the lossless invariant: build new projections or repaired copies without
deleting canonical transcript, candidate, fact, or retrieval-event history.
