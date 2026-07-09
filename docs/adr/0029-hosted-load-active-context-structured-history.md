# Hosted load_active_context returns structured session history under the fresh-context capability

Status: accepted

## Context

Coalescent/AgentOS is cutting over to hosted Vexic for both chat history and
memory (COA-341): the host stops keeping a local transcript and must
reconstruct each turn's model message history from the hosted service. No
existing surface returns replayable structure — `search_transcript` returns
text hits, `expand_history` returns rendered text, and `fresh_context`
(ADR 0024) returns rendered priming text. The storage layer already holds
everything needed: `messages.message_json` stores each cleaned message as one
serialized JSON document, and `load_active_context_messages` (token-budgeted
tail, idle-gap/local-3am fresh-window boundary over the summary frontier) and
`render_session_recap` were extracted from the reference host along with the
rest of the session-summaries slice.

COA-338 tracks this endpoint. The consuming host client is COA-341.

## Decision

### One structured read operation, mirrored across the contract layers

`load_active_context` joins the `MemoryService` contract
(`LoadActiveContextRequest` / `LoadActiveContextResult`), `LocalMemoryService`,
`HostedMemoryService`, and the hosted HTTP app as `POST /v1/load_active_context`.
The service method composes the existing storage primitives; it introduces no
new storage behavior.

- Request: session-scoped, redaction-required, `token_budget` (default
  `24_000`, HTTP layer enforces the shared fresh-context range of 1–24,000)
  and `timezone_name` (default `"UTC"`) for the fresh-window boundary
  heuristic.
- Result: `messages_json` — individually serialized transcript messages in
  transcript-storage form (validate each with the transcript message adapter
  to replay them as model messages), ordered oldest-first; `recap_text` — the
  rendered summary-frontier recap or `None`; `truncated` — `True` when
  earlier session messages exist that the returned window omits.

### Capability: reuse `memory:fresh-context`, not `memory:expand` or a new one

Load active context serves the same purpose and trust tier as fresh context —
turn-start priming over the caller's own current session — differing only in
returning replayable structure instead of rendered text. Both read the same
underlying rows. `memory:expand` would be wrong (that capability gates
arbitrary-range verbatim reads across the session's past, not the bounded
current window), and a new capability would force a key rotation for every
existing agent key with no isolation gain: any `fresh-context` caller can
already read the same content rendered as text.

### Bounds and redaction

- Header-bound scoping identical to `fresh_context`: `X-Vexic-Project-Id`
  required, `X-Vexic-Session-Id` required, `X-Vexic-Agent-Id` optional exact
  match; full-body `scope` remains available.
- Rate limit: its own `load_active_context` bucket at the fresh-context tier
  (30/60s).
- Response size is bounded by the token budget rather than the
  `MAX_EXPAND_HISTORY_CHARS` text cap (which would gut a 24k-token history
  read); the tail walk keeps at least the most recent message even when it
  alone exceeds the budget, so the worst case is one maximal ingested message.
- Every `messages_json` entry and `recap_text` pass the fail-closed egress
  redaction guard before return.

## Consequences

- Hosts can be fully transcript-stateless: write turns through
  `ingest_source_transcript`, read them back through `load_active_context`.
- `truncated` tells such a host that older context exists behind the window;
  drill-down stays on the existing `expand_history` privileged path using the
  recap headers' message ranges.
- The summarize phase (ADR 0024/0025) now shapes what stateless hosts see as
  their active window boundary, not just rendered priming text.
- `count_session_messages` joins the transcript storage helpers to back the
  `truncated` flag.
