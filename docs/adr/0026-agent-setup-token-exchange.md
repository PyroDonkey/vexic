# Agent setup uses a short-lived setup token exchange

Status: accepted

Note (2026-07-06): the console "Connect agent" mint UI is now a concern of the
private `PyroDonkey/vexic-website` repo (ADR 0012 addendum); the CLI `--token`
exchange leg remains this repository's deliverable and is still unbuilt.

## Context

The COA-239 live smoke and a later external onboarding attempt (COA-252)
showed that connecting Claude Code to hosted Vexic requires copying values out
of the Vexic Console into a hand-assembled local command: the raw Agent API
key, project id, session id, and optional agent id. The console displays the
raw key once with a scope-template JSON, and no console surface generates a
complete setup command. This makes the browser responsible for presenting a
durable credential and local-machine setup details, and it is easy to
mis-scope.

The failure is not just cosmetic. A durable bearer credential rendered in a
browser tab can land in clipboard managers, screen shares, and shell history.
The acceptance criteria for COA-252 require a setup path that does not ask
users to copy a browser-displayed command containing a raw credential.

Relevant existing pieces:

- Agent API keys are static bearer tokens (`vx_<id>_<secret>`), SHA-256
  hashed at rest, minted by the control plane
  (`src/vexic/hosted_control_plane_http.py`) and shown once in the console.
- `vexic setup claude-code` (`src/vexic/recorders/claude_setup.py`) already
  owns the local write side: owner-only recorder config, Claude hooks, and a
  disabled MCP scaffold (ADR 0015, ADR 0017).
- The native hosted MCP surface explicitly deferred OAuth discovery and PKCE
  (ADR 0010, docs/hosted-mvp.md).

A second blocker found during the same onboarding: the setup CLI refuses to
run outside a Vexic source checkout, so a plain `pip install vexic` cannot
complete setup at all. That fix is tracked and implemented separately from
this decision (same COA-252 umbrella).

## Options considered

1. **Console-generated copy-paste command embedding the raw key.** The console
   renders one complete `vexic setup claude-code --api-key vx_... ...` command.
   Cheapest change and fixes hand-assembly, but the durable credential still
   transits the browser and clipboard, failing the core acceptance criterion.
   Rejected.
2. **Short-lived setup token exchange.** The console mints a single-use,
   short-TTL setup token bound to a project and scope choice; the CLI
   exchanges it server-side for a freshly minted Agent API key plus the
   project/session identifiers, then runs the existing installer. Small new
   endpoint surface, no OAuth machinery. Chosen.
3. **Full device-code / OAuth flow.** The CLI prints a user code, the user
   approves in the console under their Clerk session, the CLI polls for the
   grant. Most seamless and the best long-term generalization across MCP
   clients, but the largest infrastructure step: device authorization
   endpoints, polling, and consent UI. Deferred as the evolution path,
   consistent with the standing OAuth deferral in ADR 0010.

## Decision

Agent setup moves to a short-lived setup token exchange.

- The console gains a "Connect agent" action that mints a single-use setup
  token (`vxsetup_<id>_<secret>`, TTL on the order of 10 minutes) through a
  new control-plane endpoint. The token is bound at mint time to the project,
  the chosen scope template, an optional agent id, and the session strategy.
- The user copies exactly one command:
  `vexic setup claude-code --token vxsetup_... --base-url https://api.vexic.dev`.
  A single-use, short-TTL token may transit the browser and clipboard; the
  durable Agent API key may not.
- The CLI exchanges the token at a new hosted endpoint. The server consumes
  the token, mints the scoped Agent API key, and returns the raw key together
  with the project id and session id. The CLI feeds these into the existing
  `install_claude_code_setup` installer unchanged: owner-only recorder config,
  hooks, disabled MCP scaffold (ADR 0015, ADR 0017). The raw key is never
  rendered in the browser.
- Exchange is one-shot: a consumed or expired token fails authentication.
  Keys minted through exchange are ordinary Agent API keys -- revocable through
  the existing console flow -- and their records carry created-via-setup
  provenance so operators can distinguish them.
- The exchange endpoint is client-agnostic. Future `vexic setup <client>`
  targets (Codex, OpenClaw, Hermes Agent, other MCP clients) reuse the same
  mint-and-exchange contract; only the local installer differs per client.

This is a decision record. The token store, endpoints, console UI, and CLI
exchange path are implemented under follow-up issues, built test-first.

> Amended: those follow-ups have shipped. The setup-token store and the mint /
> revoke / list control-plane surfaces live in
> `src/vexic/hosted_control_plane_http.py`, the exchange surface is
> `POST /v1/setup/exchange` in `src/vexic/hosted_http.py`, the CLI exchange path
> is `exchange_setup_token` in `src/vexic/recorders/setup_exchange.py`, and the
> behavior is pinned by `tests/test_setup_exchange.py`. The console leg lives in
> the companion `vexic-website` repo. The contract described above is unchanged;
> only its "not yet built" status is.

## Security notes

- The browser handles only a credential that is single-use and expires in
  minutes; interception after use or expiry yields nothing.
- The durable key is created server-side during exchange and travels once,
  over TLS, directly into the owner-only recorder config.
- Setup tokens are stored hashed like Agent API keys and are revocable before
  use; mint and exchange events are attributable per project.
- Scope is fixed at mint time in the console, so the CLI cannot escalate the
  scope of the key it receives.

## Consequences

- One new control-plane surface (mint) and one new hosted surface (exchange),
  plus a small console UI addition -- no OAuth or polling infrastructure.
- The local installer and its security posture (ADR 0015, ADR 0017) are
  reused as-is; this decision only changes how the installer's inputs are
  obtained.
- Follow-up issues: setup-token store and endpoints; console "Connect agent"
  UI; CLI `--token` exchange path; generalization to other MCP clients.

  > Amended: all of these have shipped (see the amendment above for the store,
  > endpoints, and CLI path). The generalization to other MCP clients landed
  > with ADR 0027: `src/vexic/recorders/mcp_connect.py` provides
  > `install_codex_connect` for Codex and `install_generic_connect` for clients
  > without a dedicated installer, and `vexic setup codex` /
  > `vexic setup mcp-client` route to them (`src/vexic/cli.py`). As this ADR
  > predicted, they reuse the same mint-and-exchange contract and differ only in
  > the local installer.

## Deferred

- Full device-code / OAuth flow (evolution path once client breadth demands
  it).
- Browser-to-local deep links or downloadable config files.
- Any MCP write tooling or changes to the read-only MCP surfaces.
