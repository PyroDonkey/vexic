# Claude Code setup scaffolds a disabled Vexic MCP entry

Status: accepted

## Context

ADR 0015 settled that `vexic setup claude-code` configures a hook-triggered
transcript recorder: it writes user-local recorder config
(`~/.vexic/claude-code-recorder.json`, owner-only) and a Stop hook in
`~/.claude/settings.json`. The hook command never embeds the API key.

ADR 0010 and ADR 0001 keep the Vexic MCP read-only and thin. Two MCP transports
exist: a local stdio server (`scripts/vexic-mcp-stdio.py`, local-trusted, no
auth) and a hosted Streamable HTTP server (`POST /mcp`) that requires
`Authorization: Bearer <api-key>`.

COA-250 and COA-253 end-to-end testing confirmed the recorder hook writes hosted
transcript memory, but Claude Code still reports no visible Vexic MCP. The
recorder hook and the MCP search server are separate setup surfaces, and
`setup claude-code` configures only the recorder. COA-260 asks whether setup
should also configure MCP, where that config should live, how to avoid
duplicating raw API keys in Claude config, and whether any hosted MCP server
changes are required.

Testing also showed that a direct hosted-HTTP MCP entry with a stale or
incorrect `Authorization` header fails independently of the recorder hook.
Auto-installing a live MCP entry that embeds credentials is therefore both a
secret-duplication and a brittleness risk.

## Decision

`vexic setup claude-code` does not auto-register a live Vexic MCP server. It
scaffolds a disabled / placeholder MCP entry that Claude Code will not use until
the user performs one explicit enable step.

- The scaffold targets a project-local `.mcp.json` entry. This aligns with
  setup's per-project arguments (`--project-id`, `--session-id`) and uses Claude
  Code's new-server approval prompt as the natural "disabled until the user
  enables it" gate.
- The scaffolded server is a local stdio launcher
  (`scripts/vexic-mcp-stdio.py`) configured to read its credentials from the
  existing `~/.vexic/claude-code-recorder.json`. The launcher, not the Claude
  config, holds the path to the secret. (There is no `vexic mcp` CLI subcommand
  today; the follow-up issue may add one, but the supported launcher is the
  script.)
- The raw API key is never written into `.mcp.json` or `~/.claude.json`. The
  recorder config remains the single source of truth for the base URL and key.
- No hosted MCP server changes are required. This is a setup/install-UX decision
  only; the hosted `POST /mcp` adapter (ADR 0010) and the stdio launcher are
  used as they already exist.

This is a decision record. The scaffold behavior is implemented under a
follow-up issue, built test-first.

## Security Notes

- Secrets stay in user-local `~/.vexic/claude-code-recorder.json` (owner-only,
  atomic write); Claude config files never receive the raw key.
- The scaffold is inert until the user enables it, so a stale credential cannot
  silently break a running agent through an auto-installed live entry.
- The local stdio transport is local-trusted and carries no `Authorization`
  header to leak or go stale; the hosted-backed launcher reuses the single
  stored credential.

## Consequences

- Setup keeps a clean separation: the recorder hook write path is unchanged, and
  MCP search becomes an opt-in the user explicitly enables.
- Users get a one-step path to the read-only MCP search surface without
  hand-authoring launcher config or copying keys.
- Vexic does not mutate Claude Code's large, app-managed `~/.claude.json`; the
  scaffold lives in a project-local `.mcp.json` it owns.
- A follow-up implementation issue must build the scaffold install (and a
  matching uninstall mirroring the recorder's idempotent `vexicHookId` removal),
  with tests asserting the entry is written disabled and carries no raw key.

## Deferred

- Auto-enabling a live MCP server by default.
- A direct hosted-HTTP `.mcp.json` / `~/.claude.json` entry that embeds an
  `Authorization` header (rejected: duplicates the key and is brittle when the
  header goes stale).
- User-level (`~/.claude.json`) scaffolding and the exact placeholder field
  shape; the follow-up issue decides these implementation details.
- Cross-agent (Codex, OpenClaw, Hermes) MCP scaffolding.
