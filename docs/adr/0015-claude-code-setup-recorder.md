# Claude Code setup recorder is hook-triggered

Status: accepted

## Context

ADR 0002 says host recorders ingest complete cleaned visible user/assistant
transcript rows and leave extraction to later dream phases. ADR 0014 says
transcript writes are out-of-band hosted HTTP ingest, not MCP writes.

The earlier recorder deliberation considered an external file-tail recorder to
serve no-install and cross-agent goals. Those goals are no longer MVP
requirements. The MVP now optimizes for the most reliable Claude Code first-run
experience after one explicit setup command.

## Decision

Vexic ships a Claude Code setup-command recorder as the MVP baseline:

```powershell
vexic setup claude-code
```

Setup configures Claude Code hooks and user-local Vexic recorder config. The
hook-triggered recorder reads the hook-provided transcript path and session id,
normalizes Claude Code JSONL rows into `SourceTranscriptMessage` records, and
sends cleaned source rows to hosted `/v1/ingest_source_transcript`.

The recorder may reread a transcript on each hook invocation and rely on hosted
source-ledger idempotency. A local cursor can be added for efficiency, but
correctness must not depend on it.

The external file-tail daemon is not the MVP baseline. A bounded manual or
hook-triggered reconcile path may exist for recovery, but Vexic does not install
an always-on watcher in this slice.

## Consequences

- The first supported write loop is Claude Code only.
- Cross-agent recording is deferred until a second agent's transcript and
  trigger surfaces are empirically verified.
- Claude Code hook setup is an install/configuration step, but gives a clearer
  user experience than requiring a resident tail process.
- Setup must keep secrets in user-local config and avoid project-local hook
  files that embed credentials.
- The source-ledger key includes the resolved hosted scope's optional
  `agent_id`; setup must choose a stable `agent_id` policy.

## Deferred

- Codex, OpenClaw, and Hermes recorder adapters.
- Optional external tail mode for agents without hooks.
- Hosted/server-side capture for fully hosted runtimes.
