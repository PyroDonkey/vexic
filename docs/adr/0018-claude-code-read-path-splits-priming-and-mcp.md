# Claude Code read path splits SessionStart priming and MCP on-demand pull

Status: accepted

## Context

ADR 0015 made `vexic setup claude-code` install the hosted write path: a
Claude Code Stop hook records cleaned transcript rows through the hosted ingest
API. ADR 0017 then added a disabled project MCP scaffold, so users can opt in
to targeted read-only search without copying raw API keys into Claude config.

That still left the default first-run read experience thin: transcript rows
could flow into Vexic, but a new Claude Code session would not receive memory
unless the user enabled MCP and the model chose to call it.

The read path has two different retrieval moments. A session can be primed once
at startup with a small bounded memory slice, while later turns need targeted,
model-chosen searches.

## Decision

`vexic setup claude-code` installs two Claude Code hook legs:

- a Stop hook for out-of-band transcript writes; and
- a SessionStart hook for best-effort read priming.

The SessionStart hook invokes:

```powershell
python -m vexic.cli recorder prime --config <recorder-config>
```

The hook reads the existing user-local recorder config
(`~/.vexic/claude-code-recorder.json`) for base URL, API key, project, session,
and optional agent scope. It does not add a second secret store and does not
write raw credentials to Claude settings.

Priming emits Claude Code `additionalContext` only for `startup` and `clear`
SessionStart sources. It skips `resume`, `compact`, and unknown sources so an
existing session is not re-dumped with duplicate memory. The payload is hard
character-capped and may be empty if hosted reads fail or return no useful
memory.

The MCP scaffold remains the on-demand pull leg of the read path. It is still
disabled by default and is enabled by the user when they want model-chosen
queries, multi-hop recall, or `expand_history` in a trusted local setup.

UserPromptSubmit relevance injection is not part of this slice.

## Security Notes

- The SessionStart command carries only `--config <path>`, never the raw API
  key.
- The hook output is checked so the configured API key is not emitted back into
  Claude Code context.
- Hosted read calls bind tenant identity from the API key and project/session
  scope from `X-Vexic-*` headers, matching the hosted MCP read binding.
- Priming is best-effort and fail-open: a hosted outage must not block Claude
  Code startup.
- Injected memory becomes model-visible context. The cap and source gating keep
  this from becoming an unbounded prompt-injection or transcript-feedback path.

## Consequences

- `vexic setup claude-code` gives users default readback without requiring the
  MCP approval step.
- MCP is easier to explain: it is targeted on-demand recall, not the only read
  surface.
- The recorder config remains the single local credential source for both write
  and setup-owned read paths.
- The current priming implementation reuses existing hosted search endpoints
  with a broad priming query, then caps and formats returned facts/hits.

## Deferred

- A dedicated no-query fresh-context endpoint that ranks by scope, recency,
  salience, and session summary/tail without relying on search terms.

  > Amended by ADR 0024. This shipped. A `summarize` dream phase compacts Tier 1
  > spans (`src/vexic/summarize.py`), and `POST /v1/fresh_context`
  > (`src/vexic/hosted_http.py`, capability `MemoryCapability.FRESH_CONTEXT`)
  > returns a bounded recap-plus-tail with no search query. SessionStart priming
  > no longer depends on the broad-query search reuse the Consequences describe.
- UserPromptSubmit per-turn relevance injection for mid-session topic drift.
- Cross-agent priming hooks for Codex and other runtimes.
- Changing the MCP enable/disable model from ADR 0017.

  > Amended by ADR 0027. This was changed. The connect leg no longer hand-writes
  > a disabled `.mcp.json` scaffold; `vexic setup <client>` prints the client's
  > own `mcp add` command and the user running it is the opt-in step
  > (`src/vexic/recorders/mcp_connect.py`). MCP stays off by default, so this
  > ADR's split between default priming and opt-in on-demand pull is unchanged.
