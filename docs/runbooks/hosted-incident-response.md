# Hosted Incident Response And Pre-Beta Security Review Runbook

This is a `docs/runbooks` artifact incorporating settled adversarial-review
fixes. It is not runtime implementation, not a legal breach notice package,
and not external readiness by itself.

Completing this runbook or a tabletop does not by itself make hosted Vexic
external/customer-data ready. Any `blocked`, `fail`, or unaccepted
`pass-with-caveats` row keeps the hosted readiness gate blocked unless the
designated security/engineering risk owner accepts the risk in Linear or an ADR.

## Evidence Tiers

- `doc-only`: written procedure or decision. Useful as documentation; not
  enough for production control claims.
- `tabletop-synthetic`: dry-run evidence using fake tenants, fake keys, fake
  events, and metadata-only artifacts.
- `requires-prod-control`: runtime, infrastructure, or operational-control
  evidence. No pass without concrete artifact or acceptance by the designated
  risk owner.

Owner issue `Done` never means gate pass. Gate pass requires row evidence that
matches the tier and gate rule.

## Severity, IC, Evidence, And Retention

- SEV-1: likely/confirmed cross-tenant exposure, broad credential compromise,
  no-operator-raw-memory violation with memory viewed, or destructive data
  loss.
- SEV-2: contained tenant incident, compromised scoped key, unauthorized
  processor access without confirmed exposure, failed/degraded restore, runaway
  spend, or abuse without confirmed exposure.
- SEV-3: suspicious signal, failed control, tabletop finding, or near miss.
- Classify up when unsure. Highest applicable severity wins.
- Every real incident and tabletop has one named Incident Commander and one
  deputy or explicit deputy gap.
- Default IC is the on-call Incident Commander or designated incident owner.
- Risk acceptance must be recorded by the designated security/engineering risk
  owner in Linear or an ADR.
- IC owns severity, containment authorization, customer notification timing and
  content, metadata-only evidence discipline, and follow-up todos.
- If the IC caused or is implicated in the incident, record the conflict and
  require a delegate or secondary review.
- When the IC is conflicted, delegate or secondary review must approve
  containment relaxation, route unblock, processor safe-marking/re-enable
  guidance, and final status acceptance.
- IC authority does not permit raw memory access, customer processor re-enable,
  or bypass of containment rules.
- Optional helper roles are evidence recorder, customer comms, and technical
  lead.

Evidence is metadata-only. Do not collect raw memory, transcript text, fact
text, candidate text, prompt/tool bodies, retrieval query text, request bodies,
raw API keys, provider secrets, database tokens, embeddings/vector blobs, or
configured forbidden values.

Operational evidence follows the hosted 400-day operational telemetry retention
rule. No legal-hold override is documented in repo docs; seed a follow-up before
claiming legal-hold behavior. Tenant-scoped `retrieval_events` and
`candidate_retrieval_events` may be used only through metadata, counts, and
checksums unless a separate privileged procedure exists.

## Evidence Allowlists

Internal evidence may include:

- incident id, severity, IC, deputy, status, timestamps, and timeline;
- affected tenant/project ids or redacted labels;
- control-plane audit event ids, usage counters, job ids, and status changes;
- Agent API key id/name, scope, created/revoked timestamps, and hash/checksum,
  never the key value or partial key;
- Clerk/session id hashes, user id, tenant id, IP/ASN, user agent, and auth
  decision metadata;
- route status, redacted catalog handle/checksum, database identity checksum,
  schema/checkpoint metadata, row counts, and cross-tenant negative checks;
- restore id, backup id, snapshot id, object-lock metadata, restore checksum,
  and smoke-test results;
- processor consent state, processor pause/cancel state, model-port disablement
  state, spend counters, and queue depth;
- rendered customer template using fake/synthetic metadata.

Customer-facing templates may include:

- incident reference, customer account/tenant/project label, affected capability,
  current status, and observed time window;
- containment action already taken, such as route maintenance, key revoked,
  processor paused, or restore paused;
- customer action needed, next update target, and support contact;
- key id/name or processor name only when not secret and not a routing/catalog
  internal;
- restore confidence status and metadata-only verification summary.

Customer-facing templates must forbid routing/catalog internals, DB
handles/tokens, raw/partial keys, raw memory/transcript/fact/candidate text,
summaries, embeddings/vector blobs, retrieval query text, prompt/tool bodies,
source transcript excerpts, customer artifacts, provider secrets, forbidden
values, unproven root cause, fault attribution, SLA/RPO/RTO promises, physical
purge promises, and compliance claims.

## Standard Incident Flow

1. Detect and open incident.
   - Assign IC, deputy, severity, and metadata-only evidence recorder.
   - Start a timeline with fake-safe or redacted identifiers.
