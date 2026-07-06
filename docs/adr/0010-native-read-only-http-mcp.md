# Native read-only HTTP MCP is a stateless hosted adapter slice

Status: accepted

## Context

Vexic already has two agent-facing paths:

- local stdio MCP over `LocalMemoryService` for local dogfooding;
- hosted-API-backed stdio MCP, where the stdio process calls the hosted HTTP
  API.

Hosted API-key auth, project and agent binding, sanitized hosted telemetry, and
in-process hosted rate limits now exist in `HostedMemoryService`. That is
enough for a narrow native Streamable HTTP MCP slice, but not enough for a
public marketplace integration, OAuth authorization server, resumable MCP
session layer, write/admin tool surface, or production customer-data launch.

## Decision

Add `POST /mcp` to the existing internal-alpha FastAPI hosted surface as a thin
native Streamable HTTP MCP adapter. It is read-only, stateless, and JSON-only.
It delegates memory operations to `HostedMemoryService` and does not call
`LocalMemoryService` directly.

The initial tool surface is closed to:

- `search_transcript`
- `search_long_term`

`expand_history`, transcript writes, export, replay, rebuild, delete, and dream
tools are not exposed through native HTTP MCP.

> Note: the model-facing MCP tool names were later renamed -- see ADR 0021
> (`search_transcript` -> `recall_conversation_history`, `search_long_term` ->
> `recall_user_memory`). The `/v1/` HTTP endpoint names in this ADR are
> unchanged.

Every `/mcp` request requires `Authorization: Bearer <vexic-api-key>`. Query
string tokens and `X-Vexic-Api-Key` are rejected on `/mcp`; the older
`X-Vexic-Api-Key` compatibility path remains limited to the `/v1/*` hosted API
routes. Tenant, principal, and capability scope come from hosted API-key auth.
Project, session, and agent memory scope come from configured request headers
and are validated again by `HostedMemoryService` before delegation.
`X-Vexic-Session-Id` is required for every `tools/call`; the adapter fails closed
with a tool error rather than letting the service fall back to a shared `default`
session.

The adapter supports JSON-RPC `initialize`, `ping`, `tools/list`, and
`tools/call`. It returns `application/json` for request responses, `202
Accepted` with no body for accepted client notifications or JSON-RPC response
messages, and `405 Method Not Allowed` for `GET /mcp` because this slice does
not offer SSE.

## Security Notes

- No query-string tokens.
- No token passthrough.
- No outbound OAuth, discovery, redirect, or metadata calls in this slice.
- Present `Origin` headers must match `VEXIC_MCP_ALLOWED_ORIGINS`; absent
  `Origin` is allowed for CLI agents.
- Request bodies inherit the hosted HTTP body cap.
- Redaction egress uses configured forbidden values and fails closed before
  returning tool content.
- TLS is assumed at the edge for any hosted deployment; local HTTP remains
  staging-only.

## Deferred

- OAuth 2.1 protected-resource metadata, discovery, PKCE, audience validation,
  and redirect/SSRF handling.
- SSE, resumability, and redelivery.
- Stateful MCP sessions and `MCP-Session-Id`.
- Write, admin, and privileged `expand_history` MCP tools.
- Public marketplace or plugin distribution.
- Production customer-data readiness.

## Consequences

This keeps MCP adapters thin and aligned with ADR 0001 and ADR 0006: auth,
scope binding, capability checks, redaction, telemetry, and rate limiting stay
behind the hosted service boundary. It also keeps mature remote MCP features
explicitly out of scope until a later decision names the OAuth/session/hardening
slice.
