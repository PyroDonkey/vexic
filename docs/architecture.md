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
- `vexic.rem` - REM boost phase (local embedding-centrality heuristic)
- `vexic.deep` - Deep promotion/supersession phase
- `vexic.subagents.retrieval` - hybrid Tier 3 retrieval and candidate fallback
- `vexic.mcp_stdio` - read-only local stdio MCP MVP
- `vexic.mcp_http` - read-only native HTTP MCP adapter over hosted auth
- `vexic.ports` - host-supplied model-agent ports
- `vexic.redaction` - persistence and egress secret guard

Session summaries and the fresh-context read path are core memory behavior.
The `Summarize` dream phase (`vexic.summarize`) compacts Tier 1 spans into
`session_summaries` rows, and `LocalMemoryService.fresh_context` reads that
summary frontier plus a token-budgeted raw tail into one bounded recap
(`FreshContextRequest`/`FreshContextResult`, capability `memory:fresh-context`,
see "Dream Pipeline" and "Retrieval" below). Hosted Claude Code SessionStart
priming (an adapter-side concern, documented in `docs/hosted-mvp.md`) is what
injects that recap into new sessions; the core only builds the bounded text.

The package must not import legacy `engine.*` modules. The private source host
is a consumer, not a dependency.

## Goals

- Lossless transcript: Tier 1 rows are never updated or deleted, with one
  deliberate exception -- `purge_scope` physically erases a tombstoned scope
  (ADR 0022).
- Glass-box facts: every durable fact carries provenance, confidence, category,
  and editability metadata.
- Replayable projections: FTS, vectors, and summaries are rebuildable from
  canonical rows and code.
- Explicit scope: requests carry `MemoryScope`, and the local adapter validates
  the opened SQLite context against it.
- Agent isolation: `agent_id` is an optional `MemoryScope` refinement. `NULL`
  means shared memory inside the same parent scope, never a wildcard.
- Host-neutral core: providers, secrets, auth, and managed operations live in
  adapters or hosts. Embeddings may come from a host port or the optional lazy
  local adapter described in ADR 0016.

## Non-goals

- Private host runtime wiring.
- Host-specific application features and integrations layered on the private
  source host (messaging, content generation, scheduling, or model routing).
- Hosted auth, billing, dashboards, public HTTP, or mature remote MCP in the
  v0.1 core. The native HTTP MCP slice is a thin hosted adapter, not core
  memory behavior.
- External vector databases for the local core.
- Destructive chat-window compression.

## Memory Tiers

### Tier 1: Transcript

`messages` is the ground-truth transcript table.

- Writers append serialized Pydantic AI messages.
- Existing rows are never updated or deleted by ordinary operation; the only
  destructive path is `purge_scope` over a tombstoned scope (ADR 0022).
- Stored text is cleaned replay material, not raw provider payload. Cleaned
  is not redacted: cleaning strips provider metadata, tool payloads, and
  system prompts, while visible user/assistant text is stored verbatim --
  including any secret or personal detail the user typed into the chat.
- Agent scope is stored as nullable `agent_id`; existing `NULL` rows are shared
  agent-scope transcript rows and are not backfilled.
- `source_transcript_ledger` records idempotent host-recorder source keys and
  points to `messages`; source columns do not live on `messages`.
- `messages_fts` is a rebuildable FTS5 projection over clean user/assistant
  text and transcript scope metadata.
- `message_json` may be codec-encoded at rest when a host supplies a
  `ContentCodec` (ADR 0023); the FTS projection stays plaintext, derived
  before encoding, as the documented searchable residue.
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

The memory pipeline has four named phases. The phase functions exist in the
package, but model-backed agent work (Light extraction, the optional Deep
contradiction judge, and Summarize) requires host-supplied agents through
ports. Embedding can use a host port or the optional local adapter. REM is
local and deterministic and uses no model port (ADR 0020).

### Light

`vexic.pipeline.run_light_phase` reads transcript rows since the last
watermark, renders stable message ids, asks a host-supplied extraction agent for
structured `FactCandidate` output, validates source ids, embeds fact text
through the supplied embedding port or optional local adapter, and commits
candidate inserts/merges with a `dream_runs` audit row.

Dream watermarks are scoped by compatible memory scope including `agent_id`.
Existing `NULL` dream-run rows are shared agent-scope progress rows.

### REM

`vexic.rem.run_rem_phase` loads active unpromoted candidates and computes a
local deterministic embedding-centrality boost per candidate: the mean cosine
similarity to its top-3 most similar embedded same-scope peers, clamped to
[0, 1], read from the embeddings the Light phase already stored. Candidates
without an embedding score 0.0, which also resets any stale boost from an
earlier cycle. REM makes no model calls and writes `rem_boost` only. It does
not promote, retire, or insert durable facts. See ADR 0020.

### Deep

`vexic.deep.run_deep_phase` scores candidates and promotes selected candidates
to Tier 3. When a contradiction judge is supplied, Deep performs the existing
supersession checks and retires losing facts or candidates. When contradiction
is deferred, selected candidates promote without judging; Tier 3 may
temporarily contain contradictory active facts until a later audit runs.
Promotion is idempotent and non-destructive.

### Summarize

`vexic.summarize.run_summarize_phase` compacts Tier 1 transcript spans into
`session_summaries` rows (ADR 0024). It runs two passes per compactable
session, mirroring Light's usage-accumulation and fail-closed host-agent
conventions:

- Leaf pass: walks `find_session_compaction_span` until no span remains,
  rendering each span and asking a host-supplied summary agent for a
  plain-text summary, recorded as a `leaf` row.
- Condense pass: once the session's summary frontier exceeds
  `CONDENSE_MAX_FRONTIER_LEAVES` (8) entries or `TAU_SOFT // 3` tokens, the
  oldest contiguous run of frontier summaries (the prefix whose message-id
  ranges are adjacent, stopping at the first gap) is condensed into one
  `condensed` row that replaces it.

Summarize requires a host-supplied `build_summary_agent` port; without it the
phase fails closed with `HostPortNotConfigured`, matching Light and Deep. A
per-session failure (including a redaction violation) is isolated and logged;
the phase continues with the next session. `session_summaries` rows are a
rebuildable derived projection, not source of truth -- they can be
regenerated from Tier 1.

## Retrieval

Vexic has two retrieval families.

### Transcript Search

Transcript search reads `messages_fts`, scoped by session and agent scope, and
returns clean message hits with message-id provenance. Agent-specific reads do
not implicitly union shared rows; shared memory is fetched explicitly.

### Long-term Search

Long-term search uses hybrid retrieval:

1. Optional query rewrite through a host-supplied agent when available.
2. FTS5 keyword search over `long_term_memory_fts`.
3. sqlite-vec KNN over `long_term_memory_embeddings` using host-supplied query
   embeddings or the optional local embedding adapter.
4. Reciprocal Rank Fusion.
5. Top facts returned with provenance.
6. One `retrieval_events` row per surfaced fact plus `retrieved_count`
   increment in the same transaction.

If no durable Tier 3 facts match, candidate fallback searches active unpromoted
Tier 2 candidates and logs `candidate_retrieval_events`. Candidate fallback
does not write Tier 3 retrieval events.

### Fresh Context

Fresh context is a third, no-query retrieval family for priming a new
conversation rather than answering a targeted query (ADR 0024, deferred by
ADR 0018). `FreshContextRequest` is session-scoped, redaction-required, and
carries a `token_budget` (default 6,000). `LocalMemoryService.fresh_context`
requires capability `memory:fresh-context` -- a dedicated capability, not
`memory:expand`, because it returns a bounded recap plus tail rather than
arbitrary-range verbatim transcript. It reads the session's summary frontier
up to a covered-prefix boundary, then fills the remaining token budget with
the most recent raw transcript tail; an empty frontier falls back to a
full-budget raw tail. The shared `render_recap_blocks` renderer formats each
frontier summary as `[Recap of messages N-M -- verbatim via expand_history]`,
and the egress redaction guard applies to the assembled text before it
returns. `FreshContextResult` carries `summaries`, `recent`, the assembled
`text`, and a `truncated` flag.

## Redaction

Redaction is a persistence and egress guard. Callers pass configured forbidden
values. Vexic checks relevant write and privileged egress surfaces and raises
on violations.

The guard is intentionally simple and fail-closed. It rejects exact non-empty
forbidden values; it does not sanitize payloads or discover secrets itself.

Recorders and importers pass no forbidden values by default, so clean user and
assistant text is ingested verbatim (ADR 0002). Pattern-based masking is host
policy: a host that needs scrubbing applies it before ingest, or configures
forbidden values to reject offending writes outright. Vexic core deliberately
does not rewrite stored ground-truth text.

## Storage

The v0.1 local core uses SQLite:

- WAL mode is enabled by `init_db`.
- FTS5 backs transcript, candidate, and long-term keyword search.
- sqlite-vec backs candidate and long-term vector search.
- `embedding_metadata` guards embedding model, dimension, and distance metric
  compatibility.
- Vexic core stores and validates vectors. Embedding models are host-supplied
  by default, with one optional lazy local adapter available through
  `vexic[local-embed]`; missing local adapter dependencies fail with an
  actionable install error.

Current local isolation is one opened SQLite database per memory context, with
`LocalMemoryService` validating `MemoryScope.tenant_id` against the service's
configured tenant id. Future hosted storage must satisfy the same behavior and
scope contract, but does not need to match the physical SQLite schema.

SQLite schema migrations add nullable `agent_id` columns to scope-bearing
canonical rows, projections, and telemetry. Rebuildable projections may be
recreated from canonical rows; append-only transcript rows are not updated to
assign agent scope.

Hosted v1 extends that posture as one isolated SQLite-compatible Customer
Memory Database per customer tenant. The hosted adapter owns routing,
provisioning, backup, restore, and migration orchestration outside the core
package. Project, user, and session scopes remain `MemoryScope` filters inside
that database. Storage design should stay Postgres-ready by keeping canonical
rows portable through export/replay, proving hosted adapter conformance against
local SQLite behavior, and treating FTS/vector tables as rebuildable projections
rather than source of truth.

## v0.1 Service Surface

`LocalMemoryService` implements the local read/write core for transcript ingest,
source-ledger transcript ingest, long-term search, retrieval telemetry, fact
retirement, export, replay, rebuild, and scope tombstones. Dream phase
orchestration is deliberately port-backed: the local adapter authorizes and
checks lifecycle state, executes Light, REM, or Deep only when explicit dream
phase ports are supplied, and fails closed with `HostPortNotConfigured` when no
host execution adapter is supplied. Within those ports, embedding may fall back
to the optional local adapter and Deep contradiction may be deferred; REM runs
entirely locally and consumes no model port, but still sits inside the same
fail-closed gate (ADR 0020). This is not an invitation to import private host
runtime code.

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
