# Architecture Decision Records

> `COA-###` references throughout these ADRs are internal issue-tracker IDs,
> kept for provenance only. They are not publicly resolvable.

This index is the canonical in-repo list of Vexic ADRs. It is authoritative
over any downstream tracking view (the project roadmap/todo). When an ADR is
added, changed, or its status moves, update this index in the same change and
reconcile the downstream tracking roadmap/todo against it. See "Docs Are
Downstream Of Code" in `AGENTS.md`, which defines the reconciliation
triggers.

Every ADR file in this directory must appear in the table below.
`scripts/check_doc_drift.py --ci` flags any ADR file that is missing from this
index (and the reverse).

| ADR  | Title                                                           | Status   |
| ---- | --------------------------------------------------------------- | -------- |
| 0001 | Product and agent integration surfaces                          | accepted |
| 0002 | Host recorders ingest complete cleaned transcripts              | accepted |
| 0003 | Host-triggered, Vexic-committed promotion                       | accepted |
| 0004 | Native agent memory is host integration policy                  | accepted |
| 0005 | Hosted v1 memory storage starts SQLite-compatible, Postgres-ready | accepted |
| 0006 | Hosted rate limiting starts with edge WAF plus in-process quotas | accepted |
| 0007 | Agent scope is exact and shared rows are explicit               | accepted |
| 0008 | Hosted data protection uses provider encryption, PITR, exports  | accepted |
| 0009 | Production telemetry boundary is settled before product analytics | accepted |
| 0010 | Native read-only HTTP MCP is a stateless hosted adapter slice   | accepted |
| 0011 | Hosted migration is operator-run canonical row migration        | accepted |
| 0012 | Vexic Console starts as one Next.js app                         | accepted |
| 0013 | Hosted control-plane HTTP API is a console-facing adapter slice | accepted |
| 0014 | Transcript writes are out-of-band auto-record, not an MCP tool   | accepted |
| 0015 | Claude Code setup recorder is hook-triggered                   | accepted |
| 0016 | Local embedding and deferrable contradiction lower the LLM floor | accepted |
| 0017 | Claude Code setup scaffolds a disabled Vexic MCP entry           | superseded by ADR 0027 |
| 0018 | Claude Code read path splits SessionStart priming and MCP on-demand pull | accepted |
| 0019 | Hosted storage cutover starts Turso-only, Neon deferred         | accepted |
| 0020 | Heuristic REM lowers the dream-phase LLM floor                  | accepted |
| 0021 | MCP memory surfaces are proactive and prose-first               | accepted |
| 0022 | Physical purge erases tombstoned scopes from the primary database | accepted |
| 0023 | Hosted content encryption flows through a core ContentCodec port | accepted |
| 0024 | Hosted fresh-conversation context ships as a Summarize dream phase plus a dedicated fresh_context capability | accepted |
| 0025 | Automatic summarize triggering ships as an async trigger endpoint, hourly cron, and a detached SessionStart backstop | accepted |
| 0026 | Agent setup uses a short-lived setup token exchange             | accepted |
| 0027 | Agent MCP connect uses each client's own `mcp add` command, opt-in | accepted |
| 0028 | Control-plane destructive ops are audited, confirmed, and soft-deleted | accepted |
| 0029 | Hosted load_active_context returns structured session history under the fresh-context capability | accepted |
| 0030 | The hosted service schedules per-tenant dreaming itself with an in-server sweeper | accepted |

Notes:

- 0005, 0006, and 0008 record hosted decisions (storage, abuse protection,
  encryption/backup). They are the source of truth for those hosted topics; a
  downstream "SaaS Stack Plan" or similar planning doc must not contradict them.
  The remaining pre-launch abuse gates named in 0006 (durable distributed quota,
  dream-phase concurrency lock, spend caps, edge throttles, alerting, abuse
  response) are tracked by COA-263. The 0005/0008 Turso/Neon production cutover
  (the hosted alpha currently runs SQLite on a Railway volume) is tracked by
  COA-264, and ADR 0019 records how that cutover starts: Turso-only as a
  bootstrap posture (customer memory and the control-plane catalog both on
  managed libSQL), with the Neon Postgres control plane deferred to a later
  promotion before external-customer memory. ADR 0019's 2026-07-01 addendum
  records the implementation clarifications from a real-Turso verification spike
  and design audit (token is a separate `connect` arg via a secret-bearing
  `StorageTarget`, per-tenant tokens minted short-lived, init-once schema memo,
  verify-gated generation-stamped restore). The full-posture implementation is
  owned by its own Linear issue; testing is COA-272.
- 0007 corresponds to the multi-agent scoping work. The repo, not a tracking
  view, defines the accepted scope semantics.
- 0011 corresponds to the local/self-host to hosted migration-path decision for
  COA-202. The operator runbook and drill, not a public import API, are the
  readiness owner for that path.
- 0012 corresponds to the COA-190 website and account dashboard implementation
  path. Vexic Console is a separate Next.js control-plane app; it does not move
  dashboard concerns into `src/vexic`.
