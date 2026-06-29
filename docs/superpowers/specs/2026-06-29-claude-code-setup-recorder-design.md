# Claude Code Setup Recorder Design

## Goal

Give a user one explicit setup command that makes Claude Code conversations
record automatically into hosted Vexic and become readable through the existing
read-only MCP path.

The MVP optimizes for a reliable Claude Code first-run experience. It does not
promise install-free setup or cross-agent support. Other agents can be added
after their transcript and trigger surfaces are verified.

## Decision

Use a setup-command-first Claude Code recorder:

```powershell
vexic setup claude-code
```

The setup command configures Claude Code hooks, hosted write credentials, stable
scope headers, and status/debug files. Claude Code hooks provide the transcript
path and session id; the Vexic recorder reads the transcript, normalizes visible
user/assistant turns, and sends cleaned source rows to
`/v1/ingest_source_transcript`.

The external file-tail daemon is not the MVP baseline. A bounded reconcile pass
is allowed for catch-up, but there is no always-on watcher in the first slice.

## User Experience

The happy path is:

1. User creates or selects a hosted Vexic agent key.
2. User runs `vexic setup claude-code`.
3. Setup validates the key and writes Claude Code hook configuration.
4. User starts or resumes a Claude Code session.
5. On turn/session hook events, the recorder ingests new cleaned transcript
   rows.
6. A later Claude Code session reads the transcript through read-only MCP.

The setup command should print the installed scope, where status is written, and
how to uninstall. The user should not need to manage a daemon or find transcript
files manually.

## Architecture

### Setup Command

`vexic setup claude-code` is responsible for:

- validating hosted API reachability and credentials;
- writing local Vexic recorder config;
- choosing a stable `project_id`, `session_id` strategy, and optional
  `agent_id`;
- merging Claude Code hook settings without clobbering existing hooks;
- installing only a hook command that points to a trusted local Vexic entry
  point;
- writing a status location for last success, last error, last source session,
  and last ingested source message.

Setup must be idempotent. Re-running it updates the Vexic-owned hook block and
leaves unrelated hooks intact.

By default, setup writes user-local Vexic config and user-local Claude Code hook
settings. Project-local hook setup is an explicit option, not the default.

### Hook Trigger

Claude Code hook events are the primary trigger. The hook command invokes a
Vexic recorder entry point with the hook payload or payload path. The recorder
uses the hook-provided transcript path and session id instead of discovering
files.

The MVP should prefer a per-turn hook for freshness and may add a session-end or
session-start hook for bounded catch-up. Hook work must stay small enough not to
make Claude Code feel blocked.

### Recorder

The recorder:

- reads the Claude Code JSONL transcript for the provided path;
- normalizes rows using the same cleaning logic as the existing Claude JSONL
  importer;
- keeps only visible replayable user/assistant text;
- ignores non-transcript rows such as metadata, sidechain rows, summaries,
  thinking, tool calls, and tool results;
- sends cleaned rows to hosted `/v1/ingest_source_transcript`;
- records a durable status result after each run.

The recorder may reread the transcript and rely on source-ledger idempotency,
or it may keep a local cursor for efficiency. Correctness must not depend on
the cursor. The hosted source ledger remains the duplicate guard.

### Bounded Reconcile

The MVP can include a bounded catch-up path:

- setup-time verification ingest against a known transcript path, if available;
- session-start catch-up for the current transcript;
- manual `vexic recorder sync` for support and debugging.

This is not a daemon. It exists to recover missed hook runs, network failures,
and transcript flush timing issues.

## Scope And Identity

Hosted writes use scope-free bodies. Scope comes from trusted local config and
headers:

- tenant: bound by the Agent API key;
- project: required `X-Vexic-Project-Id`;
- session: required `X-Vexic-Session-Id`;
- agent: optional `X-Vexic-Agent-Id`.

Setup must choose and persist a stable `agent_id` policy. If `agent_id` is used,
it must be sent consistently so source-ledger idempotency does not split the
same source transcript across multiple identities. If it is omitted for the MVP,
that omission should be deliberate and documented.

Claude Code source rows use:

- `source_host = "claude-code"`;
- `source_session_id = <Claude Code session id>`;
- `source_message_id = <Claude Code row uuid>`.

## Security And Safety

The hook command is executable local configuration, so setup must be careful:

- do not write API keys into files likely to be committed;
- prefer user-local config for secrets;
- if project-local hook config is supported, keep secrets indirect through
  user-local config;
- make hook commands reference an installed Vexic entry point by absolute path
  or resolved command name, not generated project-local executable text;
- merge hook settings rather than overwriting user settings;
- provide uninstall and status commands;
- show exactly what hook command was installed;
- fail closed if required hosted scope is missing.

Project-local setup should avoid creating a surprise hook that runs for other
contributors with the original user's credentials. If project-local hook files
are needed, they should reference only a local Vexic config key name, not raw
secrets.

## Error Handling

Recorder failure must be visible:

- write last success/error state to a status file;
- write hook stderr for immediate diagnostics;
- include HTTP status and safe error codes, not secrets;
- keep failed rows replayable through the next hook or manual sync;
- do not delete or mutate source transcripts.

Network errors, expired keys, forbidden values, invalid rows, and hosted
rejections should be distinguishable in status output.

## Testing

Minimum tests for the MVP:

- setup merges a Vexic hook block into existing Claude Code settings;
- setup is idempotent and supports uninstall;
- setup does not write raw secrets to project-local files;
- recorder reuses the importer cleaning behavior;
- recorder posts cleaned source rows with required hosted headers;
- recorder can run twice against the same transcript without duplicates;
- polluted rows are ignored by the adapter or rejected before persistence;
- status records last success and last failure;
- an end-to-end hosted test verifies a newly recorded turn is readable through
  read-only MCP search.

## Follow-Up Work

Defer these until the Claude Code loop works:

- Codex transcript/trigger research;
- OpenClaw and Hermes adapter research;
- optional external tail mode for agents without hooks;
- hosted/server-side capture for fully hosted runtimes;
- broader adapter interfaces after a second concrete adapter exists.

## ADR Update

Add a new ADR for the recorder deployment model. It should record:

- setup-command-first Claude Code MVP;
- hook-triggered recorder, not file-tail daemon baseline;
- bounded reconcile as recovery, not a watcher;
- no MCP writes;
- raw cleaned Tier 1 only;
- source-ledger idempotency and stable `agent_id` policy;
- future adapters deferred until verified.