2. Contain first.
   - Use tenant as the default containment unit. Projects are scope filters
     inside the Customer Memory Database.
   - Block affected tenant route when routing integrity is suspect.
   - Revoke scoped keys for credential compromise or unclear key exposure.
   - Pause/cancel processors or workers for unauthorized access or runaway
     spend.
   - Stop export/delete/admin egress for affected routes when exposure is in
     scope.
   - Use bland maintenance/status responses for blocked customer routes. Do not
     disclose routing or catalog internals.
3. Preserve metadata-only evidence.
   - Collect allowed evidence, checksums, counts, audit ids, and control states.
   - Run forbidden-data checks on artifacts before sharing or storing.
4. Assess blast radius.
   - Confirm tenant/project scope, route state, key scope, processor consent,
     and cross-tenant negative checks.
5. Notify when required.
   - Notify for likely/confirmed customer-memory exposure, unauthorized
     processor access, failed/degraded restore, customer-visible processor
     pause, or customer-visible route maintenance.
6. Recover.
   - Unblock only after content-blind evidence passes: catalog handle, no
     stale/orphan routable handles, database identity/schema/checkpoint, counts
     and checksums, cross-tenant negative checks, scoped API smoke tests,
     processor authorization state, and redacted audit trace.
   - Customer or authorized project admin re-enables processors.
7. Close.
   - Record final status, evidence pointers, customer notices, follow-up todos,
     and risk acceptance if any.

## Escalation Triggers

| Trigger | Default severity | Immediate action |
| --- | --- | --- |
| Likely/confirmed cross-tenant exposure | SEV-1 | Block affected tenant route, stop egress, preserve metadata, notify path. |
| Unauthorized processor access | SEV-2, SEV-1 if exposure likely | Pause processor, cancel/drain jobs, notify customer, require customer re-enable. |
| No Operator Raw Memory Access violation | SEV-1 if content viewed, else SEV-2 | Stop access, preserve audit metadata, remove access path, notify path. |
| Redaction fail-closed failure | SEV-1 if persisted/egressed, else SEV-2 | Stop write/egress path, quarantine artifacts, run forbidden-value checks. |
| Export/delete/admin egress exposure | SEV-1 | Disable egress route, preserve request metadata, assess tenant blast radius. |
| Failed/partial restore | SEV-2, SEV-1 if destructive loss | Freeze repoint, preserve backup/restore ids, run checksums, notify if customer-visible. |
| Compromised scoped key | SEV-2, SEV-1 if broad/cross-tenant | Revoke key, rotate if needed, review scope and usage metadata. |
| Runaway worker/model spend | SEV-2 | Pause/cancel worker, disable model port if needed, notify for visible pause. |
| Clerk/session abuse | SEV-2, SEV-1 if broad/exposure | Revoke sessions, disable account route if needed, review auth metadata. |
| Tenant routing suspicion | SEV-2, SEV-1 if exposure likely | Block tenant route by default, stop reads/writes/egress, verify content-blind. |

## Scenario Playbooks

Data exposure is not a standalone playbook. It is a shared
escalation/notification path invoked by any playbook when criteria are met.

### Compromised Or Abused Agent API Key

1. Revoke the scoped key and block affected capabilities if abuse is active.
2. Capture key id/name, scope, tenant/project, created/revoked time, and usage
   event ids.
3. Check for export/delete/admin egress, routing anomalies, and processor jobs.
4. Issue customer notice if key was customer-visible or access/exposure is
   likely.
5. Re-enable only with a new scoped key and clean metadata checks.

### Clerk/Session Abuse

1. Revoke affected sessions and require account re-auth.
2. Capture session hashes, user id, tenant id, IP/ASN, user agent, and auth
   decision metadata.
3. Check for key creation, processor enablement, export/delete/admin egress, and
   route changes.
4. Block tenant route if auth-to-tenant mapping is suspect.
5. Notify if customer-visible access or data exposure is likely.

### Tenant Routing Mistake Or Suspected Cross-Tenant Exposure

1. Put affected tenant route in maintenance by default.
2. Stop reads/search, writes, export/replay/rebuild/delete/admin egress.
3. Verify catalog handle, database identity, schema/checkpoint, row counts,
   checksums, no stale/orphan routable handles, cross-tenant negative tests,
   scoped API smoke tests, processor authorization state, and redacted audit
   trace.
4. Revoke keys only if credential exposure, abuse, or unclear key integrity is
   in scope.
5. Unblock only after content-blind evidence passes and IC records approval.

### Data Loss, Corruption, Failed Restore, Or Degraded Restore Confidence

1. Freeze catalog repoint and preserve old active database/backup handles by
   metadata reference only.
2. Capture restore id, backup id, snapshot id, object-lock metadata, counts,
   checksums, and smoke-test results.
