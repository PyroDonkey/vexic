# Vexic

Vexic is the standalone memory system extracted from Coalescent: a
provenance-first, replayable memory core for long-running agents.

This first package slice is a local Python core with a SQLite adapter, public
contract models, and conformance tests. Hosted auth, billing, dashboards, HTTP,
remote MCP, and managed operations are intentionally out of scope for v0.1. The
read-only local stdio MCP MVP below is the narrow in-scope adapter slice.

## Running the Project

Install and test with `uv`:

```powershell
uv run pytest
```

## Agent Workflow

Agents should follow `AGENTS.md`: sync `main` and `dev` before edits, land work
on `dev`, push completed updates to `origin/dev`, and keep Linear issues current
for non-trivial plans and changes.

## Local MCP MVP

Run the read-only stdio MCP server against a local Vexic database:

```powershell
uv run python scripts\vexic-mcp-stdio.py --db-path .\memory.db --tenant-id local --session-id default
```

The MVP exposes `search_transcript` and `search_long_term` only. Transcript
writes, verbatim history expansion, export, delete, rebuild, and admin tools are
intentionally not registered. Long-term vector search requires a host-supplied
embedding adapter; without one, `search_long_term` returns a configuration
error instead of loading a model from Vexic core.

Codex-style MCP config:

```toml
[mcp_servers.vexic]
command = "uv"
args = [
  "run",
  "python",
  "scripts\\vexic-mcp-stdio.py",
  "--db-path",
  ".\\memory.db",
  "--tenant-id",
  "local",
  "--session-id",
  "default",
]
cwd = "C:\\Users\\Ryan\\Documents\\GitHub\\Vexic"
```

<!-- memory-reliability-gate -->

The memory reliability gate is:

```powershell
uv run pytest tests/test_memory_reliability.py
```

<!-- memory-reliability-live-smoke -->

For existing tenant database smoke tests, open a current `memory.db` with the
Vexic SQLite adapter and verify memory tables continue working without touching
host-owned extension tables such as `background_tool_audit`.
