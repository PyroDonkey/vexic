# Automatic summarize triggering ships as an async trigger endpoint, hourly cron, and a detached SessionStart backstop

Status: accepted

## Context

ADR 0024 (COA-254) shipped the Summarize dream phase, `/v1/fresh_context`,
and SessionStart recap priming, but nothing ran Summarize automatically:
`session_summaries` only grew when an operator ran `run-dream-phase --phase
summarize` by hand. Priming's recap leg degrades gracefully when the
frontier is empty or stale, but the intended experience -- a new session
opening with "Prior conversation recap:" -- only happens if summarize has
already run recently enough. This work (COA-254 follow-on) makes that
automatic: recaps should effectively always be ready when `recorder prime`
reads, session start must add zero blocking work, and model spend must stay
bounded regardless of how often triggers fire.

Design principles carried through every decision below:

- Prime never does model work itself and gains no serial latency.
- Producers run when material is ripe (2h-idle/3am-local boundaries): hourly
  cron is the primary producer, the SessionStart trigger is a backstop.
- Cost scales with transcript volume, not with trigger frequency: redundant
  triggers cost one dedup check, not one summarize run.

## Decision

### D1 -- A dedicated trigger endpoint, summarize-only in v1

`POST /v1/trigger_dream_phase`, body `{"phase": "summarize"}`, returns `202`
with `{"status": "scheduled"}` or `{"status": "skipped", "reason":
"already_running"}`. v1 hard-rejects any phase other than `"summarize"`
(`400`): light/rem/deep triggering has a materially different cost and abuse
profile (they run on candidate/fact volume, not raw transcript volume) and is
a separate decision if it is ever wanted.

**Honest scope, not project-scoped:** tenant databases are shared across a
tenant's projects, and `messages`/`session_summaries` carry no `project_id`
column -- `list_compactable_session_ids` filters by `agent_id` only. The
sweep and the daily span budget (D7) are therefore **tenant(+agent)-wide, not
project-scoped**. The request's project header still authenticates and binds
the call exactly like every other hosted route, but it does not narrow which
sessions get summarized or whose budget is spent. `docs/hosted-mvp.md` and
`docs/memory-service-contract.md` state this explicitly rather than implying
project isolation that does not exist. Adding a `project_id` column to
`messages`/`session_summaries` (a schema migration plus a query rewrite) is
out of scope here; it is a separate future issue if a multi-project tenant
ever needs per-project spend/sweep isolation.

### D2 -- A new capability, not reuse of `memory:admin:rebuild`

`MemoryCapability.DREAM_TRIGGER = "memory:dream:trigger"` is a new
capability. `RunDreamPhaseRequest.required_capability` is hardcoded to
`ADMIN_REBUILD` and enforced in `_bind_request`; naively reusing
`RunDreamPhaseRequest` for the trigger boundary would 403 any trigger-only
key (the recorder, the cron workflow) that does not also carry admin-rebuild.
Granting every trigger caller full admin-rebuild just to schedule a
summarize sweep would be a materially larger blast radius than the operation
needs.

Fix: a new, deliberately thin `TriggerDreamPhaseRequest` with
`required_capability = DREAM_TRIGGER`. The hosted service validates and binds
*this* request at the trigger boundary, then internally constructs the
existing `RunDreamPhaseRequest` with a server-minted scope carrying
`ADMIN_REBUILD` to actually run the phase -- the same "header-bound scope
minting with the operation's own capability" precedent `fresh_context` already
uses (`hosted_http.py`). Recorder/cron keys are issued with
`memory:fresh-context` + `memory:dream:trigger`; existing keys are
unaffected.

**Routing, not reuse, for execution.** The minted `RunDreamPhaseRequest`
must never re-enter `HostedBackgroundJobRunner.run_dream_phase` ->
`_call` -> `_bind_request`: `effective_capabilities = scope.capabilities &
auth.capabilities` would strip the minted `ADMIN_REBUILD` (the calling key
does not hold it), 403-ing the phase in the background where nothing could
observe or retry it; re-entering `_call` would also double-count the 6/hour
`run_dream_phase` rate bucket and double-record audit events for one
trigger. Instead, after the trigger boundary's own authenticate/bind/rate
check, the service calls `_run_dream_phase_with_usage(bound, tenant)`
directly -- a pre-bound entry point -- wrapping the existing
`_record_job`/`record_job_usage` telemetry around it with the
already-obtained `auth`. This is the same class of bug as a 403 in
production would have been if the two paths had been collapsed; verified in
tests via a trigger key that holds `memory:dream:trigger` but *not*
`memory:admin:rebuild` succeeding end-to-end.

The minted request's `RedactionContext(forbidden_values=())` is a deliberate
choice matching the `fresh_context` header-bound precedent; the phase still
receives adapter-level `forbidden_secret_values` via the dream-phase ports'
secrets.

### D3 -- Async execution: background task, worker-thread event loop, in-process dedup

