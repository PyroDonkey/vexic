# The dream in-flight lock is a durable control-plane lease

Status: accepted

## Context

ADR 0030 gave the sweeper a per-(tenant, agent) in-flight lock so a trigger and
a sweep could not dream one scope twice. That lock is a process-local Python
set (`HostedMemoryService._dream_trigger_inflight`), and ADR 0030 recorded the
matching limit as an accepted risk: "Single-replica assumptions stand
(in-process lock, in-process sweeper); multi-replica coordination remains
future work."

A rolling deploy violates that assumption on every release. Railway starts the
incoming container before draining the outgoing one, and `DreamSweeper.run`
sweeps immediately on boot rather than waiting out its first tick. So during
each deploy two processes sweep the same scope against the same tenant
database, and the process-local lock cannot see across the boundary. The writes
collide; the managed libSQL backend rejects the loser at commit and raises a
bare `ValueError` (ADR 0019), which fails the phase and halts the chain.

This is not hypothetical. All six hosted Light failures (2026-07-10 and
2026-07-12) landed inside a deploy window, each within ~90 seconds of a
container coming up:

| Failure (UTC)     | Deploy window |
| ----------------- | ------------- |
| 07-10 16:41       | 16:39, 16:40  |
| 07-10 19:50 (x2)  | 19:49, 19:51  |
| 07-10 20:42       | 20:41         |
| 07-12 02:23       | 02:23         |
| 07-12 04:54       | 04:52, 04:53  |

None of them wrote a `dream_runs` error row, because the error-recording write
collided too. Each was transient and never reproduced on a manual re-run, when
only one container was alive. Multi-replica coordination is therefore not
future work; a single-replica service already runs two replicas every time it
ships.

## Decision

Back the in-flight lock with a durable lease in the control-plane catalog.

- `dream_sweep_lease` (tenant_id, agent_id, holder, expires_at), keyed per
  scope, in the control-plane database -- not the tenant's customer-memory
  database, which is the resource being protected.
- `HostedTenantCatalog.acquire_dream_lease` is a single conditional upsert, so
  two containers racing one scope resolve at the database: exactly one row
  write wins, and the loser skips the scope. `release_dream_lease` is scoped to
  the holder, so a late release cannot free a scope another container has since
  claimed.
- The in-process set stays as the fast path. Both layers are taken and released
  through the existing `_acquire_dream_trigger_lock` seam, so the sweeper and
  the trigger endpoint are both covered without a second authority model.
- `expires_at` bounds a holder that dies mid-chain: `DREAM_LEASE_TTL` is 20
  minutes, comfortably above a full Light -> REM -> Deep -> Summarize chain
  (Deep alone has run 8 minutes in production) so a live holder is never stolen
  from, and below the 30-minute sweep tick so a crashed holder costs at most one
  skipped sweep rather than a permanently wedged scope.

## Consequences

- Dreaming survives a deploy. Two overlapping containers no longer collide on
  one tenant database, so the chain stops halting on release.
- The lease is the coordination primitive multi-replica would need anyway, so
  ADR 0030's single-replica caveat narrows to the sweeper *loop* (every replica
  still ticks) rather than the dream *work* (one lease holder per scope).
  Running N replicas is still not a supported deployment; this ADR only makes
  the transient overlap safe.
- A crashed holder blocks its scope for up to the lease TTL. That is the
  deliberate trade against the alternative failure -- a stolen lease running a
  second Deep over candidates the first holder is mid-promotion on.
- The lease adds one control-plane round trip per scheduled dream. Scheduling is
  already per-scope per-tick, so the cost is negligible against a chain that
  makes model calls.
