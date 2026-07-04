# Hosted fresh-conversation context ships as a Summarize dream phase plus a dedicated fresh_context capability

Status: accepted

## Context

ADR 0018 split the Claude Code read path into SessionStart priming (broad
search queries, capped and formatted) and opt-in MCP on-demand pull, and
explicitly deferred "a dedicated no-query fresh-context endpoint that ranks by
scope, recency, salience, and session summary/tail without relying on search
terms." COA-254 builds that deferred endpoint: a new Claude Code session (or
one primed after a long gap) should open with a compact recap of the prior
conversation, not just whatever a fixed keyword query happens to surface.

Pre-existing core primitives this ADR builds on, unchanged by this work:

- The Light/REM/Deep dream-phase pipeline and its host-port, fail-closed
  execution gate (`missing_host_port` / `HostPortNotConfigured`).
- `messages_fts` transcript search and `SearchTranscriptRequest`.
- `ExpandHistoryRequest`/`memory:expand` privileged verbatim egress.
- `ContentCodec` (ADR 0023) for at-rest encoding of hosted content.
- The SessionStart priming hook and its fail-open, best-effort posture
  (ADR 0018).

What this ADR adds: a `session_summaries`-producing dream phase, a read
operation that assembles those summaries plus a raw tail into one bounded
recap, a capability scoped to that read alone, and a two-layer guard so the
injected recap never becomes transcript input to itself.

## Decision

### Summarize as a dream phase, not a separate job

Session summarization is `DreamPhase.SUMMARIZE`, a fourth named phase
alongside Light/REM/Deep, run through the same `run-dream-phase` CLI and the
same host-port fail-closed gate. It needs a host-supplied
`build_summary_agent` port; adapters that only supply
`embed_texts`/`build_extraction_agent`/`build_contradiction_agent` can still
run `light`/`rem`/`deep`, but `summarize` fails closed with
`HostPortNotConfigured` until the adapter adds the summary port.

Rejected alternative: a standalone summarization job/CLI outside the dream
pipeline. That would duplicate the fail-closed host-port gate, the per-phase
usage accounting, and the CLI wiring the other three phases already have, for
no behavioral benefit -- summarization has the same "host must supply a
model-backed agent or the operation must not silently run" shape as Light and
Deep.

### Condensation as a same-job second pass

`run_summarize_phase` runs two passes per compactable session:

- **Leaf pass**: walks `find_session_compaction_span` (the existing
  compaction-span primitive) until no span remains, asking the summary agent
  for a plain-text summary per span, recorded as a `leaf` `session_summaries`
  row.
- **Condense pass**: once the session's frontier of `session_summaries` rows
  exceeds `CONDENSE_MAX_FRONTIER_LEAVES` (8) entries or `TAU_SOFT // 3`
  tokens, the oldest contiguous run of frontier summaries -- the prefix whose
  message-id ranges are adjacent, stopping at the first gap -- is condensed
  into one `condensed` row that replaces it via `replaces_summary_ids`.

Condensing only the oldest contiguous run (not the whole frontier) keeps a
condensed row's message-id range exactly matching the rows it replaces; a gap
in the run would mean a condensed summary claims coverage over messages no
summary actually summarized. `TAU_SOFT` (the existing Light-phase compaction
trigger) is reused as the condense threshold's scale, not repurposed as a new
constant -- the condense pass is triggered by frontier size, `TAU_SOFT` stays
a compaction/context-budget signal elsewhere in the pipeline.

Per-session error isolation matches Light: a failing session (a raising
agent, or a redaction violation on its output) is logged and skipped; the
phase continues with the next session rather than aborting the run.

`session_summaries` rows are a rebuildable derived projection, like FTS and
vector tables -- never source of truth. They can be regenerated from Tier 1
transcript rows.

### A dedicated `memory:fresh-context` capability, not `memory:expand`

`FreshContextRequest` (session-scoped, redaction-required, `token_budget`
defaulting to 6,000) requires `MemoryCapability.FRESH_CONTEXT =
"memory:fresh-context"`, a capability distinct from `memory:expand`.

This is deliberate, not an oversight: `memory:expand` grants arbitrary-range
verbatim transcript egress (`ExpandHistoryRequest`, any `first_message_id`/
`last_message_id`), the same privilege level as a human operator pulling raw
history. Fresh context is scoped tightly -- it returns exactly the assembled
recap-plus-tail for the caller's own session, budgeted, redaction-checked, and
capped -- and is meant to be handed routinely to an automated SessionStart
primer. Reusing `memory:expand` for that would mean every priming key
implicitly carries unrestricted verbatim read access to the whole session,
which is a materially larger blast radius than "prime this session's next
turn." A key can carry `memory:fresh-context` without `memory:expand`.