3. Do not promise RPO/RTO, physical purge, or full recovery until verified.
4. Notify if customer-visible availability, integrity, or restore confidence is
   degraded.
5. Repoint only after checksum and scoped API smoke tests pass.

### Runaway Worker/Model Spend

1. Pause/cancel worker path and drain queued/in-flight jobs where possible.
2. If cancellation is unavailable, disable the processor or host model port as
   the emergency lever.
3. Capture job ids, queue depth, model-port state, spend counters, and affected
   tenant/project metadata.
4. Revoke keys only if abuse, compromise, or auth integrity is in scope.
5. Notify for every customer-visible processor pause.

### Unauthorized Customer-Enabled Memory Processor Access

1. Pause processor path immediately and cancel/drain jobs where possible.
2. Capture processor consent state, job ids, affected tenant/project metadata,
   and model-port state.
3. Treat as a data-exposure incident even when no human viewed memory.
4. Notify the customer with metadata only.
5. Vexic may mark the path safe after review; only the customer/account owner or
   authorized project admin may re-enable it.

### No Operator Raw Memory Access Violation

1. Stop the access path and remove the operator/session from the incident.
2. Capture audit metadata, actor id, access path, timestamps, tenant/project, and
   whether raw content was viewed.
3. Do not copy or summarize the raw content into evidence.
4. Escalate SEV-1 if content was viewed; otherwise classify no lower than SEV-2.
5. Add follow-up for support workflow and audit enforcement gaps.

## Customer Communication Templates

These templates are metadata-only skeletons with fake placeholders. They are
not legal breach notices.

### Blocked Route Or Maintenance

Subject: Hosted Vexic route maintenance for `[tenant-label]`

We placed `[capability-or-route]` for `[tenant-label]` into maintenance at
`[time-window]` while we verify metadata-only routing checks. Customer memory
content is not included in this notice. Next update target: `[time]`.

### Compromised Or Revoked Agent API Key

Subject: Agent API key revoked for `[tenant-label]`

We revoked Agent API key `[key-name-or-id]` scoped to `[scope-label]` at
`[revoked-at]` after detecting `[metadata-signal]`. Create a replacement key
only after your internal review. We will provide another metadata-only update by
`[time]`.

### Paused Customer-Enabled Memory Processor

Subject: Memory processor paused for `[tenant-label]`

We paused `[processor-name]` for `[tenant-label]` at `[paused-at]` while we
review `[metadata-signal]`. Jobs in scope: `[job-count]`. You or an authorized
project admin must re-enable the processor after the path is cleared.

### Unauthorized Processor Access

Subject: Unauthorized processor access under review for `[tenant-label]`

We detected processor access outside the expected authorization state for
`[tenant-label]` during `[time-window]`. The processor is paused. Current
metadata scope: `[project-labels]`, `[job-count]` jobs. Next update target:
`[time]`.

### Likely Or Confirmed Memory Exposure

Subject: Hosted Vexic memory exposure investigation for `[tenant-label]`

We are investigating `[likely-or-confirmed]` exposure affecting
`[tenant-label]` during `[time-window]`. Containment actions completed:
`[actions]`. This notice contains metadata only. We will provide the next update
by `[time]`.

### Failed Or Degraded Restore

Subject: Hosted Vexic restore status for `[tenant-label]`

Restore `[restore-id]` for `[tenant-label]` is `[failed-or-degraded]` based on
metadata verification at `[checked-at]`. We are holding the current route state
while checks continue. Next update target: `[time]`.

### Investigation Ongoing

Subject: Hosted Vexic investigation update for `[tenant-label]`

Investigation `[incident-id]` remains open for `[tenant-label]`. Current status:
`[status]`. Containment state: `[containment]`. No customer memory content is
included in this update. Next update target: `[time]`.

## Tabletop Artifact Template

Status vocabulary: `pass`, `pass-with-caveats`, `fail`, `blocked`.

`pass-with-caveats` may satisfy documentation requirements only; it does not make the
hosted readiness gate green or external/customer-data ready. `blocked` creates
a follow-up todo and leaves the gate blocked.

```markdown
# Hosted Incident Tabletop - [YYYY-MM-DD]

- Scenario: compromised scoped Agent API key with suspected tenant-routing mistake
- Incident id: [synthetic-incident-id]
- Status: [pass | pass-with-caveats | fail | blocked]
- Severity tested: [SEV-1 | SEV-2 | SEV-3]
- IC: [name]
- Deputy: [name or blocked]
- IC conflict/deputy check: [tested | not-tested | blocked]
- IC/designated incident owner implicated in config/routing change:
  [yes | no | synthetic]
- Affected tenant/project labels: [fake labels]
- Evidence tier: tabletop-synthetic
- Metadata-only evidence pointers: [audit ids, counts, checksums, fake notices]
- Forbidden-data check: [pass | fail]
- Containment actions exercised: [route block, key revoke, processor pause]
- Customer template rendered: [template name]
- Caveats:
- Follow-up todos:
- Risk acceptance pointer, if any: [Linear/ADR link]
```

