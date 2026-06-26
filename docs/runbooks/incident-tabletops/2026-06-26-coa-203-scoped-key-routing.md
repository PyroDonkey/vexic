# COA-203 Tabletop - Scoped Key Abuse With Tenant Routing Suspicion

Date: 2026-06-26

Scenario: compromised scoped Agent API key with a suspected tenant routing mistake. This is a synthetic dry run only. All identifiers, counts, and evidence pointers are fake metadata.

Status: pass-with-caveats. This satisfies COA-203 documentation/tabletop evidence only. It does not satisfy COA-177 external hosted/customer-memory readiness.

## Scope Guardrails

This tabletop contains metadata only. It does not include raw memory, transcript text, fact text, candidate text, retrieval query text, prompt or tool bodies, raw or partial keys, provider secrets, database handles or tokens, routing/catalog internals, customer artifacts, unproven root cause, SLA/RPO/RTO promises, physical purge promises, or compliance claims.

## Metadata-Only Incident Evidence Ledger

| Field | Synthetic value |
| --- | --- |
| Incident id | INC-SYN-2026-06-26-COA203-01 |
| Severity | Synthetic SEV-2 exercise |
| IC | Ryan, with conflict checkpoint active |
| Deputy/secondary reviewer | Dana Example, not yet operationally trained |
| Affected tenant/project ids | tenant_demo_7f3a, project_demo_19c2 |
| Timeline | 09:00 signal opened; 09:06 scoped key marked suspect; 09:12 key revoked; 09:18 processor pause considered; 09:31 synthetic access review complete; 09:45 tabletop closed pass-with-caveats |
| Trigger signal | Synthetic anomaly: scoped key used outside expected project metadata boundary |
| Containment actions | Revoked fake key id key_demo_revoked_5d91; blocked synthetic route group route_demo_blocked_42; paused processor family proc_demo_agent_ingest if needed |
| Customer notifications | Template drafted with fake tenant/project metadata only; not sent |
| Evidence pointers | tabletop log TBL-SYN-203-01; redacted event count 14; checksum sha256:redacted-81f0c2 |
| Forbidden-data check | Pass: no raw memory, prompts, tool bodies, partial keys, provider secrets, DB handles, routing internals, or customer artifacts included |
| Follow-up todos | See COA-231 follow-up todos below |
| Final status | Pass-with-caveats for documentation/tabletop evidence only |

## Internal Evidence Checklist

Internal evidence may contain redacted identifiers, event counts, timestamps, and checksums. It must not contain DB handles, access tokens, provider secrets, raw or partial keys, routing/catalog internals, raw customer artifacts, or memory payloads.

- [x] Incident id recorded with synthetic metadata.
- [x] Affected tenant/project ids redacted to fake exercise ids.
- [x] Suspect key recorded by fake key id only.
- [x] Containment actions recorded as status metadata only.
- [x] Evidence pointer recorded as synthetic tabletop log and redacted checksum.
- [x] Forbidden-data check recorded before customer-facing template review.
- [ ] Deputy/secondary reviewer operational training complete.
- [ ] Customer contact/status channel validated with non-production template.

Customer-facing template fields must be bland and metadata-only: status, affected synthetic scope, action taken, next update window, and contact channel. Do not include DB handles, routing/catalog internals, root-cause claims, evidence checksums, raw memory, query text, prompts, tool bodies, keys, secrets, or customer artifacts.

## IC Conflict Checkpoint

Exercise condition: Ryan is implicated in the suspect config/routing change. The secondary reviewer/deputy is identified but not yet operationally trained.

Result: pass-with-caveats, not a clean pass. The tabletop confirms the conflict checkpoint is visible, but the deputy path is not operationally ready. Follow-up required under COA-231.

## Customer-Facing Template - Synthetic

Subject: Maintenance notice for scoped Agent API key access

We detected an issue affecting scoped Agent API key access for a limited project scope during an internal review. The affected key has been revoked, and access for the impacted route has been blocked while maintenance is in progress.

Synthetic tenant: tenant_demo_7f3a

Synthetic project: project_demo_19c2

Current status: contained; monitoring continues.

Customer action: create a replacement scoped key if continued access is needed after maintenance clears.

Next update: by 2026-06-26 18:00 UTC, or sooner if status changes.

## Pre-Beta Checklist Impact Summary

The following rows remain blocked or default blocked after this tabletop:

- Support/admin workflow: blocked; no trained customer support path or audit enforcement yet.
- Restore drills: blocked; tabletop did not prove restore behavior.
- Durable production control-plane evidence: blocked; synthetic artifact only.
- Abuse enforcement: default blocked; key revocation and route block were tabletop actions only.
- Worker spend/job cancellation: default blocked; processor pause was considered, not production-proven.
- Legal hold: blocked; no legal-hold policy or evidence retention decision made.

## COA-231 Follow-Up Todos

- COA-177 mapping/evidence tiers: separate tabletop evidence from external readiness evidence.
- Support workflow/audit enforcement: define who can act, what is logged, and how access is reviewed.
- Restore drills: run and record scoped restore exercises with synthetic data.
- Durable production-control evidence reconciliation: map runbook artifacts to durable control-plane records.
- Abuse enforcement: define and test scoped key abuse actions and escalation gates.
- Processor controls: prove worker spend and job cancellation paths under fake/synthetic load.
- Legal-hold policy: document retention, hold, and release behavior without physical purge promises.
- Deputy/secondary reviewer path: train and test a deputy who can act when the IC is conflicted.
- Customer contact/status channel: validate the template and channel with synthetic metadata only.