### Endpoint shape and budgets

`LocalMemoryService.fresh_context` calls `load_fresh_context_rows`, which:

- fetches the session's `session_summaries` frontier;
- computes the covered-prefix boundary from that frontier (the last message
  id any frontier summary accounts for);
- fills the remaining `token_budget` with the most recent raw transcript
  hits strictly after that boundary;
- falls back to a full-`token_budget` raw tail from the start of the session
  when the frontier is empty.

Frontier summaries are rendered through the shared `render_recap_blocks`
helper as `[Recap of messages N-M -- verbatim via expand_history]` blocks --
the same renderer `render_session_recap` (an existing local helper) uses, so
the two call sites cannot drift on format. The egress redaction guard runs
over the assembled text before it returns. `FreshContextResult` carries
`summaries`, `recent`, `text`, and `truncated`.

`vexic.hosted_http` exposes `POST /v1/fresh_context`: header-bound scope (like
the other hosted search routes) or a full request body, an optional body
`redaction`, `token_budget` validated to `1..24_000` (`_error_response(400,
...)` outside that range), a `403` when the calling key lacks
`memory:fresh-context`, a `30/min` rate rule matching `expand_history`, and
`_cap_result` truncation on an oversized result. 6,000 is kept as the
contract default (a reasonable single-session recap size); 24,000 is the
hosted cap (roughly the largest budget worth serving from one request before
a caller should paginate or use `expand_history` directly instead).

### Two-layer prime re-ingestion guard

`PRIME_CONTEXT_HEADER = "Vexic memory priming:"` marks host-injected priming
context (which now leads with a "Prior conversation recap:" section built
from `fresh_context`, ahead of the existing long-term/transcript search
sections). Two independent layers keep that injected text from re-entering
Tier 1 and being re-summarized or re-extracted:

1. **Recorder-side**: the Claude Code JSONL parser (`recorders/claude_code.py`)
   skips any row whose text contains `PRIME_CONTEXT_HEADER` before it is ever
   sent to `ingest_source_transcript`.
2. **Ingest-side backstop**: `ingest_source_messages` independently rejects
   any row containing the header (`reason="prime context is not transcript
   text"`), so a recorder bug, a different host, or a manually replayed JSONL
   cannot smuggle a recap back into Tier 1.

Belt-and-suspenders is intentional: layer 1 is the cheap common-path filter,
layer 2 is the correctness guarantee that does not depend on every recorder
implementation getting the filter right. Without both, a recap that
re-entered Tier 1 would eventually be summarized into a `session_summaries`
row containing a summary of a summary, then surfaced again next session --
silent unbounded drift with no data-loss signal to notice it by.

### SessionStart priming integration

`vexic.recorders.hosted_prime.fetch_prime_context` now calls
`fetch_fresh_context` (token budget = `max_chars // 4`) before its existing
long-term/transcript search calls, and prepends the returned recap text as a
"Prior conversation recap:" section when present. The call is fail-open: an
HTTP error, timeout, or a key without `memory:fresh-context` (`403`) all
result in `fetch_fresh_context` returning `None`, and priming continues with
whatever the long-term/transcript search legs return -- unchanged from the
ADR 0018 posture that a hosted outage must not block Claude Code startup.

## Out Of Scope / Deferred

- Tagging fresh-context-sourced transcript rows with `source == "compact"`
  and tuning the leaf/condense thresholds against real usage is COA-268. The
  seam for that work is `fetch_fresh_context`
  (`src/vexic/recorders/hosted_prime.py`); this ADR's endpoint and phase
  shapes are not expected to change for it.
- UserPromptSubmit per-turn relevance injection (still deferred by ADR 0018).
- Cross-agent priming hooks for Codex and other runtimes (still deferred by
  ADR 0018).
- Ranking fresh context by salience beyond recency/summary-frontier order.

## Consequences

- Extends the Dream Pipeline from three phases to four; `docs/architecture.md`
  and `docs/memory-service-contract.md` document `DreamPhase.SUMMARIZE`
  alongside Light/REM/Deep.
- Adds a third retrieval family (transcript search, long-term search --
  which includes candidate fallback -- and now fresh context) with its own
  capability, rather than overloading `memory:search` or `memory:expand`.
- `session_summaries` becomes load-bearing for the hosted read path, not just
  a standalone helper table; it stays a rebuildable projection, so a
  corrupted or missing frontier degrades to the raw-tail fallback rather than
  failing the request.
- Operators issuing priming keys must add `--capability memory:fresh-context`
  explicitly; existing keys without it keep working, just without the recap
  leg, per the fail-open design above.
