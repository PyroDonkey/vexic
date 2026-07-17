# The hosted service schedules per-tenant dreaming itself with an in-server sweeper

Status: accepted

## Context

ADR 0003 settled that hosts request promotion timing and Vexic owns the write
semantics, with no automatic background daemon in v0.1. ADR 0025 shipped
automatic summarize triggering as an async endpoint plus two producers -- an
hourly single-tenant GitHub cron workflow and a SessionStart backstop -- and
deliberately deferred an in-server scheduler.

Two things changed. First, the cron producer never scaled past the dogfood
tenant: it swept exactly one hardcoded tenant/project via repo secrets, so
every other tenant's recaps refreshed only on their own SessionStart triggers,
and Light/REM/Deep promotion still ran only when an operator invoked the CLI.
Second, the project maintainer ratified (2026-07-09, during the Coalescent
hosted cutover COA-341) that dreaming is a built-in hosted-service feature:
Coalescent deleted its dream schedule entirely, so without in-server
scheduling no host triggers full dreaming at all and the memory loop never
completes on its own.

This ADR extends ADR 0003's "no background daemon in v0.1" posture: the
maintainer explicitly started this workstream (COA-339, subsuming COA-294).

## Decision

### One in-server sweeper, two cadences

`vexic.hosted_sweeper.DreamSweeper` runs in the deployed app's FastAPI
lifespan and ticks every `VEXIC_DREAM_SWEEP_TICK_SECONDS` (default 1800).
Per active tenant, per recorded agent scope (each distinct `agent_id`
including the NULL shared scope, because sweeps match `agent_id` exactly):

- **Summarize cadence**: when the tenant has new transcript rows since the
  last swept watermark, schedule a summarize sweep. The watermark check is a
  single `MAX(id)` query so idle tenants cost almost nothing per tick.
- **Dream cadence**: when `VEXIC_DREAM_INTERVAL_SECONDS` (default 86400) has
  elapsed since the tenant's last completed chain, schedule one
  Light -> REM -> Deep -> Summarize chain. A due dream folds summarize into
  the same job because the per-(tenant, agent) in-flight lock would otherwise
  make same-tick summarize and dream jobs collide.

### Reuse the trigger seam's containment, add no new authority model

`HostedMemoryService.schedule_system_dream` is the sweeper's only entry: it
mints pre-bound `RunDreamPhaseRequest`s under a `system` principal
(`dream-sweeper`) exactly like the trigger endpoint mints its summarize
request, never re-enters `_call`/`_bind_request`, executes on a worker-thread
event loop, records per-phase job events and usage under the system
principal, and shares the per-(tenant, agent) in-flight dedup lock with
user-triggered sweeps. A failing phase stops its chain (Deep must not promote
over a failed Light) and fails closed content-free; missing dream ports skip
scheduling entirely.

> Amended for COA-395. The pre-phase prelude of `_run_dream_phase_with_usage`
> -- the execution-time retirement re-check plus live local-service
> construction, before any phase runs -- now retries in-process on a
> `retryable_operational` fault (bounded attempts with linear backoff over the
> shared `is_retryable_operational_error` predicate), so a single transient
> Turso blip at first tenant/connection touch shortly after container start no
> longer fails the whole sweep until the next tick. Phase execution itself is
> deliberately NOT retried: each phase durably records its own `dream_runs`
> error row before re-raising, so a whole-phase retry would double-record and
> re-spend model calls -- mid-phase transient faults keep the fail-closed stop
> above. `MutationOutcomeUnknown` is excluded from the retry (a lost commit
> acknowledgement is unsafe to re-run) and also fails closed.

### State, knobs, and retirement

- Sweeper bookkeeping lives in the control-plane catalog's `dream_sweep_state`
  table -- the managed Turso control-plane database (ADR 0019 Addendum 5;
  previously the local `control-plane.db` on the Railway volume), separate from
  the per-tenant customer-memory databases --
  keyed per (tenant, agent) scope: the last summarize watermark whose
  job ran to completion and when the scope's last dream chain finished.
  Writes are monotonic and happen only after the scheduled job completes
  (a cancelled job leaves state untouched); ripeness and budgets are still
  computed downstream per run, so lost state merely causes a cheap redundant
  sweep.

  > Amended for COA-385. The dream stamps record the job's terminal time,
  > minted inside the job while the durable lease (ADR 0032) is still held,
  > rather than the wall-clock time at which the asynchronous recorder later
  > persists them. The lease is still released in the job's own `finally`
  > before the recorder runs -- the lifecycle is unchanged -- but a recorder
  > stalled across a rolling deploy now persists a stamp that correctly loses,
  > via the monotonic column guards, to a newer stamp another container wrote
  > in the meantime. A `failed == completed` due-check tie favors the failure
  > backoff: its worst case is one bounded early retry, while favoring
  > completion suppresses a real failure's retry for up to 24h.
- Per-tenant opt-out: `tenants.dream_scheduling` column,
  `HostedTenantCatalog.set_dream_scheduling`.
- Kill switch: `VEXIC_DREAM_SWEEPER=off`. Tenants are staggered within a
  tick; a broken tenant logs content-free and the tick continues.
- `.github/workflows/dream-cron.yml` and its three repo secrets are retired.
  The SessionStart backstop trigger (ADR 0025) remains as a between-tick
  freshness assist.

## Consequences

- Every hosted tenant gets summarize recaps and nightly promotion with zero
  per-tenant setup; hosts need no dream scheduling of their own (the
  Coalescent cutover relies on this).
- Dream model spend is now service-initiated; the cost and audit controls are
  the existing daily span budget, the per-(tenant, agent) in-flight lock (one
  chain per scope at a time; the worker thread pool bounds global
  concurrency), per-phase job events, and `dream_scheduling = 0` as the
  per-tenant brake. There is no per-hour rate rule on the system path -- the
  tick interval is the cadence control.
- Single-replica assumptions stand (in-process lock, in-process sweeper);
  multi-replica coordination remains future work tracked with the other
  ADR 0006 launch gates.

  > Amended by ADR 0032. The in-process lock was not sufficient even for a
  > single-replica service: a rolling deploy overlaps two containers, each
  > sweeping on boot, and a process-local lock cannot see across that boundary.
  > The in-flight lock is now backed by a durable control-plane lease. The
  > sweeper *loop* is still single-replica by assumption; the dream *work* is
  > now coordinated per scope.
- Exact per-session +2h timers stay approximated by the tick interval, as
  COA-294 accepted.