The trigger boundary schedules `asyncio.create_task` wrapping the pre-bound
execution path (job events, telemetry, and usage attribution all preserved
from the existing `HostedBackgroundJobRunner` machinery) and returns `202`
immediately.

**Worker-thread event loop, not the serving loop.** `run_summarize_phase` is
itself a coroutine that mixes `await agent.run` with synchronous sqlite I/O.
Running it directly on the serving event loop would stall every other
in-flight request for the duration of a sweep. Because `run_summarize_phase`
is a coroutine (not an ordinary blocking function), a bare
`asyncio.to_thread(run_summarize_phase, ...)` does not apply -- there would
be no event loop in the worker thread to drive it. The fix is
`await asyncio.to_thread(asyncio.run, coro_factory())`: the worker thread
gets its own fresh event loop via `asyncio.run`, and the serving loop is
free the entire time. SQLite thread-safety holds because every storage call
opens and closes its own connection in-thread; the job-events telemetry sink
(a plain list) is confined behind a `threading.Lock` since it is now written
from a real OS thread rather than only from cooperative coroutines on one
loop.

**Per-(tenant, agent) in-process in-flight lock.** A concurrent trigger for
the same (tenant_id, agent_id) while a sweep is already running returns
`{"status": "skipped", "reason": "already_running"}` instead of starting a
second overlapping sweep. Task failures land in the existing `_record_job`
error path and never crash the server. The existing `run_dream_phase` rate
rule (6/hour, shared with the CLI/admin dream-phase path) still applies on
top of the lock and yields `429` on exceed.

**Accepted risks, deliberately not solved here:**

- The in-process task is lost on restart or redeploy mid-sweep. The next
  trigger (cron or SessionStart) re-runs idempotently -- Summarize's
  span-finding is frontier-based, so nothing is double-counted -- but an
  in-flight sweep at deploy time is simply abandoned, not resumed or queued.
- The in-flight lock and the rate limiter are both process-local. The
  current deploy is verified single-process/single-instance (the Dockerfile
  and hosted entrypoint; `docs/hosted-mvp.md` already documents the
  in-memory rate limiter on this basis). Under a future multi-replica scale-
  out, the lock stops deduping across replicas and the 6/hour cap becomes
  6/hour *per replica* rather than a global cap. Revisit with a durable
  queue or a shared limiter before scaling the hosted service horizontally;
  tracked as future work, not solved in this ADR.

### D4 -- Recorder prime trigger: detached subprocess, not a fourth serial call

`recorder prime`'s existing serial chain already runs up to three sequential
`urlopen` calls at a 15s default timeout each, against the SessionStart
hook's 30s kill budget -- a pre-existing marginal budget (see follow-up
below), not something this work should make worse. Adding synchronous
trigger call as a fourth serial leg was rejected outright.

Instead, `recorder prime` spawns a **detached, fire-and-forget one-shot
subprocess** before doing its normal priming work:
`[sys.executable, "-m", "vexic.cli", "recorder", "trigger-dream", "--config",
<path>]`. The child POSTs the trigger with its own 5s timeout and exits;
prime does not wait on it. Three hardening details matter:

- `stdin`/`stdout`/`stderr` are all `subprocess.DEVNULL` and
  `start_new_session=True`. An inherited stdout pipe would keep the
  SessionStart hook's own stdout open until the child process exits,
  silently defeating the "zero added latency" goal even though prime itself
  returned immediately -- the parent's stdout close is gated on every
  inheriting descriptor closing, not on the parent process exiting. Tests
  assert the parent's stdout closes before the child exits.
- `sys.executable -m vexic.cli`, never a bare `python` invocation: the
  SessionStart hook's `PATH` may not resolve to the project's venv
  interpreter.
- Credentials travel via `--config <path>` only, never as an `--api-key`
  argv value -- argv is visible in `ps` output for the child's entire
  lifetime, a config file is not.

Spawn failure, trigger timeout, and a non-2xx trigger response are all
swallowed with a stderr warning; the subcommand always exits 0 and prime's
own output and exit code are unaffected either way -- fail-open by
construction, the same posture ADR 0018 established for priming generally.

**Follow-up, pre-existing and out of scope here:** prime's worst-case
3 x 15s serial timeout budget already exceeds the SessionStart hook's 30s
kill in the worst case. Tightening per-call timeouts or parallelizing the
existing fetches is a separate, already-known follow-up; this ADR does not
fix it and this work does not make it worse (the new subprocess is detached
and adds no serial wait).

### D5 -- Cron producer: a new, deliberately dumb scheduled workflow

