# Vexic

Vexic is the standalone memory system extracted from Coalescent: a
provenance-first, replayable memory core for long-running agents.

This first package slice is a local Python core with a SQLite adapter, public
contract models, and conformance tests. Managed billing, dashboards, public
HTTP, remote MCP, and production hosted operations are intentionally out of
scope for v0.1. The read-only local stdio MCP MVP below is the narrow in-scope
adapter slice.

## Running the Project

Install and test with `uv`:

```powershell
uv run pytest
```

## Agent Workflow

Agents should follow `AGENTS.md`: sync `main` and `dev` before edits, do all
project work on `dev`, push completed updates to `origin/dev`, and keep Linear
issues current for non-trivial plans and changes. Do not create feature,
`codex/*`, worktree, cleanup, or recovery branches unless Ryan explicitly names
that branch in the same request. Before opening a `dev` to `main` PR, agents
must fetch origin, ensure `dev` is not behind `origin/main`, and verify GitHub's
compare file list only contains intended files.

## Local MCP MVP

Run the read-only stdio MCP server against a local Vexic database:

```powershell
uv run python scripts\vexic-mcp-stdio.py --db-path .\memory.db --tenant-id local --session-id default
```

For v0.1, `scripts\vexic-mcp-stdio.py` is the supported launcher. A package
entry point can wait for release packaging.

By default, the MVP exposes `search_transcript` and `search_long_term` only.
Transcript writes, export, delete, rebuild, and admin tools are intentionally
not registered. Long-term vector search requires a host-supplied embedding
adapter; without one, `search_long_term` returns a configuration error instead
of loading a model from Vexic core.

Privileged verbatim history egress is disabled by default. For a local,
session-bound agent that explicitly needs it, pass `--enable-expand-history` to
register `expand_history`. That tool requires `MemoryCapability.EXPAND_HISTORY`,
uses the configured scope only, applies forbidden-value redaction before
egress, and caps both returned messages and returned text. The local stdio MVP
does not yet have a dedicated audit hook for this privileged egress path.

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
  # Optional privileged egress:
  # "--enable-expand-history",
]
cwd = "C:\\Users\\Ryan\\Documents\\GitHub\\Vexic"
```

Claude Code local MCP config:

```powershell
claude mcp add --scope local vexic -- uv run python scripts\vexic-mcp-stdio.py --db-path .\memory.db --tenant-id local --session-id default
```

The stdio tool schemas cap `query` at 1000 characters, `limit` at 1-20 results,
and privileged `expand_history` responses at 100 returned messages and 20000
characters.

## Claude Code Transcript Import

Import cleaned Claude Code JSONL transcript rows into a local Vexic database:

```powershell
uv run python scripts\import-claude-code-jsonl.py --db-path .\memory.db --tenant-id local --session-id default <path-to-session.jsonl>
```

The importer is a repo-local host transcript recorder. It reads Claude Code
JSONL, keeps visible user/assistant text, maps source keys as
`claude-code`/`sessionId`/`uuid`, and delegates writes to
`LocalMemoryService.ingest_source_transcript`. It does not expose MCP writes.

<!-- memory-reliability-gate -->

The memory reliability gate is:

```powershell
uv run pytest tests/test_memory_reliability.py
```

<!-- memory-reliability-live-smoke -->

For existing tenant database smoke tests, open a current `memory.db` with the
Vexic SQLite adapter and verify memory tables continue working without touching
host-owned extension tables such as `background_tool_audit`.

## Hosted MVP Shell

The dependency-free hosted shell in `vexic.hosted` binds authenticated tenant
scope before delegation and can route sanitized request/job usage events to an
adapter-owned telemetry sink. Concrete tenant provisioning and API-key storage
live in adapters outside `src/vexic`.
It is an internal in-process boundary, not a public HTTP service. See
`docs/hosted-mvp.md`. External customer-memory readiness is blocked by the
hosted readiness gate
([COA-177](https://linear.app/ryan-boissonnault/issue/COA-177/define-hosted-security-privacy-backup-and-abuse-readiness-gate))
in Linear.
