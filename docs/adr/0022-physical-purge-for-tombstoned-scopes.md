# Physical purge erases tombstoned scopes from the primary database

Status: accepted

## Context

Vexic retained memory forever by design: `delete_scope` inserted a
`scope_tombstones` row that blocked retrieval, export, replay, and rebuild,
but the canonical rows stayed intact (`physical_purge_deferred = 1`), and no
code path deleted content. The contract said "Physical purge is backend and
SLA specific, and remains deferred", and the architecture listed purge
semantics as a non-goal.

The 2026-07 privacy audit made this the top R1 gap: a user's "delete my data"
did not delete data, blocking right-to-be-forgotten expectations, and leaving
Vexic behind comparable memory systems that ship real erasure. A multi-model
design review (Fuse deep pass) converged on the design below.

## Decision

Purge is the second deliberate step of erasure, after `delete_scope`:

- `purge_scope` is a `MemoryService` operation guarded by
  `MemoryCapability.ADMIN_LIFECYCLE`. It requires an existing tombstone whose
  target fields exactly equal the request's `target_scope`, and fails
  otherwise. `LifecycleAction.PURGE` names the action.
- The purge runs as one explicit-`BEGIN` transaction (managed libSQL
  auto-commits per statement otherwise) that physically deletes the scope's
  content: `messages` (and its trigger-less `messages_fts` shadow rows,
  deleted by hand), `memory_candidates` and `long_term_memory` (their
  external-content FTS shadows follow through AFTER DELETE triggers), both
  sqlite-vec embedding stores (no triggers, deleted by hand),
  `memory_dedup_events`, `promotion_labels`, `session_summaries`,
  `retrieval_events`, `candidate_retrieval_events`, and
  `source_transcript_ledger` rows pointing at purged messages.
- Scope matching follows ADR 0007: `agent_id` is exact (`IS` semantics; a
  NULL target purges shared-scope rows only), and a NULL target session
  selects all sessions in the tenant database. Project and user selectors
  have no columns inside a Customer Memory Database; routing to the right
  database is the hosted adapter's job.
- Derived content follows the source-intersection rule: any candidate, fact,
  or dedup event whose `source_message_ids` (or
  `incoming_source_message_ids`) touches a purged message is deleted, and a
  fact promoted from a purged candidate is deleted even when its own listed
  sources survive. Facts spanning purged and surviving sessions are deleted
  whole: fact text distills content from the erased conversation, so partial
  retention would leak it.
- `dream_runs` rows survive (they carry pipeline watermarks) with
  `error_detail` wiped for the scope; it is the only content-bearing column.
- The matching tombstones survive as the audit record: the same transaction
  flips `physical_purge_deferred` to 0 and records `purged_at` plus per-table
  `purged_counts` JSON.
- `dry_run` executes the identical transaction and rolls it back, returning
  exact projected counts. A repeated purge is idempotent: it deletes nothing
  further and refreshes the audit fields.
- Purge is operator-run for now (ADR 0011 posture). A scheduled runner, a
  distinct purge capability separated from `ADMIN_LIFECYCLE` (two-person
  rule), and an append-only audit log outside the tenant database are
  deliberate deferrals until hosted operations need them.

Retention for content-bearing telemetry rides the same decision:
`expire_retrieval_queries` blanks `retrieval_events.query` (empty string; the
column is NOT NULL), nulls `rewritten_query`, and blanks
`candidate_retrieval_events.query` for rows older than a host-chosen window,
keeping the rows because `retrieved_count`/`used_count` derive from them. The
hosted adapter should run it at a 90-day default; the local core retains by
default because the database sits in the user's own custody.

## Erasure Horizon

Purge is immediate and irreversible in the authoritative tenant database.
It does not reach into provider backups: Turso PITR history and S3 export
objects persist until their own retention expires (ADR 0008 already forbids
promising otherwise). The documented erasure horizon is therefore purge time
in the primary plus the maximum of the PITR window and export retention for
residual copies. Customer-facing wording must not claim instantaneous global
erasure.

## Consequences

- The Tier 1 "rows are never updated or deleted" invariant now carries one
  exception: rows of a tombstoned scope may be physically purged by this
  operation. Everything else about the lossless posture stands, including
  append-only writers and rebuildable projections.
- Counters on surviving multi-session facts can overstate historical
  retrievals after their session-scoped events are purged; counters are
  advisory and downstream consumers must tolerate that.
- Purged message ids are never reused (AUTOINCREMENT), so dream watermarks
  stay monotonic; a watermark may reference a purged id, which is harmless.
- Purge must not run concurrently with dream phases for the same scope;
  operators stop recorders and pipelines for the scope first. This ADR
  originally left writes to a tombstoned scope unblocked, so a late write
  could slip in silently and be erased or orphaned by the deferred purge.
  Amendment (2026-07, COA-334): that gap is closed, in three parts.
  - Direct writes -- `append_transcript`, `ingest_source_transcript`,
    `record_retrieval_event`, and `retire_fact` -- fail closed with a
    `PermissionError` when a tombstone matches the write's session (a NULL
    target session matches every session) plus exact agent, regardless of
    which lifecycle flags the tombstone carries. That is the key the purge
    erases messages and session-scoped telemetry by; project and user fields
    on a tombstone do not narrow the physical erase (the tables carry no
    such columns), so they do not exempt a write either. Because the
    tombstone survives the purge as the audit record, this block persists
    after the purge completes: a purged scope cannot be silently
    re-populated.
  - Dream phases are blocked agent-wide while any tombstone for the exact
    agent has its physical purge pending (`physical_purge_deferred = 1`).
    A per-session gate cannot protect them: the sweeps read sources by agent
    across all sessions, so a run under any session scope could consolidate
    a doomed session's still-present rows into candidates, facts, or
    summaries the purge then erases by source intersection. Once the purge
    completes the doomed sources are gone, so only the per-session write
    gate remains -- the agent-wide block must not outlive the purge, or one
    session's erasure would disable dreaming for the agent forever.
  - The `search_long_term` candidate fallback is skipped entirely (no
    tentative notes, no retrieval telemetry recorded) when the write gate
    matches the scope; the search itself remains a read governed by the
    tombstone's retrieval flag.
  Accepted residuals: `retire_fact` under a session-granular tombstone may
  over-block a fact whose sources span other sessions (it errs closed);
  `rebuild`'s projection repair stays exempt from the write gate because
  projections are non-canonical and rebuildable (memory invariant 3), and it
  remains gated by the tombstone's rebuild flag; and the gate and the
  subsequent insert run on separate connections, so a `delete_scope`
  committing between them can still land rows that a later purge erases --
  covered by the same "operators stop recorders and pipelines first" posture
  above.
- Supersedes the "purge deferred" wording in `docs/memory-service-contract.md`
  and the "physical purge semantics" non-goal in `docs/architecture.md`; both
  are updated with this ADR. ADR 0008's backup posture is unchanged.
