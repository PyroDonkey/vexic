# Host recorders ingest complete cleaned transcripts

Status: accepted

Host transcript recorders should append every cleaned user/assistant turn from
each completed agent run or session, rather than filtering for material that
already looks memory-worthy. Vexic preserves Tier 1 as the replayable source of
truth and derives Tier 2 candidates and Tier 3 facts later; host-side filtering
would make missed facts unrecoverable and weaken provenance.

Complete does not mean raw host event logs. Recorders ingest visible,
replayable user/assistant transcript material and exclude system/developer
prompts, dynamic instructions, thinking, tool calls, tool returns, internal
structured fields, failure payloads, compaction summaries, and other
non-transcript provenance events.

Hosts adapt and normalize their own runtime logs, but Vexic remains the
gatekeeper for Tier 1 transcript legality. A recorder bug should be rejected at
the append boundary rather than silently turning polluted host payloads into
append-only source material.

New transcript ingestion should reject polluted payloads instead of silently
stripping known host junk. Silent sanitization hides recorder defects, and Tier
1 append-only rows are too costly to repair after polluted material is stored.
Existing read-side cleanup may remain as compatibility defense for legacy rows
until Vexic has a deliberate scan and repair path; it should not define the
contract for new recorder/importer writes.

Importer idempotency should use a durable per-message source ledger, keyed by a
normalized source host plus the source session id and source message id. For
Claude Code JSONL, that key is `claude-code`, `sessionId`, and `uuid`.
Re-importing the same source skips ledgered rows and inserts only missing clean
rows. Do not use content hashes, source columns on `messages`, or session-level
markers as the primary duplicate guard. Message, FTS, and ledger writes must
commit atomically; if a source row with an existing key later has different
content, the first ingested Tier 1 row wins.
