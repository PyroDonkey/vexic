# Hosted Restore Drill Runbook

Role: operator procedure and evidence template for hosted memory restore drills.

This runbook proves the hosted backup and restore posture described in ADR 0008.
It is not an incident report, compliance binder, or customer-facing SLA.

Drill records should be stored in this directory as sanitized Markdown files and
linked from the project tracker. Do not store raw memory payloads, raw API keys,
database tokens, provider credentials, or unredacted database handles in drill
records.

## When To Run

Run at least these drills before external beta or real hosted customer memory:

- one single-customer Turso PITR restore drill; and
- one S3 export fallback restore drill.

The drill must include Neon catalog repoint validation. If the selected Neon
control-plane tier or recovery workflow changes, run a catalog recovery drill
before relying on the new posture.

Bootstrap and internal dogfood may use shorter manual notes. They do not prove
customer-data readiness.

## Preconditions

For customer-readiness drills:

- the environment uses the intended paid or prod-like Turso tier;
- the Neon control-plane tier and backup window have been verified;
- S3 export backups use the configured backup account, SSE-KMS, Versioning,
  Object Lock governance mode, and CloudTrail/KMS audit;
- the drill tenant is synthetic or explicitly approved for restore testing;
- configured forbidden values are available for redaction checks;
- the operator has least-privilege restore permissions; and
- the runbook version or commit SHA is recorded.

## Scenario A: Turso PITR Restore

1. Record the tenant fixture, current active database handle, and chosen PITR
   timestamp using redacted identifiers.
2. Start the drill timer.
3. Restore the Customer Memory Database to a new isolated replacement database.
4. Create or retrieve the replacement database token through the hosted secret
   store. Do not paste the token into evidence.
5. Verify the replacement database before routing traffic to it:
   - schema version matches the expected hosted adapter version;
   - canonical row counts or checksums match the expected restore point;
   - transcript, candidate, fact, retrieval-event, tombstone, and catalog-linked
     rows are present where expected;
   - forbidden values are rejected by redaction checks;
   - cross-tenant read, search, export, replay, rebuild, and delete attempts
     fail;
   - search smoke checks work after projection rebuild; and
   - export/replay smoke checks return only the target tenant fixture.
6. Rebuild FTS, vector, and other rebuildable projections.
7. Atomically repoint the Neon catalog from the old active database handle to
   the verified replacement handle.
8. Confirm the one-customer to one-active-database invariant.
9. Smoke-test the hosted API through the normal tenant-bound route.
10. Mark the old handle stale or quarantined. Decommission it only after rollback
    needs expire.
11. Stop the timer and record the measured RTO and observed data-loss window.

## Scenario B: S3 Export Fallback Restore

1. Record the tenant fixture and export manifest using redacted identifiers.
2. Start the drill timer.
3. Retrieve the selected export object and manifest from S3.
4. Verify Object Lock metadata, object version, checksum, and KMS decrypt
   authorization.
5. Restore the export into a new isolated Customer Memory Database.
6. Run the same validation, rebuild, catalog repoint, and stale-handle steps as
   Scenario A.
7. Record the export timestamp, measured RTO, and fallback data-loss window.

## Scenario C: Neon Catalog Recovery

Neon recovery is required because the catalog owns the active memory database
pointer. A memory restore is not complete until the catalog can safely repoint
the tenant.

1. Restore or verify the Neon control-plane catalog using the selected paid-tier
   recovery mechanism.
2. Verify tenant, project, API-key metadata, audit, usage, job, and routing rows
   needed for the drill fixture.
3. Confirm the catalog can represent exactly one active memory database handle
   for the customer.
4. Confirm stale or orphaned handles are detected.
5. Run an atomic repoint transaction against the drill fixture.
6. Confirm the hosted API observes the new active handle and rejects stale
   handles.

## Validation Checklist

Every customer-readiness drill record should report pass, fail, or not run for:

- schema version check;
- canonical row count or checksum check;
- projection rebuild;
- search smoke check;
- export smoke check;
- replay smoke check;
- redaction fail-closed check;
- cross-tenant negative read/search/export/replay/rebuild/delete check;
- catalog one-active-database invariant;
- atomic catalog repoint;
- stale-handle quarantine or decommission plan;
- sanitized audit event written; and
- raw payloads and secrets absent from evidence.

## Evidence Template

Create a new sanitized Markdown record in this directory for each drill. Use a
filename like `YYYY-MM-DD-pitr-tenant-fixture.md` or
`YYYY-MM-DD-export-fallback-tenant-fixture.md`.

```markdown
# Restore Drill: <scenario> <date>

Status: pass | pass-with-caveats | fail | blocked

## Summary

- Drill ID:
- Date:
- Operator:
- Environment:
- Runbook reference:
- Scenario: Turso PITR | S3 export fallback | Neon catalog recovery
- Tenant fixture: synthetic | redacted customer
- Customer-data readiness claim: yes | no

## Recovery Source

- PITR timestamp:
- Export object key/version:
- Export checksum or manifest id:
- Neon catalog restore point:
- Expected RPO:
- Observed data-loss window:

## Timing

- Started at:
- Replacement database ready at:
- Validation passed at:
- Catalog repointed at:
- Completed at:
- Measured RTO:

## Restored Resources

- Source Customer Memory Database id: redacted
- Replacement Customer Memory Database id: redacted
- Catalog row or routing record id: redacted
- Old handle state: active | quarantined | stale | decommissioned

## Validation

| Check | Result | Notes |
| --- | --- | --- |
| Schema version | pass/fail/not run |  |
| Canonical row counts or checksums | pass/fail/not run |  |
| Projection rebuild | pass/fail/not run |  |
| Search smoke | pass/fail/not run |  |
| Export smoke | pass/fail/not run |  |
| Replay smoke | pass/fail/not run |  |
| Redaction fail-closed | pass/fail/not run |  |
| Cross-tenant negative tests | pass/fail/not run |  |
| One-active-database invariant | pass/fail/not run |  |
| Atomic catalog repoint | pass/fail/not run |  |
| Stale-handle cleanup | pass/fail/not run |  |
| Evidence sanitized | pass/fail/not run |  |

## Follow-Ups

- Issue:
- Owner:
- Required before customer readiness: yes | no

## Notes

- Sanitized command transcript or log reference:
- Anomalies:
- Decision:
```

## What This Does Not Prove

One successful drill does not prove a public SLA, multi-tenant disaster recovery,
cross-region recovery, customer-managed-key recovery, or continuous data-loss
prevention. Those remain long-term hardening work until a customer, compliance,
scale, or launch-review requirement pulls them forward.