`.github/workflows/dream-cron.yml` is the first `schedule:`-triggered
workflow in this repo (every existing workflow is push- or
`workflow_dispatch`-triggered). It fires hourly plus `workflow_dispatch` for
hand-testing, and does nothing but `curl --max-time 30 --retry 2 -X POST` the
trigger endpoint for one configured tenant/project via GitHub secrets
(`VEXIC_DREAM_TRIGGER_URL`, `VEXIC_DREAM_TRIGGER_KEY`, and a project-id
secret). The endpoint owns all real logic -- dedup, rate limiting, budget --
so the workflow deliberately stays dumb: no matrix, no retry logic beyond
curl's own, no per-tenant looping. A non-2xx response fails the run red on
purpose, which is the intended v1 alerting signal; no additional retry layer
sits on top of it.

### D6 -- Live adapter port

`adapters/openrouter_live_adapter.py` gains `build_summary_agent`, reading
`VEXIC_SUMMARY_MODEL` (default `deepseek/deepseek-v4-pro`, matching the
extraction/contradiction default so all dream-phase legs share one model
absent an explicit override) and reusing the adapter's existing
`_agent`/model-settings helpers. `DreamPhasePorts.summary_agent_factory` was
already wired end-to-end by ADR 0024; only the adapter symbol was missing.

### D7 -- Daily span budget with an explicit, mockable clock

`VEXIC_SUMMARIZE_DAILY_SPAN_BUDGET` (default `50`) caps how many
`session_summaries` rows a tenant(+agent) can add per UTC calendar day; leaf
writes and condense writes both count against it, checked inside
`run_summarize_phase`'s per-session loop before each write. Per D1, the
budget is tenant(+agent)-wide, matching the sweep's own scope.

**Explicit `created_at`, not the DB default.** `session_summaries.created_at`
was `DEFAULT CURRENT_TIMESTAMP` (the DB's own clock), which cannot be frozen
in a test -- there was no way to write a deterministic "budget resets at the
next UTC day" test. `record_session_summary` now accepts an explicit
`created_at`, threaded from a single `now_utc` reading taken once per
`run_summarize_phase` call (frozen via the `now_utc` parameter in tests); the
DB default remains for legacy callers that do not pass it. The budget count
is `count_session_summaries_since(agent_id=..., created_at_floor=<UTC day
start>)`.

**Format must match SQLite's own `CURRENT_TIMESTAMP` exactly.** The explicit
value is written as `now_utc.strftime("%Y-%m-%d %H:%M:%S")` -- the same
space-separated format SQLite's `CURRENT_TIMESTAMP` emits -- rather than
`datetime.isoformat()`'s `T`-separated form. `created_at` has TEXT affinity,
so the budget's `>=` comparison is a string comparison: a `T`-separated
explicit row would sort *below* a space-separated UTC-day-start floor and
silently escape the budget count. Tests mix a legacy DB-default row with
explicit-`created_at` rows and assert both are counted.

**Two clocks, two concerns, on purpose.** The budget's UTC-day window and
the phase's 2h-idle/3am-*local* ripeness heuristic (which sessions are worth
summarizing right now) are different clocks governing different concerns --
spend accounting versus work selection -- and are not expected to agree or
be unified. Race safety: the budget check runs inside the per-session loop,
and D3's per-(tenant, agent) in-flight lock prevents the only concurrency
mode that exists today (two overlapping triggers) from double-spending the
budget between check and write. The budget count is currently a table scan
(no index on `created_at`); acceptable at MVP volume, with
`idx_session_summaries_created_at` deferred until it actually shows up in
practice.

## Out Of Scope / Deferred

- Triggering light/rem/deep phases through this endpoint (D1) -- different
  cost/abuse profile, separate decision if ever wanted.
- Project-scoped storage for multi-project tenants that want per-project
  sweep/budget isolation (D1) -- requires a schema migration and query
  rewrite; a separate future issue if it is ever needed.
- Multi-replica hardening for the in-flight lock and rate limiter (D3) --
  durable queue or shared limiter, deferred until the hosted service
  actually scales beyond one instance.
- Tightening prime's pre-existing serial-timeout budget (D4) -- a known,
  pre-existing follow-up, unrelated to and not worsened by this work.

## Consequences

- `docs/hosted-mvp.md`'s prior "no dream-phase HTTP route exists, summarize
  triggering is a manual CLI run" framing is now false and has been updated:
  the trigger endpoint, its capability, the cron secrets, and the budget/model
  env vars are documented, and the previously-stubbed
  `host_port_not_configured` `503` test now exercises a real route.
- `docs/memory-service-contract.md` gains the `DREAM_TRIGGER` capability row,
  the `TriggerDreamPhaseRequest`/`TriggerDreamPhaseResult` operation entry,
  and the tenant(+agent)-wide scope and budget semantics.
- `docs/architecture.md` notes that Summarize now has automatic producers
  (cron plus the SessionStart detached trigger) rather than only a manual
  CLI entry point; the three-retrieval-families taxonomy from ADR 0024 is
  unchanged by this work.
- Operators who want the automatic path must configure the cron workflow's
  GitHub secrets and issue keys carrying `memory:dream:trigger`; existing
  keys and the manual `run-dream-phase --phase summarize` CLI path both keep
  working unchanged.
