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
MCP should come after hosted auth is stable. The default local MCP slice is
read-only search: transcript append, export, delete, rebuild, and admin tools
wait until their ingress, egress, and lifecycle guards are explicit.

COA-174 decision: remote MCP remains deferred, while local stdio MCP may expose
verbatim history expansion before remote MCP only as a disabled-by-default
privileged egress slice. Operators must opt in at process launch, the adapter
must require `MemoryCapability.EXPAND_HISTORY`, and callers may only expand
bounded ranges in the configured session scope. Forbidden values must fail
closed before egress, and the adapter must cap both requested range and returned
text. Vexic v0.1 has no dedicated local MCP audit hook for this path; until that
host port exists, the missing audit dependency is documented here and the tool
must not become a default MCP capability.
