# Control-plane destructive ops are audited, confirmed, and soft-deleted

Status: accepted

## Context

COA-320 is a go-live gate (not a public-repo blocker): before production
traffic, Vexic must be hard to mass-delete by accident or by a single
privileged action. A manual pre-launch reset had wiped `control-plane.db` and
tenant memory databases through a raw `railway ssh` + `DELETE`, which is
tolerable for pre-launch cleanup but not once real customer data exists.

The full control set spans infrastructure that is out of this repo's scope
(Turso point-in-time restore and scheduled snapshots, the restore-drill
runbook, and Railway SSH-key restriction) and the credential-scoping work in
`adapters/` (per-tenant read-only tokens; a `destroy_database` allow-list).
Those are tracked separately. This ADR records the three in-repo, TDD-covered
controls that live in the Vexic core.

Two findings shaped the scope. First, there is no HTTP `DELETE`/purge/reset
route anywhere; the control-plane edge already avoids a bulk-delete surface,
and key/token revocation is already a soft `UPDATE ... SET revoked_at`. Second,
only data-plane memory operations emitted `hosted_audit_events` (through
`HostedMemoryService._record_request`); control-plane mutations left no audit
trail, and `hosted_projects`/`tenants` had no working soft-delete
(`tenants.active` was a dead flag).

## Decision

1. **Audit destructive control-plane ops.** The destructive control-plane
   mutations record a `hosted_audit_events` row on success through the existing
   ledger: API-key revocation (`revoke_key`, and therefore
   `revoke_control_plane_key`, which delegates to it), setup-token revocation
   (`revoke_setup_token`), and the new project/tenant retirements below. The
   `hosted_audit_events` table gains `project_id` and `key_id` columns
   (control-plane events are key/project-scoped; the data-plane path leaves
   them NULL). Non-destructive provisioning/creation (`provision_tenant`,
   `provision_project`, `create_key`, `create_setup_token`) is deliberately
   *not* audited in this slice: the ticket's audit goal is recording deletes,
   and the existing data-plane telemetry philosophy already excludes creation
   from the audit ledger. The sanitization invariant is unchanged — no raw
   credential appears in an audit row.

2. **Confirm whole-scope purge.** `PurgeScopeRequest` gains
   `confirm_whole_scope: bool = False`. A null `target_scope.session_id` purges
   every session for the target agent scope in one call (ADR 0022 scope
   matching); `purge_scope` now rejects that whole-scope erasure unless
   `confirm_whole_scope=True`, failing before any deletion and regardless of
   `dry_run` so even a preview requires opting in. Session-scoped purges are
   unchanged. The field is additive with a safe default, so `CONTRACT_VERSION`
   stays `0.1.0`. The contract models set `extra="forbid"`, so strictly a
   version-skewed old server would reject a payload carrying the new field;
   pre-launch this cannot occur (a single first-party consumer, client and
   server import the same contract module), so the version is deliberately held
   at `0.1.0` and version policy is revisited at the first external release.

3. **Soft-delete control-plane projects and tenants.** `hosted_projects` and
   `tenants` gain inline `retired_at`/`retired_by` columns (ALTER-guarded
   migration, mirroring the `revoked_at`/`revoked_by` idiom already used for
   keys and setup tokens). `retire_control_project` and `retire_tenant` stamp
   the row in place, non-destructively: the row survives for recovery and
   audit, active listings filter `retired_at IS NULL`, and `retire_tenant` also
   sets `active = 0` so the existing `active = 1` gates exclude it.
   `provision_tenant` reactivates a tenant and clears the retirement stamp. No
   hard `DELETE` path is added; there is no delete path for these rows today,
   so this establishes the recoverable soft-delete surface a future removal
   path must use rather than fixing a live leak.

## Consequences

- Extends ADR 0022's tombstone posture (memory scopes) to control-plane rows,
  using the inline `revoked_at`-style idiom for keys/tokens/projects/tenants
  and keeping the separate `scope_tombstones` table for memory scopes.
- Recoverability over prevention: a privileged admin can still delete at the
  storage layer, so the infra safety net (PITR/backups, tracked separately)
  remains the primary control. This ADR narrows the accidental and
  single-action blast radius inside the core.
- `retire_control_project`/`retire_tenant` are the recoverable soft-delete
  primitive, not a full removal path. They mark the row (listing + audit +
  `active = 0` for tenants) but deliberately do not yet cut live access:
  `retire_control_project` leaves the `tenant_projects` routing membership and
  existing key bindings intact, and the active-project readers filter only
  `hosted_projects.retired_at` (not the owning tenant's state). A future
  removal/off-boarding path builds access revocation on top of these
  primitives; until it exists these methods have no runtime callers.
- Audit rows for the destructive ops commit in the same transaction as the
  state change, and revocation audits fire only on the `NULL -> revoked`
  transition, so a repeated (idempotent) revoke does not forge a second event
  and a committed delete never lacks its audit row.
- The migrations are idempotent and non-destructive: a pre-COA-320
  `control-plane.db` is ALTERed in place and existing rows are preserved.
- Deferred to their own workstreams: read-only default tenant tokens and a
  `destroy_database` allow-list in `adapters/`; Turso PITR/backups, the
  restore-drill runbook, and Railway SSH-key restriction (infra/ops).
