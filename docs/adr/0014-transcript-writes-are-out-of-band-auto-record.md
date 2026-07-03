# Transcript writes are out-of-band auto-record, not an MCP tool

Status: accepted

## Context

Vexic exposes agent-facing MCP on two surfaces: the local stdio MCP over
`LocalMemoryService`, and the hosted native Streamable HTTP `/mcp` over
`HostedMemoryService`. ADR 0010 made the hosted `/mcp` read-only
(`search_transcript`, `search_long_term`) and deferred write/admin tools. A
local stdio MCP write tool was tracked separately as a possible future
"`append_transcript` via MCP" slice (COA-175).

The MVP goal is automatic memory: a Claude Code conversation is captured into
Vexic without the agent or user taking a deliberate write action, then read
back so fresh sessions start with useful prior context. ADR 0002 already
establishes that host recorders ingest complete cleaned transcripts, and the
hosted HTTP write routes exist (`/v1/append_transcript`,
`/v1/ingest_source_transcript`).

The open question was whether agents should additionally write transcript
through an MCP tool.

## Decision

Vexic does not add an MCP write tool on any surface. The MCP surface - local
stdio and hosted `/mcp` - stays read-only: agents read memory
(`search_transcript`, `search_long_term`) and never write through MCP.

> Note: these MCP tool names were later renamed — see ADR 0021
> (`recall_conversation_history`, `recall_user_memory`). The read-only decision
> here is unchanged.

Transcript writes happen automatically, out-of-band, through a recorder that
captures completed user/assistant turns and sends cleaned rows to the hosted
HTTP ingest routes (`/v1/append_transcript`, `/v1/ingest_source_transcript`),
consistent with ADR 0002. The recorder, not the agent, is responsible for
writing.

Clean-ingress rules apply to that write path: reject tool calls and tool
returns, prompt payloads, hidden/thinking content, sidechain/meta rows, and
configured forbidden values before persistence; bind tenant/project/session/
agent scope from trusted config rather than arbitrary agent input; keep the
transcript append-only and transactional.

COA-175 (the MCP write slice) is canceled. The recorder is tracked under
COA-253, and its capture-mechanism deliberation under COA-257.

## Consequences

- MCP adapters stay thin and read-only, aligned with ADR 0010 and ADR 0001.
  There is one fewer write surface to police, and the append-only transcript
  cannot be polluted by arbitrary agent tool calls.
- Writing is passive and cannot be "forgotten" by an agent that declines to
  call a write tool. This suits the automatic-memory goal better than an
  agent-driven write.
- A deliberate agent-driven "remember this" commit, if ever wanted, is a
  separate non-MCP capability that requires a new decision. It is not
  reintroduced as an MCP write tool by default.

## Deferred

- Deliberate agent-initiated memory-commit semantics over a non-MCP path, if a
  concrete use case appears.
- Any remote or marketplace MCP write/admin tooling, already deferred by
  ADR 0010.
