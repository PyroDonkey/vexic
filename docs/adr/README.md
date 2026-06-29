# Architecture Decision Records

This index is the canonical in-repo list of Vexic ADRs. It is authoritative
over any downstream tracking view (the project roadmap/todo). When an ADR is
added, changed, or its status moves, update this index in the same change and
reconcile the downstream tracking roadmap/todo against it. See "Docs Are
Downstream Of Code" in `docs/ai/AGENTS.md`, which defines the reconciliation
triggers.

Every ADR file in this directory must appear in the table below. The
`.claude/hooks/check_doc_drift.py` SessionStart hook flags any ADR file that is
missing from this index (and the reverse).

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
| 0017 | Claude Code setup scaffolds a disabled Vexic MCP entry           | accepted |

Notes:

- 0005, 0006, and 0008 record hosted decisions (storage, abuse protection,
  encryption/backup). They are the source of truth for those hosted topics; a
  downstream "SaaS Stack Plan" or similar planning doc must not contradict them.
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
  issue owns the scaffold install/uninstall, built test-first.
- These numbers are the Vexic `docs/adr/` series. Some source comments under
  `src/vexic` cite an `upstream ADR-00NN` label from the extraction source
  (for example `upstream ADR-0010` for candidate-fallback retrieval); those
  labels are deliberately namespaced and do not map to the files here.
