# MCP memory surfaces are proactive and prose-first

Status: accepted

## Context

Live Claude Code sessions surfaced two UX failures in the MCP read path that
ADR 0018 left open (it wired the on-demand pull leg but did not govern how the
model is told to use it):

- The client model did not call the search tools unprompted. Asked "what's my
  favourite pizza?", it answered that it did not know instead of searching,
  and only searched when the user explicitly named the tools. The tool
  descriptions and server instructions were read-only *disclaimers* ("does not
  expose verbatim history expansion or write memory") with no guidance on when
  to reach for the tools.
- When the model did search, it parroted internal metadata back to the user
  ("Msg 256/278 (2026-07-03) ..."). Tool results were raw pydantic
  `model_dump` JSON exposing `message_id`, `session_id`, `timestamp`,
  `confidence`, and `source_message_ids`, and nothing told the model to
  present results naturally.

The tool names also under-described their scope. `search_transcript` reads as
a single-conversation lookup, but the recorder binds one durable Vexic session
per install, so transcript search spans the current *and earlier* Claude Code
conversations.

## Decision

The MCP tool surface is a model-facing prompt surface and is written as one,
in a single shared module (`vexic.mcp_presentation`) used by both the stdio
and hosted HTTP servers so the two cannot drift:

- Tools are renamed for model comprehension: `search_transcript` becomes
  `recall_conversation_history` (current and earlier conversations) and
  `search_long_term` becomes `recall_user_memory` (durable facts and
  preferences). `expand_history` keeps its name; it is tied to the
  `EgressKind` contract value and audit operations.
- Tool descriptions lead with when-to-use triggers (user references earlier
  conversation, asks about preferences or past decisions, or the model is
  about to say it does not know something about the user), then state the
  read-only constraint. All tools carry MCP annotations
  (`readOnlyHint`, `idempotentHint`, `openWorldHint: false`).
- Server instructions direct proactive search and natural presentation:
  answer in the model's own words as if it simply remembers, never narrate
  the retrieval (searching, transcripts, memory systems, prior turns, or
  save status), never surface tool names, message ids, fact ids, raw
  timestamps, or confidence scores; phrase timing as natural prose when it
  matters; give provenance only when the user asks; treat
  tentative/unverified notes as uncertain. Because models attend more to
  tool results than to server instructions, every rendered search result
  ends with a one-line presentation reminder restating this.
- Search results are rendered as prose, not JSON. Transcript hits keep their
  timestamps (for recency judgment) but omit message and session ids; long-term
  facts render as fact text plus category, dropping ids, confidence,
  importance, source message ids, and counters. Candidate-note fallback reuses
  the `UNVERIFIED_NOTES_PREAMBLE` tentative framing. Message ids appear only
  when `--enable-expand-history` is on, because they are the handles
  `expand_history` needs, with an inline note that they are internal.
- SessionStart priming (ADR 0018) appends one line advertising that more
  memory is searchable via the Vexic MCP tools when they are enabled. The line
  is appended last so the existing character cap truncates it first.

The REST `/v1` endpoints remain the machine-readable contract; nothing there
changes. `expand_history` keeps its JSON result shape because its consumer is
tooling-oriented and privileged.

## Consequences

- MCP tool text is no longer parseable JSON. Programmatic consumers must use
  the REST `/v1` endpoints; only tests parsed the old MCP JSON.
- Clients with tool-name allowlists (for example Claude Code permission
  settings naming `mcp__vexic__search_transcript`) must re-approve the renamed
  tools once.
- The read-only security posture (ADR 0010, ADR 0014) is unchanged: same
  tools, same scopes, same fail-closed secret-egress guard, now applied to the
  rendered prose string.
- Tool descriptions, instructions, and result rendering have one home;
  wording changes are single-file edits with unit tests in
  `tests/test_mcp_presentation.py`.