- 0013 is accepted for COA-247. It records the control-plane HTTP surface and
  distinct control-plane auth boundary for the hosted adapter.
- 0014 settles that transcript writes are out-of-band auto-record (the recorder,
  COA-253; its design deliberation, COA-257) and that MCP stays read-only on
  both surfaces. It cancels the MCP write slice (COA-175) and affirms ADR 0002
  and ADR 0010.
- 0015 settles the Claude Code auto-record MVP as setup-command hook capture
  rather than an external file-tail daemon.
- 0016 records the optional local embedding adapter and the first-pass Deep
  promotion path that defers contradiction judging to a later audit.
- 0017 settles COA-260: `vexic setup claude-code` scaffolds a disabled,
  user-enabled Vexic MCP entry (local stdio launcher reusing the recorder
  config) rather than auto-installing a live entry, keeps raw keys out of Claude
  config, and requires no hosted MCP server changes. It builds on the COA-250 /
  COA-253 evidence and affirms ADR 0010 and ADR 0015. A follow-up implementation
  issue owns the scaffold install/uninstall, built test-first. Its install
  mechanism is superseded by ADR 0027; the no-inline-secret and opt-in intent
  carry forward.
- 0018 settles COA-262: the default Claude Code read path is SessionStart
  priming from the existing recorder config plus an opt-in MCP on-demand pull
  leg. UserPromptSubmit relevance injection and a dedicated no-query priming
  endpoint stay deferred.
- 0020 settles COA-275 and extends 0016's LLM-floor reduction: REM becomes a
  local deterministic embedding-centrality heuristic, the REM agent port and
  adapter symbol are deleted, and the remaining dream-phase LLM legs are Light
  extraction plus the (deferrable per 0016) Deep contradiction judge on the
  `deepseek/deepseek-v4-pro` hosted default.
- 0021 governs the model-facing MCP surface introduced in response to observed
  Claude Code UX failures: renamed recall tools with trigger-first
  descriptions, shared proactive/presentation server instructions, and prose
  search results. It affirms the read-only posture of 0010/0014 and extends
  the 0018 read path.
- 0024 settles COA-254 and implements the "dedicated no-query fresh-context
  endpoint" ADR 0018 deferred: a `summarize` dream phase compacts Tier 1 spans
  into `session_summaries` rows, `fresh_context`/`memory:fresh-context` reads
  a bounded recap-plus-tail for SessionStart priming, and a two-layer guard
  (recorder skip plus `ingest_source_transcript` rejection on
  `PRIME_CONTEXT_HEADER`) keeps the injected recap out of Tier 1. Tagging
  fresh-context-sourced rows with `source == "compact"` and threshold tuning
  is COA-268.
- 0025 settles the COA-254 follow-on that makes Summarize triggering
  automatic: `POST /v1/trigger_dream_phase` (capability
  `memory:dream:trigger`, summarize-only in v1) schedules an async sweep run
  on its own worker-thread event loop, an hourly `dream-cron.yml` workflow is
  the primary producer, and a detached subprocess spawned from `recorder
  prime` is a SessionStart backstop. It documents the tenant(+agent)-wide
  sweep/budget scope honestly (project header authenticates, does not scope
  the sweep) and the accepted single-process risks (in-process dedup lock
  and rate limiter, task loss on restart). It affirms ADR 0018 and extends
  ADR 0024.
- 0026 settles the COA-252 setup-UX decision: agent setup moves to a
  single-use, short-TTL setup token minted in the console and exchanged by the
  CLI for a scoped Agent API key, so the raw key never transits the browser.
  Full device-code/OAuth stays deferred per ADR 0010. Follow-up issues own the
  token store/endpoints, console UI, and CLI exchange path.
- 0027 settles COA-303's connect posture: agent MCP connect uses each client's
  own `mcp add` command (Claude Code, Codex) and stays opt-in -- `vexic setup`
  writes the owner-only credential file and prints the vendor command rather than
  auto-writing client config; the user running it is the deliberate enable step.
  It supersedes the install mechanism of ADR 0017 (keeping its no-inline-secret
  and opt-in intent) and requires no server changes. The generic path covers
  clients without a dedicated installer; recorder generalization is tracked
  separately.
- 0028 settles COA-320's in-repo control-plane hardening: destructive
  control-plane ops record `hosted_audit_events` (with new `project_id`/`key_id`
  columns), a whole-scope purge (`PurgeScopeRequest` with a null session)
  requires `confirm_whole_scope=True`, and `hosted_projects`/`tenants` gain
  inline `retired_at`/`retired_by` soft-delete. Extends ADR 0022. Infra
  controls (PITR/backups, Railway SSH) and the `adapters/` credential-scoping
  work stay deferred to their own workstreams.
- These numbers are the Vexic `docs/adr/` series and are self-contained.
  `src/vexic` source no longer carries any `upstream ADR-00NN` extraction-source
  labels (they were removed when the COA boundary policy was clarified), so there
  is no cross-series namespace to disambiguate.