## Pre-Beta Security Review Checklist

Status vocabulary:

- `pass`: evidence satisfies the row with no known readiness caveat.
- `pass-with-caveats`: not gate-green unless the caveat is explicitly
  non-gating or has acceptance by the designated risk owner.
- `fail`: tested/evaluated and did not satisfy the row; blocks external beta
  unless the designated security/engineering risk owner accepts the risk in
  Linear or an ADR.
- `blocked`: prerequisite work/tooling/drill is missing; blocks external beta
  unless the designated security/engineering risk owner accepts the risk in
  Linear or an ADR.

Each review row must record status, evidence pointer, notes/caveat, follow-up
todo when not a clean pass, and risk-acceptance pointer when proceeding
despite a gap.

| Row | Evidence tier | Evidence expected | Gate rule |
| --- | --- | --- | --- |
| 1. Auth, Clerk/session abuse, and Agent API Key scope/revocation | requires-prod-control | Revocation and session-abuse artifacts | Blocks unless concrete auth/key evidence or acceptance by the designated risk owner exists. |
| 2. Tenant isolation and routing integrity | requires-prod-control | Production-control catalog/routing checks | Blocks unless content-blind isolation evidence or acceptance by the designated risk owner exists. |
| 3. No Operator Raw Memory Access and support metadata boundaries | requires-prod-control | Support metadata workflow and audit enforcement | Blocks unless support metadata controls exist or acceptance by the designated risk owner exists. |
| 4. Redaction fail-closed paths | requires-prod-control | Request/persistence/log/export evidence plus contract/tests | Blocks unless fail-closed evidence covers egress and persistence. |
| 5. Encryption, TLS, secrets, and key rotation | requires-prod-control | Deployed TLS/encryption/rotation evidence | Blocks unless deployed control evidence or acceptance by the designated risk owner exists. |
| 6. Backup, PITR, immutable/export backup, and restore drills | requires-prod-control | Restore drill artifacts; a Railway-volume alpha drill is covered in `docs/runbooks/restore-drills/2026-06-26-coa-232-railway-alpha-volume.md` | Blocks unless customer-readiness restore drill artifacts pass or acceptance by the designated risk owner exists. |
| 7. Audit, usage, job, incident ledgers, and detection signals | requires-prod-control | Durable-ledger and detection artifacts | Blocks unless durable ledger and detection artifacts exist. |
| 8. Rate limits, payload caps, abuse controls, origin protection, and alerting | requires-prod-control | Abuse enforcement, origin protection, and alert evidence | Blocks unless enforcement and alert evidence exists. |
| 9. Worker/model processor consent, spend caps, and job cancellation | requires-prod-control | Processor consent, spend-cap, and pause/cancel evidence | Blocks unless consent, cap, pause/cancel evidence exists. |
| 10. Migration/import/repoint safety | tabletop-synthetic | Local runbook and drill evidence; covered by tabletop drill | Does not gate-green external readiness without hosted route evidence. |
| 11. Incident response tabletop and customer communications | tabletop-synthetic | Covered by tabletop drill: `docs/runbooks/incident-tabletops/2026-06-26-coa-203-scoped-key-routing.md` | Satisfies documentation/tabletop evidence only; caveats do not gate-green external readiness. |
| 12. Compliance-claim guardrails | doc-only | Written guardrails; no SOC2/HIPAA claims | Allows docs only; blocks claims beyond documented controls. |

## Follow-Up Todo Seeds

- production control-plane catalog and durable ledger store;
- support metadata workflow and audit enforcement;
- Cloudflare/WAF and origin lock-down;
- auth-failure throttling;
- distributed quotas;
- token/dollar spend caps;
- job cancellation and queue drain;
- processor consent/re-enable controls;
- alerting and detection signals;
- production Turso PITR, Neon control-plane recovery, and S3 Object Lock
  restore drills;
- legal-reviewed exposure/regulatory notices;
- legal-hold policy and retention override decision;
- customer contact-of-record and status-page process;
- deputy IC / incident role training;
- external reviewer or secondary review path for IC conflict.

## What This Does Not Do

- No public readiness claim.
- No legal breach notice package.
- No SOC2, HIPAA, or enterprise compliance claim.
- No runtime code, hosted auth, billing, dashboard, or support portal
  implementation.
- No promise of physical purge, RPO, RTO, external beta, or customer-data
  readiness.
