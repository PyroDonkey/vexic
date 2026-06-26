# Restore Drill: Railway Alpha Volume 2026-06-26

Status: pass-with-caveats

## Summary

- Drill ID: coa-232-20260626T153308Z-3eeed3
- Date: 2026-06-26
- Operator: Codex on Ryan's Railway-linked workstation
- Environment: Railway production hosted alpha volume
- Runbook reference: `docs/runbooks/restore-drills/hosted-restore-drill.md`
- Scenario: hosted alpha Railway-volume SQLite backup/restore
- Tenant fixture: synthetic
- Customer-data readiness claim: no

## Recovery Source

- Source root: `/data/vexic`
- Source backup/export: SQLite backup API over `*.db` files into an ephemeral
  `/tmp` drill workspace
- Restore target: isolated ephemeral `/tmp` restore root
- Raw artifact retention: removed after validation
- PITR timestamp: not applicable to Railway-volume alpha
- Export object key/version: not applicable
- Export checksum or manifest id: row-count digest `1f4958eb2693a576`
- Neon catalog restore point: blocked; current hosted alpha uses SQLite
  control-plane storage on the Railway volume
- Expected RPO: not claimed for customer readiness
- Observed data-loss window: not measured for customer readiness

## Timing

- Started at: 2026-06-26T15:33:08.026679Z
- Replacement database ready at: 2026-06-26T15:33:08Z
- Validation passed at: 2026-06-26T15:33:08.174042Z
- Catalog repointed at: not run; restored copy stayed isolated
- Completed at: 2026-06-26T15:33:08.174042Z
- Measured RTO: under one second for the small alpha fixture

## Restored Resources

- Source Customer Memory Database id: redacted
- Replacement Customer Memory Database id: redacted
- Catalog row or routing record id: redacted
- Databases backed up and restored: 7 SQLite files
- Old handle state: active; no live catalog repoint was performed
- Synthetic source API key state: revoked after drill

## Validation

| Check | Result | Notes |
| --- | --- | --- |
| Schema version | pass | Live health returned contract `0.1.0`; restored service initialized from copied control-plane and customer DB files. |
| Canonical row counts or checksums | pass | Backup and restore canonical table counts matched; digest `1f4958eb2693a576`. Virtual projection tables were excluded from counts and verified by rebuild/search. |
| Projection rebuild | pass | Restored service rebuilt projections and search still found the synthetic canary. |
| Search smoke | pass | Restored recovered-key search and restored append/search smoke both passed on synthetic data. |
| Export smoke | not run | Current drill targeted Railway-volume backup/restore, not scoped export. |
| Replay smoke | not run | Current drill targeted Railway-volume backup/restore, not transcript replay. |
| Redaction fail-closed | pass | Restored append with configured forbidden value was rejected and the forbidden value was not searchable afterward. |
| Cross-tenant negative tests | pass | Wrong tenant, wrong project, and wrong agent requests failed closed. |
| One-active-database invariant | pass | Restored catalog routed the synthetic tenant to one active customer database. |
| Atomic catalog repoint | not run | No live repoint was performed for the alpha volume drill. |
| Stale-handle cleanup | not run | No replacement handle was activated. |
| Evidence sanitized | pass | Evidence contains counts, statuses, timestamps, and redacted handles only. |

## Production Caveats

- Customer Memory Database Turso PITR restore: blocked; no Turso provider
  configuration is exposed in this deployment.
- Neon control-plane recovery: blocked; the current hosted alpha uses a local
  SQLite control-plane database on the Railway volume, not Neon.
- S3 Object Lock export restore: blocked; no AWS/S3/KMS backup path is
  configured in this deployment.

## Follow-Ups

- Issue: COA-232
- Run the customer-readiness Turso PITR drill once the paid/prod-like Turso
  posture exists.
- Run the Neon control-plane recovery drill once the production control-plane
  store exists.
- Run the S3 Object Lock export fallback drill once the backup account, bucket,
  KMS, Versioning, Object Lock, and restore role are configured.
