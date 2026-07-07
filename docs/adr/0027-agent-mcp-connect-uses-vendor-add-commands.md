# Agent MCP connect uses each client's own `mcp add` command, opt-in

Status: accepted (supersedes the install mechanism of ADR 0017)

## Context

ADR 0026 settled that agent setup exchanges a single-use console setup token
for a scoped Agent API key. ADR 0017 then settled how `vexic setup claude-code`
wires the read-only MCP surface: it hand-writes a project `.mcp.json` entry in a
disabled state and records the disablement in `disabledMcpjsonServers`, rather
than auto-installing a live entry. COA-303 generalizes the connect leg to a
second client (Codex) plus a generic path, and that forced a re-examination of
the ADR 0017 mechanism.

Two of ADR 0017's three reasons for hand-writing a disabled scaffold do not
survive that re-examination:

1. Its load-bearing reason -- "auto-installing a live entry that embeds
   credentials is a secret-duplication and brittleness risk" -- describes a
   hosted-HTTP entry carrying a stale `Authorization` header. The scaffold it
   actually installs is a local stdio launcher that carries no header and reads
   credentials fresh from an owner-only file each run. The cited danger was
   already designed out by the chosen mechanism, so it does not justify an inert
   default.
2. "Do not mutate the app-managed client config" argues against hand-editing a
   large, fragile config file. Both Claude Code (`claude mcp add`) and Codex
   (`codex mcp add`) ship their own supported command that owns the config
   write. Invoking the vendor command removes the corruption risk without us
   parsing or merging the file, so it is not a reason to deviate from the
   standard path.

What genuinely remains is a product/privacy stance: connecting an agent should
not silently wire it to pull a user's stored memory. That is the only reason to
keep an explicit enable step -- and it is a deliberate choice, not a technical
necessity.

## Decision

Agent MCP connect uses each client's own `mcp add` command, and stays opt-in.

- `vexic setup <client> --token ...` exchanges the token, writes the owner-only
  credential file (Claude reuses `~/.vexic/claude-code-recorder.json`; Codex and
  other MCP-only clients get a dedicated `~/.vexic/<client>-mcp.json` of the same
  `base_url`/`api_key`/`project_id`/`session_id`/`agent_id?` shape the stdio
  proxy already reads), and then **prints** the exact vendor `mcp add` command.
  It does not run it.
- The printed command names only the local stdio launcher plus the *path* to the
  credential file (`... -- python -m vexic.mcp_stdio_main --recorder-config
  <path>`). No raw key ever appears in the command or in any client config --
  the no-inline-secret posture of ADR 0015/0017 is preserved.
- The user running that vendor command is the deliberate, per-client opt-in.
  Off until run, on once run. The Vexic Console shows the same per-client
  command as its "how to enable" instructions.
- Claude Code uses default (local) `claude mcp add` scope. Project scope is not
  used: it writes a repo file and triggers Claude Code's project-server approval
  prompt -- a second friction point with no privacy gain once off-by-default is
  handled by "we do not auto-add."
- Clients without a dedicated installer get a generic instruction path that
  prints the same launcher command and credential-file location to add to
  whatever MCP config that client uses.
- This is a connect/install-UX decision only. The exchange endpoint, the stdio
  launcher, and the hosted `/mcp` surface are unchanged; no server changes.

## Consequences

- The existing Claude Code leg is refactored: the hand-written disabled
  `.mcp.json` scaffold and `disabledMcpjsonServers` management are removed in
  favor of the printed `claude mcp add` command. `docs/usage.md` and the
  `claude_setup` tests move with it.
- The install mechanism of ADR 0017 is superseded; its no-inline-secret and
  opt-in *intent* is retained, now expressed through the vendor command rather
  than a Vexic-managed disabled scaffold.
- Setup no longer owns or mutates any client's MCP config, so uninstall for the
  MCP leg is likewise the printed vendor `mcp remove` command plus deleting the
  credential file, rather than Vexic rewriting client config.
- The recorder (write path) is still Claude-Code-only. Generalizing the recorder
  to Codex/OpenClaw/Hermes is tracked separately; COA-303 covers the MCP connect
  leg only.

## Deferred

- Dedicated installers for OpenClaw and Hermes Agent (config formats not yet
  pinned); the generic path covers them in the interim.
- Recorder/write-path generalization beyond Claude Code.
- Full device-code / OAuth flow (still deferred per ADR 0010 and ADR 0026).
