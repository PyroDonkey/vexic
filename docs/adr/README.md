# Architecture Decision Records

This index is the canonical in-repo list of Vexic ADRs. It is authoritative
over any downstream tracking view (the project roadmap/todo). When an ADR is
added, changed, or its status moves, update this index in the same change and
reconcile the downstream tracking roadmap/todo against it. See "Docs Are
Downstream Of Code" in `AGENTS.md`, which names the tracking system and the
reconciliation triggers.

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

Notes:

- 0005, 0006, and 0008 record hosted decisions (storage, abuse protection,
  encryption/backup). They are the source of truth for those hosted topics; a
  downstream "SaaS Stack Plan" or similar planning doc must not contradict them.
- 0007 corresponds to the multi-agent scoping work. The repo, not a tracking
  view, defines the accepted scope semantics.
- These numbers are the Vexic `docs/adr/` series. Some source comments under
  `src/vexic` cite an `upstream ADR-00NN` label from the extraction source
  (for example `upstream ADR-0010` for candidate-fallback retrieval); those
  labels are deliberately namespaced and do not map to the files here.
