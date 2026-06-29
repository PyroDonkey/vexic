# Vexic

Vexic gives long-running AI agents a memory they can trust. It stores cleaned
conversation history, stages possible memories for review, and promotes durable
facts with provenance so agents can recall what happened without replaying raw
logs or guessing at stale context.

Reliable agent memory matters because recall needs to be auditable, scoped, and
reversible. Vexic treats transcript rows as the source of truth, keeps derived
search indexes rebuildable, and records where each long-term fact came from so
memory behavior can be tested, migrated, and debugged.

Vexic is for engineers building agent products, internal automation, or
research systems that need local-first memory primitives today and a path to
hosted integrations later. The current package is a Python core with a SQLite
reference service, public contract models, retrieval primitives, and
conformance tests.

## Running the Project

Install and test the Python memory core with `uv`:

```powershell
uv run pytest
```

The Vexic Console source lives in `console/` as a repo-local Next.js
control-plane app. It is not Vexic package runtime, not a `vexic.*` entrypoint,
and must not move under `src/vexic`; ADR 0012 keeps dashboard concerns outside
the memory core. The repository root remains `uv`-managed.

For Vercel, Console may carry the isolated npm build contract in
`console/package.json` and `console/package-lock.json`. Do not add Node package
files at the repository root, and do not treat Console dependencies as memory
engine install requirements.

## Maintainer Notes

Operational AI-agent and maintainer instructions live in `docs/ai/`. They are
not product documentation, but they describe how automated agents should work in
this repository. Agents should follow `docs/ai/AGENTS.md`: sync `main` and
`dev` before edits, do all project work on `dev`, push completed updates to
`origin/dev`, and keep Linear issues current for non-trivial plans and changes.
Do not create feature, `codex/*`, worktree, cleanup, or recovery branches
unless the requester explicitly names that branch in the same request. Before
opening a `dev` to `main` PR, agents must fetch origin, ensure `dev` is not
behind `origin/main`, and verify GitHub's compare file list only contains
intended files.

For Linear-backed traceability, review relevant issues at session start, map
non-trivial work to an issue, keep status/comments current when scope, blockers,
decisions, or follow-ups change, and reconcile roadmap/todo docs when the
`docs/ai/AGENTS.md` reconciliation triggers fire. If Linear tooling is
unavailable, say so and do not invent issue IDs; record the required
reconciliation in the commit message or session report.

To confirm the agent rules stay tracker-neutral:

```powershell
rg -n "COA-[0-9]|Linear" docs/ai/AGENTS.md
```

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
not registered. Long-term vector search uses a host-supplied embedding adapter
when one is configured, otherwise it uses the optional local embedding adapter
from `vexic[local-embed]`. Without that extra, `search_long_term` returns an
actionable configuration error.

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
cwd = "<absolute-path-to-vexic-repo>"
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
`docs/ai/CLAUDE.md` remains useful project instruction context, but it is prompt
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

For hosted Claude Code recording, install the user-local hook and recorder
config:

```powershell
uv run --with-editable . python -m vexic.cli setup claude-code --base-url https://api.vexic.dev --api-key <raw-key> --project-id project-a --session-id session-a
```

The setup command updates the user's Claude Code hook config and writes a Vexic
recorder config outside the repository. On Claude Code stop events, the
recorder reads the JSONL transcript, keeps visible user/assistant text, maps
source keys as `claude-code`/`sessionId`/`uuid`, and posts cleaned rows to the
hosted `/v1/ingest_source_transcript` route.

To replay a missed hosted hook manually, point the recorder at the setup config
and a hook payload containing `session_id` and `transcript_path`:

```powershell
uv run --with-editable . python -m vexic.cli recorder ingest --config "$env:USERPROFILE\.vexic\claude-code-recorder.json" --hook-input .\claude-hook-replay.json
```

For local recovery/import, import cleaned Claude Code JSONL transcript rows into
a local Vexic database:

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
  --fixture .\tests\fixtures\longmemeval_s_smoke.jsonl `
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
by design because it is host-owned provider wiring, not package core. Embedding
can alternatively use the optional local `vexic[local-embed]` adapter.

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

Hosted transcript writes use the same project/session/agent headers as hosted
MCP reads. The write body does not include `scope` or `tenant_id`; the tenant is
bound from the Agent API key.

Console-created projects expose `tenantId`; Agent API Key create/list responses
include a `scopeTemplate` with the correct `tenant_id`, `project_id`,
`principal`, `trust_boundary`, and key capabilities. Use that template for
direct `/v1/search_*` calls instead of guessing a tenant id.

Claude Code hosted auto-recording is installed with `vexic setup claude-code`.
It writes cleaned transcript rows through `/v1/ingest_source_transcript`; the
read-only hosted MCP server is still used for search.

Likewise, the hosted fresh-conversation context API and agent-side recap
injection - assembling new hosted sessions from session summaries plus recent
messages - are not built yet. The local `vexic.storage` summary, active-context,
and recap helpers exist, but that hosted product slice is tracked separately in
[COA-254](https://linear.app/ryan-boissonnault/issue/COA-254/expose-hosted-fresh-conversation-context-from-summaries-plus-recent).

```powershell
curl.exe -s https://api.vexic.dev/v1/append_transcript `
  -H "Authorization: Bearer <raw-key>" `
  -H "X-Vexic-Project-Id: project-a" `
  -H "X-Vexic-Session-Id: session-a" `
  -H "Content-Type: application/json" `
  -d "{\"messages_json\":[\"<clean-model-message-json>\"],\"redaction\":{\"forbidden_values\":[]}}"
```

Search the hosted memory API with the copied `scopeTemplate`. Add `session_id`
for session-scoped transcript search:

```powershell
curl.exe -s https://api.vexic.dev/v1/search_transcript `
  -H "Authorization: Bearer <raw-key>" `
  -H "Content-Type: application/json" `
  -d "{\"scope\":{\"tenant_id\":\"tenant_from_console\",\"project_id\":\"project-a\",\"session_id\":\"session-a\",\"agent_id\":\"agent-a\",\"principal\":{\"principal_id\":\"agent-a\",\"principal_type\":\"agent\"},\"trust_boundary\":\"networked\",\"capabilities\":[\"memory:search\"]},\"query\":\"cedar\",\"limit\":5}"

curl.exe -s https://api.vexic.dev/v1/search_long_term `
  -H "Authorization: Bearer <raw-key>" `
  -H "Content-Type: application/json" `
  -d "{\"scope\":{\"tenant_id\":\"tenant_from_console\",\"project_id\":\"project-a\",\"agent_id\":\"agent-a\",\"principal\":{\"principal_id\":\"agent-a\",\"principal_type\":\"agent\"},\"trust_boundary\":\"networked\",\"capabilities\":[\"memory:search\"]},\"query\":\"cedar\",\"limit\":5}"
```

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
