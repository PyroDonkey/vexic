# Product And Agent Integration Surfaces

Status: accepted

Vexic will use the hosted memory API as the canonical service boundary and MCP
adapters as the primary agent integration boundary. MCP exists to connect agent
runtimes such as Claude Code, Codex, OpenClaw, and Hermes Agent to selected
memory operations, while auth, billing, admin, backup, deletion, and metering
remain owned by the hosted service layer around the Vexic core.

## Consequences

MCP adapters must stay thin: they delegate to the public memory contract and
inherit the same scope, capability, redaction, audit, and rate-limit rules as
the hosted API. Local stdio MCP should come first for coding agents; remote HTTP
MCP should come after hosted auth is stable. The first local MCP slice is
read-only search: transcript append, verbatim history expansion, export,
delete, rebuild, and admin tools wait until their ingress, egress, and lifecycle
guards are explicit.
