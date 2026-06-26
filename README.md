# Vexic

Vexic is the standalone memory system extracted from a private source host: a
provenance-first, replayable memory core for long-running agents.

This first package slice is a local Python core with a SQLite adapter, public
contract models, and conformance tests. Managed billing, dashboards, public
HTTP, mature remote MCP, and production hosted operations are intentionally out
of scope for v0.1. The read-only local stdio MCP MVP and the hosted
internal-alpha read-only HTTP MCP adapter are the narrow in-scope MCP slices.

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
Pass `--agent-id <id>` to bind the server to one agent-specific memory scope;
omit it to bind the server to the explicit shared agent scope.

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
  # Optional agent-specific memory scope:
  # "--agent-id",
  # "agent-a",
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

### Native Agent Memory

[ADR 0004](docs/adr/0004-native-agent-memory-is-host-integration-policy.md)
treats runtime-native memory suppression as host integration policy. When Claude
Code, Codex, or another local agent is connected to Vexic, disable that
runtime's own durable memory where the runtime exposes a supported switch. Vexic
core cannot prevent local runtime memory writes and must not grow Claude-,
Codex-, or provider-specific suppression code.

For Claude Code, disable auto memory in the settings layer used to launch the
Vexic-connected agent:

```json
{
  "autoMemoryEnabled": false
}
```

Alternatively, launch Claude Code with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
`CLAUDE.md` files remain useful project instructions, but they are prompt
context rather than storage enforcement.

For Codex/local agents, keep Codex memories disabled for the Vexic profile. If a
profile would otherwise enable memories, pin the Vexic profile off:

```toml
[features]
memories = false

[memories]
generate_memories = false
use_memories = false
disable_on_external_context = true
```

If a runtime cannot disable native memory, Vexic is authoritative only for
memory ingested through its recorder or importer path. Runtime-local memory
remains outside Vexic replay, export, redaction, and deletion semantics.
For the host transcript recorder flow, see
[Claude Code Transcript Import](#claude-code-transcript-import) and
[ADR 0002](docs/adr/0002-host-recorders-ingest-complete-cleaned-transcripts.md).

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

The opt-in live provider retrieval smoke is:

```powershell
uv run --with-editable . python -m vexic.live_retrieval_baseline `
  --allow-live `
  --fixture .\longmemeval_s_smoke.jsonl `
  --adapter .\adapters\openrouter_live_adapter.py `
  --provider openrouter `
  --model-group retrieval-smoke `
  --output-dir .\artifacts\live-retrieval `
  --max-rows 1 `
  --max-provider-calls 6 `
  --timeout-seconds 120
```

Without `--allow-live`, the command exits 0 before importing the adapter or
calling providers. The host-owned OpenRouter adapter reads `OPENROUTER_API_KEY`
from the process environment and supplies `build_extraction_agent`,
`build_rem_agent`, `build_contradiction_agent`, and `embed_texts`; Vexic core
does not read provider secrets. The adapter lives under repo-local `adapters/`
by design because it is host-owned provider wiring, not package core.

Fixture rows are JSONL objects with `id`, `transcript`, `question`, and
`expected_fact`. `transcript` may be a list of strings or `{ "role": "user" |
"assistant", "content": "..." }` objects mapped from a host-supplied
LongMemEval_S artifact. Do not vendor the benchmark artifact into this repo.

The harness runs each row in a disposable SQLite database and writes
`retrieval_metrics.json` and `answer_synthesis_metrics.json` under
`--output-dir`. Retrieval metrics classify failures as extraction miss,
promotion miss, retrieval miss, candidate fallback, or provider/runtime failure;
answer synthesis is recorded separately as `not_run` with the reserved
`judge_synthesis_issue` taxonomy slot for this retrieval-only smoke.

## Hosted MVP Shell

The dependency-free hosted shell in `vexic.hosted` binds authenticated tenant
scope before delegation and can route sanitized request/job usage events to an
adapter-owned telemetry sink. Concrete tenant provisioning, API-key storage,
and the internal-alpha HTTP transport live in adapters outside the memory core.
The Railway alpha at `https://api.vexic.dev` is for throwaway internal testing,
not a public product service. See `docs/hosted-mvp.md`. External
customer-memory readiness is blocked by the
hosted readiness gate
([COA-177](https://linear.app/ryan-boissonnault/issue/COA-177/define-hosted-security-privacy-backup-and-abuse-readiness-gate))
in Linear.

## Native HTTP MCP

The hosted FastAPI app also exposes `POST /mcp` as a stateless, read-only,
JSON-only Streamable HTTP MCP slice. It requires `Authorization: Bearer
<vexic-api-key>`, binds project/session/agent scope from `X-Vexic-*` headers,
and exposes only `search_transcript` and `search_long_term`.

Minimum smoke request:

```powershell
curl.exe -s https://api.vexic.dev/mcp `
  -H "Authorization: Bearer <raw-key>" `
  -H "Accept: application/json, text/event-stream" `
  -H "X-Vexic-Project-Id: project-a" `
  -H "X-Vexic-Session-Id: session-a" `
  -H "Content-Type: application/json" `
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"
```
