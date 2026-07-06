# Hosted data protection uses provider encryption, PITR, and drilled exports

Status: accepted

## Addendum (2026-07-06): later decisions supersede parts of this record

The Neon control-plane wording below is superseded by ADR 0019: the hosted
storage cutover starts Turso-only, so no Neon control-plane database exists in
the landed implementation. The statement that "Vexic does not build app-level
searchable-memory encryption" is superseded by ADR 0023: hosted content
encryption now runs through an app-level ContentCodec.

## Context

Hosted Vexic memory will store sensitive long-running agent memory. Before real
customer memory is accepted, the hosted path needs a deliberate and testable
encryption, backup, restore, and compromised-key posture.

This decision builds on ADR 0005. Customer memory remains one isolated
SQLite-compatible Customer Memory Database per customer tenant. The hosted
adapter owns routing, provisioning, backup, restore, and migration
orchestration outside the core package. The separate control-plane catalog maps
customers to active memory databases and stores non-memory operational metadata.

This decision also sits beside ADR 0006. Edge hardening, abuse controls, and
rate limits remain separate hosted-readiness concerns.

## Decision

Hosted v1 uses a provider-managed data protection baseline:

- Turso/libSQL-compatible Customer Memory Databases store per-customer memory.
- Neon Postgres stores the production control-plane catalog, customer metadata,
  API-key metadata, audit events, usage events, and job ledgers.
- AWS S3 stores independent export backups before customer-data readiness.
- TLS is required for API, worker, database, catalog, and object-storage
  traffic.
- Provider-managed encryption at rest is the v1 baseline for Turso and Neon.
- S3 export artifacts use SSE-KMS, Versioning, Object Lock in governance mode,
  and CloudTrail/KMS audit events.
- Vexic does not build app-level searchable-memory encryption or BYOK support
  in v1.

These choices are readiness targets, not day-one bootstrap requirements.
Internal staging and dogfood may use cheaper provider tiers and simpler manual
exports if they make no customer-data-readiness claim.

## Hosted Storage Surfaces

| Surface | v1 posture |
| --- | --- |
| Customer memory | One Turso/libSQL-compatible Customer Memory Database per customer tenant. |
| Control plane | Neon Postgres for routing catalog, key metadata, audit, usage, and job metadata. |
| Independent exports | AWS S3 backup account with encrypted, versioned, retention-locked objects. |
| Logs and artifacts | Sanitized operational records only; no raw memory payloads or raw API keys. |

The original Neon-centric wording for customer memory is superseded by ADR
0005. Neon remains the selected control-plane database unless a later decision
reopens that stack choice.

## Encryption

V1 relies on established provider encryption and access controls:

- Turso and Neon provide provider-managed encryption at rest.
- AWS S3 export artifacts are encrypted with SSE-KMS using an environment-level
  customer-managed KMS key.
- All network paths use TLS.
- Raw API keys, provider credentials, database tokens, and KMS permissions stay
  in hosted/control-plane secret management, not in `src/vexic`.
- Export, replay, rebuild, delete, and restore evidence are privileged surfaces
  and must remain redaction-aware.

V1 deliberately does not add:

- app-level envelope encryption for searchable transcript or fact text;
- per-customer memory encryption keys;
- customer-managed keys or BYOK;
- key-loss semantics for customer-managed keys;
- automated re-encryption or rekey workflows;
- search over app-encrypted memory.

Those are long-term or enterprise features that need a concrete customer,
compliance, or threat-model driver.

## Backups

Turso PITR is the primary customer-memory recovery mechanism. Real customer
memory targets Turso Scaler or an equivalent tier with a 30-day PITR window.
Cheaper tiers are acceptable for bootstrap, demos, internal staging, and dogfood
only.

Neon PITR and backup posture must be verified against the selected paid control
plane tier before customer readiness. The control plane is part of recovery
because it owns the active database pointer and API-visible routing state.

Independent exports are the fallback when provider PITR is unavailable,
insufficient, or outside the desired restore path. Before customer readiness,
hosted operations should export both:

- each Customer Memory Database; and
- the Neon control-plane catalog data required to reconstruct routing, key
  metadata, audit, usage, and job state.

The v1 fallback export cadence is daily. Daily export fallback accepts up to
24 hours of data loss and a same-business-day restore target. A tighter cadence
is deferred until customer or SLA requirements justify it.

The customer-readiness export target is:

- a separate AWS backup account;
- a private S3 bucket created with Object Lock enabled;
- S3 Versioning enabled;
- Object Lock governance retention;
- SSE-KMS with an environment-level KMS key in the bucket region;
- CloudTrail S3 data events and KMS API audit;
- least-privilege writer and restore roles; and
- governance bypass limited to a break-glass role.

S3 export hardening is not required during bootstrap. It is required before
external beta or real hosted customer memory.

## Restore And RPO/RTO

The normal single-customer restore path is:

1. Restore the Customer Memory Database to an isolated replacement database.
2. Verify schema, canonical rows, tenant isolation, redaction behavior, and
   search/rebuild behavior.
3. Rebuild FTS, vector, and other rebuildable projections.
4. Atomically repoint the Neon catalog from the old active database handle to
   the verified replacement handle.
5. Quarantine, then decommission stale database handles after rollback needs
   expire.

Turso PITR restores create a replacement database rather than restoring in
place, so catalog repointing is part of the restore path. The old active
database may be unavailable during an incident; hosted API behavior should use a
maintenance or blocked state rather than widening access or serving unverified
data.

For v1 customer beta:

- Turso PITR accepts the provider-documented small pre-timestamp recovery gap.
- Single-customer PITR restore targets 2 to 4 hours end to end until drills
  prove a tighter number.
- Daily export fallback accepts up to 24 hours of data loss and same-business
  day restore.
- No external SLA is claimed until timed drills prove the numbers.

The hosted restore drill runbook lives at
`docs/runbooks/restore-drills/hosted-restore-drill.md`.

## Rebuild And Replay

V1 does not need an auto-rebuild daemon. Restore is an operator-run procedure or
a small hosted operator command.

Rebuild repairs derived projections, including FTS and vector tables. Rebuild
does not recover canonical transcript, candidate, fact, tombstone, retrieval,
or catalog rows lost after the last good PITR point or export.

If an upstream host has durable transcript/source logs, host transcript replay
may be used as a manual reconciliation step after restore. Without that upstream
source, the documented RPO remains the true data-loss bound.

## Compromised Credentials

The rotation procedure lives in `docs/runbooks/secret-rotation.md`. Database
tokens are minted short-lived with an `exp` claim; a token without `exp` is
itself a rotation trigger. Live secrets live in deploy-platform secret
management, never in a working-tree file.

The v1 compromised-credential response is runbook-driven:

- revoke affected Vexic API keys;
- rotate Turso database tokens for affected tenants;
- rotate Neon credentials and hosted control-plane secrets;
- rotate S3 writer/restore credentials and KMS permissions;
- audit access and restore/repoint affected databases if needed;
- create fresh exports under current keys after rotation; and
- avoid promising physical purge from provider backups before retention expiry.

Object Lock and SSE-KMS exports should not be treated as in-place re-encryption
targets. If a backup key or export access path is compromised, the safe path is
revocation, new credentials or keys, fresh exports, and documented retention of
old locked objects until they expire or a break-glass procedure is approved.

## Logs, Artifacts, And Exports

Logs and operational evidence must avoid raw memory payloads, raw API keys,
database tokens, and forbidden values. Restore evidence should use row counts,
checksums, redacted identifiers, and pass/fail validation rows rather than
payload dumps.

Exports are privileged egress. Export jobs and restore drills must use
redaction fail-closed behavior where forbidden values are configured. Failed
redaction checks should block artifact persistence or return.

Export/replay/rebuild artifacts are plaintext full-content snapshots written
owner-only. `LocalMemoryService` accepts an `artifact_dir` so hosts can route
them to a managed location instead of the OS temp dir, and exposes
`prune_artifacts` for lifecycle cleanup; artifacts are consumed and discarded,
not retained.

## Timing

| Phase | Required posture |
| --- | --- |
| Bootstrap | Free or cheap Turso/Neon tiers and simple manual exports are acceptable. No customer-readiness claim. |
| Internal dogfood | Restore notes are useful, but paid-tier S3 Object Lock evidence is not required. |
| External beta or customer memory | Paid Turso/Neon posture, S3 export target, and successful restore-drill evidence are required. |
| Long term | BYOK, app-level encryption, private networking, cross-region copies, WORM compliance, SIEM, automated drills, and external SLAs only when driven by customer, compliance, scale, or launch review needs. |

Private networking, IP allowlists, and mTLS are deferred from bootstrap and
ordinary v1 customer beta. They become pre-GA, enterprise, compliance, or
meaningful-volume hardening if a launch review or customer requirement pulls
them forward.

## Consequences

This decision closes the encryption and backup posture without adding provider
SDKs, hosted secrets, billing, dashboards, public HTTP, or production operations
code to `src/vexic`.

The decision does not itself make hosted Vexic customer-data ready. Customer
readiness still requires a paid/prod-like setup, successful restore drills,
verified Neon control-plane recovery, evidence records, and any adjacent hosted
readiness gates such as abuse protection, support access, incident response,
and security review.

Sources checked on 2026-06-23:

- [Turso Point-in-Time Recovery](https://docs.turso.tech/features/point-in-time-recovery)
- [Turso pricing](https://turso.tech/pricing)
- [Neon plans](https://neon.com/docs/introduction/plans)
- [Neon backup and restore](https://neon.com/docs/guides/backup-restore)
- [Amazon S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- [Amazon S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/versioning-workflows.html)
- [Amazon S3 SSE-KMS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html)
