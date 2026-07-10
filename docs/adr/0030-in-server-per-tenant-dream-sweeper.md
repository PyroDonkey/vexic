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
- Exact per-session +2h timers stay approximated by the tick interval, as
  COA-294 accepted.
