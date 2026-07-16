# Claude Code harness envelopes are filtered, not ingested

Status: accepted

## Context

Raw Claude Code slash-command envelopes (`<command-name>`,
`<command-message>`, `<command-args>`, `<local-command-stdout>`) and
`<system-reminder>` blocks were reaching Tier 1 `messages` through the
recorder -> hosted ingest path (COA-378, found while diagnosing COA-358).
The same path later admitted `<task-notification>` blocks -- harness-injected
background-task completion payloads carrying verbatim subagent reports inside
user turns (COA-392). These payloads arrive as plain-string content inside
ordinary user turns in the Claude Code JSONL transcript, so the existing
structural gates -- the recorder's text-part extraction and the ingest-side
`_polluted_transcript_reason` check -- passed them through untouched: neither
inspected the string body.

That violates Memory Invariant #2: stored transcript is the cleaned,
replayable conversation log, and prompt payloads and dynamic instructions do
not belong in searchable transcript text. The stored envelopes were indexed
by FTS and vector search, fed to Light extraction as input, and eligible to
become `source_message_ids` provenance for Tier 3 facts. ADR 0002 already
mandates rejecting polluted payloads at the append boundary; ADR 0014 sets
the clean-ingress rules for the auto-record path; the
`PRIME_CONTEXT_HEADER` guard (ADR 0018/0024, WI-6) established the dual-guard
precedent for exactly this class of harness-injected text.

## Decision

Harness envelopes are filtered with a dual guard, mirroring the
`PRIME_CONTEXT_HEADER` treatment:

1. **Recorder drops or strips.** `source_message_from_claude_code_row`
   applies two rules to the extracted text:
   - Rows containing any command-envelope marker (`<command-name>`,
     `<command-message>`, `<command-args>`, `<local-command-stdout>`) are
     dropped whole. These rows are never conversation.
   - Paired `<system-reminder>...</system-reminder>` blocks are stripped and
     the surrounding genuine user text is kept, because reminder blocks can
     share a turn with real user speech. A row that is empty after stripping,
     or that carries an unpaired reminder tag, is dropped (fail closed).
   - Paired `<task-notification>...</task-notification>` blocks get the same
     strip-and-keep treatment (COA-392): a notification can share a turn with
     real user speech, so the block is stripped, the rest kept, and a row
     that is empty after stripping or carries an unpaired tag is dropped
     (fail closed). Two hardenings beyond the reminder treatment, both from
     adversarial review: stripping applies only when open and close tags are
     balanced -- nested or malformed blocks would let a non-greedy strip
     surface inner payload as apparent user text, so unbalanced text is left
     untouched and the surviving tags are rejected downstream; and detection
     matches the tag prefixes (`<task-notification`, `</task-notification`)
     rather than the exact closed form, so attribute or whitespace tag
     variants also fail closed.
2. **Ingest rejects, never mutates.** `ingest_source_messages` runs the same
   marker check beside the existing prime-context backstop and rejects the
   row per-row (`status="rejected"` with a reason). The boundary never
   strips: mutating text at ingest would make stored content diverge from
   the recorder-sent payload and break the ledger's replay duplicate
   detection. Any reminder tag surviving to the boundary -- even a
   well-formed pair -- means a recorder guard was bypassed, and the row is
   rejected.

The marker constants and the pure helpers
(`strip_system_reminder_blocks`, `strip_task_notification_blocks`,
`harness_envelope_reason`) live in `vexic.contract` beside
`PRIME_CONTEXT_HEADER`, because the dependency lattice is
`contract <- storage <- recorders` and both layers consume them.

## Consequences

- The fix is forward-only. Envelope rows persisted before it stay in the
  append-only `messages` table (Invariant #1) and remain as retrieval noise;
  they are accepted as-is, with no Tier 1 mutation and no audited cleaning
  path. Rebuilding derived projections does not remove them, because the
  canonical rows remain.
- Substring detection can false-positive on a user genuinely quoting a
  marker literal in conversation. Accepted: same trade-off as the
  `PRIME_CONTEXT_HEADER` guard.
- Affirms ADR 0002 and ADR 0014; extends the ADR 0018/0024 dual-guard
  precedent to harness command envelopes.
